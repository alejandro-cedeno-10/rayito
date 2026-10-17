/**
 * Del `argv` a una invocación: opciones globales, ruta del comando y valores
 * validados. Usa `node:util parseArgs` (sin dependencias) para las opciones
 * de cada comando; cualquier problema de uso es un `UsageError` (código 2).
 */

import { parseArgs } from "node:util";
import {
  type CommandDefinition,
  CommandValues,
  type GlobalOptions,
  type GroupDefinition,
  metavarOf,
  type ParameterSpec,
  type ParsedValue,
} from "./command.js";
import { UsageError } from "./failures.js";
import {
  HELP_FLAG,
  JSON_FLAG,
  PROFILE_FLAG,
  REGION_FLAG,
  VERBOSE_FLAG,
  VERSION_FLAG,
} from "./globals.js";

const LONG_PREFIX = "--";
const SHORT_PREFIX = "-";
const END_OF_OPTIONS = "--";
const DECIMAL_RADIX = 10;
const INTEGER_PATTERN = /^[+-]?\d+$/;
const WHITESPACE = /\s+/;
const QUOTED_FLAG_PATTERN = /'(-[^'\s]*)/;

/** Las rutas de comandos y grupos que la CLI conoce. */
export interface CommandTree {
  readonly groups: readonly GroupDefinition[];
  readonly commands: readonly CommandDefinition[];
}

export type Invocation =
  | { readonly kind: "version" }
  | {
      readonly kind: "help";
      readonly path: readonly string[];
      /** `true` si no se pasó nada más: sale con código de uso, como `no_args_is_help`. */
      readonly bare: boolean;
    }
  | {
      readonly kind: "run";
      readonly command: CommandDefinition;
      readonly values: CommandValues;
      readonly globals: GlobalOptions;
    };

interface GlobalScan {
  readonly globals: GlobalOptions;
  readonly version: boolean;
  readonly help: boolean;
  readonly rest: readonly string[];
}

function takeValue(flag: string, inline: string | undefined, rest: string[]): string {
  const value = inline ?? rest.shift();
  if (value === undefined) {
    throw new UsageError(`la opción ${flag} necesita un valor`);
  }
  return value;
}

function scanGlobals(argv: readonly string[]): GlobalScan {
  const rest = [...argv];
  let profile: string | undefined;
  let region: string | undefined;
  const state = { json: false, verbose: false, version: false, help: false };
  while (rest.length > 0 && (rest[0] ?? "").startsWith(SHORT_PREFIX)) {
    const token = rest.shift() ?? "";
    const [flag = "", inline] = token.includes("=") ? splitOnce(token) : [token, undefined];
    if (flag === JSON_FLAG) {
      state.json = true;
    } else if (flag === VERBOSE_FLAG) {
      state.verbose = true;
    } else if (flag === VERSION_FLAG) {
      state.version = true;
    } else if (flag === HELP_FLAG) {
      state.help = true;
    } else if (flag === PROFILE_FLAG || flag === REGION_FLAG) {
      const value = takeValue(flag, inline, rest);
      if (flag === PROFILE_FLAG) {
        profile = value;
      } else {
        region = value;
      }
    } else {
      throw new UsageError(`opción desconocida: ${flag}`);
    }
  }
  const globals: GlobalOptions = { profile, region, json: state.json, verbose: state.verbose };
  return { globals, version: state.version, help: state.help, rest };
}

function splitOnce(token: string): [string, string] {
  const index = token.indexOf("=");
  return [token.slice(0, index), token.slice(index + 1)];
}

function samePath(left: readonly string[], right: readonly string[]): boolean {
  return left.length === right.length && left.every((word, index) => word === right[index]);
}

function childNames(tree: CommandTree, parent: readonly string[]): string[] {
  const names = new Set<string>();
  for (const definition of [...tree.groups, ...tree.commands]) {
    const path = definition.path;
    if (path.length === parent.length + 1 && samePath(path.slice(0, parent.length), parent)) {
      names.add(path[parent.length] ?? "");
    }
  }
  return [...names].sort();
}

function toLong(parameter: ParameterSpec): string {
  const long = parameter.flags.find((candidate) => candidate.startsWith(LONG_PREFIX)) ?? "";
  return long.slice(LONG_PREFIX.length);
}

/** Las banderas largas sin el prefijo `--`: la primera enciende, las demás (`--no-x`) apagan. */
function longNames(parameter: ParameterSpec): string[] {
  return parameter.flags
    .filter((candidate) => candidate.startsWith(LONG_PREFIX))
    .map((candidate) => candidate.slice(LONG_PREFIX.length));
}

