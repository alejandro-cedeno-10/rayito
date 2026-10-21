/**
 * Lo común a `agnostic-deploy.e2e.test.ts` (ADR-033): los proyectos de ejemplo
 * de `testdata/` copiados con un nombre propio de la ejecución, los secretos que
 * crea la prueba y la CLI en un subproceso. Variables, coste y tope: cabecera de
 * `clients/python/tests/e2e/agnostic_deploy_support.py` (las mismas, con
 * `RAYITO_E2E_AGNOSTIC=1`). Los valores de los secretos son aleatorios, se
 * comprueban por su resumen y nunca se imprimen.
 */

import { execFile } from "node:child_process";
import { createHash, randomBytes } from "node:crypto";
import { cpSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { promisify } from "node:util";

export const REPO_ROOT = join(import.meta.dirname, "../../../..");
export const TESTDATA = join(REPO_ROOT, "testdata");
export const STRUCTURED_SAMPLE = join(
  TESTDATA,
  "agent-structured-samples",
  "python",
  "langgraph-structured",
);
export const SECRET_ENV_SAMPLE = join(TESTDATA, "agent-secret-env", "secret-env-echo");
export const OPENAI_COMPATIBLE_SAMPLE = join(
  TESTDATA,
  "agent-project-e2e",
  "ts",
  "openai-compatible-env",
);
export const AGNOSTIC_ENABLED = process.env.RAYITO_E2E_AGNOSTIC === "1";
export const RUN_ID = process.env.RAYITO_E2E_RUN_ID ?? "";
export const REGION = process.env.AWS_REGION ?? "";
const IGNORED_DIRS = [".venv", "node_modules", "__pycache__", ".git", "expected"];
const NAME_LINE = /^name = "[^"]*"$/m;
const CLI_ENTRY = new URL("../../dist/cli.mjs", import.meta.url);
/** Los grafos de `langgraph-structured` (`triage.py`): un id que empieza por `T-1` es urgente. */
export const TRIAGE_GRAPH = "triage";
export const URGENT_TICKET = "T-100";
export const NORMAL_TICKET = "T-200";
/** Las variables de `secret-env-echo` (`agent.py`) y su resumen: 8 caracteres del SHA-256. */
export const SECRET_ENV_NAMES = ["DATABASE_URL", "TICKETS_TOKEN", "APP_DB_HOST"] as const;
const DIGEST_CHARS = 8;
const SECRET_VALUE_BYTES = 16;
const MS_PER_SECOND = 1000;
export const CLI_DEPLOY_BUDGET_MS = 3000 * MS_PER_SECOND;
export const CLI_READ_BUDGET_MS = 180 * MS_PER_SECOND;
const CLI_MAX_BUFFER_BYTES = 64 * 1024 * 1024;

export interface StageOptions {
  readonly name: string;
  readonly replace?: Readonly<Record<string, string>>;
  readonly extra?: readonly string[];
  readonly base?: string;
  readonly dropFrom?: string;
}

/** Una copia del ejemplo con el nombre de la ejecución, como `staged_project` de Python. */
export function stagedProject(source: string, root: string, options: StageOptions): string {
  const target = join(root, options.name);
  mkdirSync(root, { recursive: true });
  cpSync(source, target, {
    recursive: true,
    filter: (path) => !IGNORED_DIRS.some((ignored) => path.split("/").includes(ignored)),
  });
  const projectFile = join(target, "rayito-agent.toml");
  let text = existsSync(projectFile)
    ? readFileSync(projectFile, "utf8").replace(NAME_LINE, `name = "${options.name}"`)
    : `schema = "rayito.agent-project/1"\nname = "${options.name}"\n`;
  if (options.dropFrom !== undefined) {
    text = text.split(options.dropFrom)[0] ?? "";
  }
  for (const [old, replacement] of Object.entries(options.replace ?? {})) {
    if (!text.includes(old)) {
      throw new Error(`el ejemplo no contiene ${old}`);
    }
    text = text.split(old).join(replacement);
  }
  const lines = [text.trimEnd(), ...(options.extra ?? [])];
  if (options.base !== undefined) {
    lines.push("", "[image]", `base = "${options.base}"`);
  }
  writeFileSync(projectFile, `${lines.join("\n")}\n`);
  return target;
}

