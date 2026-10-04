/**
 * `reincarnate()` reaplica todas las secciones 0.6 de `ConfigureSandbox` que
 * mandó el `create()` original (m15-reincarnate-configure-replay): el
 * sucesor pasa por el mismo camino de `create()` (`planFeatures` →
 * `plannedSections` → `#applyConfigureSections`), así que `events` deriva
 * `k_sbx` del *nuevo* `sandboxId`, `mounts` espera otra vez a `mounted` y
 * `telemetry` se resuelve con los hechos del sucesor, todo en un único
 * `Configure`. Espejo de `test_reincarnate_configure_replay.py`.
 */

import { create } from "@bufbuild/protobuf";
import { describe, expect, test } from "vitest";
import { relaunchFeatures } from "../../src/feature-options.js";
import {
  ConfigSection,
  SectionCode,
  SectionResultSchema,
} from "../../src/gen/rayito/v1/configure_pb.js";
import { AgentFeaturesSchema } from "../../src/gen/rayito/v1/features_pb.js";
import {
  S3MountPhase,
  S3MountStateSchema,
  S3MountsStatusSchema,
} from "../../src/gen/rayito/v1/s3_mounts_pb.js";
import { S3Prefix } from "../../src/index.js";
import { deriveSandboxKey } from "../../src/lifecycle-events/keys.js";
import { LifecycleEvents } from "../../src/lifecycle-events/service.js";
import { S3Mount } from "../../src/s3-mounts/domain.js";
import { type LaunchOptions, relaunchCreateOptions } from "../../src/sandbox/persistence.js";
import { Sandbox } from "../../src/sandbox/sandbox.js";
import { OptionalStacks } from "../../src/stacks/service.js";
import { TelemetryExport } from "../../src/telemetry-export/domain.js";
import { FakeControlPlane, IMAGE_ARN, SANDBOX_ID } from "./fake/control-plane.js";
import type { FakeRayd } from "./fake/server.js";
import { ACCESS_TOKEN, startRayd } from "./helpers.js";
import { FakeStackProvisioner } from "./m15-fake-stacks.js";

const SUCCESSOR_ID = "microvm-00000000-0000-0000-0000-000000000009";
const STACK_KEY = Buffer.from("the-stack-wide-hmac-secret", "utf8");
const SECRET_ID = "arn:aws:secretsmanager:us-east-1:123456789012:secret:stack-key-abc123";
const ROLE_ARN = "arn:aws:iam::123456789012:role/rayito-execution";
const BUCKET = "my-bucket";
const MOUNT_PATH = "/mnt/data";
const MOUNTS = { [MOUNT_PATH]: new S3Mount({ bucket: "data-bucket" }) };
/** Cada sandbox: un `ConfigureStatus` aún `PENDING` y otro ya `MOUNTED`. */
const STATUS_POLLS_PER_SANDBOX = 2;

function deployedEvents(): LifecycleEvents {
  return new LifecycleEvents({
    region: "us-east-1",
    stacks: new OptionalStacks({ provisioner: new FakeStackProvisioner() }),
    getSecretValue: async () => ({ SecretString: STACK_KEY.toString("utf8") }),
    stackKeySecretIdForTests: SECRET_ID,
  });
}

function mountStatus(phase: S3MountPhase) {
  return create(S3MountsStatusSchema, {
    mounts: [create(S3MountStateSchema, { mountPath: MOUNT_PATH, phase })],
  });
}

/** Un agente 0.6 con las tres funciones; `mounts` vuelve `PENDING` y se
 * asienta en el segundo `ConfigureStatus` de cada sandbox. */
function scriptAgent(rayd: FakeRayd): void {
  rayd.health.features = create(AgentFeaturesSchema, {
    configure: true,
    s3Mounts: true,
    lifecycleEvents: true,
    telemetryExport: true,
  });
  rayd.configure.nextResults = [
    create(SectionResultSchema, { section: ConfigSection.S3_MOUNTS, code: SectionCode.PENDING }),
    create(SectionResultSchema, {
      section: ConfigSection.TELEMETRY_EXPORT,
      code: SectionCode.APPLIED,
    }),
    create(SectionResultSchema, {
      section: ConfigSection.LIFECYCLE_EVENTS,
      code: SectionCode.APPLIED,
    }),
  ];
  rayd.configure.s3MountsStatuses = [
    mountStatus(S3MountPhase.PENDING),
    mountStatus(S3MountPhase.MOUNTED),
  ];
}

