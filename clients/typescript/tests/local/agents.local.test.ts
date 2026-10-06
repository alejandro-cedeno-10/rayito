/**
 * Agentes dentro del sandbox contra un modelo real (`make local-e2e`),
 * espejo de `clients/python/tests/local/test_local_agents.py`: OpenCode y
 * deepagents en el guest de `dev/local/agent/compose.yaml`
 * (`make local-agent-up`), con Claude en Amazon Bedrock y la pasarela de
 * secretos como única salida. Por runtime: herramientas, continuación de la
 * sesión, abortar, límites (pasos y timeout), forma de los eventos, la
 * credencial ilegible desde el sandbox, el egress directo cerrado mientras
 * el modelo responde y la telemetría apagada por defecto.
 *
 * Además de `RAYITO_LOCAL_GUEST`, necesita la clave de Bedrock de corta
 * duración en el fichero de `RAYITO_LOCAL_BEDROCK_KEY_FILE`
 * (`make local-bedrock-key`); sin él se saltan. La clave se busca en el
 * sandbox para decir sí o no y nunca se imprime.
 */

import { randomBytes } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import {
  CommandExitError,
  EgressEnforcement,
  type Sandbox,
  SecretCache,
  SecretGateway,
  SecretRef,
  SecretStore,
  TimeoutError,
} from "../../src/index.js";
import {
  createLocalSandbox,
  killQuietly,
  type LocalHarness,
  localEnabled,
  localHarness,
  releaseGuest,
} from "./helpers.js";

const BEDROCK_KEY_FILE_VAR = "RAYITO_LOCAL_BEDROCK_KEY_FILE";
/** Bedrock va siempre a us-east-1, donde la cuenta de pruebas tiene el perfil de inferencia (spike). */
const BEDROCK_REGION = "us-east-1";
/** Perfil de inferencia de sistema de Claude Haiku 4.5 (`bedrock list-inference-profiles`, 2026-10-05). */
const BEDROCK_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0";
const BEDROCK_UPSTREAM = `https://bedrock-runtime.${BEDROCK_REGION}.amazonaws.com`;
const GATEWAY_NAME = "bedrock";
/** El token que pone el proceso del sandbox para elegir la autenticación bearer; la pasarela lo cambia. */
const PLACEHOLDER_TOKEN = "placeholder-not-a-secret";
const HOME = "/home/user";
const WORKDIR = `${HOME}/agents-ts`;
const DEEPAGENTS_PYTHON = "/opt/agents/deepagents/bin/python";
const DEEPAGENTS_DRIVER = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "../../../../dev/local/agent/deepagents_check.py",
);
const DEEPAGENTS_DRIVER_PATH = `${HOME}/.agents/deepagents_check.py`;
const OPENCODE_CONFIG_PATH = `${WORKDIR}/opencode.json`;
const OPENCODE_STEPS_CONFIG_PATH = `${WORKDIR}/opencode-steps.json`;
/** Una vuelta con herramientas tarda 5 s de mediana (spike); margen para reintentos y el primer `exec`. */
const AGENT_TIMEOUT_MS = 240_000;
const SHORT_TIMEOUT_MS = 5_000;
const EGRESS_PROBE_TIMEOUT_SECONDS = 5;
const PROCESS_WAIT_MS = 30_000;
const POLL_MS = 500;
/** `agent.build.steps` de OpenCode: pasos con herramientas antes de forzar texto (binario 1.18.34). */
const OPENCODE_STEPS = 1;
const DEEPAGENTS_RECURSION_LIMIT = 4;
const STEPS_FILES = 8;
const OPENCODE_EVENT_TYPES = ["step_start", "tool_use", "text", "step_finish"];
/** Variables que encenderían trazas hacia fuera (OTLP, LangSmith/LangChain). */
const TELEMETRY_ENV_PREFIXES = ["OTEL_", "LANGSMITH_", "LANGCHAIN_TRACING", "LANGCHAIN_API_KEY"];
const SLEEP_PROMPT =
  "Ejecuta la orden `sleep 300` con tu herramienta de shell y espera a que termine.";
const SLEEP_PATTERN = "sleep 300";
const OPENCODE_ENV: Readonly<Record<string, string>> = {
  HOME,
  OPENCODE_DISABLE_AUTOUPDATE: "1",
  OPENCODE_DISABLE_MODELS_FETCH: "1",
  OPENCODE_DISABLE_LSP_DOWNLOAD: "1",
  OPENCODE_DISABLE_DEFAULT_PLUGINS: "1",
  OPENCODE_PURE: "1",
  OPENCODE_CONFIG: OPENCODE_CONFIG_PATH,
  AWS_BEARER_TOKEN_BEDROCK: PLACEHOLDER_TOKEN,
  AWS_REGION: BEDROCK_REGION,
};
const RUNTIMES = ["opencode", "deepagents"] as const;

