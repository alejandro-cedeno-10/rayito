/**
 * Formas de resultado del servidor MCP y su mapeo desde los modelos del SDK
 * (espejo de `rayito.mcp._results`). Funciones puras, sin I/O y sin importar
 * el SDK de MCP: los bloques de contenido son objetos con la forma del
 * protocolo.
 *
 * Los nombres de campo son los del contrato compartido con Python
 * (`snake_case`, `testdata/mcp/tools.json`). Las marcas de tiempo salen en
 * ISO 8601 con `Z` (`Date.toISOString`); Python usa `+00:00`, y ambas formas
 * son ISO 8601 válidas para el cliente.
 */

import { MCP_MAX_OUTPUT_CHARS } from "../limits.js";
import {
  type CommandResult,
  type EntryInfo,
  type Execution,
  executionErrorToJson,
  type Result,
  type SandboxListItem,
} from "../models.js";
import { JSON_INDENT } from "../units.js";

export const PNG_MIME = "image/png";
export const JPEG_MIME = "image/jpeg";
export const SVG_MIME = "image/svg+xml";
export const RESULT_URI_SCHEME = "rayito";
const UNKNOWN_EXECUTION_COUNT = "unknown";

export interface CommandOutput {
  readonly stdout: string;
  readonly stderr: string;
  readonly exit_code: number;
  readonly truncated: boolean;
}

export interface FileContent {
  readonly path: string;
  readonly content: string;
  readonly size: number;
  readonly truncated: boolean;
}

export interface WriteReceipt {
  readonly path: string;
  readonly size: number;
}

export interface FileEntry {
  readonly name: string;
  readonly path: string;
  readonly type: string | null;
  readonly size: number;
  readonly modified_time: string;
}

export interface DirectoryListing {
  readonly path: string;
  readonly entries: readonly FileEntry[];
}

export interface SandboxSummary {
  readonly sandbox_id: string;
  readonly state: string;
  readonly template: string;
  readonly template_version: string;
  readonly started_at: string;
  readonly current: boolean;
}

export interface SandboxList {
  readonly sandboxes: readonly SandboxSummary[];
}

export interface TextBlock {
  readonly type: "text";
  readonly text: string;
}

export interface ImageBlock {
  readonly type: "image";
  readonly data: string;
  readonly mimeType: string;
}

export interface ResourceBlock {
  readonly type: "resource";
  readonly resource: { readonly uri: string; readonly mimeType: string; readonly text: string };
}

export type ContentBlock = TextBlock | ImageBlock | ResourceBlock;

export interface Truncated {
  readonly text: string;
  readonly cut: boolean;
}

/** Cuenta caracteres Unicode (no unidades UTF-16), como `len` de Python. */
export function characterCount(text: string): number {
  let count = 0;
  for (const _ of text) {
    count += 1;
  }
  return count;
}

/**
 * Corta `text` a `limit` caracteres con el sufijo `… [truncado: <n>
 * caracteres más]`; `cut` dice si hubo corte.
 */
export function truncateText(text: string, limit: number = MCP_MAX_OUTPUT_CHARS): Truncated {
  if (text.length <= limit) {
    return { text, cut: false };
  }
  const characters = Array.from(text);
  if (characters.length <= limit) {
    return { text, cut: false };
  }
  const remaining = characters.length - limit;
  const head = characters.slice(0, limit).join("");
  return { text: `${head}\n… [truncado: ${remaining} caracteres más]`, cut: true };
}

function truncateOptional(text: string | undefined, limit: number): Truncated | undefined {
  return text === undefined ? undefined : truncateText(text, limit);
}

/**
 * Primero un bloque de texto JSON con el resumen, después una imagen por
 * `png`/`jpeg` y un recurso embebido por `svg`.
 */
export function executionToBlocks(
  execution: Execution,
  options: { readonly sandboxId: string; readonly limit?: number },
): ContentBlock[] {
  const summary = executionSummary(execution, options.limit ?? MCP_MAX_OUTPUT_CHARS);
  return [
    { type: "text", text: JSON.stringify(summary, null, JSON_INDENT) },
    ...imageBlocks(execution.results),
    ...svgBlocks(execution.results, options.sandboxId, execution.executionCount),
  ];
}