describe("reincarnate() replays the Configure sections", () => {
  test("mounts, telemetry and events reach the successor's single Configure", async () => {
    const rayd = await startRayd(ACCESS_TOKEN);
    scriptAgent(rayd);
    const plane = new FakeControlPlane({ endpoint: rayd.host, states: ["RUNNING"] });
    try {
      const original = await Sandbox.create({
        template: IMAGE_ARN,
        idle: null,
        accessToken: ACCESS_TOKEN,
        controlPlane: plane,
        transport: rayd.transport,
        executionRoleArn: ROLE_ARN,
        logging: "cloudwatch",
        persist: new S3Prefix({ bucket: BUCKET }),
        mounts: MOUNTS,
        telemetry: new TelemetryExport(),
        events: deployedEvents(),
      });
      // El sucesor vuelve a pasar por PENDING antes de MOUNTED.
      rayd.configure.s3MountsStatuses = [
        mountStatus(S3MountPhase.PENDING),
        mountStatus(S3MountPhase.MOUNTED),
      ];
      plane.sandboxIds.push(SUCCESSOR_ID);
      const successor = await original.reincarnate();
      try {
        expect(successor.sandboxId).toBe(SUCCESSOR_ID);
        const [first, second] = rayd.configure.configureRequests;
        expect(rayd.configure.configureRequests).toHaveLength(2);
        for (const request of [first, second]) {
          expect(request?.s3Mounts?.mounts.map((mount) => mount.mountPath)).toEqual([MOUNT_PATH]);
          expect(request?.telemetryExport).toBeDefined();
          expect(request?.lifecycleEvents).toBeDefined();
        }
        expect(first?.lifecycleEvents?.sandboxId).toBe(SANDBOX_ID);
        expect(second?.lifecycleEvents?.sandboxId).toBe(SUCCESSOR_ID);
        const successorKey = Buffer.from(second?.lifecycleEvents?.sandboxKey ?? []);
        expect(successorKey.equals(deriveSandboxKey(STACK_KEY, SUCCESSOR_ID))).toBe(true);
        expect(successorKey.equals(Buffer.from(first?.lifecycleEvents?.sandboxKey ?? []))).toBe(
          false,
        );
        expect(rayd.configure.configureStatusHeaders).toHaveLength(2 * STATUS_POLLS_PER_SANDBOX);
      } finally {
        successor.close();
      }
    } finally {
      await rayd.close();
    }
  });

  test("a sandbox without 0.6 options sends no Configure on reincarnate", async () => {
    const rayd = await startRayd(ACCESS_TOKEN);
    const plane = new FakeControlPlane({ endpoint: rayd.host, states: ["RUNNING"] });
    try {
      const original = await Sandbox.create({
        template: IMAGE_ARN,
        idle: null,
        accessToken: ACCESS_TOKEN,
        controlPlane: plane,
        transport: rayd.transport,
        executionRoleArn: ROLE_ARN,
        persist: new S3Prefix({ bucket: BUCKET }),
      });
      plane.sandboxIds.push(SUCCESSOR_ID);
      const successor = await original.reincarnate();
      successor.close();
      expect(rayd.configure.configureRequests).toHaveLength(0);
    } finally {
      await rayd.close();
    }
  });
});

describe("relaunch helpers", () => {
  test("relaunchFeatures keeps every section and drops the size", () => {
    const events = deployedEvents();
    const telemetry = new TelemetryExport();
    const kept = relaunchFeatures({ mounts: MOUNTS, size: "2gb", events, telemetry });
    expect(kept).toEqual({ mounts: MOUNTS, events, telemetry });
    expect("size" in kept).toBe(false);
  });

  test("relaunchCreateOptions spreads the features and never re-sends the size", () => {
    const events = deployedEvents();
    const launch = {
      template: IMAGE_ARN,
      size: { name: "2gb" },
      features: relaunchFeatures({ mounts: MOUNTS, events }),
    } as unknown as LaunchOptions;
    const options = relaunchCreateOptions(launch);
    expect(options.mounts).toBe(MOUNTS);
    expect(options.events).toBe(events);
    expect(options.size).toBeUndefined();
    expect("features" in options).toBe(false);
  });
});
