/**
 * Tabla de compatibilidad SDK ↔ `rayd` (`agent_version`), espejo de
 * `rayito.cli._compat`. Una fila por serie `MAJOR.MINOR` del SDK con el
 * `agent_version` mínimo; SDK y `rayd` avanzan `MAJOR.MINOR` a la vez y el
 * parche puede divergir. La versión de imagen no entra: es el contador de
 * builds de cada imagen en cada cuenta. `testdata/cli/compatibility.json`
 * lleva la misma tabla y sus evaluaciones, y `docs/site/docs/limits.md` la
 * publica.
 */

import { pythonStringRepr } from "../../strings.js";

const SEMVER_PATTERN = /^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$/;
const SEMVER_PARTS = 3;

export type CompatibilityStatus = "OK" | "WARN" | "FAIL";

export interface CompatibilityRow {
  readonly sdkSeries: string;
  readonly minAgentVersion: string;
  readonly note: string;
}

export const COMPATIBILITY: readonly CompatibilityRow[] = [
  {
    sdkSeries: "0.1",
    minAgentVersion: "0.1.0",
    note: "M6: imds_blocked, hook_anomalies y metadata exigen el rayd del tag rayd-v0.1.0",
  },
  {
    sdkSeries: "0.2",
    minAgentVersion: "0.2.0",
    note: "M7: Checkpoint/Restore (persist=) y language= exigen el rayd del tag rayd-v0.2.0",
  },
  {
    sdkSeries: "0.3",
    minAgentVersion: "0.3.0",
    note:
      "M9: max_lifetime/on_timeout, set_timeout, get_metrics_history, network= y los kernels " +
      "Deno exigen el rayd del tag rayd-v0.3.0",
  },
  {
    sdkSeries: "0.4",
    minAgentVersion: "0.4.0",
    note:
      "0.4: UnimplementedError único, SetTimeout validado en el dominio y mensajes del agente " +
      "en español exigen el rayd del tag rayd-v0.4.0",
  },
  {
    sdkSeries: "0.5",
    minAgentVersion: "0.5.0",
    note:
      "0.5: /suspend con sync acotado por sistema de ficheros y las funciones opcionales " +
      "(secretos, índice, OTel) se validan con el rayd del tag rayd-v0.5.0",
  },
  {
    sdkSeries: "0.6",
    minAgentVersion: "0.6.0",
    note:
      "0.6: ConfigureSandbox y las funciones 0.6 (montajes S3, eventos, OTLP, " +
      "pasarela de secretos, start/ready de templates) exigen el rayd del tag rayd-v0.6.0",
  },
  {
    sdkSeries: "0.7",
    minAgentVersion: "0.7.0",
    note:
      "0.7: los volúmenes EFS (create(volumes=)) y el endurecimiento del agente (hooks " +
      "aislados, persistencia ligada al /run) exigen el rayd del tag rayd-v0.7.0",
  },
  {
    sdkSeries: "0.8",
    minAgentVersion: "0.8.0",
    note:
      "0.8: el agente de IA (sbx.agent, AgentTemplate y el warmup de los pools) se valida " +
      "con el rayd del tag rayd-v0.8.0",
  },
  {
    sdkSeries: "0.9",
    minAgentVersion: "0.9.0",
    note:
      "0.9: connect() recupera sbx.gateways desde ConfigureStatus y el arranque rápido del " +
      "agente sin la opción D se validan con el rayd del tag rayd-v0.9.0",
  },
  {
    sdkSeries: "0.10",
    minAgentVersion: "0.10.0",
    note:
      "0.10: el timeout y abort() del agente paran todo el árbol de procesos (kill_tree) " +
      "y los proveedores del agente se validan con el rayd del tag rayd-v0.10.0",
  },
  {
    sdkSeries: "0.11",
    minAgentVersion: "0.11.0",
    note:
      "0.11: el arranque rápido de AgentTemplate (kernel_warmup=False por defecto) y las " +
      "claves de artefacto por ejecución se validan con el rayd del tag rayd-v0.11.0",
  },
  {
    sdkSeries: "0.12",
    minAgentVersion: "0.12.0",
    note:
      "0.12: el panel de gestión (rayito dashboard y la pila dashboard) se validan con el " +
      "rayd del tag rayd-v0.12.0",
  },
  {
    sdkSeries: "0.13",
    minAgentVersion: "0.13.0",
    note:
      "0.13: la CLI y el servidor MCP de TypeScript (ADR-027) y el formato de instantes en UTC " +
      "de la CLI se validan con el rayd del tag rayd-v0.13.0",
  },
  {
    sdkSeries: "0.14",
    minAgentVersion: "0.14.0",
    note:
      "0.14: el despliegue sin configuración de un agente que ya tienes (Strands, Hermes, " +
      "LangGraph, OpenCode o propio), sus opciones tipadas de escalado y la CLI rayito agent " +
      "se validan con el rayd del tag rayd-v0.14.0",
  },
  {
    sdkSeries: "0.15",
    minAgentVersion: "0.15.0",
    note:
      "0.15: rayito setup (preparar una cuenta en un comando) y el despliegue de agentes en " +
      "Amazon Bedrock AgentCore Runtime (--target agentcore) se validan con el rayd del tag " +
      "rayd-v0.15.0",
  },
];

