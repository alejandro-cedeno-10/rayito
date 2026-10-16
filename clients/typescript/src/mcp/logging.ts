/**
 * El logger del servidor MCP: líneas a `stderr`, porque en stdio `stdout` es
 * el protocolo. Sólo recibe nombres de herramienta, tamaños, duraciones,
 * `sandboxId`, exit codes y nombres de error: nunca código, comandos,
 * contenidos ni salidas (`SECURITY.md`, "Higiene de logging").
 */

import type { Logger } from "../logger.js";
import { LOG_LEVELS, type LogLevel } from "./settings.js";

export interface LineSink {
  write(line: string): unknown;
}

const LOGGER_NAME = "rayito.mcp";

type Method = "debug" | "info" | "warn" | "error";

const METHOD_LEVEL: Readonly<Record<Method, LogLevel>> = {
  debug: "DEBUG",
  info: "INFO",
  warn: "WARNING",
  error: "ERROR",
};

function formatFields(fields: Readonly<Record<string, unknown>> | undefined): string {
  if (fields === undefined) {
    return "";
  }
  return Object.entries(fields)
    .map(([key, value]) => ` ${key}=${String(value)}`)
    .join("");
}

export function createStderrLogger(level: LogLevel, sink: LineSink): Logger {
  const threshold = LOG_LEVELS.indexOf(level);
  const emit =
    (method: Method) =>
    (message: string, fields?: Readonly<Record<string, unknown>>): void => {
      const label = METHOD_LEVEL[method];
      if (LOG_LEVELS.indexOf(label) >= threshold) {
        sink.write(`${LOGGER_NAME} ${label} ${message}${formatFields(fields)}\n`);
      }
    };
  return { debug: emit("debug"), info: emit("info"), warn: emit("warn"), error: emit("error") };
}
