/**
 * `sbx.agent` contra AWS real, espejo de
 * `clients/python/tests/e2e/test_agent_e2e.py` (Q150 y Q152 de
 * `AWS_API_NOTES.md` §16): OpenCode y deepagents en un sandbox de una imagen
 * de `AgentTemplate`, con Claude en Amazon Bedrock y `bedrockGateway` como
 * única salida. Herramientas y continuación de la sesión por runtime, egress
 * directo cerrado, la pasarela restringida a las rutas del modelo elegido y
 * la credencial ilegible desde el sandbox.
 *
 * Además de `RAYITO_E2E=1` y `RAYITO_TEMPLATE`, necesita
 * `RAYITO_E2E_AGENT_TEMPLATE` (imagen de `AgentTemplate` con los dos
 * runtimes) y `RAYITO_E2E_BEDROCK_SECRET` (secreto que ya guarda
 * `Bearer <clave de API de Bedrock de corta duración>`); sin ellas se salta.
 * Coste: un sandbox de 2 GB durante unos minutos y ≈ 6 llamadas a Haiku
 * (≈ $0,15). La clave se busca en el sandbox para decir sí o no y nunca se
 * imprime.
 */

import { randomBytes } from "node:crypto";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import {
  type AgentEvent,
  AgentModel,
  AgentSpec,
  bedrockGateway,
  CommandExitError,
  EgressEnforcement,
  Sandbox,
  SecretCache,
  SecretStore,
} from "../../src/index.js";
import { e2eEnabled, TEST_SANDBOX_TIMEOUT_MS, useE2E } from "./helpers.js";

const AGENT_TEMPLATE_VAR = "RAYITO_E2E_AGENT_TEMPLATE";
/** El nombre de la variable de entorno (no el secreto ni su nombre). */
const BEDROCK_SECRET_VAR: string = "RAYITO_E2E_BEDROCK_SECRET";
/** Perfil de inferencia de sistema de Claude Haiku 4.5 en us-east-1, el del spike. */
const DEFAULT_BEDROCK_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0";
const DEFAULT_BEDROCK_REGION = "us-east-1";
/** Un modelo que la pasarela no permite: sólo se usa su ruta, nunca llega a Bedrock. */
const OTHER_BEDROCK_MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0";
const BEDROCK_MODEL = process.env.RAYITO_E2E_BEDROCK_MODEL || DEFAULT_BEDROCK_MODEL;
const BEDROCK_REGION = process.env.RAYITO_E2E_BEDROCK_REGION || DEFAULT_BEDROCK_REGION;
const BEDROCK_UPSTREAM = `https://bedrock-runtime.${BEDROCK_REGION}.amazonaws.com`;
const GATEWAY_NAME = "bedrock";
const RUNTIMES = ["opencode", "deepagents"] as const;
const WORKDIR = "/home/user";
/** Una vuelta con herramientas tarda 5 s de mediana (spike); margen para reintentos y el primer `exec` (Q142). */
const AGENT_TIMEOUT_MS = 240_000;
const EGRESS_PROBE_TIMEOUT_SECONDS = 5;
/** `curl -w` para una ruta fuera de `allow`: `rayd` responde 403 sin cuerpo (§28); un 403 de Bedrock trae JSON. */
const GATEWAY_DENIED = "403 0";

const agentConfigured =
  Boolean(process.env[AGENT_TEMPLATE_VAR]) && Boolean(process.env[BEDROCK_SECRET_VAR]);

function shellQuote(value: string): string {
  return `'${value.replaceAll("'", "'\\''")}'`;
}

