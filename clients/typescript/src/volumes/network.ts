/**
 * Dominio puro de la comprobación previa de `EfsVolumes` (`m15-efs-volumes`,
 * experimental): qué VPC y subredes **ya existentes** acepta la pila
 * `efs-volumes` y qué hay que avisar antes de desplegarla. Espejo de
 * `rayito._volumes._network`. Sin SDK de AWS: los hechos los trae el puerto
 * `NetworkInspector` (`vpc.ts`, sólo `Describe*` de EC2) y esto los evalúa.
 * La pila sólo añade recursos nuevos dentro de la VPC: nunca toca la VPC,
 * sus subredes, tablas de rutas, NACLs ni grupos de seguridad existentes.
 */

import { InvalidArgumentError } from "../errors.js";
import type { CostStatement } from "../stacks/model.js";

/** Formatos de EC2 (`vpc-`/`subnet-` y 8 o 17 hex): AWS_API_NOTES.md §22. */
const VPC_ID_PATTERN = /^vpc-[0-9a-f]{8,17}$/;
const SUBNET_ID_PATTERN = /^subnet-[0-9a-f]{8,17}$/;
/** `arn:<partición>:elasticfilesystem:<región>:<cuenta>:access-point/fsap-…`. */
const ACCESS_POINT_ARN_PATTERN =
  /^arn:aws[a-z-]*:elasticfilesystem:[a-z0-9-]+:\d{12}:access-point\/fsap-[0-9a-f]{8,40}$/;

/** `infra/efs-volumes.yaml` declara tres ranuras de mount target
 * (`MountTarget1..3`); EFS admite como mucho uno por AZ. */
export const MIN_SUBNETS = 1;
export const MAX_SUBNETS = 3;
/** Una IP para el mount target y al menos una para una ENI del conector
 * (Lambda crea una por combinación de subred y grupo de seguridad); cuántas
 * más usa bajo carga es EFS-20, aún sin medir. */
export const REQUIRED_FREE_IPS_PER_SUBNET = 2;
/** `State` de EC2 para una subred o VPC utilizable (modelo `ec2`). */
const AVAILABLE_STATE = "available";

export type DefaultRouteTarget = "nat" | "internet-gateway" | "other";
export type FindingLevel = "OK" | "WARN" | "FAIL";
const LEVEL_ORDER: readonly FindingLevel[] = ["OK", "WARN", "FAIL"];

export interface VpcFacts {
  readonly vpcId: string;
  readonly state: string;
  readonly dnsSupport: boolean;
  readonly dnsHostnames: boolean;
}

export interface SubnetFacts {
  readonly subnetId: string;
  readonly vpcId: string;
  readonly availabilityZone: string;
  readonly availableIps: number;
  readonly state: string;
}

export interface RouteTableFacts {
  readonly subnetIds: readonly string[];
  readonly main: boolean;
  readonly defaultRoute: DefaultRouteTarget | undefined;
}

/** Lo único que la comprobación previa lee de EC2 (AWS_API_NOTES.md §22). */
export interface NetworkInspector {
  vpc(vpcId: string): Promise<VpcFacts | undefined>;
  subnets(subnetIds: readonly string[]): Promise<SubnetFacts[]>;
  routeTables(vpcId: string): Promise<RouteTableFacts[]>;
}

/** Un resultado: `code` es el mismo conjunto cerrado que en Python. */
export interface NetworkFinding {
  readonly level: FindingLevel;
  readonly code: string;
  readonly message: string;
}

/** Lo que `EfsVolumes.check()` devuelve sin crear nada. */
export interface EfsNetworkReport {
  readonly vpcId: string;
  readonly subnetIds: readonly string[];
  readonly availabilityZones: readonly string[];
  readonly findings: readonly NetworkFinding[];
  /** El `CostStatement` del componente `efs-volumes` (la fuente de `rayito stack list`). */
  readonly cost: CostStatement;
  /** `true` si ningún hallazgo es `FAIL`: `deploy()` se niega si no. */
  readonly ok: boolean;
  readonly status: FindingLevel;
}

export function validateVpcId(value: unknown): string {
  if (typeof value !== "string" || !VPC_ID_PATTERN.test(value)) {
    throw new InvalidArgumentError("vpcId inválido: se esperaba 'vpc-' y 8-17 hex");
  }
  return value;
}

