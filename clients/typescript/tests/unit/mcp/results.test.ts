/**
 * Mapeo puro de los modelos del SDK a los resultados MCP: `truncateText`,
 * `executionToBlocks` sobre `Execution` sintéticas y los constructores de
 * las formas de salida. Espejo de `test_mcp_results.py`.
 */

import { describe, expect, it } from "vitest";
import {
  CommandExitError,
  type CommandResult,
  type EntryInfo,
  Execution,
  FileType,
  Result,
} from "../../../src/index.js";
import { MCP_MAX_OUTPUT_CHARS } from "../../../src/limits.js";
import {
  commandOutputFrom,
  directoryListingFrom,
  executionToBlocks,
  fileContentFrom,
  fileEntryFrom,
  sandboxListFrom,
  sandboxSummaryFrom,
  truncateText,
  writeReceiptFrom,
} from "../../../src/mcp/results.js";
import { sandboxListItem } from "../../../src/models.js";

const SANDBOX_ID = "microvm-00000000-0000-0000-0000-000000000001";
const PNG_BASE64 =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/x8AAwMB/6X4zvQAAAAASUVORK5CYII=";
const SVG_TEXT = '<svg xmlns="http://www.w3.org/2000/svg"/>';
const MODIFIED_AT = new Date(Date.UTC(2026, 8, 16, 12, 0, 0));
const STARTED_AT = new Date(Date.UTC(2026, 8, 16, 11, 30, 0));
const IMAGE_ARN = "arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base";

function summaryOf(execution: Execution): Record<string, unknown> {
  const first = executionToBlocks(execution, { sandboxId: SANDBOX_ID })[0];
  if (first?.type !== "text") {
    throw new Error("el primer bloque debe ser texto");
  }
  return JSON.parse(first.text) as Record<string, unknown>;
}

function textResult(text: string): Result {
  return new Result({ text, isMainResult: true, raw: { "text/plain": text } });
}

describe("truncateText", () => {
  it("corta por encima del límite y no en él", () => {
    expect(truncateText("abc", 5)).toEqual({ text: "abc", cut: false });
    expect(truncateText("abcde", 5)).toEqual({ text: "abcde", cut: false });
    expect(truncateText("abcdefgh", 5)).toEqual({
      text: "abcde\n… [truncado: 3 caracteres más]",
      cut: true,
    });
  });

  it("el límite por defecto es el compartido de limits.json", () => {
    const result = truncateText("x".repeat(MCP_MAX_OUTPUT_CHARS + 10));
    expect(result.cut).toBe(true);
    expect(result.text.endsWith("… [truncado: 10 caracteres más]")).toBe(true);
  });

  it("cuenta caracteres Unicode y no unidades UTF-16, como Python", () => {
    const emoji = "😀".repeat(5);
    expect(truncateText(emoji, 5)).toEqual({ text: emoji, cut: false });
    expect(truncateText(emoji, 3)).toEqual({
      text: "😀😀😀\n… [truncado: 2 caracteres más]",
      cut: true,
    });
  });
});

describe("executionToBlocks", () => {
  it("un resultado de texto es un solo bloque JSON", () => {
    const execution = new Execution({
      results: [textResult("42")],
      logs: { stdout: ["hola\n"], stderr: [] },
      executionCount: 3,
    });
    expect(executionToBlocks(execution, { sandboxId: SANDBOX_ID })).toHaveLength(1);
    expect(summaryOf(execution)).toEqual({
      text: "42",
      stdout: "hola\n",
      stderr: "",
      error: null,
      execution_count: 3,
      results: [{ index: 0, mime_types: ["text/plain"] }],
      truncated: false,
    });
  });

  it("un png con gráfico adjunta un solo bloque de imagen", () => {
    const result = new Result({
      png: PNG_BASE64,
      raw: { "image/png": PNG_BASE64, "e2b/chart": "{}" },
    });
    const execution = new Execution({ results: [result], executionCount: 1 });
    const blocks = executionToBlocks(execution, { sandboxId: SANDBOX_ID });
    expect(blocks.map((block) => block.type)).toEqual(["text", "image"]);
    expect(blocks[1]).toEqual({ type: "image", data: PNG_BASE64, mimeType: "image/png" });
    expect(summaryOf(execution).results).toEqual([
      { index: 0, mime_types: ["image/png", "e2b/chart"] },
    ]);
    expect(summaryOf(execution).text).toBeNull();
  });

  it("un jpeg adjunta un bloque jpeg", () => {
    const result = new Result({ jpeg: PNG_BASE64, raw: { "image/jpeg": PNG_BASE64 } });
    const blocks = executionToBlocks(new Execution({ results: [result] }), {
      sandboxId: SANDBOX_ID,
    });
    expect(blocks[1]).toEqual({ type: "image", data: PNG_BASE64, mimeType: "image/jpeg" });
  });

  it("un svg adjunta un recurso embebido con su uri", () => {
    const result = new Result({ svg: SVG_TEXT, raw: { "image/svg+xml": SVG_TEXT } });
    const execution = new Execution({ results: [textResult("1"), result], executionCount: 7 });
    const blocks = executionToBlocks(execution, { sandboxId: SANDBOX_ID });
    expect(blocks.map((block) => block.type)).toEqual(["text", "resource"]);
    expect(blocks[1]).toEqual({
      type: "resource",
      resource: {
        uri: `rayito://${SANDBOX_ID}/results/7/1.svg`,
        mimeType: "image/svg+xml",
        text: SVG_TEXT,
      },
    });
  });

  it("los demás mime types se listan pero no se adjuntan", () => {
    const result = new Result({ html: "<b>x</b>", raw: { "text/html": "<b>x</b>" } });
    const execution = new Execution({ results: [result], executionCount: 2 });
    expect(executionToBlocks(execution, { sandboxId: SANDBOX_ID })).toHaveLength(1);
    expect(summaryOf(execution).results).toEqual([{ index: 0, mime_types: ["text/html"] }]);
  });

  it("un error del kernel es dato y el texto es null", () => {
    const execution = new Execution({
      error: { name: "ValueError", value: "x", traceback: "Traceback…" },
      executionCount: 4,
    });
    const summary = summaryOf(execution);
    expect(summary.error).toEqual({ name: "ValueError", value: "x", traceback: "Traceback…" });
    expect(summary.text).toBeNull();
  });

  it("sin contador de ejecución la uri del svg usa unknown", () => {
    const result = new Result({ svg: SVG_TEXT, raw: { "image/svg+xml": SVG_TEXT } });
    const blocks = executionToBlocks(new Execution({ results: [result] }), {
      sandboxId: SANDBOX_ID,
    });
    expect(blocks[1]).toMatchObject({
      resource: { uri: `rayito://${SANDBOX_ID}/results/unknown/0.svg` },
    });
  });

  it("un stdout largo se trunca y lo marca", () => {
    const execution = new Execution({
      logs: { stdout: ["x".repeat(50_005), "y".repeat(50_005)], stderr: [] },
    });
    const summary = summaryOf(execution);
    expect((summary.stdout as string).endsWith("… [truncado: 10 caracteres más]")).toBe(true);
    expect(summary.truncated).toBe(true);
  });

  it("un texto largo se trunca y lo marca", () => {
    const execution = new Execution({
      results: [textResult("z".repeat(MCP_MAX_OUTPUT_CHARS + 1))],
    });
    const summary = summaryOf(execution);
    expect(summary.truncated).toBe(true);
    expect((summary.text as string).endsWith("… [truncado: 1 caracteres más]")).toBe(true);
  });
});

