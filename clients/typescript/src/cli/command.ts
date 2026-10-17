/**
 * Árbol declarativo de la CLI (ADR-027, D2). Cada comando es un
 * `defineCommand({ path, help, parameters, run })`; la CLI es sólo una
 * cáscara sobre servicios públicos del SDK. Los parámetros se describen con
 * la misma forma que `testdata/cli/surface.json` (el golden que genera la CLI
 * de Python), así un test compara los dos árboles campo a campo.
 */

import type { CliConsole } from "./console.js";
import type { AgentProjectTools } from "./ops-ports.js";
import type { CliServices } from "./services.js";

export type ParameterType = "str" | "int" | "float" | "boolean" | "path";
export type ParameterDefault = string | number | boolean | readonly string[] | null;

/** Un parámetro: opción (`--flag`) o argumento posicional. */
export interface ParameterSpec {
  /** Nombre en snake_case; el de la clave del golden. */
  readonly name: string;
  readonly kind: "option" | "argument";
  readonly flags: readonly string[];
  readonly type: ParameterType;
  readonly required: boolean;
  readonly multiple: boolean;
  readonly default: ParameterDefault;
  /** Sólo las opciones: `true` si no lleva valor. */
  readonly isFlag?: boolean;
  /** Sólo las opciones: la variable de entorno que la rellena. */
  readonly envvar?: string | null;
  readonly choices?: readonly string[];
  /** Sólo los argumentos: consume todos los posicionales que queden. */
  readonly variadic: boolean;
  readonly help: string;
  readonly metavar?: string;
}

interface ParameterSettings {
  readonly help: string;
  readonly name?: string;
  readonly type?: ParameterType;
  readonly default?: ParameterDefault;
  readonly multiple?: boolean;
  readonly required?: boolean;
  readonly envvar?: string;
  readonly choices?: readonly string[];
  readonly metavar?: string;
}

const LONG_FLAG_PREFIX = "--";

function nameFromFlags(flags: readonly string[]): string {
  const long = flags.find((flag) => flag.startsWith(LONG_FLAG_PREFIX)) ?? "";
  return long.slice(LONG_FLAG_PREFIX.length).replaceAll("-", "_");
}

/** Una opción con valor (`--template NAME`). */
export function option(flags: readonly string[], settings: ParameterSettings): ParameterSpec {
  return {
    name: settings.name ?? nameFromFlags(flags),
    kind: "option",
    flags,
    type: settings.type ?? "str",
    required: settings.required ?? false,
    multiple: settings.multiple ?? false,
    default: settings.default ?? null,
    isFlag: false,
    envvar: settings.envvar ?? null,
    ...(settings.choices === undefined ? {} : { choices: settings.choices }),
    variadic: false,
    help: settings.help,
    ...(settings.metavar === undefined ? {} : { metavar: settings.metavar }),
  };
}

/** Una opción booleana sin valor (`--yes`), apagada por defecto. */
export function flag(
  flags: readonly string[],
  settings: Pick<ParameterSettings, "help" | "name">,
): ParameterSpec {
  return {
    name: settings.name ?? nameFromFlags(flags),
    kind: "option",
    flags,
    type: "boolean",
    required: false,
    multiple: false,
    default: false,
    isFlag: true,
    envvar: null,
    variadic: false,
    help: settings.help,
  };
}

/**
 * Una opción booleana con su negación (`--prefetch/--no-prefetch`): la primera
 * bandera larga la enciende y la segunda la apaga; sin ninguna vale `default`
 * (sin `default`, `optionalFlag` da `undefined`).
 */
export function toggle(
  flags: readonly [string, string],
  settings: { readonly help: string; readonly default?: boolean | undefined },
): ParameterSpec {
  return {
    name: nameFromFlags(flags),
    kind: "option",
    flags,
    type: "boolean",
    required: false,
    multiple: false,
    default: settings.default ?? null,
    isFlag: true,
    envvar: null,
    variadic: false,
    help: settings.help,
  };
}

/** Un argumento posicional; `variadic` consume el resto. */
export function argument(
  name: string,
  settings: ParameterSettings & { readonly variadic?: boolean },
): ParameterSpec {
  return {
    name: settings.name ?? name,
    kind: "argument",
    flags: [name],
    type: settings.type ?? "str",
    required: settings.required ?? false,
    multiple: settings.multiple ?? false,
    default: settings.default ?? null,
    variadic: settings.variadic ?? false,
    help: settings.help,
    ...(settings.metavar === undefined ? {} : { metavar: settings.metavar }),
  };
}

