/**
 * La superficie de la CLI de TypeScript frente al golden que genera la de
 * Python (`testdata/cli/surface.json`, ADR-027): cada comando y grupo que
 * TypeScript define coincide campo a campo con la entrada del golden (ruta,
 * opciones, tipos, valores por defecto, obligatoriedad). Lo que TypeScript
 * todavía no define lo informa `scripts/check_sdk_parity.py --cli`.
 */

import { describe, expect, it } from "vitest";
import type { ParameterSpec } from "../../../src/cli/command.js";
import { ROOT_PARAMETERS } from "../../../src/cli/globals.js";
import { COMMANDS, GROUPS } from "../../../src/cli/registry.js";
import { loadCliTestdata } from "./paths.js";

interface GoldenParameter {
  readonly name: string;
  readonly kind: string;
  readonly flags: string[];
  readonly type: string;
  readonly required: boolean;
  readonly multiple: boolean;
  readonly default: unknown;
  readonly is_flag?: boolean;
  readonly envvar?: string | null;
  readonly choices?: string[];
}

interface GoldenEntry {
  readonly path: string[];
  readonly group: boolean;
  readonly invoke_without_command: boolean;
  readonly hidden: boolean;
  readonly parameters: GoldenParameter[];
}

const golden = loadCliTestdata<{ commands: GoldenEntry[] }>("surface.json").commands;

function describeParameter(parameter: ParameterSpec): GoldenParameter {
  return {
    name: parameter.name,
    kind: parameter.kind,
    flags: [...parameter.flags],
    type: parameter.type,
    required: parameter.required,
    multiple: parameter.multiple,
    default: parameter.default,
    ...(parameter.kind === "option"
      ? { is_flag: parameter.isFlag === true, envvar: parameter.envvar ?? null }
      : {}),
    ...(parameter.choices === undefined ? {} : { choices: [...parameter.choices] }),
  };
}

function goldenFor(path: readonly string[]): GoldenEntry | undefined {
  return golden.find((entry) => entry.path.join(" ") === path.join(" "));
}

function ours(): GoldenEntry[] {
  const group = (
    path: readonly string[],
    parameters: readonly ParameterSpec[],
    invokeWithoutCommand: boolean,
  ): GoldenEntry => ({
    path: [...path],
    group: true,
    invoke_without_command: invokeWithoutCommand,
    hidden: false,
    parameters: parameters.map(describeParameter),
  });
  return [
    group([], ROOT_PARAMETERS, goldenFor([])?.invoke_without_command ?? false),
    ...GROUPS.map((entry) =>
      group(entry.path, entry.root?.parameters ?? [], entry.root !== undefined),
    ),
    ...COMMANDS.map(
      (command): GoldenEntry => ({
        path: [...command.path],
        group: false,
        invoke_without_command: false,
        hidden: false,
        parameters: command.parameters.map(describeParameter),
      }),
    ),
  ];
}

describe("superficie de la CLI frente al golden de Python", () => {
  for (const entry of ours()) {
    const label = entry.path.length === 0 ? "(raíz)" : entry.path.join(" ");
    it(`${label} coincide con surface.json`, () => {
      expect(goldenFor(entry.path)).toBeDefined();
      expect(entry).toEqual(goldenFor(entry.path));
    });
  }

  it("no define dos veces la misma ruta", () => {
    const paths = ours().map((entry) => entry.path.join(" "));
    expect(new Set(paths).size).toBe(paths.length);
  });

  it("cada grupo definido tiene al menos un comando", () => {
    for (const group of GROUPS) {
      const runsAlone = group.root !== undefined;
      expect(runsAlone || COMMANDS.some((command) => command.path[0] === group.path[0])).toBe(true);
    }
  });
});