/** `"subnet-a,subnet-b"` (la forma del `--param`) o un array; de
 * `MIN_SUBNETS` a `MAX_SUBNETS`, sin repetir. */
export function validateSubnetIds(values: unknown): readonly string[] {
  const items = typeof values === "string" ? values.split(",") : values;
  if (!Array.isArray(items)) {
    throw new InvalidArgumentError("subnetIds espera una lista de ids de subred");
  }
  const subnetIds = items.map((item) => String(item).trim()).filter((item) => item !== "");
  if (subnetIds.length < MIN_SUBNETS || subnetIds.length > MAX_SUBNETS) {
    throw new InvalidArgumentError(
      `subnetIds admite de ${MIN_SUBNETS} a ${MAX_SUBNETS} subredes ` +
        "(una por AZ: un mount target por subred)",
    );
  }
  if (!subnetIds.every((subnetId) => SUBNET_ID_PATTERN.test(subnetId))) {
    throw new InvalidArgumentError("subnetIds: cada id es 'subnet-' y 8-17 hex");
  }
  if (new Set(subnetIds).size !== subnetIds.length) {
    throw new InvalidArgumentError("subnetIds no admite subredes repetidas");
  }
  return subnetIds;
}

export function validateAccessPointArns(values: readonly string[]): readonly string[] {
  for (const arn of values) {
    if (typeof arn !== "string" || !ACCESS_POINT_ARN_PATTERN.test(arn)) {
      throw new InvalidArgumentError(
        "accessPointArns: cada valor es el ARN de un access point de EFS " +
          "(arn:aws:elasticfilesystem:<región>:<cuenta>:access-point/fsap-…)",
      );
    }
  }
  return [...values];
}

function vpcFindings(vpc: VpcFacts | undefined): NetworkFinding[] {
  if (vpc === undefined) {
    return [
      { level: "FAIL", code: "vpc-missing", message: "la VPC no existe en esta cuenta y región" },
    ];
  }
  const findings: NetworkFinding[] = [];
  if (vpc.state !== AVAILABLE_STATE) {
    findings.push({
      level: "FAIL",
      code: "vpc-unavailable",
      message: `la VPC está en estado ${JSON.stringify(vpc.state)}`,
    });
  }
  if (!vpc.dnsSupport) {
    findings.push({
      level: "WARN",
      code: "vpc-dns-support",
      message:
        "la VPC no tiene enableDnsSupport: el nombre DNS del sistema de ficheros " +
        "no resuelve (montar por la IP del mount target sí funciona)",
    });
  }
  if (!vpc.dnsHostnames) {
    findings.push({
      level: "WARN",
      code: "vpc-dns-hostnames",
      message: "la VPC no tiene enableDnsHostnames: EFS lo pide para montar por nombre DNS",
    });
  }
  return findings;
}

function subnetFindings(
  vpcId: string,
  subnetIds: readonly string[],
  subnets: readonly SubnetFacts[],
): NetworkFinding[] {
  const known = new Map(subnets.map((subnet) => [subnet.subnetId, subnet]));
  const findings: NetworkFinding[] = [];
  const seenAzs = new Map<string, string>();
  for (const subnetId of subnetIds) {
    const subnet = known.get(subnetId);
    if (subnet === undefined) {
      findings.push({
        level: "FAIL",
        code: "subnet-missing",
        message: `${subnetId}: la subred no existe`,
      });
      continue;
    }
    if (subnet.vpcId !== vpcId) {
      findings.push({
        level: "FAIL",
        code: "subnet-other-vpc",
        message: `${subnetId}: la subred es de otra VPC`,
      });
    }
    if (subnet.state !== AVAILABLE_STATE) {
      findings.push({
        level: "FAIL",
        code: "subnet-unavailable",
        message: `${subnetId}: la subred está en estado ${JSON.stringify(subnet.state)}`,
      });
    }
    const other = seenAzs.get(subnet.availabilityZone);
    if (other === undefined) {
      seenAzs.set(subnet.availabilityZone, subnetId);
    } else {
      findings.push({
        level: "FAIL",
        code: "subnet-same-az",
        message:
          `${subnetId} y ${other} están en la misma AZ (${subnet.availabilityZone}): ` +
          "EFS admite un mount target por AZ",
      });
    }
    if (subnet.availableIps < REQUIRED_FREE_IPS_PER_SUBNET) {
      findings.push({
        level: "FAIL",
        code: "subnet-free-ips",
        message:
          `${subnetId}: ${subnet.availableIps} IPs libres, hacen falta al menos ` +
          `${REQUIRED_FREE_IPS_PER_SUBNET} (mount target + ENI del conector)`,
      });
    }
  }
  return findings;
}

