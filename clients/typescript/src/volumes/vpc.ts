/**
 * Adaptador de `NetworkInspector` (`network.ts`) sobre `@aws-sdk/client-ec2`
 * (peer opcional, cargado sólo en el primer `check()`): sólo lectura de EC2
 * con las credenciales del llamante. Espejo de `rayito._volumes._vpc`. Las
 * únicas operaciones y parámetros son los de AWS_API_NOTES.md §22
 * ("Comprobación previa de la VPC").
 */

import { awsCode, LazyAwsApi, loadOptionalSdkClient } from "../aws/optional-client.js";
import type { Credentials } from "./efs.js";
import type {
  DefaultRouteTarget,
  NetworkInspector,
  RouteTableFacts,
  SubnetFacts,
  VpcFacts,
} from "./network.js";

export const EC2_PEER = "@aws-sdk/client-ec2";
/** `DescribeVpcs` con un `VpcIds` inexistente falla con este código (modelo `ec2`). */
const VPC_NOT_FOUND = "InvalidVpcID.NotFound";
/** La ruta por defecto de IPv4 (`Route.DestinationCidrBlock`). */
const DEFAULT_ROUTE_CIDR = "0.0.0.0/0";
/** Prefijo de un internet gateway en `Route.GatewayId`. */
const INTERNET_GATEWAY_PREFIX = "igw-";

interface Ec2Route {
  readonly DestinationCidrBlock?: string | undefined;
  readonly NatGatewayId?: string | undefined;
  readonly GatewayId?: string | undefined;
}

/** Lo que Rayito usa de EC2 (y sólo esto). */
export interface Ec2Api {
  describeVpcs(input: { VpcIds: string[] }): Promise<{
    Vpcs?: Array<{ VpcId?: string | undefined; State?: string | undefined }> | undefined;
  }>;
  describeVpcAttribute(input: {
    VpcId: string;
    Attribute: "enableDnsSupport" | "enableDnsHostnames";
  }): Promise<{
    EnableDnsSupport?: { Value?: boolean | undefined } | undefined;
    EnableDnsHostnames?: { Value?: boolean | undefined } | undefined;
  }>;
  describeSubnets(input: { Filters: Array<{ Name: string; Values: string[] }> }): Promise<{
    Subnets?:
      | Array<{
          SubnetId?: string | undefined;
          VpcId?: string | undefined;
          AvailabilityZone?: string | undefined;
          AvailableIpAddressCount?: number | undefined;
          State?: string | undefined;
        }>
      | undefined;
  }>;
  describeRouteTables(input: {
    Filters: Array<{ Name: string; Values: string[] }>;
    NextToken?: string;
  }): Promise<{
    RouteTables?:
      | Array<{
          Associations?:
            | Array<{ SubnetId?: string | undefined; Main?: boolean | undefined }>
            | undefined;
          Routes?: Ec2Route[] | undefined;
        }>
      | undefined;
    NextToken?: string | undefined;
  }>;
}

interface Ec2Module {
  readonly EC2Client: new (config: object) => { send(command: unknown): Promise<unknown> };
  readonly DescribeVpcsCommand: new (input: object) => unknown;
  readonly DescribeVpcAttributeCommand: new (input: object) => unknown;
  readonly DescribeSubnetsCommand: new (input: object) => unknown;
  readonly DescribeRouteTablesCommand: new (input: object) => unknown;
}

async function ec2Api(region: string, credentials: Credentials): Promise<Ec2Api> {
  const { sdk, send } = await loadOptionalSdkClient<Ec2Module>(
    EC2_PEER,
    "EC2 (EfsVolumes.check, comprobación previa de la VPC)",
    (module) => module.EC2Client,
    region,
    credentials,
  );
  return {
    describeVpcs: (input) => send(new sdk.DescribeVpcsCommand(input)),
    describeVpcAttribute: (input) => send(new sdk.DescribeVpcAttributeCommand(input)),
    describeSubnets: (input) => send(new sdk.DescribeSubnetsCommand(input)),
    describeRouteTables: (input) => send(new sdk.DescribeRouteTablesCommand(input)),
  };
}

