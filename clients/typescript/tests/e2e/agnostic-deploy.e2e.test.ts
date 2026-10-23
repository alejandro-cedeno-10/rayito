/**
 * El despliegue agnóstico de ADR-033 en AWS real con el SDK y la CLI de
 * TypeScript, en AgentCore Runtime y en una flota de MicroVMs: entrada
 * estructurada con `--graph` (`invoke({ input })`, `stream`, la sesión que
 * continúa, `invalid_input`, `input_required` y `rayito agent invoke --input`),
 * secretos como variables de entorno con `--secret-env` y `--secret-env-from`
 * (el agente ve el valor; `GetAgentRuntime` no lo devuelve) y el modelo elegido
 * por el código (`openai-compatible-env` de TypeScript: con `externalModel` en
 * AgentCore, sin ningún permiso de Bedrock, y detrás de la pasarela en MicroVM).
 * Espejo de `clients/python/tests/e2e/test_agnostic_deploy.py`: el guest y el
 * host (logs, imagen, rotación, fallo en cerrado, `langgraph-api`) se prueban
 * una vez, en Python. Ningún grafo llama a un modelo.
 *
 * Variables, coste y tope: cabecera de `agnostic-deploy-support.ts`. Nunca corre
 * sin `RAYITO_E2E=1` y `RAYITO_E2E_AGNOSTIC=1`; no lo ejecuta ningún gate local.
 */

import { randomBytes } from "node:crypto";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import {
  type AgentCoreFleet,
  AgentError,
  type AgentFleet,
  type AgentResult,
  deployAgent,
} from "../../src/index.js";
import { DEFAULT_SECRET_PREFIX } from "../../src/secrets/names.js";
import {
  BASE,
  GATEWAY_NAME as BEDROCK_GATEWAY,
  BEDROCK_SECRET,
  BUCKET as MICROVM_BUCKET,
} from "./agent-zero-config-support.js";
import {
  agentcoreE2eReady,
  BUCKET,
  runtimeRoleOf,
  simulatedDecision,
  TEST_TIMEOUT_MS,
  tagsFromEnvironment,
} from "./agentcore-deploy-support.js";
import {
  AGNOSTIC_ENABLED,
  CLI_DEPLOY_BUDGET_MS,
  CLI_READ_BUDGET_MS,
  cli,
  digest,
  foundIn,
  NORMAL_TICKET,
  OPENAI_COMPATIBLE_SAMPLE,
  REGION,
  RUN_ID,
  type RunSecrets,
  randomValue,
  runtimeEnvironment,
  SECRET_ENV_NAMES,
  SECRET_ENV_SAMPLE,
  STRUCTURED_SAMPLE,
  secretName,
  stagedProject,
  TRIAGE_GRAPH,
  URGENT_TICKET,
  withRunSecrets,
} from "./agnostic-deploy-support.js";
import { e2eEnabled } from "./helpers.js";

const AGENTCORE_READY = AGNOSTIC_ENABLED && agentcoreE2eReady();
const MICROVM_READY =
  AGNOSTIC_ENABLED && e2eEnabled() && Boolean(BASE && MICROVM_BUCKET && BEDROCK_SECRET && RUN_ID);
const URGENT_INPUT = { ticket_id: URGENT_TICKET };
const NORMAL_INPUT = { ticket_id: NORMAL_TICKET };
const URGENT_OUTPUT = { priority: "high", summary: `ticket ${URGENT_TICKET}: prioridad high` };
const NORMAL_OUTPUT = { priority: "normal", summary: `ticket ${NORMAL_TICKET}: prioridad normal` };
const NOT_THE_SCHEMA = { ticket: URGENT_TICKET };
const PROMPT = "Clasifica el ticket, por favor.";
const SECRET_ENV_TABLE = "[secret_env]";
const ALLOWED = "allowed";
const LLM_GATEWAY = "llm";
const PLACEHOLDER_GATEWAY_KEY = "Bearer e2e-not-a-real-key";
const OPENAI_COMPATIBLE_BASE_URL = "https://llm-gateway.example.com/v1";
/** El modelo de los ejemplos: con `externalModel` el rol no puede invocar ninguno. */
const EXAMPLE_MODEL_ARN_TEMPLATE =
  "arn:aws:bedrock:{region}::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0";
const TOKEN_FILE_NAME = "agent.token";
const ACCESS_TOKEN_BYTES = 32;