export function digest(value: string): string {
  return createHash("sha256").update(value, "utf8").digest("hex").slice(0, DIGEST_CHARS);
}

export function randomValue(): string {
  return randomBytes(SECRET_VALUE_BYTES).toString("hex");
}

export function secretName(kind: string): string {
  return `rayito-e2e/${RUN_ID}/${kind}`;
}

/** Las posiciones de `values` que aparecen en algún texto (nunca el valor). */
export function foundIn(texts: Iterable<string>, values: readonly string[]): number[] {
  const found = new Set<number>();
  for (const text of texts) {
    values.forEach((value, index) => {
      if (text.includes(value)) {
        found.add(index);
      }
    });
  }
  return [...found].sort();
}

interface SecretsModule {
  readonly SecretsManagerClient: new (
    config: object,
  ) => {
    send(command: unknown): Promise<unknown>;
  };
  readonly CreateSecretCommand: new (input: object) => unknown;
  readonly DeleteSecretCommand: new (input: object) => unknown;
}

/** Los secretos que crea la prueba; `deleteAll` los borra sin ventana de recuperación. */
export class RunSecrets {
  readonly #created = new Set<string>();
  readonly #tags: Readonly<Record<string, string>>;

  constructor(tags: Readonly<Record<string, string>>) {
    this.#tags = tags;
  }

  async create(name: string, value: string | Readonly<Record<string, string>>): Promise<string> {
    const sdk = await secretsModule();
    const client = new sdk.SecretsManagerClient({ region: REGION });
    const output = (await client.send(
      new sdk.CreateSecretCommand({
        Name: name,
        SecretString: typeof value === "string" ? value : JSON.stringify(value),
        Tags: Object.entries(this.#tags).map(([Key, Value]) => ({ Key, Value })),
      }),
    )) as { ARN?: string };
    this.#created.add(name);
    return output.ARN ?? "";
  }

  async deleteAll(): Promise<void> {
    const sdk = await secretsModule();
    const client = new sdk.SecretsManagerClient({ region: REGION });
    for (const name of [...this.#created]) {
      await client.send(
        new sdk.DeleteSecretCommand({ SecretId: name, ForceDeleteWithoutRecovery: true }),
      );
      this.#created.delete(name);
    }
  }
}

async function secretsModule(): Promise<SecretsModule> {
  return (await import("@aws-sdk/client-secrets-manager")) as unknown as SecretsModule;
}

interface ControlModule {
  readonly BedrockAgentCoreControlClient: new (
    config: object,
  ) => {
    send(command: unknown): Promise<unknown>;
  };
  readonly GetAgentRuntimeCommand: new (input: object) => unknown;
}

/** `environmentVariables` de `GetAgentRuntime`, como lo ve quien puede leer el runtime. */
export async function runtimeEnvironment(runtimeId: string): Promise<Record<string, string>> {
  const sdk = (await import(
    "@aws-sdk/client-bedrock-agentcore-control"
  )) as unknown as ControlModule;
  const client = new sdk.BedrockAgentCoreControlClient({ region: REGION });
  const output = (await client.send(
    new sdk.GetAgentRuntimeCommand({ agentRuntimeId: runtimeId }),
  )) as { environmentVariables?: Record<string, string> };
  return output.environmentVariables ?? {};
}

/** La CLI de TypeScript (`dist/cli.mjs`) con `--json`: su documento. */
export async function cli(
  args: readonly string[],
  timeoutMs: number,
): Promise<Record<string, unknown>> {
  const { stdout } = await promisify(execFile)(
    process.execPath,
    [CLI_ENTRY.pathname, "--region", REGION, "--json", ...args],
    { timeout: timeoutMs, maxBuffer: CLI_MAX_BUFFER_BYTES },
  );
  return JSON.parse(stdout) as Record<string, unknown>;
}
