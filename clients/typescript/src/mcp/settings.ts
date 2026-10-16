/**
 * Configuración del servidor MCP: sólo variables de entorno (espejo de
 * `rayito.mcp._settings`). Los hosts MCP configuran servidores con un bloque
 * `env`, así que no hay flags que dupliquen estas variables. La región, el
 * perfil y las credenciales de AWS los resuelve el SDK de AWS por su cuenta.
 * Este módulo no importa el SDK de MCP: `parseMcpSettings` se puede validar
 * sin los peers opcionales instalados.
 */

import { IDLE_MAX_IDLE_MIN_SECONDS, MAX_DURATION_SECONDS } from "../limits.js";
import type { IdlePolicy } from "../models.js";
import { type LoggingOption, TEMPLATE_ENV_VAR } from "../sandbox/launch.js";

export const TEMPLATE_VERSION_ENV_VAR = "RAYITO_TEMPLATE_VERSION";
export const EXECUTION_ROLE_ENV_VAR = "RAYITO_EXECUTION_ROLE_ARN";
export const TIMEOUT_ENV_VAR = "RAYITO_MCP_TIMEOUT_SECONDS";
export const IDLE_ENV_VAR = "RAYITO_MCP_IDLE_SECONDS";
export const LOG_LEVEL_ENV_VAR = "RAYITO_MCP_LOG_LEVEL";

export const DEFAULT_TIMEOUT_SECONDS = 3600;
export const DEFAULT_IDLE_SECONDS = 300;
export const DEFAULT_LOG_LEVEL = "INFO";
export const MIN_TIMEOUT_SECONDS = 60;
export const IDLE_DISABLED = 0;
export const LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] as const;

export type LogLevel = (typeof LOG_LEVELS)[number];

const INTEGER_PATTERN = /^\s*[+-]?\d+\s*$/;

/** Un valor de entorno malformado; el mensaje nombra la variable. */
export class McpSettingsError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "McpSettingsError";
  }
}

export interface McpSettingsFields {
  readonly template?: string | undefined;
  readonly templateVersion?: string | undefined;
  readonly executionRoleArn?: string | undefined;
  readonly timeoutSeconds?: number | undefined;
  readonly idleSeconds?: number | undefined;
  readonly logLevel?: LogLevel | undefined;
}

/**
 * Lo que el servidor necesita para crear su único sandbox.
 *
 * `template` puede faltar: no se valida al arrancar sino en la primera
 * herramienta que lo necesita, para que `rayito-mcp --help` y `tools/list`
 * funcionen sin AWS. `idleSeconds === 0` desactiva la auto-suspensión; si no,
 * debe ser menor que `timeoutSeconds` (lo exige el SDK al crear el sandbox, y
 * aquí se comprueba al arrancar para que no falle cada herramienta).
 * `logging` es `cloudwatch` sólo con execution role.
 */
export class McpSettings {
  readonly template: string | undefined;
  readonly templateVersion: string | undefined;
  readonly executionRoleArn: string | undefined;
  readonly timeoutSeconds: number;
  readonly idleSeconds: number;
  readonly logLevel: LogLevel;

  constructor(fields: McpSettingsFields = {}) {
    this.template = fields.template;
    this.templateVersion = fields.templateVersion;
    this.executionRoleArn = fields.executionRoleArn;
    this.timeoutSeconds = fields.timeoutSeconds ?? DEFAULT_TIMEOUT_SECONDS;
    this.idleSeconds = fields.idleSeconds ?? DEFAULT_IDLE_SECONDS;
    this.logLevel = fields.logLevel ?? DEFAULT_LOG_LEVEL;
    if (this.idleSeconds !== IDLE_DISABLED && this.idleSeconds >= this.timeoutSeconds) {
      throw new McpSettingsError(
        `${IDLE_ENV_VAR}=${this.idleSeconds} debe ser menor que ` +
          `${TIMEOUT_ENV_VAR}=${this.timeoutSeconds} (o ${IDLE_DISABLED} para ` +
          "desactivar la auto-suspensión)",
      );
    }
  }

  get idlePolicy(): IdlePolicy | null {
    if (this.idleSeconds === IDLE_DISABLED) {
      return null;
    }
    return { maxIdleSeconds: this.idleSeconds, autoResume: true };
  }

  get logging(): LoggingOption {
    return this.executionRoleArn === undefined ? "disabled" : "cloudwatch";
  }
}

/** Lee las variables; `McpSettingsError` en español nombrando la variable ante un valor malformado. */
export function parseMcpSettings(env: Readonly<Record<string, string | undefined>>): McpSettings {
  return new McpSettings({
    template: nonEmpty(env[TEMPLATE_ENV_VAR]),
    templateVersion: nonEmpty(env[TEMPLATE_VERSION_ENV_VAR]),
    executionRoleArn: nonEmpty(env[EXECUTION_ROLE_ENV_VAR]),
    timeoutSeconds: parseTimeoutSeconds(env[TIMEOUT_ENV_VAR]),
    idleSeconds: parseIdleSeconds(env[IDLE_ENV_VAR]),
    logLevel: parseLogLevel(env[LOG_LEVEL_ENV_VAR]),
  });
}

function nonEmpty(raw: string | undefined): string | undefined {
  return raw === undefined || raw === "" ? undefined : raw;
}

function parseInteger(raw: string, variable: string): number {
  if (!INTEGER_PATTERN.test(raw)) {
    throw new McpSettingsError(`${variable} debe ser un entero, recibido '${raw}'`);
  }
  return Number.parseInt(raw, 10);
}

function parseTimeoutSeconds(raw: string | undefined): number {
  if (raw === undefined || raw === "") {
    return DEFAULT_TIMEOUT_SECONDS;
  }
  const value = parseInteger(raw, TIMEOUT_ENV_VAR);
  if (value < MIN_TIMEOUT_SECONDS || value > MAX_DURATION_SECONDS) {
    throw new McpSettingsError(
      `${TIMEOUT_ENV_VAR} debe estar entre ${MIN_TIMEOUT_SECONDS} y ` +
        `${MAX_DURATION_SECONDS} segundos, recibido ${value}`,
    );
  }
  return value;
}

function parseIdleSeconds(raw: string | undefined): number {
  if (raw === undefined || raw === "") {
    return DEFAULT_IDLE_SECONDS;
  }
  const value = parseInteger(raw, IDLE_ENV_VAR);
  if (value !== IDLE_DISABLED && value < IDLE_MAX_IDLE_MIN_SECONDS) {
    throw new McpSettingsError(
      `${IDLE_ENV_VAR} debe ser ${IDLE_DISABLED} (sin auto-suspensión) o un entero ` +
        `>= ${IDLE_MAX_IDLE_MIN_SECONDS}, recibido ${value}`,
    );
  }
  return value;
}

function parseLogLevel(raw: string | undefined): LogLevel {
  if (raw === undefined || raw === "") {
    return DEFAULT_LOG_LEVEL;
  }
  const level = raw.toUpperCase();
  const known = LOG_LEVELS.find((candidate) => candidate === level);
  if (known === undefined) {
    throw new McpSettingsError(
      `${LOG_LEVEL_ENV_VAR} debe ser uno de ${LOG_LEVELS.join(", ")}, recibido '${raw}'`,
    );
  }
  return known;
}