/** Un access token de sandbox nuevo (base64url de 32 bytes). */
function newAccessToken(): string {
  return randomBytes(ACCESS_TOKEN_BYTES).toString("base64url");
}

function nameOf(kind: string): string {
  return `agn-${kind}-ts-${RUN_ID}`;
}

function imageNameOf(kind: string): string {
  return `agn-${RUN_ID}-t${kind}`;
}

function echoed(result: AgentResult | Record<string, unknown>): Record<string, unknown> {
  return JSON.parse(String(result.text)) as Record<string, unknown>;
}

async function expectInputError(
  call: () => Promise<AgentResult>,
  detailCode: "invalid_input" | "input_required",
): Promise<void> {
  const error = await call().then(
    () => undefined,
    (thrown: unknown) => thrown,
  );
  expect(error).toBeInstanceOf(AgentError);
  expect((error as AgentError).reason).toBe("protocol_error");
  expect((error as AgentError).detailCode).toBe(detailCode);
}

/** `input` con su `output`, la sesión que continúa, el `stream` y los dos errores de entrada. */
async function checkStructuredGraph(fleet: AgentFleet | AgentCoreFleet): Promise<void> {
  const first = await fleet.invoke({ input: URGENT_INPUT });
  expect(first.output).toEqual(URGENT_OUTPUT);
  const continued = await fleet.invoke({ input: NORMAL_INPUT }, { sessionId: first.sessionId });
  expect(continued.output).toEqual(NORMAL_OUTPUT);
  const stream = await fleet.stream({ input: URGENT_INPUT });
  const kinds: string[] = [];
  for await (const event of stream) {
    kinds.push(event.type);
  }
  expect(kinds.at(-1)).toBe("done");
  expect((await stream.result()).output).toEqual(URGENT_OUTPUT);
  await expectInputError(() => fleet.invoke({ input: NOT_THE_SCHEMA }), "invalid_input");
  await expectInputError(() => fleet.invoke(PROMPT), "input_required");
}

/** Los nombres de los tres secretos de `secret-env-echo` que crea una prueba. */
interface SecretEnvNames {
  readonly dbUrl: string;
  readonly tickets: string;
  readonly config: string;
}

/** Crea los secretos de `secret-env-echo` con nombres propios de `scope` (destino y prueba). */
async function createdSecretEnv(
  secrets: RunSecrets,
  scope: string,
): Promise<{ values: Record<string, string>; names: SecretEnvNames }> {
  const values = Object.fromEntries(SECRET_ENV_NAMES.map((name) => [name, randomValue()]));
  const names: SecretEnvNames = {
    dbUrl: secretName(scope, "ts-db-url"),
    tickets: secretName(scope, "ts-tickets"),
    config: secretName(scope, "ts-config"),
  };
  await secrets.create(names.dbUrl, values.DATABASE_URL as string);
  await secrets.create(names.tickets, { token: values.TICKETS_TOKEN as string });
  await secrets.create(names.config, {
    DB_HOST: values.APP_DB_HOST as string,
    DB_USER: randomValue(),
  });
  return { values, names };
}

function secretEnvFlags(names: SecretEnvNames): string[] {
  return [
    "--secret-env",
    `DATABASE_URL=${names.dbUrl}`,
    "--secret-env",
    `TICKETS_TOKEN=${names.tickets}#token`,
    "--secret-env-from",
    `${names.config},prefix=APP_,keys=DB_HOST`,
  ];
}

function expectedDigests(values: Record<string, string>): Record<string, string> {
  return Object.fromEntries(Object.entries(values).map(([name, value]) => [name, digest(value)]));
}