/** Cómo se llama un argumento en la ayuda y en los errores de uso (`ID`, `CMD`). */
export function metavarOf(parameter: ParameterSpec): string {
  if (parameter.metavar !== undefined) {
    return parameter.metavar;
  }
  return parameter.kind === "argument" ? (parameter.flags[0] ?? "").toUpperCase() : "TEXTO";
}

export type ParsedValue = string | number | boolean | readonly string[] | undefined;

/** Los valores ya validados de una invocación, con lectores tipados por nombre. */
export class CommandValues {
  readonly #values: Readonly<Record<string, ParsedValue>>;

  constructor(values: Readonly<Record<string, ParsedValue>>) {
    this.#values = values;
  }

  string(name: string): string | undefined {
    const value = this.#values[name];
    return typeof value === "string" ? value : undefined;
  }

  strings(name: string): readonly string[] {
    const value = this.#values[name];
    return Array.isArray(value) ? (value as readonly string[]) : [];
  }

  number(name: string): number {
    const value = this.#values[name];
    if (typeof value !== "number") {
      throw new TypeError(`el parámetro ${name} no es numérico`);
    }
    return value;
  }

  /** Como `number`, pero una opción sin valor ni valor por defecto es `undefined`. */
  optionalNumber(name: string): number | undefined {
    return typeof this.#values[name] === "number" ? this.number(name) : undefined;
  }

  flag(name: string): boolean {
    return this.#values[name] === true;
  }

  /** `true`/`false` si se pasó la bandera o su negación; `undefined` si ninguna. */
  optionalFlag(name: string): boolean | undefined {
    const value = this.#values[name];
    return typeof value === "boolean" ? value : undefined;
  }
}

/** Opciones globales de la raíz, antes del primer subcomando. */
export interface GlobalOptions {
  readonly profile: string | undefined;
  readonly region: string | undefined;
  readonly json: boolean;
  readonly verbose: boolean;
}

/** Lo que recibe `run`: valores, salida y servicios perezosos de AWS. */
export interface CommandContext {
  readonly values: CommandValues;
  readonly globals: GlobalOptions;
  readonly console: CliConsole;
  readonly env: Readonly<Record<string, string | undefined>>;
  readonly now: () => Date;
  /** Duerme `ms` o hasta que se cancele la invocación (Ctrl-C). */
  readonly sleep: (ms: number) => Promise<void>;
  /** Se cancela con Ctrl-C: los comandos con bucle (`metrics --follow`) paran limpiamente. */
  readonly signal: AbortSignal;
  /** Construye la sesión de AWS la primera vez que un comando la pide. */
  readonly services: () => Promise<CliServices>;
  /** Abre una URL en el navegador del usuario (`rayito dashboard --open`). */
  readonly openUrl: (url: string) => Promise<void>;
  /**
   * `AgentProject` y `AgentDeployment` para `rayito agent`. Cargar un proyecto
   * no pide la sesión de AWS (`init` y `export` no la necesitan); sólo
   * `build`, `run`, `deploy` y `connect` la construyen.
   */
  readonly agentProjects: AgentProjectTools;
}

export interface CommandDefinition {
  readonly path: readonly string[];
  readonly help: string;
  readonly parameters: readonly ParameterSpec[];
  /** Devuelve el código de salida; `undefined` es éxito. */
  readonly run: (context: CommandContext) => Promise<number | undefined>;
}

export interface GroupDefinition {
  readonly path: readonly string[];
  readonly help: string;
  /**
   * El comando que corre el propio grupo sin subcomando (`invoke_without_command`
   * de Typer, p. ej. `rayito dashboard --port 8080`); sus opciones son las del grupo.
   */
  readonly root?: CommandDefinition;
}

/** Los parámetros no repiten nombre ni bandera: un error de programación, no de uso. */
export function defineCommand(definition: CommandDefinition): CommandDefinition {
  const names = new Set<string>();
  const flags = new Set<string>();
  for (const parameter of definition.parameters) {
    if (names.has(parameter.name)) {
      throw new Error(`${definition.path.join(" ")}: parámetro repetido ${parameter.name}`);
    }
    names.add(parameter.name);
    for (const candidate of parameter.kind === "option" ? parameter.flags : []) {
      if (flags.has(candidate)) {
        throw new Error(`${definition.path.join(" ")}: bandera repetida ${candidate}`);
      }
      flags.add(candidate);
    }
  }
  return definition;
}

export function defineGroup(definition: GroupDefinition): GroupDefinition {
  return definition;
}