describe.runIf(e2eEnabled() && agentConfigured)("sbx.agent en AWS real (Bedrock)", () => {
  const e2e = useE2E(AGENT_TEMPLATE_VAR);
  const secretName = process.env[BEDROCK_SECRET_VAR] ?? "";
  const spec = new AgentSpec({
    model: new AgentModel({
      provider: "bedrock",
      id: BEDROCK_MODEL,
      gateway: GATEWAY_NAME,
      region: BEDROCK_REGION,
    }),
  });
  let key = "";
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

  async function gatewayStatus(method: string, path: string): Promise<string> {
    const url = sandbox.gateways.get(GATEWAY_NAME)?.url ?? "";
    const probe = await sh(
      `curl -sS -o /dev/null -w '%{http_code} %{size_download}' -m ${EGRESS_PROBE_TIMEOUT_SECONDS} ` +
        `-X ${method} -H 'content-type: application/json' -d '{}' ${shellQuote(url + path)}`,
    );
    return probe.stdout.trim();
  }

  async function collect(stream: AsyncIterable<AgentEvent>): Promise<AgentEvent[]> {
    const events: AgentEvent[] = [];
    for await (const event of stream) {
      events.push(event);
    }
    return events;
  }

  beforeAll(async () => {
    const store = new SecretStore({ region: e2e.controlPlane.region });
    const cache = new SecretCache({ store });
    key = (await cache.get(secretName)).replace(/^Bearer /, "");
    sandbox = await Sandbox.create({
      template: e2e.templateArn,
      timeoutMs: TEST_SANDBOX_TIMEOUT_MS,
      idle: null,
      executionRoleArn: e2e.settings.executionRoleArn,
      logging: e2e.settings.logging,
      controlPlane: e2e.controlPlane,
      allowInternetAccess: false,
      gateways: {
        [GATEWAY_NAME]: bedrockGateway(secretName, {
          region: BEDROCK_REGION,
          models: [BEDROCK_MODEL],
        }),
      },
      secretCache: cache,
    });
    e2e.created.push(sandbox);
  }, AGENT_TIMEOUT_MS);

  afterAll(() => {
    key = "";
  });

  it("el egress directo está cerrado", async () => {
    expect((await sandbox.getHealth()).egressEnforcement).toBe(EgressEnforcement.GUEST_ROUTES);
    for (const target of ["https://example.com", BEDROCK_UPSTREAM]) {
      const probe = await sh(`curl -sS -o /dev/null -m ${EGRESS_PROBE_TIMEOUT_SECONDS} ${target}`);
      expect(probe.exitCode, target).not.toBe(0);
    }
    const direct = await sh(
      `curl -sS -o /dev/null -m ${EGRESS_PROBE_TIMEOUT_SECONDS} --noproxy '*' https://1.1.1.1`,
    );
    expect(direct.exitCode).not.toBe(0);
  });

  it("la pasarela sólo deja pasar el modelo elegido (y éste sí llega)", async () => {
    const other = encodeURIComponent(OTHER_BEDROCK_MODEL);
    const chosen = encodeURIComponent(BEDROCK_MODEL);
    expect(await gatewayStatus("POST", `/model/${chosen}/converse`)).not.toBe(GATEWAY_DENIED);
    expect(await gatewayStatus("POST", `/model/${other}/converse`)).toBe(GATEWAY_DENIED);
    expect(await gatewayStatus("POST", `/model/${chosen}/invoke`)).toBe(GATEWAY_DENIED);
    expect(await gatewayStatus("GET", `/model/${chosen}/converse`)).toBe(GATEWAY_DENIED);
    expect(await gatewayStatus("GET", "/foundation-models")).toBe(GATEWAY_DENIED);
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
      expect(events.at(-1)?.type).toBe("done");
      const kinds = new Set(events.map((event) => event.type));
      for (const kind of ["step_started", "step_finished", "tool_call"]) {
        expect(kinds.has(kind as AgentEvent["type"]), kind).toBe(true);
      }
      expect(kinds.has("agent_failed")).toBe(false);
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

  it("la credencial no es legible desde el sandbox", async () => {
    const env = await sh("env");
    const inEnv = env.stdout.includes(key);
    expect(inEnv).toBe(false);
    const rayd = await sh("cat /proc/1/environ 2>&1");
    expect(rayd.exitCode).not.toBe(0);
    const inRaydEnviron = rayd.stdout.includes(key);
    expect(inRaydEnviron).toBe(false);
    const needle = key.slice(0, Math.floor(key.length / 2));
    const files = await sh(
      `grep -rlsF ${shellQuote(needle)} / --exclude-dir=proc --exclude-dir=sys ` +
        "--exclude-dir=dev 2>/dev/null | head -5",
    );
    const readableFiles = files.stdout.split(/\s+/).filter(Boolean).length;
    expect(readableFiles).toBe(0);
  });
});
