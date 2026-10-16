/**
 * `parseMcpSettings`: los casos compartidos con Python
 * (`testdata/mcp/settings-cases.json`, que también ejecuta pytest), los
 * valores por defecto y los límites del contrato.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { IDLE_MAX_IDLE_MIN_SECONDS, MAX_DURATION_SECONDS } from "../../../src/limits.js";
import {
  DEFAULT_IDLE_SECONDS,
  DEFAULT_LOG_LEVEL,
  DEFAULT_TIMEOUT_SECONDS,
  McpSettings,
  McpSettingsError,
  MIN_TIMEOUT_SECONDS,
  parseMcpSettings,
} from "../../../src/mcp/settings.js";

interface SettingsCase {
  readonly name: string;
  readonly env: Record<string, string>;
  readonly expected?: Record<string, unknown>;
  readonly errorVariable?: string;
}

const SHARED = JSON.parse(
  readFileSync(
    join(import.meta.dirname, "../../../../../testdata/mcp/settings-cases.json"),
    "utf8",
  ),
) as { defaults: Record<string, unknown>; cases: SettingsCase[] };

function describeSettings(settings: McpSettings): Record<string, unknown> {
  return {
    template: settings.template ?? null,
    templateVersion: settings.templateVersion ?? null,
    executionRoleArn: settings.executionRoleArn ?? null,
    timeoutSeconds: settings.timeoutSeconds,
    idleSeconds: settings.idleSeconds,
    logLevel: settings.logLevel,
    logging: settings.logging,
    idlePolicy: settings.idlePolicy,
  };
}

describe("parseMcpSettings", () => {
  it("usa los valores por defecto y los límites del contrato compartido", () => {
    expect(SHARED.defaults).toEqual({
      timeoutSeconds: DEFAULT_TIMEOUT_SECONDS,
      idleSeconds: DEFAULT_IDLE_SECONDS,
      logLevel: DEFAULT_LOG_LEVEL,
      minTimeoutSeconds: MIN_TIMEOUT_SECONDS,
      maxTimeoutSeconds: MAX_DURATION_SECONDS,
      minIdleSeconds: IDLE_MAX_IDLE_MIN_SECONDS,
    });
  });

  it.each(SHARED.cases)("$name", (testCase) => {
    if (testCase.errorVariable !== undefined) {
      expect(() => parseMcpSettings(testCase.env)).toThrow(McpSettingsError);
      expect(() => parseMcpSettings(testCase.env)).toThrow(testCase.errorVariable);
      return;
    }
    expect(describeSettings(parseMcpSettings(testCase.env))).toEqual(testCase.expected);
  });

  it("valida también el constructor, no sólo el entorno", () => {
    expect(() => new McpSettings({ timeoutSeconds: 600, idleSeconds: 600 })).toThrow(
      "RAYITO_MCP_IDLE_SECONDS=600 debe ser menor que RAYITO_MCP_TIMEOUT_SECONDS=600",
    );
  });
});
