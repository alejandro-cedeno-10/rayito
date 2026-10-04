/**
 * `Sandbox.create({ events })` (m15-events-webhooks, ADR-020): la clave de
 * este sandbox (`k_sbx`, derivada de la clave del stack y el `sandboxId`)
 * viaja en el único `Configure` de `create()`, tras `run-microvm`; si el
 * agente no tiene el flag, la pila no está desplegada o la sección vuelve
 * `INVALID`, el VM se termina. Espejo de `test_m15_events_create_wiring.py`.
 */

import { create } from "@bufbuild/protobuf";
import { describe, expect, test } from "vitest";
import { SandboxError, UnimplementedError, WebhookError } from "../../src/errors.js";
import { type LaunchFacts, planFeatures, plannedSections } from "../../src/feature-options.js";
import {
  ConfigSection,
  SectionCode,
  SectionResultSchema,
} from "../../src/gen/rayito/v1/configure_pb.js";
import { AgentFeaturesSchema } from "../../src/gen/rayito/v1/features_pb.js";
import { deriveSandboxKey } from "../../src/lifecycle-events/keys.js";
import {
  LifecycleEventsSection,
  LifecycleEventsSectionFactory,
} from "../../src/lifecycle-events/section.js";
import { LifecycleEvents } from "../../src/lifecycle-events/service.js";
import { Sandbox } from "../../src/sandbox/sandbox.js";
import { OptionalStacks } from "../../src/stacks/service.js";
import { TelemetryExport } from "../../src/telemetry-export/domain.js";
import { TelemetrySectionFactory } from "../../src/telemetry-export/section.js";
import { FakeControlPlane, IMAGE_ARN, SANDBOX_ID } from "./fake/control-plane.js";
import { FakeRayd } from "./fake/server.js";
import { ACCESS_TOKEN } from "./helpers.js";
import { FakeStackProvisioner } from "./m15-fake-stacks.js";

const STACK_KEY = Buffer.from("the-stack-wide-hmac-secret", "utf8");
const SECRET_ID = "arn:aws:secretsmanager:us-east-1:123456789012:secret:stack-key-abc123";
const ROLE_ARN = "arn:aws:iam::123456789012:role/rayito-test";
const FACTS: LaunchFacts = {
  sandboxId: SANDBOX_ID,
  imageArn: IMAGE_ARN,
  imageVersion: "7.0",
  guestMemoryBytes: undefined,
};

function deployedEvents(reads: string[] = []): LifecycleEvents {
  return new LifecycleEvents({
    region: "us-east-1",
    stacks: new OptionalStacks({ provisioner: new FakeStackProvisioner() }),
    getSecretValue: async ({ SecretId }) => {
      reads.push(SecretId);
      return { SecretString: STACK_KEY.toString("utf8") };
    },
    stackKeySecretIdForTests: SECRET_ID,
  });
}

function undeployedEvents(): LifecycleEvents {
  return new LifecycleEvents({
    region: "us-east-1",
    stacks: new OptionalStacks({ provisioner: new FakeStackProvisioner() }),
    getSecretValue: async () => ({ SecretString: "unused" }),
  });
}

describe("plannedSections with events", () => {
  test("adds the events factory with the launch facts", () => {
    const events = deployedEvents();
    const [factory] = plannedSections(planFeatures({ events }, undefined, "cloudwatch"), FACTS);
    expect(factory).toBeInstanceOf(LifecycleEventsSectionFactory);
    expect((factory as LifecycleEventsSectionFactory).facts).toEqual({
      sandboxId: SANDBOX_ID,
      imageArn: IMAGE_ARN,
      imageVersion: "7.0",
    });
  });

  test("keeps telemetry and events together, in order", () => {
    const plan = planFeatures(
      { events: deployedEvents(), telemetry: new TelemetryExport() },
      undefined,
      "cloudwatch",
    );
    const kinds = plannedSections(plan, FACTS).map((entry) => entry.constructor);
    expect(kinds).toEqual([TelemetrySectionFactory, LifecycleEventsSectionFactory]);
  });

  test("plans nothing without events", () => {
    expect(plannedSections(planFeatures({}), FACTS)).toEqual([]);
  });
});

