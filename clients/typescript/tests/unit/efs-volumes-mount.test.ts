/**
 * `create({ volumes })` monta de verdad (m15-efs-volumes, experimental):
 * la elección del mount target, la sección `efs_volumes`, el plazo de la
 * llamada a `Configure`, `prepareFeatures` y el camino completo de
 * `create()`/`reincarnate()`/`volumes()` contra un `rayd` falso. Espejo de
 * `test_efs_volumes_mount.py`.
 */

import { create } from "@bufbuild/protobuf";
import { describe, expect, test } from "vitest";
import {
  type AgentFeatures,
  type ConfigureSection,
  configureTimeoutMs,
} from "../../src/configure/base.js";
import {
  InvalidArgumentError,
  UnimplementedError,
  VolumeError,
  VolumeMountError,
  VolumeNotFoundError,
} from "../../src/errors.js";
import { planFeatures, prepareFeatures } from "../../src/feature-options.js";
import {
  ConfigSection,
  ConfigureRequestSchema,
  ConfigureStatusResponseSchema,
  SectionCode,
  SectionResultSchema,
} from "../../src/gen/rayito/v1/configure_pb.js";
import {
  EfsVolumeState,
  EfsVolumeStatusSchema,
  EfsVolumesStatusSchema,
} from "../../src/gen/rayito/v1/efs_volumes_pb.js";
import { AgentFeaturesSchema } from "../../src/gen/rayito/v1/features_pb.js";
import { S3Prefix } from "../../src/index.js";
import { S3Mount } from "../../src/s3-mounts/domain.js";
import { Sandbox } from "../../src/sandbox/sandbox.js";
import { EfsVolume } from "../../src/volumes/domain.js";
import type { DescribedMountTargets, EfsMountTargetsApi } from "../../src/volumes/efs.js";
import {
  chooseMountTargetIp,
  MountTargetResolver,
  resolveMountTargets,
} from "../../src/volumes/mount-targets.js";
import {
  EfsVolumesSection,
  fromProtoStatus,
  IMAGE_REASON,
  VOLUME_APPLY_TIMEOUT_MS,
  VOLUME_SETTLE_TIMEOUT_MS,
} from "../../src/volumes/section.js";
import { deadlineFromHeaders } from "./fake/common.js";
import { FakeControlPlane, IMAGE_ARN, SANDBOX_ID } from "./fake/control-plane.js";
import type { FakeRayd } from "./fake/server.js";
import { ACCESS_TOKEN, startRayd } from "./helpers.js";

// Marcadores de documentación (cuenta y recursos ficticios).
const FILE_SYSTEM_ID = "fs-0123456789abcdef0";
const OTHER_FILE_SYSTEM_ID = "fs-0123456789abcdef1";
const ROLE = "arn:aws:iam::123456789012:role/rayito-execution";
const CONNECTOR = "arn:aws:lambda:us-east-1:123456789012:network-connector:rayito-efs";
const SUCCESSOR_ID = "microvm-00000000-0000-0000-0000-000000000009";
const MOUNT_PATH = "/mnt/datos";
const IP_A = "10.0.1.10";
const IP_B = "10.0.2.10";
const REQUEST_TIMEOUT_MS = 1_000;

function volume(options: { mountTargetIp?: string; fileSystemId?: string } = {}): EfsVolume {
  return new EfsVolume({
    fileSystemId: options.fileSystemId ?? FILE_SYSTEM_ID,
    accessPointId: "fsap-0123456789abcdef0",
    ...(options.mountTargetIp === undefined ? {} : { mountTargetIp: options.mountTargetIp }),
  });
}

function awsError(name: string): Error {
  const error = new Error("redacted");
  error.name = name;
  return error;
}

class FakeMountTargets implements EfsMountTargetsApi {
  readonly calls: string[] = [];
  error: Error | undefined;

  constructor(readonly response: DescribedMountTargets) {}