describe.runIf(AGENTCORE_READY)("ADR-033 en AgentCore (SDK y CLI de TypeScript)", () => {
  const fleets: AgentCoreFleet[] = [];

  afterEach(async () => {
    for (const fleet of fleets.splice(0)) {
      await fleet.delete().catch(() => undefined);
    }
  });

  /** Despliega y apunta la flota para borrarla; si el despliegue falla a medias
   * (un build fallido deja la pila sin runtime), borra lo que dejó por su nombre. */
  async function deployedAgentCore(
    directory: string,
    name: string,
    options: { graph?: string; externalModel?: boolean } = {},
  ): Promise<AgentCoreFleet> {
    try {
      const fleet = await deployAgent(directory, {
        target: "agentcore",
        region: REGION,
        options: { bucket: BUCKET },
        overrides: options.graph === undefined ? undefined : { graph: options.graph },
        agentcore: { tags: tagsFromEnvironment(), externalModel: options.externalModel ?? false },
      });
      fleets.push(fleet);
      return fleet;
    } catch (error) {
      await cli(
        ["agent", "delete", name, "--target", "agentcore", "--yes"],
        CLI_DEPLOY_BUDGET_MS,
      ).catch(() => undefined);
      throw error;
    }
  }

  it(
    "un grafo con esquema propio: input, output, stream, sesión, errores e invoke --input",
    async () => {
      const name = nameOf("graph");
      const directory = stagedProject(STRUCTURED_SAMPLE, join(tmpdir(), `agn-${RUN_ID}`), {
        name,
      });
      const fleet = await deployedAgentCore(directory, name, { graph: TRIAGE_GRAPH });
      await checkStructuredGraph(fleet);
      const answer = await cli(
        ["agent", "invoke", name, "--input", JSON.stringify(NORMAL_INPUT), "--target", "agentcore"],
        CLI_READ_BUDGET_MS,
      );
      expect(answer.output).toEqual(NORMAL_OUTPUT);
      const removed = await fleet.delete();
      expect(removed.runtimeDeleted).toBe(true);
    },
    TEST_TIMEOUT_MS,
  );

  it(
    "externalModel: sin permisos de Bedrock, la URL de la pasarela en el entorno y la clave por secret_env",
    () =>
      withRunSecrets(tagsFromEnvironment(), async (secrets) => {
        const key = randomValue();
        const keySecret = secretName("agentcore-extm", "ts-llm-key");
        await secrets.create(keySecret, key);
        const name = nameOf("extm");
        const directory = stagedProject(OPENAI_COMPATIBLE_SAMPLE, join(tmpdir(), `agn-${RUN_ID}`), {
          name,
          extra: ["", SECRET_ENV_TABLE, `LLM_API_KEY = "${keySecret}"`],
        });
        const fleet = await deployedAgentCore(directory, name, { externalModel: true });
        expect(echoed(await fleet.invoke(PROMPT))).toEqual({
          base_url_set: true,
          base_url_is_gateway: false,
          api_key_is_placeholder: false,
          model_env: "LLM_MODEL",
        });
        const environment = await runtimeEnvironment(fleet.runtimeId);
        expect(environment.LLM_BASE_URL).toBe(OPENAI_COMPATIBLE_BASE_URL);
        expect(foundIn(Object.values(environment), [key])).toEqual([]);
        const role = await runtimeRoleOf(fleet.name);
        const model = EXAMPLE_MODEL_ARN_TEMPLATE.replace("{region}", REGION);
        expect(await simulatedDecision(role, "bedrock:InvokeModel", model)).not.toBe(ALLOWED);
      }),
    TEST_TIMEOUT_MS,
  );

  it(
    "la CLI despliega con --secret-env y --secret-env-from: el agente ve los valores y el runtime no",
    () =>
      withRunSecrets(tagsFromEnvironment(), async (secrets) => {
        const { values, names } = await createdSecretEnv(secrets, "agentcore-senv");
        const name = nameOf("senv");
        const directory = stagedProject(SECRET_ENV_SAMPLE, join(tmpdir(), `agn-${RUN_ID}`), {
          name,
          dropFrom: SECRET_ENV_TABLE,
        });
        const tags = Object.entries(tagsFromEnvironment()).flatMap(([key, value]) => [
          "--tag",
          `${key}=${value}`,
        ]);
        try {
          const deployed = await cli(
            [
              "agent",
              "deploy",
              directory,
              ...secretEnvFlags(names),
              "--target",
              "agentcore",
              "--yes",
              "--bucket",
              BUCKET,
              ...tags,
            ],
            CLI_DEPLOY_BUDGET_MS,
          );
          expect(deployed.status).toBe("READY");
          const answer = await cli(
            ["agent", "invoke", name, PROMPT, "--target", "agentcore"],
            CLI_READ_BUDGET_MS,
          );
          expect(echoed(answer)).toEqual({
            present: [...SECRET_ENV_NAMES],
            sha256_8: expectedDigests(values),
          });
          const environment = await runtimeEnvironment(String(deployed.runtimeId));
          expect(environment.RAYITO_SECRET_ENV).toBeDefined();
          expect(foundIn(Object.values(environment), Object.values(values))).toEqual([]);
        } finally {
          const removed = await cli(
            ["agent", "delete", name, "--target", "agentcore", "--yes"],
            CLI_DEPLOY_BUDGET_MS,
          );
          expect(removed.runtimeDeleted).toBe(true);
        }
      }),
    TEST_TIMEOUT_MS,
  );
});

