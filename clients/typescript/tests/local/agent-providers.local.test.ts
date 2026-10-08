/**
 * Los presets de pasarela de cada proveedor de modelo con OpenCode y
 * deepagents (`make local-providers-up` y `make local-e2e`), espejo de
 * `clients/python/tests/local/test_local_agent_providers.py`: sin claves
 * reales ni Internet, contra el upstream HTTPS falso de
 * `dev/local/providers/fake_upstream.py`.
 *
 * Para cada preset de `testdata/agent/provider-catalogue.json` y cada
 * runtime que lo admite, con el egress cerrado: el agente completa una vuelta
 * y devuelve el texto del upstream falso; al upstream llega el valor del
 * secreto en la cabecera del preset y nunca `MODEL_CREDENTIAL_PLACEHOLDER`;
 * sólo llegan rutas de la allowlist, y una que no lo está recibe 403 de
 * `rayd` sin llegar al upstream.
 *
 * Además de `RAYITO_LOCAL_GUEST`, necesita
 * `RAYITO_LOCAL_FAKE_UPSTREAM_ADMIN` (la define
 * `dev/local/providers/compose.yaml`); sin ella se saltan.
 */

import { randomBytes } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import * as rayito from "../../src/index.js";
import {
  AgentLimits,
  AgentModel,
  AgentSpec,
  CommandExitError,
  EgressEnforcement,
  type ModelProvider,
  type Sandbox,
  SecretCache,
  type SecretGateway,
  SecretStore,
} from "../../src/index.js";
import { MODEL_CREDENTIAL_PLACEHOLDER } from "../../src/limits.js";
import {
  createLocalSandbox,
  killQuietly,
  type LocalHarness,
  localEnabled,
  localHarness,
  releaseGuest,
} from "./helpers.js";

const FAKE_UPSTREAM_ADMIN_VAR = "RAYITO_LOCAL_FAKE_UPSTREAM_ADMIN";
/** El texto con el que contesta siempre el upstream falso (`REPLY_TEXT`). */
const FAKE_REPLY = "rayito-fake-ok";
/** Uno por proveedor (`litellm_root` ya lo cubren los tests unitarios). */
const PRESETS = [
  "openai",
  "gemini",
  "azure_openai",
  "openrouter",
  "groq",
  "mistral",
  "deepseek",
  "xai",
  "litellm",
] as const;
const RUNTIMES = ["opencode", "deepagents"] as const;
/** El id de modelo cuando el preset no limita el modelo (va en el cuerpo). */
const GENERIC_MODEL_ID = "rayito-local-model";
const AGENT_TIMEOUT_MS = 240_000;
const COMMAND_TIMEOUT_MS = 60_000;
const SANDBOX_SETUP_TIMEOUT_MS = 300_000;
const EGRESS_PROBE_TIMEOUT_SECONDS = 5;
/** Una ruta que ningún preset permite. */
const FORBIDDEN_PATH = "/v1/models";
const FORBIDDEN_STATUS = 403;
const AUTHORIZATION_HEADER = "authorization";
const PROMPT = "Responde sólo con la palabra 'hola', sin usar herramientas.";

interface PresetEntry {
  readonly name: string;
  readonly typescript: string;
  readonly args: Record<string, unknown>;
  readonly model_provider: ModelProvider;
  readonly base_path: string;
  readonly upstream: string;
  readonly headers: string[];
  readonly allow: [string, string][];
}

interface Catalogue {
  readonly presets: PresetEntry[];
  readonly model_providers: Record<string, { readonly deepagents: string }>;
}

interface Received {
  readonly host: string;
  readonly path: string;
  readonly headers: Record<string, string>;
  readonly body: string;
}

const CATALOGUE = JSON.parse(
  readFileSync(
    join(
      import.meta.dirname,
      "..",
      "..",
      "..",
      "..",
      "testdata",
      "agent",
      "provider-catalogue.json",
    ),
    "utf8",
  ),
) as Catalogue;

type Preset = (secret: string, options?: Record<string, unknown>) => SecretGateway;

function camelArgs(args: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(args).map(([key, value]) => [
      key.replace(/_([a-z])/g, (_, letter: string) => letter.toUpperCase()),
      value,
    ]),
  );
}

function entryOf(name: string): PresetEntry {
  const entry = CATALOGUE.presets.find((preset) => preset.name === name);
  if (entry === undefined) {
    throw new Error(`${name} no está en el catálogo`);
  }
  return entry;
}

/** El nombre de la ruta de la pasarela (`[a-z0-9-]`). */
function routeOf(name: string): string {
  return name.replaceAll("_", "-");
}

function hostOf(entry: PresetEntry): string {
  return new URL(entry.upstream).hostname;
}

/** `Bearer <clave>` en `authorization`, la clave tal cual en las demás cabeceras. */
function secretValueFor(header: string): string {
  const key = `rayito-local-${randomBytes(12).toString("hex")}`;
  return header === AUTHORIZATION_HEADER ? `Bearer ${key}` : key;
}