  async describeMountTargets(input: { FileSystemId: string }): Promise<DescribedMountTargets> {
    this.calls.push(input.FileSystemId);
    if (this.error !== undefined) {
      throw this.error;
    }
    return this.response;
  }
}

const TWO_AZS: DescribedMountTargets = {
  MountTargets: [
    {
      MountTargetId: "fsmt-2",
      AvailabilityZoneId: "use1-az4",
      LifeCycleState: "available",
      IpAddress: IP_B,
    },
    {
      MountTargetId: "fsmt-1",
      AvailabilityZoneId: "use1-az2",
      LifeCycleState: "available",
      IpAddress: IP_A,
    },
  ],
};

function features(overrides: Partial<AgentFeatures> = {}): AgentFeatures {
  return {
    configure: true,
    s3Mounts: false,
    efsVolumes: true,
    lifecycleEvents: false,
    telemetryExport: false,
    secretGateway: false,
    templateStart: false,
    ...overrides,
  };
}

function caught(call: () => unknown): unknown {
  try {
    call();
  } catch (error) {
    return error;
  }
  throw new Error("no lanzó");
}

describe("chooseMountTargetIp", () => {
  test("picks the available target of the smallest AZ id", () => {
    expect(chooseMountTargetIp(TWO_AZS)).toBe(IP_A);
  });

  test("skips targets that are not available or have no IP", () => {
    expect(
      chooseMountTargetIp({
        MountTargets: [
          { AvailabilityZoneId: "use1-az1", LifeCycleState: "creating", IpAddress: "10.0.0.1" },
          { AvailabilityZoneId: "use1-az1", LifeCycleState: "available" },
          { AvailabilityZoneId: "use1-az6", LifeCycleState: "available", IpAddress: IP_B },
        ],
      }),
    ).toBe(IP_B);
  });

  test("ties on the AZ id are broken by MountTargetId", () => {
    expect(
      chooseMountTargetIp({
        MountTargets: [
          {
            MountTargetId: "fsmt-b",
            AvailabilityZoneId: "a",
            LifeCycleState: "available",
            IpAddress: IP_B,
          },
          {
            MountTargetId: "fsmt-a",
            AvailabilityZoneId: "a",
            LifeCycleState: "available",
            IpAddress: IP_A,
          },
        ],
      }),
    ).toBe(IP_A);
  });

  test.each([{}, { MountTargets: [] }])("no usable target is a VolumeError (%o)", (response) => {
    expect(() => chooseMountTargetIp(response)).toThrow(VolumeError);
    expect(() => chooseMountTargetIp(response)).toThrow(/available/);
  });
});

describe("MountTargetResolver", () => {
  test("one DescribeMountTargets per file system per resolver", async () => {
    const api = new FakeMountTargets(TWO_AZS);
    const resolver = new MountTargetResolver({ get: async () => api });
    const resolved = await resolveMountTargets(
      new Map([
        ["/mnt/a", volume()],
        ["/mnt/b", volume()],
        ["/mnt/c", volume({ fileSystemId: OTHER_FILE_SYSTEM_ID })],
      ]),
      resolver,
    );
    expect(api.calls).toEqual([FILE_SYSTEM_ID, OTHER_FILE_SYSTEM_ID]);
    expect([...resolved.values()].map((v) => v.mountTargetIp)).toEqual([IP_A, IP_A, IP_A]);
  });

  test("a volume that already has its IP keeps it and makes no call", async () => {
    const api = new FakeMountTargets(TWO_AZS);
    const given = volume({ mountTargetIp: IP_B });
    const resolved = await resolveMountTargets(
      new Map([[MOUNT_PATH, given]]),
      new MountTargetResolver({ get: async () => api }),
    );
    expect(resolved.get(MOUNT_PATH)).toBe(given);
    expect(api.calls).toEqual([]);
  });

  test("AWS errors are translated without the AWS message", async () => {
    const api = new FakeMountTargets(TWO_AZS);
    api.error = awsError("FileSystemNotFound");
    const resolver = new MountTargetResolver({ get: async () => api });
    await expect(resolver.resolve(FILE_SYSTEM_ID)).rejects.toBeInstanceOf(VolumeNotFoundError);
    api.error = awsError("AccessDeniedException");
    const denied = new MountTargetResolver({ get: async () => api });
    await expect(denied.resolve(FILE_SYSTEM_ID)).rejects.toThrow(
      /elasticfilesystem:DescribeMountTargets/,
    );
  });
});