describe("LifecycleEventsSection", () => {
  test("names the camelCase AgentFeatures flag", () => {
    const section = new LifecycleEventsSection(new Uint8Array([1]), FACTS);
    expect(section.section).toBe("lifecycle_events");
    expect(section.requiredFlag).toBe("lifecycleEvents");
  });

  test("never exposes k_sbx to JSON.stringify", () => {
    const key = deriveSandboxKey(STACK_KEY, SANDBOX_ID);
    const section = new LifecycleEventsSection(key, FACTS);
    expect(JSON.stringify(section)).not.toContain(key.toString("hex"));
    expect(Object.values(section)).not.toContain(key);
  });

  test("an INVALID result raises", () => {
    const section = new LifecycleEventsSection(new Uint8Array([1]), FACTS);
    expect(() => section.checkResult(SectionCode.INVALID, "invalid_section")).toThrow(
      /lifecycle_events: invalid_section/,
    );
  });

  test("reads the stack key once per LifecycleEvents instance", async () => {
    const reads: string[] = [];
    const events = deployedEvents(reads);
    await events.buildSection({ ...FACTS, sandboxId: "a" });
    await events.buildSection({ ...FACTS, sandboxId: "b" });
    expect(reads).toEqual([SECRET_ID]);
  });
});

async function createWithEvents(
  rayd: FakeRayd,
  plane: FakeControlPlane,
  events: LifecycleEvents,
): Promise<Sandbox> {
  return Sandbox.create({
    template: IMAGE_ARN,
    idle: null,
    accessToken: ACCESS_TOKEN,
    controlPlane: plane,
    transport: rayd.transport,
    executionRoleArn: ROLE_ARN,
    logging: "cloudwatch",
    events,
  });
}

describe("Sandbox.create({ events })", () => {
  test("sends k_sbx in the single Configure", async () => {
    const rayd = await FakeRayd.start({ accessToken: ACCESS_TOKEN });
    rayd.health.features = create(AgentFeaturesSchema, { configure: true, lifecycleEvents: true });
    rayd.configure.nextResults = [
      create(SectionResultSchema, {
        section: ConfigSection.LIFECYCLE_EVENTS,
        code: SectionCode.APPLIED,
      }),
    ];
    const plane = new FakeControlPlane({ endpoint: rayd.host, states: ["RUNNING"] });
    try {
      const sandbox = await createWithEvents(rayd, plane, deployedEvents());
      expect(rayd.configure.configureRequests).toHaveLength(1);
      const config = rayd.configure.configureRequests[0]?.lifecycleEvents;
      expect(
        Buffer.from(config?.sandboxKey ?? []).equals(deriveSandboxKey(STACK_KEY, SANDBOX_ID)),
      ).toBe(true);
      expect(config?.sandboxId).toBe(SANDBOX_ID);
      expect(config?.imageArn).toBe(IMAGE_ARN);
      sandbox.close();
    } finally {
      await rayd.close();
    }
  });

  test.each([
    ["no lifecycleEvents flag", { configure: true }, "deployed", UnimplementedError],
    ["a pre-0.6 agent", undefined, "deployed", UnimplementedError],
    [
      "the stack not deployed",
      { configure: true, lifecycleEvents: true },
      "undeployed",
      WebhookError,
    ],
    ["an INVALID section", { configure: true, lifecycleEvents: true }, "invalid", SandboxError],
  ] as const)("terminates the VM on %s", async (_name, features, mode, error) => {
    const rayd = await FakeRayd.start({ accessToken: ACCESS_TOKEN });
    if (features !== undefined) {
      rayd.health.features = create(AgentFeaturesSchema, features);
    }
    if (mode === "invalid") {
      rayd.configure.nextResults = [
        create(SectionResultSchema, {
          section: ConfigSection.LIFECYCLE_EVENTS,
          code: SectionCode.INVALID,
          errorClass: "invalid_section",
        }),
      ];
    }
    const plane = new FakeControlPlane({ endpoint: rayd.host, states: ["RUNNING"] });
    try {
      await expect(
        createWithEvents(
          rayd,
          plane,
          mode === "undeployed" ? undeployedEvents() : deployedEvents(),
        ),
      ).rejects.toBeInstanceOf(error);
      expect(plane.callsTo("terminateMicrovm")).toHaveLength(1);
    } finally {
      await rayd.close();
    }
  });
});
