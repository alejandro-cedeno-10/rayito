/**
 * `Sandbox.connect()` (estático y de instancia) reconstruye `sbx.gateways`
 * desde un `ConfigureStatus` de sólo lectura cuando este handle no aplicó
 * `gateways` él mismo, para que otro proceso pueda usar `sbx.agent` sin
 * recrear el sandbox: sólo viajan nombre, puerto y último error, nunca un
 * valor de cabecera. Espejo de `test_connect_recovers_gateways.py`.
 */

import { create } from "@bufbuild/protobuf";
import { describe, expect, test } from "vitest";
import { AuthenticationError } from "../../src/errors.js";
import { ConfigureStatusResponseSchema } from "../../src/gen/rayito/v1/configure_pb.js";
import { AgentFeaturesSchema } from "../../src/gen/rayito/v1/features_pb.js";
import {
  SecretGatewayRouteState,
  SecretGatewayRouteStatusSchema,
  SecretGatewayStatusSchema,
} from "../../src/gen/rayito/v1/secret_gateway_pb.js";
import { generateAccessToken } from "../../src/payload.js";
import { Sandbox } from "../../src/sandbox/sandbox.js";
import {
  EMPTY_GATEWAYS,
  GatewayHandle,
  gatewaysRecoverable,
  ownsGateways,
  recoveredGateways,
} from "../../src/secret-gateway/section.js";
import { SecretCache } from "../../src/secrets/cache.js";
import { SecretStore } from "../../src/secrets/store.js";
import { deadlineFromHeaders } from "./fake/common.js";
import { FakeControlPlane, IMAGE_ARN } from "./fake/control-plane.js";
import { FakeRayd } from "./fake/server.js";
import { ACCESS_TOKEN } from "./helpers.js";
import { gateway } from "./m15-secrets-gateway-fixtures.js";
import { FakeSecretsManager, SENTINEL_VALUE } from "./secrets-fake.js";

const FIRST_PORT = 40123;
const SECOND_PORT = 40999;
const ROUTE = "bedrock";
const CONNECT_REQUEST_TIMEOUT_MS = 7_000;

function gatewayStatus(port: number, lastErrorClass = "") {
  return create(SecretGatewayStatusSchema, {
    routes: [
      create(SecretGatewayRouteStatusSchema, {
        name: ROUTE,
        port,
        state: SecretGatewayRouteState.LISTENING,
        lastErrorClass,
      }),
    ],
  });
}

const FULL = {
  configure: true,
  s3Mounts: false,
  efsVolumes: false,
  lifecycleEvents: false,
  telemetryExport: false,
  secretGateway: true,
  templateStart: false,
} as const;

describe("recoveredGateways", () => {
  test("needs both configure and secretGateway", () => {
    expect(gatewaysRecoverable(undefined)).toBe(false);
    expect(gatewaysRecoverable({ ...FULL, secretGateway: false })).toBe(false);
    expect(gatewaysRecoverable({ ...FULL, configure: false })).toBe(false);
    expect(gatewaysRecoverable(FULL)).toBe(true);
  });

  test("ownsGateways is true only for the handle that applied gateways", () => {
    expect(ownsGateways(undefined)).toBe(false);
    expect(ownsGateways(new GatewayHandle({}))).toBe(true);
    const status = create(ConfigureStatusResponseSchema, {
      secretGateway: gatewayStatus(FIRST_PORT),
    });
    expect(ownsGateways(recoveredGateways(status))).toBe(false);
  });

  test("is EMPTY_GATEWAYS without routes", () => {
    expect(recoveredGateways(create(ConfigureStatusResponseSchema, {}))).toBe(EMPTY_GATEWAYS);
  });

  test("maps names, ports and errors, and refresh only rereads the status", async () => {
    const handle = recoveredGateways(
      create(ConfigureStatusResponseSchema, {
        secretGateway: gatewayStatus(FIRST_PORT, "upstream_timeout"),
      }),
      async () =>
        create(ConfigureStatusResponseSchema, {
          secretGateway: gatewayStatus(SECOND_PORT),
        }),
    );
    expect(handle.get(ROUTE)?.url).toBe(`http://127.0.0.1:${FIRST_PORT}`);
    expect(handle.get(ROUTE)?.lastErrorClass).toBe("upstream_timeout");
    await handle.refresh();
    expect(handle.get(ROUTE)?.port).toBe(SECOND_PORT);
  });
});