describe("EfsVolume.mountTargetIp", () => {
  test.each(["10.0.0", "10.0.0.256", "fs.example.com", "::1", " 10.0.0.1"])(
    "%s is rejected without echoing it",
    (value) => {
      const error = caught(() => volume({ mountTargetIp: value }));
      expect(error).toBeInstanceOf(InvalidArgumentError);
      expect((error as Error).message).not.toContain(value.trim() || "x");
    },
  );

  test("a dotted-quad IPv4 is accepted", () => {
    expect(volume({ mountTargetIp: IP_A }).mountTargetIp).toBe(IP_A);
  });
});

describe("EfsVolumesSection", () => {
  const section = new EfsVolumesSection(
    new Map([[MOUNT_PATH, new EfsVolume({ ...volume(), readOnly: true, mountTargetIp: IP_A })]]),
  );

  test("fills the efs_volumes section with every field", () => {
    const request = create(ConfigureRequestSchema, {});
    section.fill(request);
    expect(request.efsVolumes?.mounts).toHaveLength(1);
    const [mount] = request.efsVolumes?.mounts ?? [];
    expect(mount?.mountPath).toBe(MOUNT_PATH);
    expect(mount?.fileSystemId).toBe(FILE_SYSTEM_ID);
    expect(mount?.accessPointId).toBe("fsap-0123456789abcdef0");
    expect(mount?.readOnly).toBe(true);
    expect(mount?.mountTargetIp).toBe(IP_A);
  });

  test("an absent IP travels as the empty string", () => {
    const request = create(ConfigureRequestSchema, {});
    new EfsVolumesSection(new Map([[MOUNT_PATH, volume()]])).fill(request);
    expect(request.efsVolumes?.mounts[0]?.mountTargetIp).toBe("");
  });

  test("APPLIED and PENDING are not errors", () => {
    expect(() => section.checkResult(SectionCode.APPLIED, "")).not.toThrow();
    expect(() => section.checkResult(SectionCode.PENDING, "")).not.toThrow();
  });

  test("UNSUPPORTED names the opt-in image", () => {
    const error = caught(() => section.checkResult(SectionCode.UNSUPPORTED, ""));
    expect(error).toBeInstanceOf(UnimplementedError);
    expect((error as Error).message).toContain("rayito-base-caps-efs");
  });

  test.each([
    [SectionCode.FAILED, "iam_denied", "iam_denied"],
    [SectionCode.FAILED, "invalid_path", "invalid_path"],
    [SectionCode.INVALID, "something_new", "unknown"],
    [SectionCode.FAILED, "", "unknown"],
  ])("code %s with %s is a VolumeMountError(%s)", (code, errorClass, expected) => {
    const error = caught(() => section.checkResult(code, errorClass));
    expect(error).toBeInstanceOf(VolumeMountError);
    expect((error as VolumeMountError).code).toBe(expected);
  });

  test("requireSupport needs Health.features.efsVolumes", () => {
    expect(() => section.requireSupport(features())).not.toThrow();
    const error = caught(() => section.requireSupport(features({ efsVolumes: false })));
    expect(error).toBeInstanceOf(UnimplementedError);
    expect((error as Error).message).toContain(IMAGE_REASON);
  });

  function status(state: EfsVolumeState, lastErrorClass = "") {
    return create(ConfigureStatusResponseSchema, {
      efsVolumes: create(EfsVolumesStatusSchema, {
        volumes: [create(EfsVolumeStatusSchema, { mountPath: MOUNT_PATH, state, lastErrorClass })],
      }),
    });
  }

  test("checkStatus waits for MOUNTED, raises on FAILED and on the final read", () => {
    expect(section.checkStatus(status(EfsVolumeState.MOUNTED), false)).toBe(true);
    expect(section.checkStatus(status(EfsVolumeState.MOUNTING), false)).toBe(false);
    const failed = caught(() => section.checkStatus(status(EfsVolumeState.FAILED, "tls"), false));
    expect((failed as VolumeMountError).code).toBe("tls");
    const timedOut = caught(() => section.checkStatus(status(EfsVolumeState.MOUNTING), true));
    expect((timedOut as VolumeMountError).code).toBe("timeout");
  });

  test("the timeouts cover four volumes at rayd's 15 s helper timeout", () => {
    expect(section.applyTimeoutMs).toBe(65_000);
    expect(VOLUME_APPLY_TIMEOUT_MS).toBe(65_000);
    expect(section.settleTimeoutMs).toBe(VOLUME_SETTLE_TIMEOUT_MS);
  });

  test("fromProtoStatus maps every state", () => {
    const states = fromProtoStatus(
      status(EfsVolumeState.DEGRADED, "credentials_expired").efsVolumes,
    );
    expect(states.get(MOUNT_PATH)).toEqual({
      state: "degraded",
      lastErrorClass: "credentials_expired",
    });
    expect(fromProtoStatus(undefined).size).toBe(0);
  });
});

