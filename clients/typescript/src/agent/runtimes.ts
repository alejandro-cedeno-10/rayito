/**
 * Registro `nombre -> AgentRuntime` (`ai-agent-core`, design.md §3 y
 * ADR-028): `"opencode"`, `"deepagents"` y `"langgraph"` (el mismo adaptador
 * de deepagents, con `entrypoint` obligatorio), `"strands"`,
 * `"strands-harness"` y `"hermes"`; `"custom"` no es un nombre del registro
 * porque `CustomRuntime` necesita configuración. Un nombre sin
 * adaptador falla con `UnimplementedError` en vez de con un error opaco. Un
 * llamante siempre puede saltarse el registro pasando su propio objeto
 * `AgentRuntime` (lo que hacen los tests con un doble).
 *
 * `runtimeFromOptions` y `runtimeToOptions` traducen un runtime a y desde la
 * tabla `[runtime]` de `rayito-agent.toml` (`project.ts`).
 */

import { InvalidArgumentError, UnimplementedError } from "../errors.js";
import { CustomRuntime } from "./custom.js";
import { DeepAgents } from "./deepagents.js";
import { HermesRuntime } from "./hermes.js";
import { OpenCodeRuntime } from "./opencode.js";
import type { AgentRuntime } from "./runtime.js";
import { STRANDS_HARNESS_NAME, STRANDS_NAME, StrandsRuntime } from "./strands.js";

/** Quita y pon: cada adaptador añade su entrada aquí cuando aterriza, sin
 * tocar el resto de este módulo. */
export const AGENT_RUNTIMES: Record<string, AgentRuntime> = {
  opencode: new OpenCodeRuntime(),
  deepagents: new DeepAgents(),
  langgraph: new DeepAgents({ requireEntrypoint: true }),
  [STRANDS_NAME]: new StrandsRuntime(),
  [STRANDS_HARNESS_NAME]: new StrandsRuntime({ harness: true }),
  hermes: new HermesRuntime(),
};

/** Valores de `[runtime] kind`, en el orden de la documentación. */
export const RUNTIME_KINDS = [
  "opencode",
  "deepagents",
  "langgraph",
  "strands",
  "strands-harness",
  "hermes",
  "custom",
] as const;
/** El nombre que pide un objeto de configuración en vez de un nombre. */
export const CUSTOM_RUNTIME_KIND = "custom";
/** Las claves de `[runtime]` (además de `kind`) que admite cada `kind`. */
export const RUNTIME_OPTION_KEYS: Readonly<Record<string, readonly string[]>> = Object.freeze({
  opencode: [],
  deepagents: [],
  langgraph: ["entrypoint"],
  strands: ["entrypoint", "language", "requirements"],
  "strands-harness": ["entrypoint", "language", "requirements"],
  hermes: ["toolsets", "skills_dir"],
  custom: ["install", "requirements", "command", "entrypoint", "app", "protocol", "python"],
});

function looksLikeRuntime(candidate: unknown): candidate is AgentRuntime {
  if (typeof candidate !== "object" || candidate === null) {
    return false;
  }
  const required = ["buildConfig", "command", "newState", "parseLine", "finish", "warmupSteps"];
  return required.every((key) => typeof (candidate as Record<string, unknown>)[key] === "function");
}

/** `runtime` ya es un `AgentRuntime` (duck typing con los métodos del
 * puerto) o un nombre de `AGENT_RUNTIMES`. Un nombre desconocido es
 * `UnimplementedError`; `"custom"` y cualquier otro valor son
 * `InvalidArgumentError`. */
export function resolveRuntime(runtime: string | AgentRuntime): AgentRuntime {
  if (typeof runtime === "string") {
    if (runtime === CUSTOM_RUNTIME_KIND) {
      throw new InvalidArgumentError(
        "runtime: 'custom' no es un nombre del registro: pasa un objeto CustomRuntime(...)",
      );
    }
    const found = Object.hasOwn(AGENT_RUNTIMES, runtime) ? AGENT_RUNTIMES[runtime] : undefined;
    if (found === undefined) {
      throw new UnimplementedError(
        `runtime: "${runtime}"`,
        "no hay ningún adaptador registrado con ese nombre",
        "https://rayito.dev/referencia/errores",
      );
    }
    return found;
  }
  if (!looksLikeRuntime(runtime)) {
    throw new InvalidArgumentError(
      "runtime debe ser un nombre registrado en AGENT_RUNTIMES o un objeto que implemente AgentRuntime",
    );
  }
  return runtime;
}

type RuntimeBuilder = (options: Readonly<Record<string, unknown>>) => AgentRuntime;

function langgraph(options: Readonly<Record<string, unknown>>): AgentRuntime {
  const entrypoint = options.entrypoint;
  if (entrypoint === undefined) {
    throw new InvalidArgumentError("entrypoint es obligatorio con kind='langgraph'");
  }
  if (typeof entrypoint !== "string") {
    throw new InvalidArgumentError("entrypoint debe ser una cadena");
  }
  return new DeepAgents({ entrypoint, requireEntrypoint: true });
}

/** `kind` -> constructor desde la tabla `[runtime]`. Un `kind` sin entrada se
 * resuelve por el registro (y falla con `UnimplementedError` si aún no tiene
 * adaptador). Cada adaptador nuevo añade la suya. */
const FROM_OPTIONS: Readonly<Record<string, RuntimeBuilder>> = Object.freeze({
  langgraph,
  [STRANDS_NAME]: (options) => StrandsRuntime.fromOptions(STRANDS_NAME, options),
  [STRANDS_HARNESS_NAME]: (options) => StrandsRuntime.fromOptions(STRANDS_HARNESS_NAME, options),
  hermes: (options) => HermesRuntime.fromOptions(options),
  [CUSTOM_RUNTIME_KIND]: (options) => CustomRuntime.fromOptions(options),
});

/** El runtime de `[runtime] kind = ...` con sus `options` (ya validadas
 * contra `RUNTIME_OPTION_KEYS`). */
export function runtimeFromOptions(
  kind: string,
  options: Readonly<Record<string, unknown>>,
): AgentRuntime {
  const builder = Object.hasOwn(FROM_OPTIONS, kind) ? FROM_OPTIONS[kind] : undefined;
  return builder === undefined ? resolveRuntime(kind) : builder(options);
}

/** La tabla `[runtime]` canónica de `runtime`: `{ kind }` más sus opciones.
 * Un objeto que Rayito no conoce no se puede escribir en un fichero y falla
 * con `InvalidArgumentError`. */
export function runtimeToOptions(runtime: string | AgentRuntime): Record<string, unknown> {
  if (typeof runtime === "string") {
    return { kind: runtime };
  }
  if (runtime instanceof OpenCodeRuntime) {
    return { kind: "opencode" };
  }
  if (runtime instanceof DeepAgents) {
    return runtime.entrypoint === undefined
      ? { kind: "deepagents" }
      : { kind: "langgraph", entrypoint: runtime.entrypoint };
  }
  if (
    runtime instanceof StrandsRuntime ||
    runtime instanceof HermesRuntime ||
    runtime instanceof CustomRuntime
  ) {
    return runtime.toOptions();
  }
  throw new InvalidArgumentError(
    "este runtime no tiene forma de fichero: sólo los de Rayito se escriben en rayito-agent.toml",
  );
}
