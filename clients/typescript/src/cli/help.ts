/**
 * La ayuda de cada nivel del árbol, generada de las mismas descripciones que
 * validan los argumentos: no hay un texto de ayuda aparte que se desalinee.
 */

import {
  type CommandDefinition,
  type GroupDefinition,
  metavarOf,
  type ParameterSpec,
} from "./command.js";
import { ROOT_HELP, ROOT_PARAMETERS } from "./globals.js";
import type { CommandTree } from "./parse.js";

const PROGRAM = "rayito";
const NAME_COLUMN_GAP = 2;

function pathLabel(path: readonly string[]): string {
  return [PROGRAM, ...path].join(" ");
}

function flagLabel(parameter: ParameterSpec): string {
  const flags = parameter.flags.join(", ");
  return parameter.isFlag === true ? flags : `${flags} ${metavarOf(parameter)}`;
}

function defaultNote(parameter: ParameterSpec): string {
  const value = parameter.default;
  const empty = value === null || value === false || (Array.isArray(value) && value.length === 0);
  return empty ? "" : ` (por defecto: ${String(value)})`;
}

function aligned(rows: ReadonlyArray<readonly [string, string]>): string[] {
  const width = Math.max(0, ...rows.map(([label]) => label.length)) + NAME_COLUMN_GAP;
  return rows.map(([label, text]) => `  ${label.padEnd(width)}${text}`.trimEnd());
}

function usageLine(command: CommandDefinition): string {
  const options = command.parameters.some((parameter) => parameter.kind === "option")
    ? " [OPCIONES]"
    : "";
  const argumentsText = command.parameters
    .filter((parameter) => parameter.kind === "argument")
    .map((parameter) => {
      const name = metavarOf(parameter);
      const wrapped = parameter.required ? name : `[${name}]`;
      return parameter.variadic ? `${wrapped}...` : wrapped;
    })
    .join(" ");
  return `Uso: ${pathLabel(command.path)}${options}${argumentsText === "" ? "" : ` ${argumentsText}`}`;
}

function parameterSection(title: string, parameters: readonly ParameterSpec[]): string[] {
  if (parameters.length === 0) {
    return [];
  }
  const rows = parameters.map((parameter): [string, string] => [
    parameter.kind === "argument" ? metavarOf(parameter) : flagLabel(parameter),
    `${parameter.help}${defaultNote(parameter)}`,
  ]);
  return ["", `${title}:`, ...aligned(rows)];
}

export function commandHelp(command: CommandDefinition): string {
  const arguments_ = command.parameters.filter((parameter) => parameter.kind === "argument");
  const options = command.parameters.filter((parameter) => parameter.kind === "option");
  return [
    usageLine(command),
    "",
    command.help,
    ...parameterSection("Argumentos", arguments_),
    ...parameterSection("Opciones", options),
  ].join("\n");
}

function childRows(path: readonly string[], tree: CommandTree): Array<[string, string]> {
  return [...tree.groups, ...tree.commands]
    .filter(
      (definition) =>
        definition.path.length === path.length + 1 &&
        path.every((word, index) => definition.path[index] === word),
    )
    .map((definition): [string, string] => [definition.path[path.length] ?? "", definition.help])
    .sort(([left], [right]) => left.localeCompare(right));
}

function groupHelpText(path: readonly string[], tree: CommandTree): string {
  const group: GroupDefinition | undefined = tree.groups.find(
    (candidate) => candidate.path.join(" ") === path.join(" "),
  );
  const help = path.length === 0 ? ROOT_HELP : (group?.help ?? "");
  const globalSection = path.length === 0 ? parameterSection("Opciones", ROOT_PARAMETERS) : [];
  return [
    `Uso: ${pathLabel(path)} ${path.length === 0 ? "[OPCIONES] " : ""}COMANDO [ARGUMENTOS]...`,
    "",
    help,
    ...globalSection,
    "",
    "Comandos:",
    ...aligned(childRows(path, tree)),
  ].join("\n");
}

/** Un grupo que también corre solo: la ayuda de su comando raíz y, si los hay, sus subcomandos. */
function rootGroupHelpText(root: CommandDefinition, tree: CommandTree): string {
  const children = childRows(root.path, tree);
  return [
    commandHelp(root),
    ...(children.length === 0 ? [] : ["", "Comandos:", ...aligned(children)]),
  ].join("\n");
}

/** La ayuda de `path`: un comando o un grupo (la raíz es la ruta vacía). */
export function helpFor(path: readonly string[], tree: CommandTree): string {
  const command = tree.commands.find((candidate) => candidate.path.join(" ") === path.join(" "));
  if (command !== undefined) {
    return commandHelp(command);
  }
  const root = tree.groups.find((candidate) => candidate.path.join(" ") === path.join(" "))?.root;
  return root === undefined ? groupHelpText(path, tree) : rootGroupHelpText(root, tree);
}
