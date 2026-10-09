/**
 * Lo que comparten los tests `local` de los proveedores de modelo (contra el
 * upstream falso y contra las APIs reales), espejo de
 * `clients/python/tests/local/providers.py`: los presets de
 * `testdata/agent/provider-catalogue.json`, el `AgentSpec` de cada uno y un
 * sandbox con su pasarela y el egress cerrado.
 */

import { randomBytes } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { expect } from "vitest";
import * as rayito from "../../src/index.js";
import {
  AgentModel,
  AgentSpec,
  CommandExitError,
  type ModelProvider,
  type Sandbox,
  SecretCache,
  type SecretGateway,
  SecretStore,
} from "../../src/index.js";
import { createLocalSandbox, killQuietly, type LocalHarness } from "./helpers.js";

/** Un preset por proveedor (`litellm_root` ya lo cubren los tests unitarios). */
export const PRESETS = [
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
export const RUNTIMES = ["opencode", "deepagents"] as const;
/** El id de modelo cuando el preset no limita el modelo (va en el cuerpo). */
export const GENERIC_MODEL_ID = "rayito-local-model";
export const AUTHORIZATION_HEADER = "authorization";
export const BEARER_PREFIX = "Bearer ";
export const COMMAND_TIMEOUT_MS = 60_000;
/** Lo que se le pide al agente: una respuesta corta y sin herramientas. */
export const PROMPT = "Responde sólo con la palabra 'hola', sin usar herramientas.";
/** La variable que define `dev/local/providers/compose.yaml` con el upstream falso. */
export const FAKE_UPSTREAM_ADMIN_VAR = "RAYITO_LOCAL_FAKE_UPSTREAM_ADMIN";

export interface PresetEntry {
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

type Preset = (secret: string, options?: Record<string, unknown>) => SecretGateway;

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

/** Los argumentos del catálogo (`snake_case`) como opciones de TypeScript. */
function camelArgs(args: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(args).map(([key, value]) => [
      key.replace(/_([a-z])/g, (_, letter: string) => letter.toUpperCase()),
      value,
    ]),
  );
}

export function entryOf(name: string): PresetEntry {
  const entry = CATALOGUE.presets.find((preset) => preset.name === name);
  if (entry === undefined) {
    throw new Error(`${name} no está en el catálogo`);
  }
  return entry;
}

export function deepagentsSupported(entry: PresetEntry): boolean {
  return CATALOGUE.model_providers[entry.model_provider]?.deepagents === "supported";
}

/**
 * Lo que guarda el secreto: `Bearer <clave>` en `authorization`, la clave
 * tal cual en las demás cabeceras (`x-goog-api-key`, `api-key`).
 */
export function secretValueFor(header: string, key: string): string {
  return header === AUTHORIZATION_HEADER ? `${BEARER_PREFIX}${key}` : key;
}

export function randomKey(): string {
  return `rayito-local-${randomBytes(12).toString("hex")}`;
}

/**
 * Un preset del catálogo; `args` y `modelOverride` sustituyen a los del
 * catálogo (el recurso de Azure, el upstream de LiteLLM, el modelo).
 */
export class Provider {
  readonly entry: PresetEntry;

  constructor(
    readonly name: string,
    readonly secretValue: string,
    readonly args?: Record<string, unknown>,
    readonly modelOverride?: string,
  ) {
    this.entry = entryOf(name);
  }

  /** El nombre de la ruta de la pasarela (`[a-z0-9-]`). */
  get route(): string {
    return this.name.replaceAll("_", "-");
  }

  get host(): string {
    return new URL(this.entry.upstream).hostname;
  }

  get header(): string {
    return this.entry.headers[0] ?? AUTHORIZATION_HEADER;
  }

  get allowedPaths(): Set<string> {
    return new Set(this.entry.allow.map(([, path]) => path));
  }

  get modelId(): string {
    const models = this.entry.args.models as string[] | undefined;
    return this.modelOverride ?? models?.[0] ?? GENERIC_MODEL_ID;
  }

  gateway(secretName: string): SecretGateway {
    const build = (rayito as unknown as Record<string, Preset>)[this.entry.typescript];
    if (build === undefined) {
      throw new Error(`${this.entry.typescript} no se exporta`);
    }
    return build(secretName, camelArgs(this.args ?? this.entry.args));
  }

  spec(): AgentSpec {
    const provider = this.entry.model_provider;
    return new AgentSpec({
      model: new AgentModel({
        provider,
        id: this.modelId,
        gateway: this.route,
        basePath: provider === "openai-compatible" ? this.entry.base_path : "",
      }),
    });
  }
}

/** Un sandbox con la pasarela de un preset y el secreto que la alimenta. */
export class ProviderBox {
  private constructor(
    readonly sandbox: Sandbox,
    readonly provider: Provider,
    private readonly store: SecretStore,
    private readonly secretName: string,
  ) {}

  /**
   * Crea el secreto en el Secrets Manager de Floci y un sandbox con
   * `allowInternetAccess: false` y la pasarela del preset; falla si el guest
   * no trae los agentes. Si algo falla a medias, borra lo que ya creó.
   */
  static async open(harness: LocalHarness, provider: Provider): Promise<ProviderBox> {
    const store = new SecretStore({ region: harness.settings.region });
    const secretName = `local-providers-ts-${provider.route}-${randomBytes(4).toString("hex")}`;
    await store.create(secretName, provider.secretValue);
    let sandbox: Sandbox;
    try {
      sandbox = await createLocalSandbox(harness, {
        allowInternetAccess: false,
        gateways: { [provider.route]: provider.gateway(secretName) },
        secretCache: new SecretCache({ store }),
      });
    } catch (error) {
      await store.destroy(secretName);
      throw error;
    }
    const box = new ProviderBox(sandbox, provider, store, secretName);
    try {
      expect((await box.sh("command -v opencode")).exitCode, "make local-agent-up").toBe(0);
    } catch (error) {
      await box.close();
      throw error;
    }
    return box;
  }

  /** Una orden como `user`, sin lanzar por un exit distinto de cero. */
  async sh(cmd: string): Promise<{ exitCode: number; stdout: string }> {
    try {
      return await this.sandbox.commands.run(cmd, { timeoutMs: COMMAND_TIMEOUT_MS });
    } catch (error) {
      if (error instanceof CommandExitError) {
        return error;
      }
      throw error;
    }
  }

  /** Mata el sandbox y borra el secreto, aunque matar falle. */
  async close(): Promise<void> {
    try {
      await killQuietly(this.sandbox);
    } finally {
      await this.store.destroy(this.secretName);
    }
  }
}
