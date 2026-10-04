/**
 * `create({ volumes })` resuelve la IP de mount target con
 * `DescribeMountTargets` (un `EFSClient` del peer opcional, con la región del
 * plano de control) antes de `run-microvm`, y no construye ningún cliente de
 * EFS cuando cada volumen ya trae su IP. El peer se sustituye con
 * `vi.mock`: ninguna llamada sale a AWS. Espejo de
 * `test_efs_volumes_create_resolve.py`.
 */

import { create } from "@bufbuild/protobuf";
import { describe, expect, test, vi } from "vitest";
import { VolumeError } from "../../src/errors.js";
import {
  ConfigSection,
  SectionCode,
  SectionResultSchema,
} from "../../src/gen/rayito/v1/configure_pb.js";
import { AgentFeaturesSchema } from "../../src/gen/rayito/v1/features_pb.js";
import { Sandbox } from "../../src/sandbox/sandbox.js";
import { EfsVolume } from "../../src/volumes/domain.js";
import { FakeControlPlane, IMAGE_ARN } from "./fake/control-plane.js";
import { ACCESS_TOKEN, startRayd } from "./helpers.js";

const efs = vi.hoisted(() => ({
  clients: [] as object[],
  commands: [] as { FileSystemId?: string }[],
  mountTargets: [] as object[],
}));

vi.mock("@aws-sdk/client-efs", () => {
  class DescribeMountTargetsCommand {
    constructor(readonly input: { FileSystemId?: string }) {}
  }
  class Unused {}
  class EFSClient {
    constructor(config: object) {
      efs.clients.push(config);
    }

    async send(command: DescribeMountTargetsCommand): Promise<object> {
      efs.commands.push(command.input);
      return { MountTargets: efs.mountTargets };
    }
  }
  return {
    EFSClient,
    DescribeMountTargetsCommand,
    CreateAccessPointCommand: Unused,
    DescribeAccessPointsCommand: Unused,
    DeleteAccessPointCommand: Unused,
    DescribeFileSystemsCommand: Unused,
    DeleteFileSystemCommand: Unused,
  };
});

// Marcadores de documentación (cuenta y recursos ficticios).
const FILE_SYSTEM_ID = "fs-0123456789abcdef0";
const ROLE = "arn:aws:iam::123456789012:role/rayito-execution";
const CONNECTOR = "arn:aws:lambda:us-east-1:123456789012:network-connector:rayito-efs";
const MOUNT_PATH = "/mnt/datos";
const IP = "10.0.1.10";

function volume(mountTargetIp?: string): EfsVolume {
  return new EfsVolume({
    fileSystemId: FILE_SYSTEM_ID,
    accessPointId: "fsap-0123456789abcdef0",
    ...(mountTargetIp === undefined ? {} : { mountTargetIp }),
  });
}

function reset(mountTargets: object[]): void {
  efs.clients.length = 0;
  efs.commands.length = 0;
  efs.mountTargets = mountTargets;
}

describe("create({ volumes }) and DescribeMountTargets", () => {
  test("a volume without IP is resolved once, before run-microvm", async () => {
    reset([{ AvailabilityZoneId: "use1-az1", LifeCycleState: "available", IpAddress: IP }]);
    const rayd = await startRayd(ACCESS_TOKEN);
    rayd.health.features = create(AgentFeaturesSchema, { configure: true, efsVolumes: true });
    rayd.configure.nextResults = [
      create(SectionResultSchema, {
        section: ConfigSection.EFS_VOLUMES,
        code: SectionCode.APPLIED,
      }),
    ];
    const plane = new FakeControlPlane({ endpoint: rayd.host, states: ["RUNNING"] });
    try {
      const sbx = await Sandbox.create({
        template: IMAGE_ARN,
        idle: null,
        accessToken: ACCESS_TOKEN,
        controlPlane: plane,
        transport: rayd.transport,
        executionRoleArn: ROLE,
        egress: [CONNECTOR],
        volumes: { [MOUNT_PATH]: volume(), "/mnt/otro": volume() },
      });
      try {
        expect(efs.clients).toHaveLength(1);
        expect(efs.commands).toEqual([{ FileSystemId: FILE_SYSTEM_ID }]);
        const [request] = rayd.configure.configureRequests;
        expect(request?.efsVolumes?.mounts.map((m) => m.mountTargetIp)).toEqual([IP, IP]);
      } finally {
        sbx.close();
      }
    } finally {
      await rayd.close();
    }
  });

  test("no available mount target fails before launching anything", async () => {
    reset([{ AvailabilityZoneId: "use1-az1", LifeCycleState: "creating", IpAddress: IP }]);
    const plane = new FakeControlPlane({ endpoint: "127.0.0.1:1", states: ["RUNNING"] });
    await expect(
      Sandbox.create({
        template: IMAGE_ARN,
        controlPlane: plane,
        executionRoleArn: ROLE,
        egress: [CONNECTOR],
        volumes: { [MOUNT_PATH]: volume() },
      }),
    ).rejects.toBeInstanceOf(VolumeError);
    expect(plane.callsTo("runMicrovm")).toHaveLength(0);
  });

  test("volumes that carry their IP build no EFS client", async () => {
    reset([]);
    const rayd = await startRayd(ACCESS_TOKEN);
    rayd.health.features = create(AgentFeaturesSchema, { configure: true, efsVolumes: true });
    const plane = new FakeControlPlane({ endpoint: rayd.host, states: ["RUNNING"] });
    try {
      const sbx = await Sandbox.create({
        template: IMAGE_ARN,
        idle: null,
        accessToken: ACCESS_TOKEN,
        controlPlane: plane,
        transport: rayd.transport,
        executionRoleArn: ROLE,
        egress: [CONNECTOR],
        volumes: { [MOUNT_PATH]: volume(IP) },
      });
      sbx.close();
      expect(efs.clients).toHaveLength(0);
    } finally {
      await rayd.close();
    }
  });

  test("a sandbox without volumes builds no EFS client", async () => {
    reset([]);
    const rayd = await startRayd(ACCESS_TOKEN);
    const plane = new FakeControlPlane({ endpoint: rayd.host, states: ["RUNNING"] });
    try {
      const sbx = await Sandbox.create({
        template: IMAGE_ARN,
        idle: null,
        accessToken: ACCESS_TOKEN,
        controlPlane: plane,
        transport: rayd.transport,
      });
      sbx.close();
      expect(efs.clients).toHaveLength(0);
    } finally {
      await rayd.close();
    }
  });
});