describe.runIf(MICROVM_READY)("ADR-033 en MicroVMs (SDK y CLI de TypeScript)", () => {
  const fleets: AgentFleet[] = [];

  afterEach(async () => {
    for (const fleet of fleets.splice(0)) {
      await fleet.stop().catch(() => undefined);
    }
  });

  async function deployedMicrovm(
    directory: string,
    secretNames: Readonly<Record<string, string>>,
    graph?: string,
  ): Promise<AgentFleet> {
    const fleet = await deployAgent(directory, {
      region: REGION,
      options: { bucket: MICROVM_BUCKET, secretNames, accessToken: newAccessToken() },
      overrides: graph === undefined ? undefined : { graph },
    });
    fleets.push(fleet);
    return fleet;
  }

  it(
    "un grafo con esquema propio: input, output, stream, sesión y errores",
    async () => {
      const directory = stagedProject(STRUCTURED_SAMPLE, join(tmpdir(), `agn-vm-${RUN_ID}`), {
        name: imageNameOf("graph"),
        base: BASE,
      });
      const fleet = await deployedMicrovm(
        directory,
        { [BEDROCK_GATEWAY]: BEDROCK_SECRET },
        TRIAGE_GRAPH,
      );
      await checkStructuredGraph(fleet);
    },
    TEST_TIMEOUT_MS,
  );

  it(
    "el modelo elegido por el código pasa por la pasarela con una clave marcador",
    () =>
      withRunSecrets(tagsFromEnvironment(), async (secrets) => {
        const gatewaySecret = secretName("microvm-model", "ts-llm");
        await secrets.create(`${DEFAULT_SECRET_PREFIX}${gatewaySecret}`, PLACEHOLDER_GATEWAY_KEY);
        const directory = stagedProject(
          OPENAI_COMPATIBLE_SAMPLE,
          join(tmpdir(), `agn-vm-${RUN_ID}`),
          { name: imageNameOf("model"), base: BASE },
        );
        const fleet = await deployedMicrovm(directory, { [LLM_GATEWAY]: gatewaySecret });
        expect(echoed(await fleet.invoke(PROMPT))).toEqual({
          base_url_set: true,
          base_url_is_gateway: true,
          api_key_is_placeholder: true,
          model_env: "LLM_MODEL",
        });
      }),
    TEST_TIMEOUT_MS,
  );

  it(
    "la CLI despliega con --secret-env y --secret-env-from y el agente ve los valores",
    () =>
      withRunSecrets(tagsFromEnvironment(), async (secrets) => {
        const { values, names } = await createdSecretEnv(secrets, "microvm-senv");
        const root = join(tmpdir(), `agn-vm-cli-${RUN_ID}`);
        const name = imageNameOf("senv");
        const directory = stagedProject(SECRET_ENV_SAMPLE, join(root, "src"), {
          name,
          dropFrom: SECRET_ENV_TABLE,
          base: BASE,
        });
        const tokenFile = join(root, TOKEN_FILE_NAME);
        try {
          const deployed = await cli(
            [
              "agent",
              "deploy",
              directory,
              ...secretEnvFlags(names),
              "--secret",
              `${BEDROCK_GATEWAY}=${BEDROCK_SECRET}`,
              "--bucket",
              MICROVM_BUCKET,
              "--yes",
              "--token-file",
              tokenFile,
            ],
            CLI_DEPLOY_BUDGET_MS,
          );
          expect(deployed.name).toBe(name);
          const answer = await cli(
            ["agent", "invoke", name, PROMPT, "--token-file", tokenFile],
            CLI_READ_BUDGET_MS,
          );
          expect(echoed(answer)).toEqual({
            present: [...SECRET_ENV_NAMES],
            sha256_8: expectedDigests(values),
          });
        } finally {
          await cli(["agent", "stop", name], CLI_READ_BUDGET_MS);
        }
      }),
    TEST_TIMEOUT_MS,
  );
});