/** La tabla asociada explícitamente o, si no hay, la principal (regla de EC2). */
function routeTableOf(
  subnetId: string,
  tables: readonly RouteTableFacts[],
): RouteTableFacts | undefined {
  return tables.find((table) => table.subnetIds.includes(subnetId)) ?? tables.find((t) => t.main);
}

function egressFinding(
  subnetIds: readonly string[],
  tables: readonly RouteTableFacts[],
): NetworkFinding {
  const withNat = subnetIds.filter(
    (subnetId) => routeTableOf(subnetId, tables)?.defaultRoute === "nat",
  ).length;
  return {
    level: "OK",
    code: "internet-egress",
    message:
      "el conector de esta pila sólo deja salir NFS (2049) hacia los mount targets; la " +
      "salida a internet de un sandbox que use la VPC depende del NAT de tu VPC " +
      `(${withNat} de ${subnetIds.length} subredes con ruta por defecto a un NAT) y de ` +
      "un conector que la permita (ninguno de esta pila)",
  };
}

/** Evalúa los hechos de EC2 para `vpcId`/`subnetIds` ya validados. */
export function assessNetwork(input: {
  readonly vpcId: string;
  readonly subnetIds: readonly string[];
  readonly vpc: VpcFacts | undefined;
  readonly subnets: readonly SubnetFacts[];
  readonly routeTables: readonly RouteTableFacts[];
  readonly cost: CostStatement;
}): EfsNetworkReport {
  const { vpcId, subnetIds, vpc, subnets, routeTables, cost } = input;
  const findings = vpcFindings(vpc);
  if (vpc !== undefined) {
    findings.push(...subnetFindings(vpcId, subnetIds, subnets));
  }
  const known = new Map(subnets.map((subnet) => [subnet.subnetId, subnet]));
  const zones = [
    ...new Set(
      subnetIds.flatMap((subnetId) => {
        const subnet = known.get(subnetId);
        return subnet === undefined ? [] : [subnet.availabilityZone];
      }),
    ),
  ];
  if (vpc !== undefined && zones.length === 1) {
    findings.push({
      level: "WARN",
      code: "single-az",
      message:
        "una sola AZ: sin redundancia de mount target, y un MicroVM que salga por " +
        "otra AZ cruza zonas (transferencia entre AZs facturada)",
    });
  }
  if (vpc !== undefined) {
    findings.push(egressFinding(subnetIds, routeTables));
  }
  const status = findings.reduce<FindingLevel>(
    (worst, finding) =>
      LEVEL_ORDER.indexOf(finding.level) > LEVEL_ORDER.indexOf(worst) ? finding.level : worst,
    "OK",
  );
  return {
    vpcId,
    subnetIds: [...subnetIds],
    availabilityZones: zones,
    findings,
    cost,
    ok: status !== "FAIL",
    status,
  };
}

/** Valida (sin I/O) y, si es correcto, lee los hechos de EC2 y los evalúa. */
export async function inspectNetwork(
  inspector: NetworkInspector,
  vpcId: unknown,
  subnetIds: unknown,
  cost: CostStatement,
): Promise<EfsNetworkReport> {
  const validatedVpc = validateVpcId(vpcId);
  const validatedSubnets = validateSubnetIds(subnetIds);
  const vpc = await inspector.vpc(validatedVpc);
  const subnets = vpc === undefined ? [] : await inspector.subnets(validatedSubnets);
  const routeTables = vpc === undefined ? [] : await inspector.routeTables(validatedVpc);
  return assessNetwork({
    vpcId: validatedVpc,
    subnetIds: validatedSubnets,
    vpc,
    subnets,
    routeTables,
    cost,
  });
}
