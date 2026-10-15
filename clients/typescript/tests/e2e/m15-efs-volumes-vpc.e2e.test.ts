/**
 * `EfsVolumes` en una VPC existente contra AWS real (`RAYITO_E2E=1`,
 * `m15-efs-volumes`), espejo de `clients/python/tests/e2e/test_m15_efs_volumes_vpc.py`:
 * la comprobación previa de sólo lectura y el ciclo completo (despliegue, CRUD
 * de un volumen y `destroy({ deleteFileSystem: true })`), probando además que
 * la VPC del llamante no cambia (tablas de rutas, NACLs y grupos de seguridad
 * idénticos antes y después).
 *
 * Necesita `RAYITO_E2E_VPC_ID` (una VPC que ya existe y que puedes usar: nunca
 * se crea, modifica ni borra) y `RAYITO_E2E_SUBNET_IDS` (de 1 a 3 subredes de
 * esa VPC separadas por comas, cada una en otra AZ). Nunca imprime ni guarda un
 * id de la VPC, de las subredes ni de lo creado: sólo cuentas y códigos. Coste:
 * un sistema de ficheros vacío y sus mount targets durante unos minutos (~$0)
 * más el conector (sin cargo listado).
 */

import { randomUUID } from "node:crypto";
import {
  DescribeNetworkAclsCommand,
  DescribeRouteTablesCommand,
  DescribeSecurityGroupsCommand,
  EC2Client,
} from "@aws-sdk/client-ec2";
import {
  DescribeFileSystemPolicyCommand,
  DescribeFileSystemsCommand,
  DescribeMountTargetsCommand,
  EFSClient,
} from "@aws-sdk/client-efs";
import { afterAll, describe, expect, test } from "vitest";
import { EfsVolumes } from "../../src/index.js";
import { e2eEnabled, sleep } from "./helpers.js";

const VPC_ID_VAR = "RAYITO_E2E_VPC_ID";
const SUBNET_IDS_VAR = "RAYITO_E2E_SUBNET_IDS";
const NFS_PORT = 2049;
const E2E_TAG_KEY = "rayito:e2e";
const STACK_NAME_PREFIX = "rayito-efs-e2e-";
const NAME_SUFFIX_LENGTH = 8;
const FILE_SYSTEM_GONE_BUDGET_MS = 120_000;
const FILE_SYSTEM_GONE_POLL_MS = 2_000;
const DELETING_STATES = new Set(["deleting", "deleted"]);
const FILE_SYSTEM_NOT_FOUND = "FileSystemNotFound";
const TEST_TIMEOUT_MS = 1_800_000;

const suiteEnabled =
  e2eEnabled() && Boolean(process.env[VPC_ID_VAR]) && Boolean(process.env[SUBNET_IDS_VAR]);

function network(): { readonly vpcId: string; readonly subnetIds: string[] } {
  return {
    vpcId: process.env[VPC_ID_VAR] as string,
    subnetIds: (process.env[SUBNET_IDS_VAR] as string)
      .split(",")
      .map((subnet) => subnet.trim())
      .filter(Boolean),
  };
}

/**
 * Una lista de la API EC2 sin depender de su orden: EC2 no garantiza el orden
 * de `Routes`, `Associations`, `Entries` ni `IpPermissions` entre dos llamadas,
 * así que se compara como conjunto.
 */
function canonical(items: readonly unknown[] | undefined): string[] {
  return (items ?? []).map((item) => JSON.stringify(item)).sort();
}

interface NetworkSnapshot {
  readonly routes: readonly (readonly [string, string[], string[]])[];
  readonly acls: readonly (readonly [string, string[], string[]])[];
  readonly groups: readonly (readonly [string, string[], string[]])[];
}

/**
 * Lo que la pila no debe tocar nunca: las tablas de rutas, las NACLs y los
 * grupos de seguridad de la VPC. Con `groupsIn`, sólo esos grupos (los que ya
 * existían antes del despliegue): uno creado entretanto por la pila, o por otro
 * despliegue en la misma VPC, no cuenta como cambio.
 */
async function networkSnapshot(
  ec2: EC2Client,
  vpcId: string,
  groupsIn?: ReadonlySet<string>,
): Promise<NetworkSnapshot> {
  const filters = [{ Name: "vpc-id", Values: [vpcId] }];
  const tables = (await ec2.send(new DescribeRouteTablesCommand({ Filters: filters }))).RouteTables;
  const acls = (await ec2.send(new DescribeNetworkAclsCommand({ Filters: filters }))).NetworkAcls;
  const groups = (await ec2.send(new DescribeSecurityGroupsCommand({ Filters: filters })))
    .SecurityGroups;
  const byId = <T>(rows: readonly [string, T, T][]) =>
    [...rows].sort((a, b) => a[0].localeCompare(b[0]));
  return {
    routes: byId(
      (tables ?? []).map(
        (table) =>
          [table.RouteTableId ?? "", canonical(table.Routes), canonical(table.Associations)] as [
            string,
            string[],
            string[],
          ],
      ),
    ),
    acls: byId(
      (acls ?? []).map(
        (acl) =>
          [acl.NetworkAclId ?? "", canonical(acl.Entries), canonical(acl.Associations)] as [
            string,
            string[],
            string[],
          ],
      ),
    ),
    groups: byId(
      (groups ?? [])
        .filter((group) => groupsIn === undefined || groupsIn.has(group.GroupId ?? ""))
        .map(
          (group) =>
            [
              group.GroupId ?? "",
              canonical(group.IpPermissions),
              canonical(group.IpPermissionsEgress),
            ] as [string, string[], string[]],
        ),
    ),
  };
}