describe("configureTimeoutMs", () => {
  test("is the request timeout unless a slow section needs more", () => {
    const quick = { section: "s3_mounts" } as unknown as ConfigureSection;
    const slow = new EfsVolumesSection(new Map([[MOUNT_PATH, volume({ mountTargetIp: IP_A })]]));
    expect(configureTimeoutMs([quick], REQUEST_TIMEOUT_MS)).toBe(REQUEST_TIMEOUT_MS);
    expect(configureTimeoutMs([quick, slow], REQUEST_TIMEOUT_MS)).toBe(VOLUME_APPLY_TIMEOUT_MS);
    expect(configureTimeoutMs([slow], 120_000)).toBe(120_000);
  });
});

describe("prepareFeatures", () => {
  const planWith = (volumes: Record<string, EfsVolume>) =>
    planFeatures({ volumes }, "base-caps-efs", undefined, [CONNECTOR], ROLE);

  test("without volumes the plan is returned as is", async () => {
    const plan = planFeatures({});
    expect(await prepareFeatures(plan, { region: "us-east-1" })).toBe(plan);
  });

  test("resolves the IPs and appends one efs_volumes section", async () => {
    const api = new FakeMountTargets(TWO_AZS);
    const prepared = await prepareFeatures(planWith({ [MOUNT_PATH]: volume() }), {
      region: "us-east-1",
      efs: { get: async () => api },
    });
    const [section] = prepared.configureSections;
    expect(section).toBeInstanceOf(EfsVolumesSection);
    expect((section as EfsVolumesSection).volumes.get(MOUNT_PATH)?.mountTargetIp).toBe(IP_A);
    expect(prepared.volumes).toBeUndefined();
    expect(api.calls).toEqual([FILE_SYSTEM_ID]);
  });

  test("volumes that carry their IP never build an EFS client", async () => {
    const prepared = await prepareFeatures(
      planWith({ [MOUNT_PATH]: volume({ mountTargetIp: IP_B }) }),
      {
        region: "us-east-1",
        efs: {
          get: () => {
            throw new Error("no debería construir un cliente de EFS");
          },
        },
      },
    );
    expect(prepared.configureSections).toHaveLength(1);
  });

  test("no available mount target fails before launch", async () => {
    const api = new FakeMountTargets({ MountTargets: [] });
    await expect(
      prepareFeatures(planWith({ [MOUNT_PATH]: volume() }), {
        region: "us-east-1",
        efs: { get: async () => api },
      }),
    ).rejects.toBeInstanceOf(VolumeError);
  });
});

