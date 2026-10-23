/**
 * Renovación opcional de la clave de corta duración de Bedrock
 * (`renewGatewayKeys`, ADR-034 D7, design D6) contra
 * `testdata/fleet-recipe/renewal.json`: cuándo se pregunta y cuándo se
 * escribe, la región de la pasarela, el valor `Bearer`, que nada nombre el
 * secreto y que sin la opción no se cargue el generador. Espejo de
 * `test_gateway_key_renewal.py`. Sin AWS.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, test } from "vitest";
import { AgentFleet } from "../../src/agent/fleet.js";
import {
  bearerValue,
  GatewayKeyRenewer,
  gatewayRegion,
  RENEW_NEEDS_BEDROCK,
  RENEW_SHARED_SECRET,
  type RenewableGateway,
  renewableGateways,
  type SecretsWriter,
} from "../../src/agent/gateway-keys.js";
import { AgentProject } from "../../src/agent/project.js";
import { copyProject } from "../../src/agent/project-copy.js";
import { InvalidArgumentError, SecretError } from "../../src/errors.js";
import { FLEET_TOKEN, FleetHarness } from "./agent-fleet-fake.js";
import { MODEL_ID } from "./agent-project-fake.js";

const TESTDATA = join(import.meta.dirname, "..", "..", "..", "..", "testdata", "fleet-recipe");
const VECTORS = JSON.parse(readFileSync(join(TESTDATA, "renewal.json"), "utf8"));
const CONTRACT = JSON.parse(readFileSync(join(TESTDATA, "contract.json"), "utf8"));
const NOW = Date.parse("2026-10-22T12:00:00Z");
const SECRET_ID = "rayito/agents/support-bot/bedrock";
const TOKEN = VECTORS.secretValue.token as string;
const MS = 1000;

type ProjectDocument = Record<string, unknown> & {
  gateways: Record<string, Record<string, unknown>>;
};

/** `bedrockProject()` serializado con más pasarelas (o cambiado por
 * `edit`), con la pasarela `bedrock` marcada con su preset como lo rehace la
 * receta. */
function withBedrockPreset(
  gateways: Record<string, Record<string, unknown>>,
  edit: (document: ProjectDocument) => void = () => {},
): AgentProject {
  const document = bedrockProject().toObject() as ProjectDocument;
  document.gateways = { ...document.gateways, ...gateways };
  edit(document);
  const project = AgentProject.fromObject(document, { baseDir: "." });
  return copyProject(project, { gatewayPresets: { bedrock: "bedrock" } });
}

function customGateway(upstream: string, secret: string): Record<string, unknown> {
  return {
    preset: "custom",
    upstream,
    headers: { authorization: secret },
    allow: [["POST", "/v1/chat/completions"]],
  };
}

function bedrockProject(preset = "bedrock"): AgentProject {
  const bedrock = preset === "bedrock";
  return AgentProject.fromObject(
    {
      schema: "rayito.agent-project/1",
      name: "support-bot",
      runtime: { kind: "deepagents" },
      model: bedrock
        ? { provider: "bedrock", id: MODEL_ID, gateway: "bedrock", region: "us-east-1" }
        : { provider: preset, id: MODEL_ID, gateway: "bedrock" },
      gateways: {
        bedrock: bedrock
          ? {
              preset,
              secret: "agents/support-bot/bedrock",
              region: "us-east-1",
              models: [MODEL_ID],
            }
          : { preset, secret: "agents/support-bot/bedrock" },
      },
    },
    { baseDir: "." },
  );
}

class FakeSecrets implements SecretsWriter {
  changed: Date | undefined;
  readonly calls: [string, Record<string, string>][] = [];
  error: string | undefined;

  async describeSecret(input: { SecretId: string }): Promise<{ LastChangedDate?: Date }> {
    this.#record("DescribeSecret", input);
    return this.changed === undefined ? {} : { LastChangedDate: this.changed };
  }

  async putSecretValue(input: {
    SecretId: string;
    SecretString: string;
    ClientRequestToken: string;
  }): Promise<unknown> {
    this.#record("PutSecretValue", input);
    return {};
  }

