/**
 * `Sandbox.create()` con `gateways`: cualquier fallo al configurar (un
 * agente anterior a 0.6, un flag ausente, una sección `FAILED`) debe cerrar
 * el cliente y terminar el MicroVM (salvo `keepOnFailure`); `refresh()`
 * debe lanzar ante una sección `FAILED` en vez de devolver un mapa vacío, y
 * empujar el valor *actual* del secreto, nunca el de la caché.
 * Espejo de `test_m15_secrets_gateway_apply.py` (hallazgos de revisión del
 * PR #78, m15-secrets-gateway).
 */

import { create } from "@bufbuild/protobuf";
import { describe, expect, test } from "vitest";
import { UnimplementedError } from "../../src/errors.js";
import {
  ConfigSection,
  SectionCode,
  SectionResultSchema,
} from "../../src/gen/rayito/v1/configure_pb.js";
import { AgentFeaturesSchema } from "../../src/gen/rayito/v1/features_pb.js";
import {
  SecretGatewayRouteStatusSchema,
  SecretGatewayStatusSchema,
} from "../../src/gen/rayito/v1/secret_gateway_pb.js";
import { Sandbox, type SandboxCreateOptions } from "../../src/sandbox/sandbox.js";
import { SecretCache } from "../../src/secrets/cache.js";
import { SecretStore } from "../../src/secrets/store.js";
import { FakeControlPlane, IMAGE_ARN } from "./fake/control-plane.js";
import { FakeRayd } from "./fake/server.js";
import { ACCESS_TOKEN } from "./helpers.js";
import { gateway } from "./m15-secrets-gateway-fixtures.js";
import { FakeSecretsManager, SENTINEL_VALUE } from "./secrets-fake.js";

/** Not a real credential: just distinct from `SENTINEL_VALUE`. */
const ROTATED_VALUE = "rotated-test-value";

function failedResult(errorClass = "listen_failed") {
  return create(SectionResultSchema, {
    section: ConfigSection.SECRET_GATEWAY,
    code: SectionCode.FAILED,
    errorClass,
  });
}

function withSecretGatewaySupport(rayd: FakeRayd): void {
  rayd.health.features = create(AgentFeaturesSchema, { configure: true, secretGateway: true });
}

/** No real AWS call: a `FakeSecretsManager` already seeded with the one
 * header this file's `gateway()` fixture needs. */
function fakeSecretCache(
  api: FakeSecretsManager = new FakeSecretsManager(),
): SandboxCreateOptions["secretCache"] {
  api.put("rayito/anthropic", SENTINEL_VALUE);
  return new SecretCache({ store: new SecretStore({ client: api, region: "us-east-1" }) });
}

/** `create()` must reject with `UnimplementedError`, terminate the VM and
 * never send the section to an agent that cannot take it. */
async function expectUnimplementedAndTerminated(rayd: FakeRayd): Promise<void> {
  const plane = new FakeControlPlane({ endpoint: rayd.host, states: ["RUNNING"] });
  await expect(
    Sandbox.create({
      template: IMAGE_ARN,
      idle: null,
      accessToken: ACCESS_TOKEN,
      controlPlane: plane,
      transport: rayd.transport,
      gateways: { anthropic: gateway() },
      secretCache: fakeSecretCache(),
    }),
  ).rejects.toBeInstanceOf(UnimplementedError);
  expect(plane.callsTo("terminateMicrovm")).toHaveLength(1);
  expect(rayd.configure.configureRequests).toHaveLength(0);
}

