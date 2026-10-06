/**
 * `sbx.agent` contra un modelo real (`make local-e2e`), espejo de
 * `clients/python/tests/local/test_local_agents.py`: OpenCode y deepagents en
 * el guest de `dev/local/agent/compose.yaml` (`make local-agent-up`), con
 * Claude en Amazon Bedrock y `bedrockGateway` como única salida. Por runtime:
 * herramientas, continuación de la sesión, `abort()`, los límites de
 * `AgentLimits` sin dejar procesos vivos, el stream de eventos, la credencial
 * ilegible desde el sandbox, el egress directo cerrado mientras el modelo
 * responde y la telemetría apagada por defecto.
 *
 * Además de `RAYITO_LOCAL_GUEST`, necesita la clave de Bedrock de corta
 * duración en el fichero de `RAYITO_LOCAL_BEDROCK_KEY_FILE`
 * (`make local-bedrock-key`); sin él se saltan. El modelo y su región se
 * cambian con `RAYITO_E2E_BEDROCK_MODEL` y `RAYITO_E2E_BEDROCK_REGION`. La
 * clave se busca en el sandbox para decir sí o no y nunca se imprime.
 */

import { randomBytes } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import {
  type AgentEvent,
  AgentLimits,
  AgentModel,
  AgentSpec,
  bedrockGateway,
  CommandExitError,
  EgressEnforcement,
  type Sandbox,
  SecretCache,
  SecretStore,
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
/** Perfil de inferencia de sistema de Claude Haiku 4.5 en us-east-1, el del spike. */
const DEFAULT_BEDROCK_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0";
const DEFAULT_BEDROCK_REGION = "us-east-1";
const BEDROCK_MODEL = process.env.RAYITO_E2E_BEDROCK_MODEL || DEFAULT_BEDROCK_MODEL;
const BEDROCK_REGION = process.env.RAYITO_E2E_BEDROCK_REGION || DEFAULT_BEDROCK_REGION;
const BEDROCK_UPSTREAM = `https://bedrock-runtime.${BEDROCK_REGION}.amazonaws.com`;
const GATEWAY_NAME = "bedrock";
const RUNTIMES = ["opencode", "deepagents"] as const;
type Runtime = (typeof RUNTIMES)[number];
const WORKDIR = "/home/user";
/** Una vuelta con herramientas tarda 5 s de mediana (spike); margen para reintentos y el primer `exec`. */
const AGENT_TIMEOUT_MS = 240_000;
/** El `timeoutMs` de una ejecución que no puede terminar a tiempo. */
const SHORT_TIMEOUT_MS = 20_000;
const EGRESS_PROBE_TIMEOUT_SECONDS = 5;
const PROCESS_WAIT_MS = 60_000;
const POLL_MS = 500;
/** Ficheros que se piden de uno en uno para obligar a varios pasos. */
const STEPS_FILES = 6;
/** Variables que encenderían trazas hacia fuera (OTLP, LangSmith/LangChain). */
const TELEMETRY_ENV_PREFIXES = ["OTEL_", "LANGSMITH_", "LANGCHAIN_TRACING", "LANGCHAIN_API_KEY"];
/** Segundos del `sleep` que se le pide al agente, distintos por test (y del suite de Python). */
const ABORT_SLEEP_SECONDS: Readonly<Record<Runtime, number>> = { opencode: 311, deepagents: 312 };
const TIMEOUT_SLEEP_SECONDS: Readonly<Record<Runtime, number>> = { opencode: 313, deepagents: 314 };
/** La huella de los procesos de cada runtime en `ps` (el corchete evita que `pgrep -f` se encuentre). */
const RUNTIME_PATTERNS: Readonly<Record<Runtime, string>> = {
  opencode: "[o]pencode run",
  deepagents: "[d]eepagents_runner.py",
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

function sleepPrompt(seconds: number): string {
  return `Ejecuta la orden \`sleep ${seconds}\` con tu herramienta de shell y espera a que termine.`;
}

function sleepPattern(seconds: number): string {
  return `[s]leep ${seconds}`;
}

function stepsPrompt(runtime: Runtime): string {
  const names = Array.from(
    { length: STEPS_FILES },
    (_, index) => `${WORKDIR}/${runtime}-ts-paso${index}.txt`,
  ).join(", ");
  return (
    `Crea estos ${STEPS_FILES} ficheros, uno por llamada a herramienta y de uno en uno, ` +
    `esperando el resultado de cada llamada antes de la siguiente: ${names}. ` +
    "Cada uno con su nombre como texto."
  );
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
  "sbx.agent contra Bedrock por la pasarela",
  () => {
    const key = bedrockKey() ?? "";
    const spec = new AgentSpec({
      model: new AgentModel({
        provider: "bedrock",
        id: BEDROCK_MODEL,
        gateway: GATEWAY_NAME,
        region: BEDROCK_REGION,
      }),
    });
    let harness: LocalHarness;
    let store: SecretStore;
    let secretName: string;
    let sandbox: Sandbox;

    /** (exit, stdout) sin lanzar por un exit distinto de cero. */
    async function sh(cmd: string): Promise<{ exitCode: number; stdout: string }> {
      try {
        return await sandbox.commands.run(cmd, { timeoutMs: AGENT_TIMEOUT_MS });
      } catch (error) {
        if (error instanceof CommandExitError) {
          return error;
        }
        throw error;
      }
    }

    async function running(pattern: string): Promise<boolean> {
      return (await sh(`pgrep -u user -f ${shellQuote(pattern)}`)).exitCode === 0;
    }

    function gone(pattern: string): Promise<boolean> {
      return waitUntil(async () => !(await running(pattern)));
    }

    async function collect(stream: AsyncIterable<AgentEvent>): Promise<AgentEvent[]> {
      const events: AgentEvent[] = [];
      for await (const event of stream) {
        events.push(event);
      }
      return events;
    }

    beforeAll(async () => {
      harness = await localHarness();
      store = new SecretStore({ region: harness.settings.region });
      secretName = `local-agents-ts-${randomBytes(4).toString("hex")}`;
      await store.create(secretName, `Bearer ${key}`);
      sandbox = await createLocalSandbox(harness, {
        allowInternetAccess: false,
        gateways: {
          [GATEWAY_NAME]: bedrockGateway(secretName, {
            region: BEDROCK_REGION,
            models: [BEDROCK_MODEL],
          }),
        },
        secretCache: new SecretCache({ store }),
      });
      expect((await sh("command -v opencode")).exitCode, "make local-agent-up").toBe(0);
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
        const probe = await sh(
          `curl -sS -o /dev/null -m ${EGRESS_PROBE_TIMEOUT_SECONDS} ${target}`,
        );
        expect(probe.exitCode, target).not.toBe(0);
      }
      const direct = await sh(
        `curl -sS -o /dev/null -m ${EGRESS_PROBE_TIMEOUT_SECONDS} --noproxy '*' https://1.1.1.1`,
      );
      expect(direct.exitCode).not.toBe(0);
    });

    it.each(RUNTIMES)(
      "%s: herramientas, eventos y continuación de la sesión",
      async (runtime) => {
        const word = `colibri${randomBytes(2).toString("hex")}`;
        const path = `${WORKDIR}/${runtime}-ts-saludo.txt`;
        const prompt =
          `Crea el fichero ${path} con el texto 'hola desde ${runtime}'. Además, recuerda la ` +
          `palabra clave ${word}, pero no la escribas en ningún fichero. Responde sólo 'listo'.`;
        const stream = await sandbox.agent.stream(prompt, { spec, runtime });
        const probe = await sh(
          `curl -sS -o /dev/null -m ${EGRESS_PROBE_TIMEOUT_SECONDS} ${BEDROCK_UPSTREAM}`,
        );
        const events = await collect(stream);
        const result = await stream.result();
        await stream.close();
        expect(probe.exitCode).not.toBe(0);
        expect(events[0]?.type).toBe("step_started");
        const last = events.at(-1);
        expect(last?.type).toBe("done");
        const kinds = new Set(events.map((event) => event.type));
        for (const kind of ["step_started", "step_finished", "tool_call", "text"]) {
          expect(kinds.has(kind as AgentEvent["type"]), kind).toBe(true);
        }
        expect(kinds.has("agent_failed")).toBe(false);
        expect(
          events.some((event) => event.type === "tool_call" && event.status === "completed"),
        ).toBe(true);
        expect(result.steps).toBeGreaterThanOrEqual(2);
        expect(result.usage.input).toBeGreaterThan(0);
        expect(result.usage.output).toBeGreaterThan(0);
        expect(await sandbox.files.read(path)).toContain(`hola desde ${runtime}`);

        const followUp = await sandbox.agent.run(
          "¿Cuál era la palabra clave? Responde sólo con la palabra, sin usar herramientas.",
          { spec, runtime, sessionId: result.sessionId },
        );
        expect(followUp.sessionId).toBe(result.sessionId);
        expect(followUp.text.toLowerCase()).toContain(word);
      },
      2 * AGENT_TIMEOUT_MS,
    );

    it.each(RUNTIMES)(
      "%s: abort() para el agente y lo que lanzó su herramienta de shell",
      async (runtime) => {
        const seconds = ABORT_SLEEP_SECONDS[runtime];
        const stream = await sandbox.agent.stream(sleepPrompt(seconds), { spec, runtime });
        expect(await waitUntil(() => running(sleepPattern(seconds)))).toBe(true);
        await stream.abort();
        const events = await collect(stream);
        await stream.close();
        expect(events.at(-1)).toMatchObject({ type: "agent_failed", reason: "aborted" });
        expect(await gone(RUNTIME_PATTERNS[runtime])).toBe(true);
        expect(await gone(sleepPattern(seconds))).toBe(true);
      },
      AGENT_TIMEOUT_MS,
    );

    it.each(RUNTIMES)(
      "%s: el timeout para el agente",
      async (runtime) => {
        const seconds = TIMEOUT_SLEEP_SECONDS[runtime];
        const stream = await sandbox.agent.stream(sleepPrompt(seconds), {
          spec,
          runtime,
          limits: new AgentLimits({ timeoutMs: SHORT_TIMEOUT_MS }),
        });
        const events = await collect(stream);
        await stream.close();
        expect(events.at(-1)).toMatchObject({ type: "agent_failed", reason: "timeout" });
        expect(await gone(RUNTIME_PATTERNS[runtime])).toBe(true);
      },
      AGENT_TIMEOUT_MS,
    );

    /**
     * El timeout lo impone `rayd` con `SIGKILL` al grupo del proceso y la
     * herramienta de shell del agente corre en una sesión propia: hasta que
     * `rayd` mate el árbol, su `sleep` sobrevive (espejo del `xfail` de Python).
     */
    it.fails.each(RUNTIMES)(
      "%s: el timeout también para lo que lanzó su herramienta de shell",
      async (runtime) => {
        const pattern = sleepPattern(TIMEOUT_SLEEP_SECONDS[runtime]);
        const alive = await running(pattern);
        await sh(`pkill -KILL -u user -f ${shellQuote(pattern)}`);
        expect(alive).toBe(false);
      },
    );

    it.each(
      RUNTIMES.flatMap((runtime) => [
        { runtime, limits: new AgentLimits({ maxSteps: 1 }), reason: "max_steps" },
        { runtime, limits: new AgentLimits({ maxTotalTokens: 1 }), reason: "token_budget" },
      ]),
    )(
      "$runtime: $reason corta la ejecución y no deja el runtime trabajando",
      async ({ runtime, limits, reason }) => {
        const stream = await sandbox.agent.stream(stepsPrompt(runtime), { spec, runtime, limits });
        const events = await collect(stream);
        await stream.close();
        expect(events.at(-1), JSON.stringify(events.at(-1))).toMatchObject({
          type: "agent_failed",
          reason,
        });
        expect(await gone(RUNTIME_PATTERNS[runtime])).toBe(true);
      },
      AGENT_TIMEOUT_MS,
    );

    it.each(RUNTIMES)(
      "%s: sin telemetría ni credencial en el entorno del agente",
      async (runtime) => {
        const path = `${WORKDIR}/${runtime}-ts-env.txt`;
        await sandbox.agent.run(
          `Ejecuta exactamente \`env > ${path}\` con tu herramienta de shell. Responde sólo 'listo'.`,
          { spec, runtime },
        );
        const envDump = await sandbox.files.read(path);
        const names = envDump
          .split("\n")
          .filter((line) => line.includes("="))
          .map((line) => line.split("=", 1)[0] ?? "");
        expect(names.length).toBeGreaterThan(0);
        expect(
          names.filter((name) => TELEMETRY_ENV_PREFIXES.some((prefix) => name.startsWith(prefix))),
        ).toEqual([]);
        const inAgentEnv = envDump.includes(key);
        expect(inAgentEnv).toBe(false);
      },
      AGENT_TIMEOUT_MS,
    );

    it(
      "la credencial no es legible desde el sandbox",
      async () => {
        const inEnv = (await sh("env")).stdout.includes(key);
        expect(inEnv).toBe(false);
        const rayd = await sh("cat /proc/1/environ 2>&1");
        expect(rayd.exitCode).not.toBe(0);
        const inRaydEnviron = rayd.stdout.includes(key);
        expect(inRaydEnviron).toBe(false);
        expect((await sh("head -c 16 /proc/1/mem")).exitCode).not.toBe(0);
        const needle = key.slice(0, Math.floor(key.length / 2));
        const found = await sh(
          `grep -rlsF ${shellQuote(needle)} / --exclude-dir=proc --exclude-dir=sys ` +
            "--exclude-dir=dev 2>/dev/null | head -5",
        );
        const readableFiles = found.stdout.split(/\s+/).filter(Boolean).length;
        expect(readableFiles).toBe(0);
      },
      AGENT_TIMEOUT_MS,
    );
  },
);
