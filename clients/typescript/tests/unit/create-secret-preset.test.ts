/**
 * `--create-secret` (`ensureModelSecrets`) por el preset de la pasarela y con
 * el nombre exacto que la pasarela lee, contra
 * `testdata/agent-deploy/create-secret.json` (el mismo fichero que
 * `test_create_secret_preset.py`). Ninguna prueba llama a AWS.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, test } from "vitest";
import {
  ensureModelSecrets,
  GATEWAY_SECRET_PREFIX,
  gatewayEnvVar,
  type SecretStorePort,
  secretNameFor,
} from "../../src/agent/aws-resolve.js";
import { GATEWAY_SECRET_NAME_TEMPLATE } from "../../src/agent/detect-project.js";
import { AgentProject } from "../../src/agent/project.js";
import { InvalidArgumentError } from "../../src/errors.js";
import type { SecretGateway } from "../../src/secret-gateway/domain.js";
import { DEFAULT_SECRET_PREFIX, SecretRef } from "../../src/secrets/names.js";

const VECTORS = JSON.parse(
  readFileSync(
    join(
      import.meta.dirname,
      "..",
      "..",
      "..",
      "..",
      "testdata",
      "agent-deploy",
      "create-secret.json",
    ),
    "utf8",
  ),
);

interface CreateSecretCase {
  readonly name: string;
  readonly gateway: string;
  readonly table: Record<string, unknown>;
  readonly existing: string[];
  readonly environment: Record<string, string>;
  readonly expected: {
    readonly envVar: string;
    readonly existsCalls: string[];
    readonly created?: string | null;
    readonly awsName?: string;
    readonly value?: string | null;
    readonly gatewaySecret?: string;
    readonly error?: string;
  };
}

const CASES = VECTORS.cases as CreateSecretCase[];

class RecordingStore implements SecretStorePort {
  readonly existing: Set<string>;
  readonly created: [string, string][] = [];
  readonly checked: string[] = [];

  constructor(existing: string[]) {
    this.existing = new Set(existing);
  }

  async exists(name: string): Promise<boolean> {
    this.checked.push(name);
    return this.existing.has(name);
  }

  async create(name: string, value: string): Promise<unknown> {
    this.created.push([name, value]);
    this.existing.add(name);
    return undefined;
  }
}

function projectOf(item: CreateSecretCase): AgentProject {
  const document = JSON.parse(JSON.stringify(VECTORS.project));
  document.model.gateway = item.gateway;
  document.gateways = { [item.gateway]: item.table };
  return AgentProject.fromObject(document, { baseDir: "." });
}

function secretOf(gateways: Readonly<Record<string, SecretGateway>>, gateway: string): string {
  const [reference] = Object.values((gateways[gateway] as SecretGateway).headers);
  return reference instanceof SecretRef ? reference.name : String(reference);
}

describe("--create-secret por preset", () => {
  test.each(CASES.map((item) => [item.name, item] as const))("%s", async (_name, item) => {
    const project = projectOf(item);
    const store = new RecordingStore(item.existing);
    const { expected } = item;
    expect(gatewayEnvVar(item.gateway, project.gatewayPresets[item.gateway])).toBe(expected.envVar);
    const run = () =>
      ensureModelSecrets(project, {
        secretNames: {},
        createFromEnv: true,
        store,
        environ: item.environment,
      });
    if (expected.error !== undefined) {
      await expect(run()).rejects.toThrow(new InvalidArgumentError(expected.error));
      expect(store.checked).toEqual(expected.existsCalls);
      return;
    }
    const resolved = await run();
    expect(project.gatewayPresets).toEqual({ [item.gateway]: item.table.preset });
    expect(store.checked).toEqual(expected.existsCalls);
    expect(store.created.map(([name]) => name)).toEqual(
      expected.created == null ? [] : [expected.created],
    );
    if (expected.value != null) {
      expect(store.created[0]?.[1]).toBe(expected.value);
    }
    const gatewaySecret = secretOf(resolved.gateways, item.gateway);
    expect(gatewaySecret).toBe(expected.gatewaySecret);
    expect(VECTORS.secretPrefix + gatewaySecret).toBe(expected.awsName);
  });

  test("el prefijo es el mismo con el que lee la pasarela", () => {
    expect(GATEWAY_SECRET_PREFIX).toBe(VECTORS.secretPrefix);
    expect(GATEWAY_SECRET_PREFIX).toBe(DEFAULT_SECRET_PREFIX);
  });

  test("la detección escribe el nombre sin el prefijo", () => {
    expect(GATEWAY_SECRET_NAME_TEMPLATE).toBe(VECTORS.detectedSecretTemplate);
    expect(
      GATEWAY_SECRET_NAME_TEMPLATE.replace("{project}", "my-agent").replace(
        "{provider}",
        "bedrock",
      ),
    ).toBe(secretNameFor("my-agent", "bedrock"));
  });

  test("gatewayPresets no entra en el documento", () => {
    const project = projectOf(CASES[0] as CreateSecretCase);
    expect(project.gatewayPresets).toEqual({ model: "bedrock" });
    expect(JSON.stringify(project.toObject())).not.toContain("gatewayPresets");
    expect(JSON.stringify(project.toObject())).not.toContain("gateway_presets");
  });

  test("gatewayPresets rechaza valores que no son cadenas", () => {
    const project = projectOf(CASES[0] as CreateSecretCase);
    expect(
      () =>
        new AgentProject({
          name: project.name,
          runtime: project.runtime,
          spec: project.spec,
          gateways: project.gateways,
          gatewayPresets: { model: 1 } as unknown as Record<string, string>,
        }),
    ).toThrow(InvalidArgumentError);
  });

  test("ni el valor ni el nombre llegan al mensaje", async () => {
    const item = CASES.find((candidate) => candidate.expected.error !== undefined);
    const project = projectOf(item as CreateSecretCase);
    const store = new RecordingStore([]);
    const error = await ensureModelSecrets(project, {
      secretNames: {},
      createFromEnv: false,
      store,
      environ: {},
    }).catch((caught: unknown) => caught as Error);
    expect(error).toBeInstanceOf(InvalidArgumentError);
    expect((error as Error).message).toContain("AWS_BEARER_TOKEN_BEDROCK");
    expect((error as Error).message).not.toContain(String(item?.table.secret));
  });
});