export function defaultRouteTarget(routes: readonly Ec2Route[]): DefaultRouteTarget | undefined {
  const route = routes.find((candidate) => candidate.DestinationCidrBlock === DEFAULT_ROUTE_CIDR);
  if (route === undefined) {
    return undefined;
  }
  if (route.NatGatewayId) {
    return "nat";
  }
  return (route.GatewayId ?? "").startsWith(INTERNET_GATEWAY_PREFIX) ? "internet-gateway" : "other";
}

/** Construirlo no llama a AWS ni carga el peer. */
export class Ec2NetworkInspector implements NetworkInspector {
  readonly #api: LazyAwsApi<Ec2Api>;

  constructor(options: {
    readonly region?: string | undefined;
    readonly credentials?: Credentials | undefined;
    /** Un cliente propio con la forma de `Ec2Api`, p. ej. en tests. */
    readonly client?: Ec2Api | undefined;
  }) {
    this.#api = new LazyAwsApi(
      options.region,
      "falta la región: pasa `region` o define AWS_REGION",
      (region) => ec2Api(region, options.credentials),
      options.client,
    );
  }

  async vpc(vpcId: string): Promise<VpcFacts | undefined> {
    const api = await this.#api.get();
    let described: Awaited<ReturnType<Ec2Api["describeVpcs"]>>;
    try {
      described = await api.describeVpcs({ VpcIds: [vpcId] });
    } catch (error) {
      if (awsCode(error) === VPC_NOT_FOUND) {
        return undefined;
      }
      throw error;
    }
    const vpc = described.Vpcs?.[0];
    if (vpc === undefined) {
      return undefined;
    }
    const support = await api.describeVpcAttribute({ VpcId: vpcId, Attribute: "enableDnsSupport" });
    const hostnames = await api.describeVpcAttribute({
      VpcId: vpcId,
      Attribute: "enableDnsHostnames",
    });
    return {
      vpcId,
      state: vpc.State ?? "",
      dnsSupport: support.EnableDnsSupport?.Value ?? false,
      dnsHostnames: hostnames.EnableDnsHostnames?.Value ?? false,
    };
  }

  async subnets(subnetIds: readonly string[]): Promise<SubnetFacts[]> {
    const api = await this.#api.get();
    // `Filters` y no `SubnetIds`: con un id inexistente `SubnetIds` falla
    // entera; el filtro devuelve las que existen.
    const described = await api.describeSubnets({
      Filters: [{ Name: "subnet-id", Values: [...subnetIds] }],
    });
    return (described.Subnets ?? []).map((subnet) => ({
      subnetId: subnet.SubnetId ?? "",
      vpcId: subnet.VpcId ?? "",
      availabilityZone: subnet.AvailabilityZone ?? "",
      availableIps: subnet.AvailableIpAddressCount ?? 0,
      state: subnet.State ?? "",
    }));
  }

  async routeTables(vpcId: string): Promise<RouteTableFacts[]> {
    const api = await this.#api.get();
    const tables: RouteTableFacts[] = [];
    let nextToken: string | undefined;
    do {
      const page = await api.describeRouteTables({
        Filters: [{ Name: "vpc-id", Values: [vpcId] }],
        ...(nextToken === undefined ? {} : { NextToken: nextToken }),
      });
      for (const table of page.RouteTables ?? []) {
        const associations = table.Associations ?? [];
        tables.push({
          subnetIds: associations.flatMap((a) => (a.SubnetId ? [a.SubnetId] : [])),
          main: associations.some((a) => a.Main === true),
          defaultRoute: defaultRouteTarget(table.Routes ?? []),
        });
      }
      nextToken = page.NextToken;
    } while (nextToken);
    return tables;
  }
}
