/**
 * `EfsVolumes` en una VPC existente (`m15-efs-volumes`): la comprobación
 * previa (`network.ts` + `vpc.ts` sobre un `Ec2Api` falso), los parámetros
 * exactos de `deploy()`, `volumeStore()` y `destroy({ deleteFileSystem })`.
 * Espejo de `test_m15_efs_volumes_network.py` y
 * `test_m15_efs_volumes_facade.py`.
 */

import { describe, expect, test } from "vitest";
import { InvalidArgumentError, VolumeError } from "../../src/errors.js";
import type { StackComponent } from "../../src/stacks/model.js";
import { OptionalStacks } from "../../src/stacks/service.js";
import type { DescribedAccessPoint, EfsFileSystemApi } from "../../src/volumes/efs.js";
import { EfsVolumes } from "../../src/volumes/efs-volumes.js";
import { MAX_SUBNETS, REQUIRED_FREE_IPS_PER_SUBNET } from "../../src/volumes/network.js";
import { VolumeStore } from "../../src/volumes/store.js";
import { defaultRouteTarget, type Ec2Api, Ec2NetworkInspector } from "../../src/volumes/vpc.js";
import { FakeStackProvisioner } from "./m15-fake-stacks.js";

const VPC_ID = "vpc-0123456789abcdef0";
const SUBNET_A = "subnet-aaaaaaaaaaaa";
const SUBNET_B = "subnet-bbbbbbbbbbbb";
const SUBNET_C = "subnet-cccccccccccc";
const OTHER_VPC_ID = "vpc-ffffffffffff";
const FILE_SYSTEM_ID = "fs-0123456789abcdef0";
const STACK = "rayito-efs-volumes";
const ACCESS_POINT_ARN =
  "arn:aws:elasticfilesystem:us-east-1:123456789012:access-point/fsap-0123456789abcdef0";

function awsError(name: string): Error {
  const error = new Error("redacted");
  error.name = name;
  return error;
}

interface FakeSubnet {
  SubnetId: string;
  VpcId: string;
  AvailabilityZone: string;
  AvailableIpAddressCount: number;
  State: string;
}

function subnet(subnetId: string, az: string, overrides: Partial<FakeSubnet> = {}): FakeSubnet {
  return {
    SubnetId: subnetId,
    VpcId: VPC_ID,
    AvailabilityZone: az,
    AvailableIpAddressCount: 250,
    State: "available",
    ...overrides,
  };
}

class FakeEc2 implements Ec2Api {
  readonly calls: string[] = [];
  vpcExists = true;
  dns = true;
  subnets: FakeSubnet[] = [
    subnet(SUBNET_A, "us-east-1a"),
    subnet(SUBNET_B, "us-east-1b"),
    subnet(SUBNET_C, "us-east-1c"),
  ];
  routeTables: Array<{
    Associations: Array<{ Main?: boolean; SubnetId?: string }>;
    Routes: Array<{ DestinationCidrBlock?: string; NatGatewayId?: string; GatewayId?: string }>;
  }> = [
    {
      Associations: [{ Main: true }],
      Routes: [{ DestinationCidrBlock: "0.0.0.0/0", NatGatewayId: "nat-0abc" }],
    },
  ];

  async describeVpcs(input: { VpcIds: string[] }) {
    this.calls.push("describeVpcs");
    if (!this.vpcExists) {
      throw awsError("InvalidVpcID.NotFound");
    }
    return { Vpcs: [{ VpcId: input.VpcIds[0], State: "available" }] };
  }

  async describeVpcAttribute(input: { Attribute: string }) {
    this.calls.push(`describeVpcAttribute:${input.Attribute}`);
    return { EnableDnsSupport: { Value: this.dns }, EnableDnsHostnames: { Value: this.dns } };
  }

  async describeSubnets(input: { Filters: Array<{ Name: string; Values: string[] }> }) {
    this.calls.push("describeSubnets");
    const wanted = new Set(input.Filters[0]?.Values ?? []);
    return { Subnets: this.subnets.filter((s) => wanted.has(s.SubnetId)) };
  }

  async describeRouteTables() {
    this.calls.push("describeRouteTables");
    return { RouteTables: this.routeTables };
  }
}