describe("Sandbox.create({ gateways }) failure handling", () => {
  test("a FAILED section closes the client and terminates the VM", async () => {
    const rayd = await FakeRayd.start({ accessToken: ACCESS_TOKEN });
    withSecretGatewaySupport(rayd);
    rayd.configure.nextResults = [failedResult()];
    const plane = new FakeControlPlane({ endpoint: rayd.host, states: ["RUNNING"] });
    try {
      await expect(
        Sandbox.create({
          template: IMAGE_ARN,
          idle: null,
          accessToken: ACCESS_TOKEN,
          controlPlane: plane,
          transport: rayd.transport,
          gateways: { anthropic: gateway() },
          secretCache: fakeSecretCache(),
        }),
      ).rejects.toThrow(/listen_failed/);
      expect(plane.callsTo("terminateMicrovm")).toHaveLength(1);
    } finally {
      await rayd.close();
    }
  });

  test("a pre-0.6 agent (no Health.features) terminates the VM", async () => {
    const rayd = await FakeRayd.start({ accessToken: ACCESS_TOKEN });
    try {
      await expectUnimplementedAndTerminated(rayd);
    } finally {
      await rayd.close();
    }
  });

  test("a 0.6 agent without the secretGateway flag terminates the VM", async () => {
    const rayd = await FakeRayd.start({ accessToken: ACCESS_TOKEN });
    rayd.health.features = create(AgentFeaturesSchema, { configure: true, secretGateway: false });
    try {
      await expectUnimplementedAndTerminated(rayd);
    } finally {
      await rayd.close();
    }
  });

  test("keepOnFailure closes the client but never terminates the VM", async () => {
    const rayd = await FakeRayd.start({ accessToken: ACCESS_TOKEN });
    withSecretGatewaySupport(rayd);
    rayd.configure.nextResults = [failedResult()];
    const plane = new FakeControlPlane({ endpoint: rayd.host, states: ["RUNNING"] });
    try {
      await expect(
        Sandbox.create({
          template: IMAGE_ARN,
          idle: null,
          accessToken: ACCESS_TOKEN,
          controlPlane: plane,
          transport: rayd.transport,
          gateways: { anthropic: gateway() },
          secretCache: fakeSecretCache(),
          keepOnFailure: true,
        }),
      ).rejects.toThrow(/listen_failed/);
      expect(plane.callsTo("terminateMicrovm")).toHaveLength(0);
    } finally {
      await rayd.close();
    }
  });

  test("a successful section leaves the VM running and sbx.gateways populated", async () => {
    const rayd = await FakeRayd.start({ accessToken: ACCESS_TOKEN });
    withSecretGatewaySupport(rayd);
    rayd.configure.secretGatewayStatus = create(SecretGatewayStatusSchema, {
      routes: [create(SecretGatewayRouteStatusSchema, { name: "anthropic", port: 41_000 })],
    });
    const plane = new FakeControlPlane({ endpoint: rayd.host, states: ["RUNNING"] });
    const sandbox = await Sandbox.create({
      template: IMAGE_ARN,
      idle: null,
      accessToken: ACCESS_TOKEN,
      controlPlane: plane,
      transport: rayd.transport,
      gateways: { anthropic: gateway() },
      secretCache: fakeSecretCache(),
    });
    try {
      expect(sandbox.gateways.get("anthropic")).toBeDefined();
      expect(plane.callsTo("terminateMicrovm")).toHaveLength(0);
    } finally {
      sandbox.close();
      await rayd.close();
    }
  });
});

describe("sbx.gateways.refresh() failure handling", () => {
  test("a FAILED section on refresh raises instead of returning an empty map", async () => {
    const rayd = await FakeRayd.start({ accessToken: ACCESS_TOKEN });
    withSecretGatewaySupport(rayd);
    const plane = new FakeControlPlane({ endpoint: rayd.host, states: ["RUNNING"] });
    const sandbox = await Sandbox.create({
      template: IMAGE_ARN,
      idle: null,
      accessToken: ACCESS_TOKEN,
      controlPlane: plane,
      transport: rayd.transport,
      gateways: { anthropic: gateway() },
      secretCache: fakeSecretCache(),
    });
    try {
      const statusCallsBefore = rayd.configure.configureStatusHeaders.length;
      rayd.configure.nextResults = [failedResult("invalid_header_value")];
      await expect(sandbox.gateways.refresh()).rejects.toThrow(/invalid_header_value/);
      // `ConfigureStatus` must never be asked for after a rejected refresh:
      // there is no new, valid state to report.
      expect(rayd.configure.configureStatusHeaders.length).toBe(statusCallsBefore);
    } finally {
      sandbox.close();
      await rayd.close();
    }
  });
});

describe("sbx.gateways.refresh() rotation", () => {
  test("pushes the value now in Secrets Manager, not the cached one", async () => {
    const rayd = await FakeRayd.start({ accessToken: ACCESS_TOKEN });
    withSecretGatewaySupport(rayd);
    const api = new FakeSecretsManager();
    const plane = new FakeControlPlane({ endpoint: rayd.host, states: ["RUNNING"] });
    const sandbox = await Sandbox.create({
      template: IMAGE_ARN,
      idle: null,
      accessToken: ACCESS_TOKEN,
      controlPlane: plane,
      transport: rayd.transport,
      gateways: { anthropic: gateway() },
      secretCache: fakeSecretCache(api),
    });
    try {
      // Rotated well inside `SecretCache`'s TTL.
      api.put("rayito/anthropic", ROTATED_VALUE, "v2");
      await sandbox.gateways.refresh();
      const sent = rayd.configure.configureRequests.map(
        (request) => request.secretGateway?.routes[0]?.headers["x-api-key"],
      );
      expect(sent).toEqual([SENTINEL_VALUE, ROTATED_VALUE]);
    } finally {
      sandbox.close();
      await rayd.close();
    }
  });
});