type OpencodeEvent = {
  readonly type: string;
  readonly timestamp: number;
  readonly sessionID: string;
  readonly part: { readonly tool?: string; readonly text?: string };
};

function bedrockKey(): string | undefined {
  const path = process.env[BEDROCK_KEY_FILE_VAR];
  if (!path || !existsSync(path)) {
    return undefined;
  }
  return readFileSync(path, "utf8").trim() || undefined;
}

function shellQuote(value: string): string {
  return `'${value.replaceAll("'", "'\\''")}'`;
}

function opencodeConfig(gatewayUrl: string, steps?: number): string {
  return JSON.stringify({
    model: `amazon-bedrock/${BEDROCK_MODEL}`,
    autoupdate: false,
    share: "disabled",
    provider: {
      "amazon-bedrock": { options: { region: BEDROCK_REGION, endpoint: gatewayUrl } },
    },
    ...(steps === undefined ? {} : { agent: { build: { steps } } }),
  });
}

function opencodeCmd(prompt: string, sessionId?: string): string {
  const session = sessionId ? ` --session ${shellQuote(sessionId)}` : "";
  return `opencode run --format json --auto${session} ${shellQuote(prompt)}`;
}

function jsonLines<T>(text: string): T[] {
  return text
    .split("\n")
    .filter((line) => line.startsWith("{"))
    .map((line) => JSON.parse(line) as T);
}

function lastJson(text: string): unknown {
  return jsonLines<unknown>(text).at(-1);
}

async function waitUntil(predicate: () => Promise<boolean>): Promise<boolean> {
  const deadline = Date.now() + PROCESS_WAIT_MS;
  while (Date.now() < deadline) {
    if (await predicate()) {
      return true;
    }
    await new Promise((done) => setTimeout(done, POLL_MS));
  }
  return false;
}