class RecordingProvisioner extends FakeStackProvisioner {
  parameters: Readonly<Record<string, string>> = {};

  override async create(
    component: StackComponent,
    options: { readonly stackName: string; readonly parameters: Readonly<Record<string, string>> },
  ): Promise<void> {
    await super.create(component, options);
    this.parameters = { ...options.parameters };
    this.stacks.set(options.stackName, {
      name: options.stackName,
      state: "CREATE_COMPLETE",
      outputs: { FileSystemId: FILE_SYSTEM_ID },
    });
  }
}

class FakeFileSystemApi implements EfsFileSystemApi {
  readonly calls: string[] = [];
  drainPolls = 1;
  exists = true;
  tags = [{ Key: "rayito", Value: "efs-volumes" }];
  accessPoints = ["fsap-1", "fsap-stale"];

  async createAccessPoint(): Promise<DescribedAccessPoint> {
    throw new Error("destroy never creates");
  }

  async describeAccessPoints() {
    this.calls.push("describeAccessPoints");
    return { AccessPoints: this.accessPoints.map((id) => ({ AccessPointId: id })) };
  }

  async deleteAccessPoint(input: { AccessPointId: string }) {
    this.calls.push(`deleteAccessPoint:${input.AccessPointId}`);
    if (input.AccessPointId === "fsap-stale") {
      throw awsError("AccessPointNotFound");
    }
    return {};
  }

  async describeFileSystems(input: { FileSystemId: string }) {
    this.calls.push("describeFileSystems");
    if (!this.exists) {
      throw awsError("FileSystemNotFound");
    }
    return { FileSystems: [{ FileSystemId: input.FileSystemId, Tags: this.tags }] };
  }

  async describeMountTargets() {
    this.calls.push("describeMountTargets");
    const remaining = this.drainPolls;
    this.drainPolls = Math.max(0, this.drainPolls - 1);
    return { MountTargets: remaining > 0 ? [{ MountTargetId: "fsmt-1" }] : [] };
  }

  async deleteFileSystem(input: { FileSystemId: string }) {
    expect(input).toEqual({ FileSystemId: FILE_SYSTEM_ID });
    this.calls.push("deleteFileSystem");
    return {};
  }
}

function facade(overrides: { ec2?: FakeEc2; efs?: FakeFileSystemApi; now?: () => number } = {}): {
  volumes: EfsVolumes;
  provisioner: RecordingProvisioner;
  ec2: FakeEc2;
  efs: FakeFileSystemApi;
} {
  const provisioner = new RecordingProvisioner();
  const ec2 = overrides.ec2 ?? new FakeEc2();
  const efs = overrides.efs ?? new FakeFileSystemApi();
  const volumes = new EfsVolumes({
    region: "us-east-1",
    stacks: new OptionalStacks({ provisioner }),
    network: new Ec2NetworkInspector({ region: "us-east-1", client: ec2 }),
    efsClient: efs,
    sleep: async () => {},
    ...(overrides.now === undefined ? {} : { now: overrides.now }),
  });
  return { volumes, provisioner, ec2, efs };
}

function codes(findings: readonly { code: string; level: string }[]): Record<string, string> {
  return Object.fromEntries(findings.map((f) => [f.code, f.level]));
}