  get names(): string[] {
    return this.calls.map(([name]) => name);
  }

  #record(name: string, input: Record<string, string>): void {
    this.calls.push([name, input]);
    if (this.error !== undefined) {
      const failure = new Error(SECRET_ID);
      failure.name = this.error;
      throw failure;
    }
  }
}

function renewerOf(secrets: FakeSecrets, clock: { now: number }, regions: string[] = []) {
  return new GatewayKeyRenewer(renewableGateways(bedrockProject()), {
    tokenProvider: async (region) => {
      regions.push(region);
      return TOKEN;
    },
    secrets,
    now: () => clock.now,
  });
}

interface RenewalCase {
  readonly id: string;
  readonly processRenewedAgoSeconds: number | null;
  readonly secretChangedAgoSeconds: number | null;
  readonly calls: string[];
  readonly renew: boolean;
}

describe("gateway key renewal", () => {
  test.each(VECTORS.cases as RenewalCase[])("$id", async (item) => {
    const secrets = new FakeSecrets();
    const clock = { now: NOW };
    const renewer = renewerOf(secrets, clock);
    if (item.processRenewedAgoSeconds !== null) {
      secrets.changed = new Date(NOW - 86_400 * MS);
      expect(await renewer.renew()).toBe(true);
      clock.now += item.processRenewedAgoSeconds * MS;
      secrets.calls.length = 0;
    }
    secrets.changed =
      item.secretChangedAgoSeconds === null
        ? undefined
        : new Date(clock.now - item.secretChangedAgoSeconds * MS);
    expect(await renewer.renew()).toBe(item.renew);
    expect(secrets.names).toEqual(item.calls);
  });

  test.each(VECTORS.regions as { upstream: string; region: string | null }[])(
    "the region of $upstream",
    ({ upstream, region }) => {
      expect(gatewayRegion(upstream)).toBe(region ?? undefined);
    },
  );

  test("the secret gets the bearer value with a fresh request token", async () => {
    const secrets = new FakeSecrets();
    secrets.changed = new Date(NOW - 3_600 * MS);
    const regions: string[] = [];
    await renewerOf(secrets, { now: NOW }, regions).renew();
    const [, put] = secrets.calls[1] as [string, Record<string, string>];
    expect(put.SecretId).toBe(SECRET_ID);
    expect(put.SecretString).toBe(VECTORS.secretValue.value);
    expect(put.SecretString).toBe(bearerValue(TOKEN));
    expect(put.ClientRequestToken).toHaveLength(36);
    expect(regions).toEqual(["us-east-1"]);
  });

  test("the deploy renewal writes even a secret that just changed", async () => {
    const secrets = new FakeSecrets();
    secrets.changed = new Date(NOW - 10 * MS);
    const clock = { now: NOW };
    const regions: string[] = [];
    const renewer = renewerOf(secrets, clock, regions);
    expect(await renewer.renew({ force: true })).toBe(true);
    clock.now += 100 * MS;
    expect(await renewer.renew()).toBe(false);
    expect(secrets.names).toEqual(["PutSecretValue"]);
    expect(regions).toEqual(["us-east-1"]);
  });

  test("without force a young secret is not rewritten", async () => {
    const secrets = new FakeSecrets();
    secrets.changed = new Date(NOW - 10 * MS);
    expect(await renewerOf(secrets, { now: NOW }).renew()).toBe(false);
    expect(secrets.names).toEqual(["DescribeSecret"]);
  });

  test("three creations in five minutes write once and ask once", async () => {
    const secrets = new FakeSecrets();
    secrets.changed = new Date(NOW - 3_600 * MS);
    const clock = { now: NOW };
    const renewer = renewerOf(secrets, clock);
    for (let index = 0; index < 3; index += 1) {
      await renewer.renew();
      clock.now += 100 * MS;
    }
    expect(secrets.names).toEqual(["DescribeSecret", "PutSecretValue"]);
  });

  test("access denied names the gateway and not the secret", async () => {
    const secrets = new FakeSecrets();
    secrets.error = "AccessDeniedException";
    let caught: unknown;
    try {
      await renewerOf(secrets, { now: NOW }).renew();
    } catch (error) {
      caught = error;
    }
    expect(caught).toBeInstanceOf(SecretError);
    expect((caught as SecretError).code).toBe("permission_denied");
    expect((caught as Error).message).toContain("'bedrock'");
    expect((caught as Error).message).not.toContain(SECRET_ID);
  });

  test("a project without a bedrock gateway cannot renew", () => {
    expect(
      () =>
        new AgentFleet({
          project: bedrockProject("openai"),
          accessToken: FLEET_TOKEN,
          renewGatewayKeys: true,
        }),
    ).toThrow(new InvalidArgumentError(RENEW_NEEDS_BEDROCK));
    expect(CONTRACT.messages.renewNeedsBedrock).toBe(RENEW_NEEDS_BEDROCK);
  });

  test("a secret shared with a non-Bedrock gateway is never renewed", () => {
    const project = withBedrockPreset({
      other: customGateway("https://llm.example.com", "agents/support-bot/bedrock"),
    });
    let message = "";
    try {
      renewableGateways(project);
    } catch (error) {
      expect(error).toBeInstanceOf(InvalidArgumentError);
      message = (error as Error).message;
    }
    expect(message).toBe(
      RENEW_SHARED_SECRET.replace("{gateway}", "bedrock").replace("{other}", "other"),
    );
    expect(message).not.toContain("agents/support-bot/bedrock");
    expect(CONTRACT.messages.renewSharedSecret).toBe(RENEW_SHARED_SECRET);
  });

  test("a secret shared with another Bedrock upstream still renews", () => {
    const project = withBedrockPreset({
      other: customGateway(
        "https://bedrock-runtime.eu-west-1.amazonaws.com",
        "agents/support-bot/bedrock",
      ),
      search: customGateway("https://search.example.com", "agents/support-bot/search"),
    });
    expect(renewableGateways(project).map((gateway) => gateway.name)).toEqual(["bedrock"]);
  });

  test("a bedrock preset with a lookalike host has nothing to renew", () => {
    const project = withBedrockPreset({}, (document) => {
      document.gateways.bedrock = {
        ...document.gateways.bedrock,
        upstream: "https://bedrock-runtime.evil.s3.amazonaws.com",
      };
    });
    expect(() => renewableGateways(project)).toThrow(new InvalidArgumentError(RENEW_NEEDS_BEDROCK));
  });

  test("the gateway repr never holds the secret in a message", () => {
    const [gateway] = renewableGateways(bedrockProject()) as [RenewableGateway];
    expect(gateway.name).toBe("bedrock");
    expect(gateway.region).toBe("us-east-1");
  });

  test("the fleet renews before each VM and refreshes the open ones", async () => {
    const h = new FleetHarness();
    const answers = [false, true];
    let calls = 0;
    const fleet = h.fleet(undefined, {
      project: bedrockProject(),
      renewGatewayKeys: true,
      keyRenewer: {
        renew: async () => {
          calls += 1;
          return answers.shift() ?? false;
        },
      },
    });
    await fleet.start();
    const opened = h.world.sandboxes.get(fleet.sandboxIds[0] ?? "");
    let refreshed = 0;
    Object.assign(opened ?? {}, {
      gateways: {
        ...opened?.gateways,
        refresh: async () => {
          refreshed += 1;
        },
      },
    });
    await fleet.scale({ minVms: 2, maxVms: 2 });
    expect(calls).toBe(2);
    expect(refreshed).toBe(1);
  });

  test("without the option no renewer is loaded", async () => {
    const h = new FleetHarness();
    const fleet = h.fleet(undefined, {
      project: bedrockProject(),
      keyRenewer: {
        renew: async () => {
          throw new Error("no debía renovar");
        },
      },
    });
    await fleet.start();
    expect(fleet.sandboxIds.length).toBe(1);
  });
});