describe("salidas de las demás herramientas", () => {
  const entry = (type: FileType | undefined): EntryInfo => ({
    name: "a.txt",
    type,
    path: "/home/user/a.txt",
    size: 4,
    mode: 0o644,
    permissions: "-rw-r--r--",
    owner: "user",
    group: "user",
    modifiedTime: MODIFIED_AT,
    symlinkTarget: undefined,
    metadata: {},
  });
  const item = (sandboxId: string) =>
    sandboxListItem({
      sandboxId,
      state: "RUNNING",
      template: IMAGE_ARN,
      templateVersion: "16.0",
      startedAt: STARTED_AT,
    });

  it("commandOutputFrom da el resultado", () => {
    const result: CommandResult = { stdout: "hola\n", stderr: "", exitCode: 0, error: undefined };
    expect(commandOutputFrom(result)).toEqual({
      stdout: "hola\n",
      stderr: "",
      exit_code: 0,
      truncated: false,
    });
  });

  it("commandOutputFrom acepta el error de exit code distinto de cero", () => {
    const error = new CommandExitError("exit 3", { exitCode: 3, stdout: "out", stderr: "err" });
    expect(commandOutputFrom(error)).toEqual({
      stdout: "out",
      stderr: "err",
      exit_code: 3,
      truncated: false,
    });
  });

  it("commandOutputFrom trunca cada flujo por separado", () => {
    const result: CommandResult = {
      stdout: "a".repeat(12),
      stderr: "b",
      exitCode: 0,
      error: undefined,
    };
    const output = commandOutputFrom(result, 10);
    expect(output.truncated).toBe(true);
    expect(output.stdout.endsWith("… [truncado: 2 caracteres más]")).toBe(true);
    expect(output.stderr).toBe("b");
  });

  it("fileContentFrom conserva el tamaño completo", () => {
    expect(fileContentFrom("/home/user/a.txt", "x".repeat(12), 10)).toMatchObject({
      path: "/home/user/a.txt",
      size: 12,
      truncated: true,
    });
  });

  it("fileEntryFrom sigue el contrato compartido", () => {
    expect(fileEntryFrom(entry(FileType.FILE))).toEqual({
      name: "a.txt",
      path: "/home/user/a.txt",
      type: "file",
      size: 4,
      modified_time: "2026-09-16T12:00:00.000Z",
    });
    expect(fileEntryFrom(entry(undefined)).type).toBeNull();
  });

  it("el recibo de escritura y el listado", () => {
    expect(writeReceiptFrom(entry(FileType.FILE))).toEqual({ path: "/home/user/a.txt", size: 4 });
    const listing = directoryListingFrom("/home/user", [entry(FileType.DIR)]);
    expect(listing.path).toBe("/home/user");
    expect(listing.entries[0]?.type).toBe("dir");
  });

  it("sandboxSummaryFrom", () => {
    expect(sandboxSummaryFrom(item(SANDBOX_ID), true)).toEqual({
      sandbox_id: SANDBOX_ID,
      state: "RUNNING",
      template: IMAGE_ARN,
      template_version: "16.0",
      started_at: "2026-09-16T11:30:00.000Z",
      current: true,
    });
  });

  it("sandboxListFrom marca sólo el sandbox actual", () => {
    const listing = sandboxListFrom([item("other"), item(SANDBOX_ID)], SANDBOX_ID);
    expect(listing.sandboxes.map((sandbox) => sandbox.current)).toEqual([false, true]);
    expect(sandboxListFrom([item("other")], undefined).sandboxes[0]?.current).toBe(false);
  });
});