describe("EfsVolumes.check", () => {
  test("a healthy three-AZ VPC passes, only reads, and reports what deploy creates", async () => {
    const { volumes, ec2 } = facade();
    const report = await volumes.check({
      vpcId: VPC_ID,
      subnetIds: [SUBNET_A, SUBNET_B, SUBNET_C],
    });
    expect(report.ok).toBe(true);
    expect(report.status).toBe("OK");
    expect(report.availabilityZones).toEqual(["us-east-1a", "us-east-1b", "us-east-1c"]);
    expect(report.cost.creates).toContain("AWS::Lambda::NetworkConnector");
    expect(codes(report.findings)).toEqual({ "internet-egress": "OK" });
    expect(ec2.calls.every((call) => call.startsWith("describe"))).toBe(true);
  });

  test("counts NAT and other default routes (e.g. a transit gateway)", async () => {
    // Medido 2026-10-04: subredes privadas con salida por un transit gateway.
    const ec2 = new FakeEc2();
    ec2.routeTables = [
      {
        Associations: [{ Main: true }],
        // `TransitGatewayId` no está en el modelo que Rayito lee: una ruta por
        // defecto sin NAT ni internet gateway es "otra puerta".
        Routes: [{ DestinationCidrBlock: "0.0.0.0/0" }],
      },
    ];
    const report = await facade({ ec2 }).volumes.check({
      vpcId: VPC_ID,
      subnetIds: [SUBNET_A, SUBNET_B],
    });
    const egress = report.findings.find((finding) => finding.code === "internet-egress");
    expect(egress?.message).toContain("0 de 2 a un NAT, 2 a otra puerta");
  });

  test("accepts the CLI comma form", async () => {
    const { volumes } = facade();
    const report = await volumes.check({ vpcId: VPC_ID, subnetIds: `${SUBNET_A}, ${SUBNET_B}` });
    expect(report.subnetIds).toEqual([SUBNET_A, SUBNET_B]);
  });

  test.each([
    [[]],
    [""],
    [[SUBNET_A, SUBNET_A]],
    [["subnet-xyz"]],
    [[SUBNET_A, SUBNET_B, SUBNET_C, "subnet-dddddddddddd"]],
  ])("rejects malformed subnet ids %j before any call", async (subnetIds) => {
    const { volumes, ec2 } = facade();
    await expect(volumes.check({ vpcId: VPC_ID, subnetIds })).rejects.toThrow(InvalidArgumentError);
    expect(ec2.calls).toEqual([]);
  });

  test("never echoes a malformed VPC id", async () => {
    const { volumes } = facade();
    await expect(
      volumes.check({ vpcId: "not-a-vpc-secret", subnetIds: [SUBNET_A] }),
    ).rejects.toThrow(/^(?!.*not-a-vpc-secret).*$/);
  });

  test("MAX_SUBNETS matches the template's three mount-target slots", () => {
    expect(MAX_SUBNETS).toBe(3);
  });

  test("a missing VPC fails and reads nothing else", async () => {
    const ec2 = new FakeEc2();
    ec2.vpcExists = false;
    const { volumes } = facade({ ec2 });
    const report = await volumes.check({ vpcId: VPC_ID, subnetIds: [SUBNET_A] });
    expect(report.ok).toBe(false);
    expect(codes(report.findings)).toEqual({ "vpc-missing": "FAIL" });
    expect(ec2.calls).toEqual(["describeVpcs"]);
  });

  test("subnets must exist, belong to the VPC, sit in distinct AZs and have free IPs", async () => {
    const ec2 = new FakeEc2();
    ec2.subnets = [
      subnet(SUBNET_A, "us-east-1a"),
      subnet(SUBNET_B, "us-east-1a", { AvailableIpAddressCount: REQUIRED_FREE_IPS_PER_SUBNET - 1 }),
      subnet(SUBNET_C, "us-east-1c", { VpcId: OTHER_VPC_ID }),
    ];
    const { volumes } = facade({ ec2 });
    const report = await volumes.check({
      vpcId: VPC_ID,
      subnetIds: [SUBNET_A, SUBNET_B, SUBNET_C],
    });
    const found = codes(report.findings);
    expect(found["subnet-same-az"]).toBe("FAIL");
    expect(found["subnet-other-vpc"]).toBe("FAIL");
    expect(found["subnet-free-ips"]).toBe("FAIL");
    const missing = await facade({ ec2 }).volumes.check({
      vpcId: VPC_ID,
      subnetIds: [SUBNET_A, "subnet-dddddddddddd"],
    });
    expect(codes(missing.findings)["subnet-missing"]).toBe("FAIL");
  });

  test("one AZ and no DNS are warnings", async () => {
    const ec2 = new FakeEc2();
    ec2.dns = false;
    const { volumes } = facade({ ec2 });
    const report = await volumes.check({ vpcId: VPC_ID, subnetIds: [SUBNET_A] });
    expect(report.ok).toBe(true);
    expect(report.status).toBe("WARN");
    expect(codes(report.findings)).toEqual({
      "vpc-dns-support": "WARN",
      "vpc-dns-hostnames": "WARN",
      "single-az": "WARN",
      "internet-egress": "OK",
    });
  });

  test.each([
    [[{ DestinationCidrBlock: "0.0.0.0/0", NatGatewayId: "nat-1" }], "nat"],
    [[{ DestinationCidrBlock: "0.0.0.0/0", GatewayId: "igw-1" }], "internet-gateway"],
    [[{ DestinationCidrBlock: "0.0.0.0/0", GatewayId: "vgw-1" }], "other"],
    [[{ DestinationCidrBlock: "10.0.0.0/16", GatewayId: "local" }], undefined],
  ])("defaultRouteTarget(%j) is %s", (routes, expected) => {
    expect(defaultRouteTarget(routes)).toBe(expected);
  });
});

