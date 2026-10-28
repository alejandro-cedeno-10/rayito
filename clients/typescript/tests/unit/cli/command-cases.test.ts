/**
 * Cada comando de la CLI de TypeScript frente a lo que imprime y devuelve la
 * de Python (`testdata/cli/command-cases.json`, ADR-027): un mundo en JSON,
 * los argumentos y, esperado, el código de salida, stdout exacto y los
 * efectos (sandboxes terminados, llamadas al provisioner). Python ejecuta los
 * mismos casos en `tests/unit/cli/test_cli_command_cases.py`.
 */

import { describe, expect, it } from "vitest";
import { buildWorld, type CaseWorld } from "./case-world.js";
import { loadCliTestdata } from "./paths.js";
import { runWithServices } from "./run-case.js";

interface CommandCase {
  readonly name: string;
  readonly args: string[];
  readonly stdin?: string;
  readonly now: string;
  readonly world?: CaseWorld;
  readonly stderrIncludes?: string[];
  readonly stderrExcludes?: string[];
  readonly expected: {
    readonly exitCode: number;
    readonly stdout: string;
    readonly effects: {
      readonly terminated: string[];
      readonly stackCalls: string[];
      readonly stackParameters: Record<string, string>;
    };
  };
}

function parsedJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

const UPPERCASE_LETTER = /[A-Z]/g;

/** Las llamadas al provisioner se llaman `put_artifact` en Python y `putArtifact` en TypeScript. */
function snakeCase(name: string): string {
  return name.replace(UPPERCASE_LETTER, (letter) => `_${letter.toLowerCase()}`);
}

const { cases } = loadCliTestdata<{ cases: CommandCase[] }>("command-cases.json");

describe("comandos de la CLI frente a los casos de Python", () => {
  it.each(cases)("$name", async (testCase) => {
    const world = buildWorld(testCase.world ?? {});
    const outcome = await runWithServices({
      args: testCase.args,
      services: world.services,
      stdin: testCase.stdin,
      now: new Date(testCase.now),
    });
    expect(outcome.exitCode).toBe(testCase.expected.exitCode);
    const expectedDocument = parsedJson(testCase.expected.stdout);
    if (expectedDocument === undefined) {
      expect(outcome.stdout).toBe(testCase.expected.stdout);
    } else {
      expect(parsedJson(outcome.stdout)).toEqual(expectedDocument);
    }
    for (const needle of testCase.stderrIncludes ?? []) {
      expect(outcome.stderr + outcome.stdout).toContain(needle);
    }
    for (const needle of testCase.stderrExcludes ?? []) {
      expect(outcome.stderr + outcome.stdout).not.toContain(needle);
    }
    expect({
      terminated: world.plane.terminated,
      stackCalls: world.provisioner.calls.map((call) => snakeCase(String(call[0]))),
      stackParameters: world.provisioner.sentParameters,
    }).toEqual(testCase.expected.effects);
  });
});
