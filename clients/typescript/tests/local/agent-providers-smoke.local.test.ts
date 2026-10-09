/**
 * Prueba de humo opcional contra las APIs reales de cada proveedor de modelo
 * (`make local-providers-smoke`), espejo de
 * `clients/python/tests/local/test_local_agent_providers_smoke.py`: una
 * vuelta corta de OpenCode y de deepagents por el preset de pasarela, con el
 * egress del sandbox cerrado.
 *
 * Cada preset se salta si no defines su clave. Variables (en el host; el
 * Makefile las pasa al runner por nombre, nunca por valor):
 *
 * - `RAYITO_SMOKE_<PRESET>_SECRET`: la clave de API, sin `Bearer` (el test
 *   lo añade cuando la cabecera es `authorization`).
 * - `RAYITO_SMOKE_<PRESET>_MODEL`: el id del modelo (o del despliegue en
 *   Azure). Obligatorio si hay clave: elige el más barato de tu cuenta.
 * - `RAYITO_SMOKE_AZURE_OPENAI_RESOURCE`: el subdominio del recurso de Azure.
 * - `RAYITO_SMOKE_LITELLM_UPSTREAM`: `https://host[:puerto]` de tu proxy.
 *
 * `<PRESET>` es el nombre del catálogo en mayúsculas: `OPENAI`, `GEMINI`,
 * `AZURE_OPENAI`, `OPENROUTER`, `GROQ`, `MISTRAL`, `DEEPSEEK`, `XAI` y
 * `LITELLM`.
 *
 * Coste: cada ejecución lleva `AgentLimits({ maxTotalTokens:
 * SMOKE_TOKEN_BUDGET })` y el prompt no usa herramientas; con un modelo de
 * hasta 0,20 USD por millón de tokens de
 * entrada, los dos runtimes juntos cuestan menos de 0,01 USD por proveedor
 * (el doble si corres también la de Python). Corre sobre
 * `make local-agent-up` (sin el upstream falso, que secuestra los nombres de
 * los proveedores). La clave entra en el Secrets Manager de Floci y sólo
 * `rayd` la lee; nunca se imprime.
 */

import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { AgentLimits } from "../../src/index.js";
import { type LocalHarness, localEnabled, localHarness, releaseGuest } from "./helpers.js";
import {
  AUTHORIZATION_HEADER,
  deepagentsSupported,
  entryOf,
  FAKE_UPSTREAM_ADMIN_VAR,
  PRESETS,
  PROMPT,
  Provider,
  ProviderBox,
  RUNTIMES,
  secretValueFor,
} from "./providers.js";

const SMOKE_VAR_PREFIX = "RAYITO_SMOKE_";
/**
 * Tope de tokens por ejecución: el prompt de sistema de OpenCode con sus
 * herramientas ronda los 8 000 tokens (medido con el upstream falso, cuerpo
 * de ~29 KB) y el de deepagents los 3 000. Con un modelo de 0,20 USD por
 * millón de tokens de entrada, dos ejecuciones al tope cuestan
 * 2 x 20 000 x 0,20 / 1e6 = 0,008 USD.
 */
const SMOKE_TOKEN_BUDGET = 20_000;
const SMOKE_TIMEOUT_MS = 180_000;
const SANDBOX_SETUP_TIMEOUT_MS = 300_000;
/** Los argumentos del preset que salen del entorno en vez del catálogo. */
const ENV_ARGS: Readonly<Record<string, Readonly<Record<string, string>>>> = {
  azure_openai: { resource: "RESOURCE" },
  litellm: { upstream: "UPSTREAM" },
};

function smokeVar(name: string, suffix: string): string {
  return `${SMOKE_VAR_PREFIX}${name.toUpperCase()}_${suffix}`;
}

/** Si el preset tiene clave en el entorno (si no, se salta). */
function configured(name: string): boolean {
  return Boolean(process.env[smokeVar(name, "SECRET")]);
}

/**
 * El preset con la clave y el modelo del entorno; lanza si falta el modelo
 * o un argumento obligatorio del preset.
 */
function smokeProvider(name: string): Provider {
  const key = process.env[smokeVar(name, "SECRET")] ?? "";
  const model = process.env[smokeVar(name, "MODEL")];
  if (!model) {
    throw new Error(`${smokeVar(name, "MODEL")} es obligatoria con ${smokeVar(name, "SECRET")}`);
  }
  const entry = entryOf(name);
  const args: Record<string, unknown> = { ...entry.args };
  for (const [arg, suffix] of Object.entries(ENV_ARGS[name] ?? {})) {
    const value = process.env[smokeVar(name, suffix)];
    if (!value) {
      throw new Error(`${smokeVar(name, suffix)} es obligatoria con ${name}`);
    }
    args[arg] = value;
  }
  if ("models" in args) {
    args.models = [model];
  }
  return new Provider(
    name,
    secretValueFor(entry.headers[0] ?? AUTHORIZATION_HEADER, key),
    args,
    model,
  );
}

/** Sin ninguna clave no se toca el guest; con el upstream falso, tampoco. */
const smokeEnabled =
  localEnabled() && !process.env[FAKE_UPSTREAM_ADMIN_VAR] && PRESETS.some(configured);

describe.skipIf(!smokeEnabled)("presets de proveedor contra las APIs reales", () => {
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
    let box: ProviderBox;

    beforeAll(async () => {
      if (configured(name)) {
        box = await ProviderBox.open(harness, smokeProvider(name));
      }
    }, SANDBOX_SETUP_TIMEOUT_MS);

    afterAll(async () => {
      if (box !== undefined) {
        await box.close();
      }
    });

    for (const runtime of RUNTIMES) {
      const unsupported = runtime === "deepagents" && !deepagentsSupported(entry);
      it.skipIf(!configured(name) || unsupported)(
        `${runtime}: una vuelta contra la API real`,
        async () => {
          const result = await box.sandbox.agent.run(PROMPT, {
            spec: box.provider.spec(),
            runtime,
            limits: new AgentLimits({
              maxTotalTokens: SMOKE_TOKEN_BUDGET,
              timeoutMs: SMOKE_TIMEOUT_MS,
            }),
          });
          expect(result.text.trim()).not.toBe("");
          expect(result.usage.input).toBeGreaterThan(0);
          expect(result.usage.output).toBeGreaterThan(0);
        },
        SMOKE_TIMEOUT_MS,
      );
    }
  });
});