/**
 * Milisegundos hasta `FileSystemNotFound` tras `DeleteFileSystem`; mientras
 * tanto sólo se admite `deleting`/`deleted` (nunca `available`).
 */
async function msUntilFileSystemGone(efs: EFSClient, fileSystemId: string): Promise<number> {
  const started = performance.now();
  for (;;) {
    try {
      const described = await efs.send(
        new DescribeFileSystemsCommand({ FileSystemId: fileSystemId }),
      );
      expect(DELETING_STATES.has(described.FileSystems?.[0]?.LifeCycleState ?? "")).toBe(true);
    } catch (error) {
      if ((error as { name?: string }).name !== FILE_SYSTEM_NOT_FOUND) {
        throw error;
      }
      return performance.now() - started;
    }
    expect(performance.now() - started).toBeLessThan(FILE_SYSTEM_GONE_BUDGET_MS);
    await sleep(FILE_SYSTEM_GONE_POLL_MS);
  }
}

describe.runIf(suiteEnabled)(
  `EfsVolumes en una VPC existente (requiere RAYITO_E2E=1, ${VPC_ID_VAR} y ${SUBNET_IDS_VAR})`,
  () => {
    const region = process.env.AWS_REGION || process.env.AWS_DEFAULT_REGION || undefined;
    const stackName = `${STACK_NAME_PREFIX}${randomUUID().slice(0, NAME_SUFFIX_LENGTH)}`;
    const clientConfig = region === undefined ? {} : { region };
    const volumes = new EfsVolumes({ stackName, region });

    afterAll(async () => {
      await volumes.destroy({ deleteFileSystem: true });
      expect(await volumes.status()).toBeUndefined();
      console.log("[efs-volumes vpc e2e] cleanup: pila y sistema de ficheros borrados");
    }, TEST_TIMEOUT_MS);

    test("is read-only and accepts the network", async () => {
      const report = await new EfsVolumes({ region }).check(network());
      console.log(
        `[efs-volumes vpc e2e] check: ${report.status} ${JSON.stringify(
          report.findings.map((finding) => [finding.code, finding.level]),
        )}`,
      );
      expect(report.ok).toBe(true);
    });

    test(
      "deploy, CRUD and destroy leave the VPC untouched",
      async () => {
        const { vpcId, subnetIds } = network();
        const ec2 = new EC2Client(clientConfig);
        const efs = new EFSClient(clientConfig);
        const before = await networkSnapshot(ec2, vpcId);

        const runTag = randomUUID().slice(0, NAME_SUFFIX_LENGTH);
        const status = await volumes.deploy({
          vpcId,
          subnetIds,
          connectorName: stackName,
          tags: { [E2E_TAG_KEY]: runTag },
        });
        const outputs = status.outputs;
        const mountGroupId = outputs.MountTargetSecurityGroupId as string;
        const clientGroupId = outputs.ClientSecurityGroupId as string;
        console.log(
          `[efs-volumes vpc e2e] deploy: ${status.state}, connector ${outputs.ConnectorState}`,
        );
        const existing = new Set(before.groups.map((group) => group[0]));
        expect(existing.has(mountGroupId) || existing.has(clientGroupId)).toBe(false);

        const mountGroup = (
          await ec2.send(new DescribeSecurityGroupsCommand({ GroupIds: [mountGroupId] }))
        ).SecurityGroups?.[0];
        const rules = mountGroup?.IpPermissions ?? [];
        expect(rules).toHaveLength(1);
        expect([rules[0]?.IpProtocol, rules[0]?.FromPort, rules[0]?.ToPort]).toEqual([
          "tcp",
          NFS_PORT,
          NFS_PORT,
        ]);
        expect(rules[0]?.UserIdGroupPairs?.map((pair) => pair.GroupId)).toEqual([clientGroupId]);
        expect(rules[0]?.IpRanges ?? []).toHaveLength(0);

        const fileSystemId = outputs.FileSystemId as string;
        const described = (
          await efs.send(new DescribeFileSystemsCommand({ FileSystemId: fileSystemId }))
        ).FileSystems?.[0];
        const tags = Object.fromEntries((described?.Tags ?? []).map((tag) => [tag.Key, tag.Value]));
        expect(described?.Encrypted).toBe(true);
        expect(tags.rayito).toBe("efs-volumes");
        console.log(
          `[efs-volumes vpc e2e] file system tags propagated from the stack: ${E2E_TAG_KEY in tags}`,
        );
        const mountTargets = (
          await efs.send(new DescribeMountTargetsCommand({ FileSystemId: fileSystemId }))
        ).MountTargets;
        expect(mountTargets).toHaveLength(subnetIds.length);
        const policy = (
          await efs.send(new DescribeFileSystemPolicyCommand({ FileSystemId: fileSystemId }))
        ).Policy;
        expect(policy).toContain("aws:SecureTransport");

        const store = await volumes.volumeStore();
        const volume = await store.create(`e2e-${runTag}`);
        expect(volume.accessPointId.startsWith("fsap-")).toBe(true);
        expect(await store.destroy(volume.name ?? "")).toBe(true);

        const after = await networkSnapshot(ec2, vpcId, existing);
        expect(after).toEqual(before);
        console.log(
          "[efs-volumes vpc e2e] vpc untouched: rutas, NACLs y grupos existentes idénticos",
        );

        await volumes.destroy({ deleteFileSystem: true });
        const goneMs = await msUntilFileSystemGone(efs, fileSystemId);
        console.log(
          `[efs-volumes vpc e2e] file system gone after (s): ${(goneMs / 1000).toFixed(1)}`,
        );
      },
      TEST_TIMEOUT_MS,
    );
  },
);