describe("Sandbox.connect() recovers gateways", () => {
  test("a second process sees the gateways another one created", async () => {
    const rayd = await FakeRayd.start({ accessToken: ACCESS_TOKEN });
    rayd.health.features = create(AgentFeaturesSchema, {
      configure: true,
      secretGateway: true,
    });
    rayd.configure.secretGatewayStatus = gatewayStatus(FIRST_PORT);
    const plane = new FakeControlPlane({
      endpoint: rayd.host,
      states: ["RUNNING"],
    });
    try {
      const sbx = await Sandbox.connect("mvm-test-connect-gateways", {
        accessToken: ACCESS_TOKEN,
        controlPlane: plane,
        transport: rayd.transport,
      });
      expect(sbx.gateways.get(ROUTE)?.port).toBe(FIRST_PORT);
      expect(rayd.configure.configureRequests).toHaveLength(0);
      rayd.configure.secretGatewayStatus = gatewayStatus(SECOND_PORT);
      await sbx.gateways.refresh();
      expect(sbx.gateways.get(ROUTE)?.port).toBe(SECOND_PORT);
      expect(rayd.configure.configureRequests).toHaveLength(0);
      sbx.close();
    } finally {
      await rayd.close();
    }
  });

  test("a wrong token does not fail connect(); the first authenticated call does", async () => {
    const rayd = await FakeRayd.start({ accessToken: ACCESS_TOKEN });
    rayd.health.features = create(AgentFeaturesSchema, {
      configure: true,
      secretGateway: true,
    });
    rayd.configure.secretGatewayStatus = gatewayStatus(FIRST_PORT);
    const plane = new FakeControlPlane({
      endpoint: rayd.host,
      states: ["RUNNING"],
    });
    try {
      const sbx = await Sandbox.connect("mvm-test-connect-gateways", {
        accessToken: generateAccessToken(),
        controlPlane: plane,
        transport: rayd.transport,
      });
      expect(sbx.gateways).toBe(EMPTY_GATEWAYS);
      await expect(sbx.commands.run("true")).rejects.toBeInstanceOf(AuthenticationError);
      sbx.close();
    } finally {
      await rayd.close();
    }
  });

  test("an agent without the feature gets no ConfigureStatus call", async () => {
    const rayd = await FakeRayd.start({ accessToken: ACCESS_TOKEN });
    const plane = new FakeControlPlane({
      endpoint: rayd.host,
      states: ["RUNNING"],
    });
    try {
      const sbx = await Sandbox.connect("mvm-test-connect-gateways", {
        accessToken: ACCESS_TOKEN,
        controlPlane: plane,
        transport: rayd.transport,
      });
      expect(sbx.gateways).toBe(EMPTY_GATEWAYS);
      expect(rayd.configure.configureStatusHeaders).toHaveLength(0);
      sbx.close();
    } finally {
      await rayd.close();
    }
  });

  test("instance connect() rereads a recovered handle with its requestTimeoutMs", async () => {
    const rayd = await FakeRayd.start({ accessToken: ACCESS_TOKEN });
    rayd.health.features = create(AgentFeaturesSchema, {
      configure: true,
      secretGateway: true,
    });
    rayd.configure.secretGatewayStatus = gatewayStatus(FIRST_PORT);
    const plane = new FakeControlPlane({
      endpoint: rayd.host,
      states: ["RUNNING"],
    });
    try {
      const sbx = await Sandbox.connect("mvm-test-connect-gateways", {
        accessToken: ACCESS_TOKEN,
        controlPlane: plane,
        transport: rayd.transport,
      });
      expect(sbx.gateways.recovered).toBe(true);
      rayd.configure.secretGatewayStatus = gatewayStatus(SECOND_PORT);
      await sbx.connect({ requestTimeoutMs: CONNECT_REQUEST_TIMEOUT_MS });
      expect(sbx.gateways.get(ROUTE)?.port).toBe(SECOND_PORT);
      const last = rayd.configure.configureStatusHeaders.at(-1);
      expect(last === undefined ? undefined : deadlineFromHeaders(last)).toBeLessThanOrEqual(
        CONNECT_REQUEST_TIMEOUT_MS,
      );
      expect(last === undefined ? undefined : deadlineFromHeaders(last)).toBeGreaterThan(
        CONNECT_REQUEST_TIMEOUT_MS / 2,
      );
      expect(rayd.configure.configureRequests).toHaveLength(0);
      sbx.close();
    } finally {
      await rayd.close();
    }
  });

  test("the creating handle keeps its own gateways on connect()", async () => {
    const rayd = await FakeRayd.start({ accessToken: ACCESS_TOKEN });
    rayd.health.features = create(AgentFeaturesSchema, {
      configure: true,
      secretGateway: true,
    });
    rayd.configure.secretGatewayStatus = gatewayStatus(FIRST_PORT);
    const plane = new FakeControlPlane({
      endpoint: rayd.host,
      states: ["RUNNING"],
    });
    const api = new FakeSecretsManager();
    api.put("rayito/anthropic", SENTINEL_VALUE);
    try {
      const sbx = await Sandbox.create({
        template: IMAGE_ARN,
        idle: null,
        accessToken: ACCESS_TOKEN,
        controlPlane: plane,
        transport: rayd.transport,
        gateways: { [ROUTE]: gateway() },
        secretCache: new SecretCache({
          store: new SecretStore({ client: api, region: "us-east-1" }),
        }),
      });
      const own = sbx.gateways;
      expect(own.recovered).toBe(false);
      const statusReads = rayd.configure.configureStatusHeaders.length;
      const configures = rayd.configure.configureRequests.length;
      await sbx.connect();
      expect(sbx.gateways).toBe(own);
      expect(rayd.configure.configureStatusHeaders).toHaveLength(statusReads);
      expect(rayd.configure.configureRequests).toHaveLength(configures);
      sbx.close();
    } finally {
      await rayd.close();
    }
  });
});