function specOf(name: string, entry: PresetEntry): AgentSpec {
  const models = entry.args.models as string[] | undefined;
  const provider = entry.model_provider;
  return new AgentSpec({
    model: new AgentModel({
      provider,
      id: models?.[0] ?? GENERIC_MODEL_ID,
      gateway: routeOf(name),
      basePath: provider === "openai-compatible" ? entry.base_path : "",
    }),
  });
}

function gatewayOf(entry: PresetEntry, secretName: string): SecretGateway {
  const build = (rayito as unknown as Record<string, Preset>)[entry.typescript];
  if (build === undefined) {
    throw new Error(`${entry.typescript} no se exporta`);
  }
  return build(secretName, camelArgs(entry.args));
}

const adminUrl = process.env[FAKE_UPSTREAM_ADMIN_VAR];

async function clearUpstream(): Promise<void> {
  await fetch(`${adminUrl}/requests`, { method: "DELETE" });
}

async function receivedBy(host: string): Promise<Received[]> {
  const entries = (await (await fetch(`${adminUrl}/requests`)).json()) as Received[];
  return entries.filter((entry) => entry.host.split(":")[0] === host);
}

describe.skipIf(!localEnabled() || !adminUrl)(
  "presets de proveedor contra el upstream falso",
  () => {
    let harness: LocalHarness;

    beforeAll(async () => {
      harness = await localHarness();
    });

    afterAll(async () => {
      if (harness !== undefined) {
        await releaseGuest(harness);
      }
    });

    describe.each(PRESETS)("%s", (name) => {
      const entry = entryOf(name);
      const header = entry.headers[0] ?? AUTHORIZATION_HEADER;
      const secretValue = secretValueFor(header);
      const allowed = new Set(entry.allow.map(([, path]) => path));
      let store: SecretStore;
      let secretName: string;
      let sandbox: Sandbox;

      async function sh(cmd: string): Promise<{ exitCode: number; stdout: string }> {
        try {
          return await sandbox.commands.run(cmd, { timeoutMs: COMMAND_TIMEOUT_MS });
        } catch (error) {
          if (error instanceof CommandExitError) {
            return error;
          }
          throw error;
        }
      }

      beforeAll(async () => {
        store = new SecretStore({ region: harness.settings.region });
        secretName = `local-providers-ts-${routeOf(name)}-${randomBytes(4).toString("hex")}`;
        await store.create(secretName, secretValue);
        sandbox = await createLocalSandbox(harness, {
          allowInternetAccess: false,
          gateways: { [routeOf(name)]: gatewayOf(entry, secretName) },
          secretCache: new SecretCache({ store }),
        });
        expect((await sh("command -v opencode")).exitCode, "make local-providers-up").toBe(0);
      }, SANDBOX_SETUP_TIMEOUT_MS);

      afterAll(async () => {
        if (sandbox !== undefined) {
          await killQuietly(sandbox);
        }
        if (store !== undefined) {
          await store.destroy(secretName);
        }
      });

      it("egress cerrado y 403 fuera de la allowlist", async () => {
        expect((await sandbox.getHealth()).egressEnforcement).toBe(EgressEnforcement.GUEST_ROUTES);
        const direct = await sh(
          `curl -sS -o /dev/null -m ${EGRESS_PROBE_TIMEOUT_SECONDS} https://${hostOf(entry)}/`,
        );
        expect(direct.exitCode).not.toBe(0);
        const url = sandbox.gateways.get(routeOf(name))?.url ?? "";
        await clearUpstream();
        const refused = await sh(
          `curl -sS -o /dev/null -w '%{http_code}' -X POST -d '{}' ${url}${FORBIDDEN_PATH}`,
        );
        expect(refused.exitCode).toBe(0);
        expect(Number(refused.stdout)).toBe(FORBIDDEN_STATUS);
        expect(await receivedBy(hostOf(entry))).toEqual([]);
      });

      it.each(RUNTIMES)(
        "%s: una vuelta con la credencial real y sin el marcador",
        async (runtime) => {
          const support = CATALOGUE.model_providers[entry.model_provider]?.deepagents;
          if (runtime === "deepagents" && support !== "supported") {
            return;
          }
          await clearUpstream();
          const result = await sandbox.agent.run(PROMPT, {
            spec: specOf(name, entry),
            runtime,
            limits: new AgentLimits({ timeoutMs: AGENT_TIMEOUT_MS }),
          });
          const received = await receivedBy(hostOf(entry));
          expect(received.length).toBeGreaterThan(0);
          expect(result.text).toContain(FAKE_REPLY);
          for (const request of received) {
            expect(allowed.has(new URL(request.path, "https://upstream").pathname)).toBe(true);
            const injectedIsReal = request.headers[header] === secretValue;
            expect(injectedIsReal, "la cabecera del preset no llevó el valor del secreto").toBe(
              true,
            );
            const placeholderLeft =
              JSON.stringify(request.headers).includes(MODEL_CREDENTIAL_PLACEHOLDER) ||
              request.body.includes(MODEL_CREDENTIAL_PLACEHOLDER);
            expect(placeholderLeft).toBe(false);
          }
        },
        AGENT_TIMEOUT_MS,
      );
    });
  },
);