function supportVolumes(rayd: FakeRayd, extra: Partial<Record<string, boolean>> = {}): void {
  rayd.health.features = create(AgentFeaturesSchema, {
    configure: true,
    efsVolumes: true,
    ...extra,
  });
}

function applied(section: ConfigSection, code = SectionCode.APPLIED, errorClass = "") {
  return create(SectionResultSchema, { section, code, errorClass });
}

function volumesStatus(state: EfsVolumeState) {
  return create(EfsVolumesStatusSchema, {
    volumes: [create(EfsVolumeStatusSchema, { mountPath: MOUNT_PATH, state })],
  });
}

function createOptions(rayd: FakeRayd, plane: FakeControlPlane) {
  return {
    template: IMAGE_ARN,
    idle: null,
    accessToken: ACCESS_TOKEN,
    controlPlane: plane,
    transport: rayd.transport,
    executionRoleArn: ROLE,
    egress: [CONNECTOR],
    requestTimeoutMs: REQUEST_TIMEOUT_MS,
    volumes: { [MOUNT_PATH]: volume({ mountTargetIp: IP_A }) },
  } as const;
}

describe("Sandbox.create({ volumes })", () => {
  test("sends efs_volumes in the single Configure, with mounts, and a ≥ 65 s deadline", async () => {
    const rayd = await startRayd(ACCESS_TOKEN);
    supportVolumes(rayd, { s3Mounts: true });
    rayd.configure.nextResults = [
      applied(ConfigSection.S3_MOUNTS),
      applied(ConfigSection.EFS_VOLUMES),
    ];
    const plane = new FakeControlPlane({ endpoint: rayd.host, states: ["RUNNING"] });
    try {
      const sbx = await Sandbox.create({
        ...createOptions(rayd, plane),
        mounts: { "/mnt/s3": new S3Mount({ bucket: "data-bucket" }) },
      });
      try {
        expect(rayd.configure.configureRequests).toHaveLength(1);
        const [request] = rayd.configure.configureRequests;
        expect(request?.efsVolumes?.mounts.map((m) => [m.mountPath, m.mountTargetIp])).toEqual([
          [MOUNT_PATH, IP_A],
        ]);
        expect(request?.s3Mounts?.mounts).toHaveLength(1);
        const [headers] = rayd.configure.configureHeaders;
        expect(deadlineFromHeaders(headers ?? {})).toBeGreaterThanOrEqual(
          VOLUME_APPLY_TIMEOUT_MS / 1000 - 1,
        );
        expect(plane.callsTo("terminateMicrovm")).toHaveLength(0);
        rayd.configure.efsVolumesStatus = volumesStatus(EfsVolumeState.MOUNTED);
        expect((await sbx.volumes()).get(MOUNT_PATH)?.state).toBe("mounted");
      } finally {
        sbx.close();
      }
    } finally {
      await rayd.close();
    }
  });

  test.each([
    ["kept", true, 0],
    ["terminated", false, 1],
  ])(
    "a FAILED mount is a VolumeMountError and the VM is %s",
    async (_, keepOnFailure, terminations) => {
      const rayd = await startRayd(ACCESS_TOKEN);
      supportVolumes(rayd);
      rayd.configure.nextResults = [
        applied(ConfigSection.EFS_VOLUMES, SectionCode.FAILED, "iam_denied"),
      ];
      const plane = new FakeControlPlane({ endpoint: rayd.host, states: ["RUNNING"] });
      try {
        const error = await Sandbox.create({ ...createOptions(rayd, plane), keepOnFailure }).catch(
          (thrown: unknown) => thrown,
        );
        expect(error).toBeInstanceOf(VolumeMountError);
        expect((error as VolumeMountError).code).toBe("iam_denied");
        expect(plane.callsTo("terminateMicrovm")).toHaveLength(terminations);
      } finally {
        await rayd.close();
      }
    },
  );

  test("an image without efs-utils is UnimplementedError, terminated, no Configure", async () => {
    const rayd = await startRayd(ACCESS_TOKEN);
    supportVolumes(rayd, { efsVolumes: false });
    const plane = new FakeControlPlane({ endpoint: rayd.host, states: ["RUNNING"] });
    try {
      await expect(Sandbox.create(createOptions(rayd, plane))).rejects.toThrow(
        /rayito-base-caps-efs/,
      );
      expect(plane.callsTo("terminateMicrovm")).toHaveLength(1);
      expect(rayd.configure.configureRequests).toHaveLength(0);
    } finally {
      await rayd.close();
    }
  });

  test("an UNSUPPORTED section is UnimplementedError and the VM is terminated", async () => {
    const rayd = await startRayd(ACCESS_TOKEN);
    supportVolumes(rayd);
    rayd.configure.nextResults = [applied(ConfigSection.EFS_VOLUMES, SectionCode.UNSUPPORTED)];
    const plane = new FakeControlPlane({ endpoint: rayd.host, states: ["RUNNING"] });
    try {
      await expect(Sandbox.create(createOptions(rayd, plane))).rejects.toBeInstanceOf(
        UnimplementedError,
      );
      expect(plane.callsTo("terminateMicrovm")).toHaveLength(1);
    } finally {
      await rayd.close();
    }
  });

  test("validation runs before run-microvm", async () => {
    const plane = new FakeControlPlane({ endpoint: "127.0.0.1:1", states: ["RUNNING"] });
    await expect(
      Sandbox.create({
        template: IMAGE_ARN,
        controlPlane: plane,
        egress: [CONNECTOR],
        volumes: { [MOUNT_PATH]: volume({ mountTargetIp: IP_A }) },
      }),
    ).rejects.toThrow(/executionRoleArn/);
    expect(plane.callsTo("runMicrovm")).toHaveLength(0);
  });

  test("without volumes no Configure is sent and volumes() is empty", async () => {
    const rayd = await startRayd(ACCESS_TOKEN);
    supportVolumes(rayd);
    const plane = new FakeControlPlane({ endpoint: rayd.host, states: ["RUNNING"] });
    try {
      const sbx = await Sandbox.create({
        template: IMAGE_ARN,
        idle: null,
        accessToken: ACCESS_TOKEN,
        controlPlane: plane,
        transport: rayd.transport,
      });
      try {
        expect(rayd.configure.configureRequests).toHaveLength(0);
        expect((await sbx.volumes()).size).toBe(0);
      } finally {
        sbx.close();
      }
    } finally {
      await rayd.close();
    }
  });

  test("reincarnate() mounts the same volumes in the successor", async () => {
    const rayd = await startRayd(ACCESS_TOKEN);
    supportVolumes(rayd);
    rayd.configure.nextResults = [applied(ConfigSection.EFS_VOLUMES)];
    const plane = new FakeControlPlane({ endpoint: rayd.host, states: ["RUNNING"] });
    try {
      const original = await Sandbox.create({
        ...createOptions(rayd, plane),
        persist: new S3Prefix({ bucket: "my-bucket" }),
      });
      plane.sandboxIds.push(SUCCESSOR_ID);
      const successor = await original.reincarnate();
      try {
        expect(original.sandboxId).toBe(SANDBOX_ID);
        expect(successor.sandboxId).toBe(SUCCESSOR_ID);
        expect(rayd.configure.configureRequests).toHaveLength(2);
        for (const request of rayd.configure.configureRequests) {
          expect(request.efsVolumes?.mounts.map((m) => m.mountPath)).toEqual([MOUNT_PATH]);
        }
        const runs = plane.callsTo("runMicrovm");
        expect(runs).toHaveLength(2);
      } finally {
        successor.close();
      }
    } finally {
      await rayd.close();
    }
  });
});