describe("EfsVolumes.deploy / volumeStore / destroy", () => {
  test("construction calls nothing", () => {
    const { volumes, provisioner, ec2, efs } = facade();
    expect(volumes.stackName).toBe(STACK);
    expect([provisioner.calls, ec2.calls, efs.calls]).toEqual([[], [], []]);
  });

  test("deploy checks first and sends exactly the template parameters", async () => {
    const { volumes, provisioner, ec2 } = facade();
    const status = await volumes.deploy({ vpcId: VPC_ID, subnetIds: [SUBNET_A, SUBNET_B] });
    expect(status.outputs.FileSystemId).toBe(FILE_SYSTEM_ID);
    expect(ec2.calls[0]).toBe("describeVpcs");
    expect(provisioner.parameters).toEqual({
      VpcId: VPC_ID,
      SubnetIds: `${SUBNET_A},${SUBNET_B}`,
      AllowWrite: "true",
      AccessPointArns: "",
      ReadOnlyAccessPointArns: "",
      ConnectorName: "rayito-efs",
    });
  });

  test("deploy denies ClientWrite on read-only access points even when writes are allowed", async () => {
    const { volumes, provisioner } = facade();
    await volumes.deploy({
      vpcId: VPC_ID,
      subnetIds: [SUBNET_A],
      readOnlyAccessPointArns: [ACCESS_POINT_ARN],
    });
    expect(provisioner.parameters.AllowWrite).toBe("true");
    expect(provisioner.parameters.ReadOnlyAccessPointArns).toBe(ACCESS_POINT_ARN);
  });

  test("deploy rejects a bad read-only access point ARN naming its option", async () => {
    const { volumes, provisioner, ec2 } = facade();
    await expect(
      volumes.deploy({ vpcId: VPC_ID, subnetIds: [SUBNET_A], readOnlyAccessPointArns: ["fsap-1"] }),
    ).rejects.toThrow(/readOnlyAccessPointArns/);
    expect([provisioner.calls, ec2.calls]).toEqual([[], []]);
  });

  test("deploy read-only and scoped to access points", async () => {
    const { volumes, provisioner } = facade();
    await volumes.deploy({
      vpcId: VPC_ID,
      subnetIds: [SUBNET_A],
      allowWrite: false,
      accessPointArns: [ACCESS_POINT_ARN],
      connectorName: "team-efs",
    });
    expect(provisioner.parameters.AllowWrite).toBe("false");
    expect(provisioner.parameters.AccessPointArns).toBe(ACCESS_POINT_ARN);
    expect(provisioner.parameters.ConnectorName).toBe("team-efs");
  });

  test("deploy refuses a VPC that fails the check and creates nothing", async () => {
    const ec2 = new FakeEc2();
    ec2.subnets = [subnet(SUBNET_A, "us-east-1a"), subnet(SUBNET_B, "us-east-1a")];
    const { volumes, provisioner } = facade({ ec2 });
    await expect(
      volumes.deploy({ vpcId: VPC_ID, subnetIds: [SUBNET_A, SUBNET_B] }),
    ).rejects.toThrow(/no se creó nada/);
    expect(provisioner.calls.filter(([call]) => call === "create" || call === "update")).toEqual(
      [],
    );
  });

  test("deploy rejects a bad access point ARN before any call", async () => {
    const { volumes, provisioner, ec2 } = facade();
    await expect(
      volumes.deploy({ vpcId: VPC_ID, subnetIds: [SUBNET_A], accessPointArns: ["fsap-1"] }),
    ).rejects.toThrow(InvalidArgumentError);
    expect([provisioner.calls, ec2.calls]).toEqual([[], []]);
  });

  test("volumeStore reads the file system from the stack", async () => {
    const { volumes } = facade();
    await expect(volumes.volumeStore()).rejects.toThrow(VolumeError);
    await volumes.deploy({ vpcId: VPC_ID, subnetIds: [SUBNET_A] });
    const store = await volumes.volumeStore();
    expect(store).toBeInstanceOf(VolumeStore);
    expect(store.fileSystemId).toBe(FILE_SYSTEM_ID);
  });

  test("destroy keeps the file system by default", async () => {
    const { volumes, provisioner, efs } = facade();
    await volumes.deploy({ vpcId: VPC_ID, subnetIds: [SUBNET_A] });
    await volumes.destroy();
    expect(provisioner.calls).toContainEqual(["delete", STACK]);
    expect(efs.calls).toEqual([]);
  });

  test("destroy({ deleteFileSystem: true }) removes everything deploy created", async () => {
    const efs = new FakeFileSystemApi();
    efs.drainPolls = 2;
    const { volumes, provisioner } = facade({ efs });
    await volumes.deploy({ vpcId: VPC_ID, subnetIds: [SUBNET_A] });
    await volumes.destroy({ deleteFileSystem: true });
    expect(provisioner.calls).toContainEqual(["delete", STACK]);
    expect(efs.calls).toEqual([
      "describeFileSystems",
      "describeMountTargets",
      "describeMountTargets",
      "describeMountTargets",
      "describeAccessPoints",
      "deleteAccessPoint:fsap-1",
      "deleteAccessPoint:fsap-stale",
      "deleteFileSystem",
    ]);
  });

  test("deleteFileSystem needs wait", async () => {
    const { volumes, provisioner } = facade();
    await expect(volumes.destroy({ deleteFileSystem: true, wait: false })).rejects.toThrow(
      InvalidArgumentError,
    );
    expect(provisioner.calls).toEqual([]);
  });

  test("an already-gone file system is a no-op", async () => {
    const efs = new FakeFileSystemApi();
    efs.exists = false;
    const { volumes } = facade({ efs });
    await volumes.deploy({ vpcId: VPC_ID, subnetIds: [SUBNET_A] });
    await volumes.destroy({ deleteFileSystem: true });
    expect(efs.calls).toEqual(["describeFileSystems"]);
  });

  test("deleteFileSystem refuses one without the stack tag", async () => {
    const efs = new FakeFileSystemApi();
    efs.tags = [{ Key: "team", Value: "data" }];
    const { volumes } = facade({ efs });
    await expect(volumes.deleteFileSystem(FILE_SYSTEM_ID)).rejects.toThrow(/etiqueta/);
    expect(efs.calls).toEqual(["describeFileSystems"]);
  });

  test("deleteFileSystem after a CLI destroy", async () => {
    const efs = new FakeFileSystemApi();
    efs.drainPolls = 0;
    efs.accessPoints = [];
    const { volumes, provisioner } = facade({ efs });
    await volumes.deleteFileSystem(FILE_SYSTEM_ID);
    expect(provisioner.calls).toEqual([]);
    expect(efs.calls).toEqual([
      "describeFileSystems",
      "describeMountTargets",
      "describeAccessPoints",
      "deleteFileSystem",
    ]);
    await expect(volumes.deleteFileSystem("not-a-file-system")).rejects.toThrow(
      InvalidArgumentError,
    );
  });

  test("stops if mount targets never drain", async () => {
    const efs = new FakeFileSystemApi();
    efs.drainPolls = 10_000;
    let clock = 0;
    const { volumes } = facade({
      efs,
      now: () => {
        clock += 60_000;
        return clock;
      },
    });
    await volumes.deploy({ vpcId: VPC_ID, subnetIds: [SUBNET_A] });
    await expect(volumes.destroy({ deleteFileSystem: true })).rejects.toThrow(/mount targets/);
    expect(efs.calls).not.toContain("deleteFileSystem");
  });
});
