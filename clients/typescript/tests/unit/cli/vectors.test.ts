/**
 * Los vectores de formato y de entrada que comparte con la CLI de Python
 * (`testdata/cli/format-vectors.json` e `input-vectors.json`, ADR-027): cada
 * función de la CLI de TypeScript produce lo que Python produce.
 */

import { describe, expect, it } from "vitest";
import { CliConsole, visibleControls } from "../../../src/cli/console.js";
import {
  age,
  eventTimestamp,
  isoUtc,
  pythonDatetimeText,
  pythonFloatText,
  pythonGeneralText,
} from "../../../src/cli/format.js";
import { shellJoin } from "../../../src/cli/shell.js";
import { renderTable } from "../../../src/cli/table-layout.js";
import { InvalidPairError, parsePairs } from "../../../src/cli/tokens.js";
import { parseSince } from "../../../src/sandbox-logs/domain.js";
import { pythonStringRepr } from "../../../src/strings.js";
import { loadCliTestdata } from "./paths.js";

interface FormatVectors {
  age: Array<{ startedAt: string; now: string; expected: string }>;
  isoUtc: Array<{ instant: string; expected: string }>;
  pythonDatetimeText: Array<{ instant: string; expected: string }>;
  eventTimestamp: Array<{ epochMs: number; expected: string }>;
  pythonStringRepr: Array<{ text: string; expected: string }>;
  pythonFloatText: Array<{ value: number; expected: string }>;
  generalText: Array<{ value: number; expected: string }>;
  visibleControls: Array<{ text: string; expected: string }>;
  jsonDocuments: Array<{ name: string; floatKeys: string[]; document: unknown; expected: string }>;
  table: Array<{ columns: string[]; rows: unknown[][]; width: number; expected: string }>;
}

interface InputVectors {
  shellJoin: Array<{ words: string[]; expected: string }>;
  pairs: Array<{ values: string[]; expected: Record<string, string> }>;
  pairErrors: Array<{ values: string[]; option: string; message: string }>;
  since: Array<{ text: string; now: string; expectedMs: number }>;
  sinceErrors: Array<{ text: string; now: string; message: string }>;
}

const format = loadCliTestdata<FormatVectors>("format-vectors.json");
const input = loadCliTestdata<InputVectors>("input-vectors.json");

describe("vectores de formato compartidos con Python", () => {
  it.each(format.age)("age $startedAt", (vector) => {
    expect(age(new Date(vector.startedAt), new Date(vector.now))).toBe(vector.expected);
  });

  it.each(format.isoUtc)("isoUtc $instant", (vector) => {
    expect(isoUtc(new Date(vector.instant))).toBe(vector.expected);
  });

  it("isoUtc sin instante es null", () => {
    expect(isoUtc(undefined)).toBeNull();
  });

  it.each(format.pythonDatetimeText)("str(datetime) $instant", (vector) => {
    expect(pythonDatetimeText(new Date(vector.instant))).toBe(vector.expected);
  });

  it.each(format.eventTimestamp)("eventTimestamp $epochMs", (vector) => {
    expect(eventTimestamp(vector.epochMs)).toBe(vector.expected);
  });

  it.each(format.pythonFloatText)("str(float) de $value", (vector) => {
    expect(pythonFloatText(vector.value)).toBe(vector.expected);
  });

  it.each(format.generalText)('format(value, "g") de $value', (vector) => {
    expect(pythonGeneralText(vector.value)).toBe(vector.expected);
  });

  it.each(format.jsonDocuments)("documento JSON: $name", (vector) => {
    const lines: string[] = [];
    const sink = { write: (text: string | Uint8Array) => lines.push(String(text)) };
    const console = new CliConsole({ stdout: sink, stderr: sink, readLine: async () => undefined });
    console.emitJson(vector.document, new Set(vector.floatKeys));
    expect(lines.join("").trimEnd()).toBe(vector.expected);
  });

  it.each(format.pythonStringRepr)("repr de $text", (vector) => {
    expect(pythonStringRepr(vector.text)).toBe(vector.expected);
  });

  it.each(format.visibleControls)("controles visibles de $text", (vector) => {
    expect(visibleControls(vector.text)).toBe(vector.expected);
  });

  it.each(format.table)("tabla de $columns a $width columnas", (vector) => {
    expect(renderTable(vector.columns, vector.rows, vector.width)).toBe(vector.expected);
  });
});

describe("vectores de entrada compartidos con Python", () => {
  it.each(input.shellJoin)("shlex.join de $words", (vector) => {
    expect(shellJoin(vector.words)).toBe(vector.expected);
  });

  it.each(input.pairs)("pares $values", (vector) => {
    expect(parsePairs(vector.values, "--env")).toEqual(vector.expected);
  });

  it.each(input.pairErrors)("par inválido $values", (vector) => {
    expect(() => parsePairs(vector.values, vector.option)).toThrow(InvalidPairError);
    expect(() => parsePairs(vector.values, vector.option)).toThrow(vector.message);
  });

  it.each(input.since)("--since $text", (vector) => {
    expect(parseSince(vector.text, new Date(vector.now))).toBe(vector.expectedMs);
  });

  it.each(input.sinceErrors)("--since inválido $text", (vector) => {
    expect(() => parseSince(vector.text, new Date(vector.now))).toThrow(vector.message);
  });
});