export function executionSummary(execution: Execution, limit: number): Record<string, unknown> {
  const text = truncateOptional(execution.text, limit);
  const stdout = truncateText(execution.logs.stdout.join(""), limit);
  const stderr = truncateText(execution.logs.stderr.join(""), limit);
  return {
    text: text?.text ?? null,
    stdout: stdout.text,
    stderr: stderr.text,
    error: execution.error === undefined ? null : executionErrorToJson(execution.error),
    execution_count: execution.executionCount ?? null,
    results: execution.results.map((result, index) => ({
      index,
      mime_types: Object.keys(result.raw),
    })),
    truncated: (text?.cut ?? false) || stdout.cut || stderr.cut,
  };
}

function imageBlocks(results: readonly Result[]): ImageBlock[] {
  return results.flatMap((result) => [
    ...(result.png === undefined
      ? []
      : [{ type: "image" as const, data: result.png, mimeType: PNG_MIME }]),
    ...(result.jpeg === undefined
      ? []
      : [{ type: "image" as const, data: result.jpeg, mimeType: JPEG_MIME }]),
  ]);
}

function svgBlocks(
  results: readonly Result[],
  sandboxId: string,
  executionCount: number | undefined,
): ResourceBlock[] {
  return results.flatMap((result, index) =>
    result.svg === undefined
      ? []
      : [
          {
            type: "resource" as const,
            resource: {
              uri: svgResultUri(sandboxId, executionCount, index),
              mimeType: SVG_MIME,
              text: result.svg,
            },
          },
        ],
  );
}

export function svgResultUri(
  sandboxId: string,
  executionCount: number | undefined,
  index: number,
): string {
  const count = executionCount === undefined ? UNKNOWN_EXECUTION_COUNT : String(executionCount);
  return `${RESULT_URI_SCHEME}://${sandboxId}/results/${count}/${index}.svg`;
}

/** Un exit code distinto de cero es dato: el error del SDK lleva los mismos campos que el resultado. */
export function commandOutputFrom(
  result: Pick<CommandResult, "stdout" | "stderr" | "exitCode">,
  limit: number = MCP_MAX_OUTPUT_CHARS,
): CommandOutput {
  const stdout = truncateText(result.stdout, limit);
  const stderr = truncateText(result.stderr, limit);
  return {
    stdout: stdout.text,
    stderr: stderr.text,
    exit_code: result.exitCode,
    truncated: stdout.cut || stderr.cut,
  };
}

export function fileContentFrom(
  path: string,
  content: string,
  limit: number = MCP_MAX_OUTPUT_CHARS,
): FileContent {
  const body = truncateText(content, limit);
  return { path, content: body.text, size: characterCount(content), truncated: body.cut };
}

export function writeReceiptFrom(entry: Pick<EntryInfo, "path" | "size">): WriteReceipt {
  return { path: entry.path, size: entry.size };
}

export function fileEntryFrom(entry: EntryInfo): FileEntry {
  return {
    name: entry.name,
    path: entry.path,
    type: entry.type ?? null,
    size: entry.size,
    modified_time: entry.modifiedTime.toISOString(),
  };
}

export function directoryListingFrom(
  path: string,
  entries: readonly EntryInfo[],
): DirectoryListing {
  return { path, entries: entries.map(fileEntryFrom) };
}

export function sandboxSummaryFrom(item: SandboxListItem, current: boolean): SandboxSummary {
  return {
    sandbox_id: item.sandboxId,
    state: item.state,
    template: item.template,
    template_version: item.templateVersion,
    started_at: item.startedAt.toISOString(),
    current,
  };
}

export function sandboxListFrom(
  items: readonly SandboxListItem[],
  currentSandboxId: string | undefined,
): SandboxList {
  return {
    sandboxes: items.map((item) => sandboxSummaryFrom(item, item.sandboxId === currentSandboxId)),
  };
}