function toShort(parameter: ParameterSpec): string | undefined {
  const short = parameter.flags.find(
    (candidate) => candidate.startsWith(SHORT_PREFIX) && !candidate.startsWith(LONG_PREFIX),
  );
  return short?.slice(SHORT_PREFIX.length);
}

type RawOption = string | boolean | (string | boolean)[] | undefined;

function parserOptions(parameters: readonly ParameterSpec[]): Record<string, object> {
  const options: Record<string, object> = {
    help: { type: "boolean" },
  };
  for (const parameter of parameters.filter((candidate) => candidate.kind === "option")) {
    const short = toShort(parameter);
    for (const name of parameter.isFlag === true ? longNames(parameter) : [toLong(parameter)]) {
      options[name] = {
        type: parameter.isFlag === true ? "boolean" : "string",
        multiple: parameter.multiple,
        ...(short === undefined ? {} : { short }),
      };
    }
  }
  return options;
}

function describeFlag(parameter: ParameterSpec): string {
  return parameter.flags.join("/");
}

function coerce(parameter: ParameterSpec, raw: string): string | number {
  if (parameter.choices !== undefined && !parameter.choices.includes(raw)) {
    throw new UsageError(
      `${describeFlag(parameter)}: admitidas: ${parameter.choices.join(", ")}; recibido ${JSON.stringify(raw)}`,
    );
  }
  if (parameter.type === "int") {
    if (!INTEGER_PATTERN.test(raw)) {
      throw new UsageError(
        `${describeFlag(parameter)}: se esperaba un entero, recibido ${JSON.stringify(raw)}`,
      );
    }
    return Number.parseInt(raw, DECIMAL_RADIX);
  }
  if (parameter.type === "float") {
    const parsed = Number(raw);
    if (raw.trim() === "" || !Number.isFinite(parsed)) {
      throw new UsageError(
        `${describeFlag(parameter)}: se esperaba un número, recibido ${JSON.stringify(raw)}`,
      );
    }
    return parsed;
  }
  return raw;
}

function defaultValue(parameter: ParameterSpec): ParsedValue {
  const value = parameter.default;
  if (value === null) {
    return parameter.multiple || parameter.variadic ? [] : undefined;
  }
  return Array.isArray(value) ? [...value] : (value as ParsedValue);
}

/** Una bandera simple vale `true` si se pasó; con negación, la negación gana y sin ninguna vale su `default` (`undefined` si éste es nulo: ni sí ni no). */
function flagValue(
  parameter: ParameterSpec,
  parsed: Readonly<Record<string, RawOption>>,
): boolean | undefined {
  const [positive = "", ...negatives] = longNames(parameter);
  if (negatives.some((name) => parsed[name] === true)) {
    return false;
  }
  if (parsed[positive] === true) {
    return true;
  }
  if (negatives.length > 0 && parameter.default === null) {
    return undefined;
  }
  return negatives.length > 0 && parameter.default === true;
}

/** Una opción repetible lee su variable de entorno como entradas separadas por espacios (como Typer). */
function envEntries(parameter: ParameterSpec, text: string): string | string[] {
  return parameter.multiple ? text.split(WHITESPACE).filter((entry) => entry !== "") : text;
}

function optionValue(
  parameter: ParameterSpec,
  parsed: Readonly<Record<string, RawOption>>,
  env: Readonly<Record<string, string | undefined>>,
): ParsedValue {
  if (parameter.isFlag === true) {
    return flagValue(parameter, parsed);
  }
  const raw = parsed[toLong(parameter)];
  const fromEnv = parameter.envvar ? env[parameter.envvar] : undefined;
  const supplied = raw ?? (fromEnv === undefined ? undefined : envEntries(parameter, fromEnv));
  if (supplied === undefined) {
    return defaultValue(parameter);
  }
  const texts = (Array.isArray(supplied) ? supplied : [supplied]).map(String);
  if (parameter.multiple) {
    return texts.map((text) => String(coerce(parameter, text)));
  }
  return coerce(parameter, texts[texts.length - 1] ?? "");
}