export interface Assessment {
  readonly status: CompatibilityStatus;
  readonly reason: string;
  readonly row: CompatibilityRow | undefined;
}

export type SemanticVersion = readonly [number, number, number];

/** `MAJOR.MINOR.PATCH` como enteros; un sufijo de pre-release se ignora y cualquier otra forma es `RangeError`. */
export function parseSemver(text: string): SemanticVersion {
  const match = SEMVER_PATTERN.exec(text.trim());
  if (match === null) {
    throw new RangeError(
      `versión no reconocida: ${pythonStringRepr(text)} (se esperaba MAJOR.MINOR.PATCH)`,
    );
  }
  const [major, minor, patch] = match.slice(1, SEMVER_PARTS + 1).map(Number);
  return [major ?? 0, minor ?? 0, patch ?? 0];
}

export function seriesOf(version: SemanticVersion): string {
  return `${version[0]}.${version[1]}`;
}

export function rowFor(sdkSeries: string): CompatibilityRow | undefined {
  return COMPATIBILITY.find((row) => row.sdkSeries === sdkSeries);
}

function isBefore(left: SemanticVersion, right: SemanticVersion): boolean {
  for (let index = 0; index < SEMVER_PARTS; index += 1) {
    const difference = (left[index] ?? 0) - (right[index] ?? 0);
    if (difference !== 0) {
      return difference < 0;
    }
  }
  return false;
}

function isNewerSeries(left: SemanticVersion, right: SemanticVersion): boolean {
  return left[0] !== right[0] ? left[0] > right[0] : left[1] > right[1];
}

function parsedPair(
  sdkVersion: string,
  agentVersion: string,
): { sdk: SemanticVersion; agent: SemanticVersion } | string {
  try {
    return { sdk: parseSemver(sdkVersion), agent: parseSemver(agentVersion) };
  } catch (error) {
    if (error instanceof RangeError) {
      return error.message;
    }
    throw error;
  }
}

/**
 * `FAIL` si el agente está por debajo del mínimo de la fila; `WARN` si es más
 * nuevo que el SDK en `MAJOR.MINOR` (funciones que el SDK no conoce); `OK` si no.
 */
export function assess(sdkVersion: string, agentVersion: string): Assessment {
  const parsed = parsedPair(sdkVersion, agentVersion);
  if (typeof parsed === "string") {
    return { status: "FAIL", reason: parsed, row: undefined };
  }
  const { sdk, agent } = parsed;
  const row = rowFor(seriesOf(sdk));
  if (row === undefined) {
    return {
      status: "FAIL",
      reason: `sin fila de compatibilidad para el SDK ${seriesOf(sdk)}`,
      row,
    };
  }
  if (isBefore(agent, parseSemver(row.minAgentVersion))) {
    return {
      status: "FAIL",
      reason:
        `agent_version ${agentVersion} por debajo del mínimo ${row.minAgentVersion} ` +
        `del SDK ${sdkVersion}: publica una imagen desde el tag rayd-v${row.minAgentVersion}`,
      row,
    };
  }
  if (isNewerSeries(agent, sdk)) {
    return {
      status: "WARN",
      reason:
        `agent_version ${agentVersion} es más nuevo que el SDK ${sdkVersion} ` +
        `(${seriesOf(agent)} > ${seriesOf(sdk)}): actualiza el SDK`,
      row,
    };
  }
  return { status: "OK", reason: `SDK ${sdkVersion}, agent_version ${agentVersion}`, row };
}