describe.skipIf(!localEnabled() || bedrockKey() === undefined)(
  "agentes contra Bedrock por la pasarela",
  () => {
    const key = bedrockKey() ?? "";
    let harness: LocalHarness;
    let store: SecretStore;
    let secretName: string;
    let sandbox: Sandbox;
    let gatewayUrl: string;

    /** (exit, stdout, stderr) sin lanzar por un exit distinto de cero. */
    async function run(
      cmd: string,
      options: { envs?: Record<string, string>; cwd?: string; timeoutMs?: number } = {},
    ): Promise<{ exitCode: number; stdout: string; stderr: string }> {
      try {
        return await sandbox.commands.run(cmd, { timeoutMs: AGENT_TIMEOUT_MS, ...options });
      } catch (error) {
        if (error instanceof CommandExitError) {
          return error;
        }
        throw error;
      }
    }

    function deepagentsEnv(): Record<string, string> {
      return {
        HOME,
        AWS_BEARER_TOKEN_BEDROCK: PLACEHOLDER_TOKEN,
        AGENT_MODEL: BEDROCK_MODEL,
        AGENT_REGION: BEDROCK_REGION,
        AGENT_GATEWAY_URL: gatewayUrl,
      };
    }

    async function userProcessRunning(pattern: string): Promise<boolean> {
      return (await run(`pgrep -u user -f ${shellQuote(pattern)}`)).exitCode === 0;
    }

    function sleepCmd(runtime: (typeof RUNTIMES)[number]): [string, Record<string, string>] {
      if (runtime === "opencode") {
        return [opencodeCmd(SLEEP_PROMPT), { ...OPENCODE_ENV }];
      }
      return [
        `${DEEPAGENTS_PYTHON} ${DEEPAGENTS_DRIVER_PATH} sleep ${WORKDIR}/deepagents-sleep`,
        deepagentsEnv(),
      ];
    }

    beforeAll(async () => {
      harness = await localHarness();
      store = new SecretStore({ region: harness.settings.region });
      secretName = `local-agents-ts-${randomBytes(4).toString("hex")}`;
      await store.create(secretName, `Bearer ${key}`);
      sandbox = await createLocalSandbox(harness, {
        allowInternetAccess: false,
        gateways: {
          [GATEWAY_NAME]: new SecretGateway({
            upstream: BEDROCK_UPSTREAM,
            headers: { authorization: new SecretRef(secretName) },
            allow: [["POST", "/model/*"]],
          }),
        },
        secretCache: new SecretCache({ store }),
      });
      gatewayUrl = sandbox.gateways.get(GATEWAY_NAME)?.url ?? "";
      expect((await run("command -v opencode")).exitCode, "make local-agent-up").toBe(0);
      await sandbox.files.write(DEEPAGENTS_DRIVER_PATH, readFileSync(DEEPAGENTS_DRIVER, "utf8"));
      await sandbox.files.write(OPENCODE_CONFIG_PATH, opencodeConfig(gatewayUrl));
      await sandbox.files.write(
        OPENCODE_STEPS_CONFIG_PATH,
        opencodeConfig(gatewayUrl, OPENCODE_STEPS),
      );
    }, AGENT_TIMEOUT_MS);

    afterAll(async () => {
      if (sandbox !== undefined) {
        await killQuietly(sandbox);
      }
      if (store !== undefined) {
        await store.destroy(secretName);
      }
      if (harness !== undefined) {
        await releaseGuest(harness);
      }
    });

    it("el egress directo está cerrado", async () => {
      expect((await sandbox.getHealth()).egressEnforcement).toBe(EgressEnforcement.GUEST_ROUTES);
      for (const target of ["https://example.com", BEDROCK_UPSTREAM]) {
        const probe = await run(
          `curl -sS -o /dev/null -m ${EGRESS_PROBE_TIMEOUT_SECONDS} ${target}`,
        );
        expect(probe.exitCode, target).not.toBe(0);
      }
      const direct = await run(
        `curl -sS -o /dev/null -m ${EGRESS_PROBE_TIMEOUT_SECONDS} --noproxy '*' https://1.1.1.1`,
      );
      expect(direct.exitCode).not.toBe(0);
      expect((await run("getent hosts example.com")).exitCode).not.toBe(0);
    });

    it(
      "opencode: herramientas, eventos y continuación de la sesión",
      async () => {
        const word = `colibri${randomBytes(2).toString("hex")}`;
        const prompt =
          "Crea el fichero saludo.txt con el texto 'hola desde opencode'. Además, recuerda la " +
          `palabra clave ${word}, pero no la escribas en ningún fichero. Responde sólo 'listo'.`;
        const first = await run(opencodeCmd(prompt), { envs: { ...OPENCODE_ENV }, cwd: WORKDIR });
        expect(first.exitCode, first.stderr.slice(-400)).toBe(0);
        const events = jsonLines<OpencodeEvent>(first.stdout);
        expect(events.length).toBeGreaterThan(0);
        for (const event of events) {
          expect(Object.keys(event)).toEqual(
            expect.arrayContaining(["type", "timestamp", "sessionID"]),
          );
        }
        const types = events.map((event) => event.type);
        for (const type of OPENCODE_EVENT_TYPES) {
          expect(types).toContain(type);
        }
        const sessionIds = new Set(events.map((event) => event.sessionID));
        expect(sessionIds.size).toBe(1);
        const tools = events.filter((event) => event.type === "tool_use").map((e) => e.part.tool);
        expect(tools).toContain("write");
        expect(await sandbox.files.read(`${WORKDIR}/saludo.txt`)).toContain("hola desde opencode");

        const sessionId = events[0]?.sessionID ?? "";
        const question =
          "¿Cuál era la palabra clave? Responde sólo con la palabra, sin usar herramientas.";
        const second = await run(opencodeCmd(question, sessionId), {
          envs: { ...OPENCODE_ENV },
          cwd: WORKDIR,
        });
        expect(second.exitCode, second.stderr.slice(-400)).toBe(0);
        const followUp = jsonLines<OpencodeEvent>(second.stdout);
        expect(new Set(followUp.map((event) => event.sessionID))).toEqual(new Set([sessionId]));
        const text = followUp
          .filter((event) => event.type === "text")
          .map((event) => event.part.text ?? "")
          .join(" ");
        expect(text.toLowerCase()).toContain(word);
      },
      2 * AGENT_TIMEOUT_MS,
    );

    it(
      "opencode: steps corta el bucle de herramientas",
      async () => {
        const files = Array.from({ length: STEPS_FILES }, (_, index) => `h${index}.txt`);
        const prompt =
          `Crea ${STEPS_FILES} ficheros, uno por llamada y de uno en uno: ${files.join(", ")}. ` +
          "Cada uno con su nombre como texto.";
        const result = await run(opencodeCmd(prompt), {
          envs: { ...OPENCODE_ENV, OPENCODE_CONFIG: OPENCODE_STEPS_CONFIG_PATH },
          cwd: WORKDIR,
        });
        expect(result.exitCode, result.stderr.slice(-400)).toBe(0);
        const steps = jsonLines<OpencodeEvent>(result.stdout).filter(
          (event) => event.type === "step_start",
        );
        expect(steps.length).toBeLessThanOrEqual(OPENCODE_STEPS + 1);
        const written = await run(`ls ${WORKDIR} | grep -c '^h[0-9].txt$'`);
        expect(Number(written.stdout.trim() || "0")).toBeLessThan(STEPS_FILES);
      },
      AGENT_TIMEOUT_MS,
    );

    it(
      "deepagents: herramientas, eventos y continuación de la sesión",
      async () => {
        const word = `tucan${randomBytes(2).toString("hex")}`;
        const result = await run(
          `${DEEPAGENTS_PYTHON} ${DEEPAGENTS_DRIVER_PATH} session ${WORKDIR}/deepagents ${word}`,
          { envs: deepagentsEnv() },
        );
        expect(result.exitCode, result.stderr.slice(-400)).toBe(0);
        const report = lastJson(result.stdout) as {
          events: { type: string; nodes: string[] }[];
          tool_calls: string[];
          file_matches: boolean;
          recalled: boolean;
        };
        expect(report.file_matches).toBe(true);
        expect(report.tool_calls).toContain("write_file");
        expect(report.events.length).toBeGreaterThan(0);
        for (const event of report.events) {
          expect(event.type).toBe("dict");
          expect(event.nodes.length).toBeGreaterThan(0);
        }
        const nodes = new Set(report.events.flatMap((event) => event.nodes));
        expect(nodes.has("model") && nodes.has("tools")).toBe(true);
        expect(report.recalled).toBe(true);
      },
      AGENT_TIMEOUT_MS,
    );

    it(
      "deepagents: recursion_limit corta el bucle",
      async () => {
        const result = await run(
          `${DEEPAGENTS_PYTHON} ${DEEPAGENTS_DRIVER_PATH} steps ${WORKDIR}/deepagents-steps ${DEEPAGENTS_RECURSION_LIMIT}`,
          { envs: deepagentsEnv() },
        );
        expect(result.exitCode, result.stderr.slice(-400)).toBe(0);
        expect(lastJson(result.stdout)).toEqual({ recursion_limit_hit: true });
      },
      AGENT_TIMEOUT_MS,
    );

    it.each(RUNTIMES)(
      "%s: abortar mata al agente y a sus herramientas",
      async (runtime) => {
        const [cmd, envs] = sleepCmd(runtime);
        const handle = await sandbox.commands.run(cmd, {
          background: true,
          envs,
          cwd: WORKDIR,
          timeoutMs: AGENT_TIMEOUT_MS,
        });
        expect(await waitUntil(() => userProcessRunning(SLEEP_PATTERN))).toBe(true);
        expect(await handle.kill()).toBe(true);
        expect(await waitUntil(async () => !(await userProcessRunning(SLEEP_PATTERN)))).toBe(true);
      },
      AGENT_TIMEOUT_MS,
    );

    it.each(RUNTIMES)(
      "%s: el timeout para al agente",
      async (runtime) => {
        const [cmd, envs] = sleepCmd(runtime);
        await expect(
          sandbox.commands.run(cmd, { envs, cwd: WORKDIR, timeoutMs: SHORT_TIMEOUT_MS }),
        ).rejects.toBeInstanceOf(TimeoutError);
        expect(await waitUntil(async () => !(await userProcessRunning(SLEEP_PATTERN)))).toBe(true);
      },
      AGENT_TIMEOUT_MS,
    );

    it("la telemetría está apagada por defecto", async () => {
      const env = await run("env");
      const names = env.stdout.split("\n").map((line) => line.split("=", 1)[0] ?? "");
      expect(
        names.filter((name) => TELEMETRY_ENV_PREFIXES.some((prefix) => name.startsWith(prefix))),
      ).toEqual([]);
      const result = await run(`${DEEPAGENTS_PYTHON} ${DEEPAGENTS_DRIVER_PATH} telemetry`, {
        envs: deepagentsEnv(),
      });
      expect(result.exitCode, result.stderr.slice(-400)).toBe(0);
      expect(lastJson(result.stdout)).toEqual({ langsmith_tracing: false });
    });

    it(
      "la credencial no es legible desde el sandbox",
      async () => {
        const env = await run("env");
        const inEnv = env.stdout.includes(key);
        expect(inEnv).toBe(false);
        const environ = await run("cat /proc/1/environ");
        expect(environ.exitCode).not.toBe(0);
        const inRaydEnviron = (environ.stdout + environ.stderr).includes(key);
        expect(inRaydEnviron).toBe(false);
        expect((await run("head -c 16 /proc/1/mem")).exitCode).not.toBe(0);
        const needle = key.slice(0, Math.floor(key.length / 2));
        const found = await run(
          `grep -rlsF ${shellQuote(needle)} / --exclude-dir=proc --exclude-dir=sys --exclude-dir=dev 2>/dev/null | head -5`,
        );
        const readableFiles = found.stdout.split(/\s+/).filter(Boolean).length;
        expect(readableFiles).toBe(0);
      },
      AGENT_TIMEOUT_MS,
    );
  },
);