function argumentValues(
  parameters: readonly ParameterSpec[],
  positionals: readonly string[],
): Record<string, ParsedValue> {
  const values: Record<string, ParsedValue> = {};
  const pending = [...positionals];
  for (const parameter of parameters.filter((candidate) => candidate.kind === "argument")) {
    if (parameter.variadic) {
      values[parameter.name] = pending.splice(0).map((text) => String(coerce(parameter, text)));
      if (parameter.required && (values[parameter.name] as string[]).length === 0) {
        throw new UsageError(`falta el argumento ${metavarOf(parameter)}`);
      }
      continue;
    }
    const next = pending.shift();
    if (next === undefined) {
      if (parameter.required) {
        throw new UsageError(`falta el argumento ${metavarOf(parameter)}`);
      }
      values[parameter.name] = defaultValue(parameter);
      continue;
    }
    values[parameter.name] = coerce(parameter, next);
  }
  if (pending.length > 0) {
    throw new UsageError(`argumentos sobrantes: ${pending.join(" ")}`);
  }
  return values;
}

function flagFromParseError(error: Error): string {
  return QUOTED_FLAG_PATTERN.exec(error.message)?.[1] ?? "";
}

function usageFromParseError(error: unknown): UsageError {
  if (!(error instanceof Error)) {
    return new UsageError(String(error));
  }
  const code = (error as { code?: string }).code;
  if (code === "ERR_PARSE_ARGS_UNKNOWN_OPTION") {
    return new UsageError(`opción desconocida: ${flagFromParseError(error)}`);
  }
  if (code === "ERR_PARSE_ARGS_INVALID_OPTION_VALUE") {
    return new UsageError(`la opción ${flagFromParseError(error)} necesita un valor`);
  }
  return new UsageError(error.message);
}

interface CommandParse {
  readonly help: boolean;
  readonly values: CommandValues;
}

/** Valida los argumentos de `command` (lo que sigue a su ruta). */
export function parseCommandArguments(
  command: CommandDefinition,
  args: readonly string[],
  env: Readonly<Record<string, string | undefined>>,
): CommandParse {
  let parsed: { values: Record<string, RawOption>; positionals: string[] };
  try {
    parsed = parseArgs({
      args: [...args],
      options: parserOptions(command.parameters) as never,
      allowPositionals: true,
      strict: true,
    }) as never;
  } catch (error) {
    throw usageFromParseError(error);
  }
  if (parsed.values.help === true) {
    return { help: true, values: new CommandValues({}) };
  }
  const values: Record<string, ParsedValue> = argumentValues(
    command.parameters,
    parsed.positionals,
  );
  for (const parameter of command.parameters.filter((candidate) => candidate.kind === "option")) {
    values[parameter.name] = optionValue(parameter, parsed.values, env);
    if (parameter.required && values[parameter.name] === undefined) {
      throw new UsageError(`falta la opción ${describeFlag(parameter)}`);
    }
  }
  return { help: false, values: new CommandValues(values) };
}

/** Resuelve `argv` contra el árbol: versión, ayuda de un nivel o un comando listo para correr. */
export function parseInvocation(
  argv: readonly string[],
  tree: CommandTree,
  env: Readonly<Record<string, string | undefined>>,
): Invocation {
  const scan = scanGlobals(argv);
  if (scan.version) {
    return { kind: "version" };
  }
  if (scan.help) {
    return { kind: "help", path: [], bare: false };
  }
  if (argv.length === 0) {
    return { kind: "help", path: [], bare: true };
  }
  const path: string[] = [];
  let remaining = [...scan.rest];
  for (;;) {
    const command = tree.commands.find((candidate) => samePath(candidate.path, path));
    if (command !== undefined) {
      return runInvocation(command, remaining, scan.globals, env);
    }
    const word = remaining[0];
    const root = tree.groups.find((candidate) => samePath(candidate.path, path))?.root;
    if (root !== undefined && (word === undefined || !childNames(tree, path).includes(word))) {
      return runInvocation(root, remaining, scan.globals, env);
    }
    if (word === undefined || word === HELP_FLAG) {
      return { kind: "help", path, bare: word === undefined };
    }
    if (word === END_OF_OPTIONS || word.startsWith(SHORT_PREFIX)) {
      throw new UsageError(`opción desconocida: ${word}`);
    }
    if (!childNames(tree, path).includes(word)) {
      const known = childNames(tree, path).join(", ");
      throw new UsageError(
        `comando desconocido: ${[...path, word].join(" ")} (disponibles: ${known})`,
      );
    }
    path.push(word);
    remaining = remaining.slice(1);
  }
}

function runInvocation(
  command: CommandDefinition,
  args: readonly string[],
  globals: GlobalOptions,
  env: Readonly<Record<string, string | undefined>>,
): Invocation {
  const parsed = parseCommandArguments(command, args, env);
  if (parsed.help) {
    return { kind: "help", path: command.path, bare: false };
  }
  return { kind: "run", command, values: parsed.values, globals };
}
