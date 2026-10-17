/**
 * Lo que `rayito agent deploy` y `rayito agent detect` hacen antes y después
 * de llamar al SDK (ADR-029). Espejo de `rayito.cli._agent_deploy` (la parte
 * sin proyecto): resolver el access token y su fichero, acortar los logs del
 * build y escribir el informe de la detección y del plan. Sin AWS ni SDK del
 * agente: cada función es pura o toca sólo ficheros locales.
 */

import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import type { AgentDeployPlan } from "../agent/deploy-plan.js";
import { AGENT_TOKEN_FILE_DEFAULT } from "../limits.js";
import { generateAccessToken } from "../payload.js";
import { readTokenFile, TOKEN_ENV_VAR, TokenFileError, writeTokenFile } from "./tokens.js";

/** Dónde guarda `rayito agent deploy` las plazas calientes para que sobrevivan
 * al comando (`JsonFilePoolBackend`, modo 0600, con los tokens de las plazas
 * aparcadas): junto al token y fuera del código que sube. */
export const AGENT_POOL_FILE_DEFAULT = join(dirname(AGENT_TOKEN_FILE_DEFAULT), "agent-pool.json");
const TOKEN_DIRECTORY_MODE = 0o700;
/** El `.gitignore` que se escribe en el directorio del token y de las plazas:
 * ignora todo, él mismo incluido, para que un `git add .` no suba un secreto. */
const STATE_DIRECTORY_GITIGNORE = ".gitignore";
const STATE_DIRECTORY_GITIGNORE_CONTENT = "*\n";
/** Decimales con los que se imprime un coste en dólares. */
const USD_DECIMALS = 4;
/** Cada cuántas líneas de log del build se imprime una de progreso. */
export const COMPACT_LOG_PROGRESS_LINES = 200;
/** Una línea de log con alguno de estos textos siempre se imprime. */
export const COMPACT_LOG_ALERT_MARKERS: readonly string[] = ["error", "failed", "fatal"];
const PAIR_SEPARATOR = "=";

/** El `onBuildLogs` por defecto de `rayito agent deploy`: un build de imagen
 * escribe cientos de líneas de log, así que sólo se imprimen las que avisan de
 * un error, una de progreso cada `COMPACT_LOG_PROGRESS_LINES` y, al acabar, un
 * resumen. `--verbose` imprime todas. */
export class CompactBuildLog {
  readonly #emit: (line: string) => void;
  #lines = 0;

  constructor(emit: (line: string) => void) {
    this.#emit = emit;
  }

  readonly log = (line: string): void => {
    this.#lines += 1;
    const lowered = line.toLowerCase();
    if (COMPACT_LOG_ALERT_MARKERS.some((marker) => lowered.includes(marker))) {
      this.#emit(line);
    } else if (this.#lines % COMPACT_LOG_PROGRESS_LINES === 0) {
      this.#emit(`construyendo la imagen: ${this.#lines} líneas de log`);
    }
  };

  finish(): void {
    if (this.#lines > 0) {
      this.#emit(`build: ${this.#lines} líneas de log (--verbose las imprime todas)`);
    }
  }
}

/** Un par `K=V` mal formado; el mensaje no repite el valor: podría ser un
 * secreto mal pegado. */
export class PairFlagError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "PairFlagError";
  }
}

/** `K=V` repetibles de una bandera como objeto. */
export function parseFlagPairs(values: readonly string[], flag: string): Record<string, string> {
  const pairs: Record<string, string> = {};
  for (const item of values) {
    const separator = item.indexOf(PAIR_SEPARATOR);
    if (separator <= 0 || separator === item.length - 1) {
      throw new PairFlagError(`${flag}: se esperaba CLAVE=VALOR`);
    }
    pairs[item.slice(0, separator)] = item.slice(separator + 1);
  }
  return pairs;
}

export interface DeployToken {
  readonly token: string | undefined;
  readonly file: string | undefined;
}

/** El token que `deploy` pasa al SDK y el fichero donde queda.
 *
 * Con `--token-file`: `RAYITO_ACCESS_TOKEN` o uno nuevo, guardado en el fichero
 * antes de crear nada (uno que ya exista es un error y no se toca: así un
 * fallo al escribirlo nunca deja un VM huérfano). Sin él, la variable de
 * entorno gana y no se escribe nada; si tampoco hay variable, se usa
 * `AGENT_TOKEN_FILE_DEFAULT`: se reutiliza si existe (volver a desplegar
 * reconecta con la misma flota) y, si no, se crea con modo `0600`. */
export function deployToken(
  tokenFile: string | undefined,
  env: Readonly<Record<string, string | undefined>>,
): DeployToken {
  const fromEnvironment = env[TOKEN_ENV_VAR] || undefined;
  if (tokenFile !== undefined) {
    const token = fromEnvironment ?? generateAccessToken();
    writeTokenFile(tokenFile, token);
    return { token, file: tokenFile };
  }
  if (fromEnvironment !== undefined) {
    return { token: fromEnvironment, file: undefined };
  }
  return { token: defaultToken(AGENT_TOKEN_FILE_DEFAULT), file: AGENT_TOKEN_FILE_DEFAULT };
}

/** La ruta del respaldo en fichero de las plazas calientes si hay que dejar
 * alguna aparcada (`warmVms > 0`); sin plazas no se crea ni el directorio. */
export function poolBackendFileFor(
  warmVms: number,
  path: string = AGENT_POOL_FILE_DEFAULT,
): string | undefined {
  if (warmVms === 0) {
    return undefined;
  }
  ensureStateDirectory(dirname(path));
  return path;
}

/** Crea el directorio del token y de las plazas (modo `0700`) con un
 * `.gitignore` que lo ignora todo; uno que ya exista no se toca. */
export function ensureStateDirectory(directory: string): void {
  mkdirSync(directory, { recursive: true, mode: TOKEN_DIRECTORY_MODE });
  const ignore = join(directory, STATE_DIRECTORY_GITIGNORE);
  if (!existsSync(ignore)) {
    writeFileSync(ignore, STATE_DIRECTORY_GITIGNORE_CONTENT, "utf8");
  }
}

export function defaultToken(path: string): string {
  if (existsSync(path)) {
    const existing = readTokenFile(path);
    if (existing !== "") {
      return existing;
    }
    throw new TokenFileError(`el fichero de token está vacío: ${path}`);
  }
  ensureStateDirectory(dirname(path));
  const token = generateAccessToken();
  writeTokenFile(path, token);
  return token;
}

/** El token de una flota ya desplegada: `--token-file`, `RAYITO_ACCESS_TOKEN`
 * o el fichero por defecto de `deploy`; `undefined` si no hay ninguno. */
export function fleetToken(
  tokenFile: string | undefined,
  env: Readonly<Record<string, string | undefined>>,
): string | undefined {
  if (tokenFile !== undefined) {
    return readTokenFile(tokenFile) || undefined;
  }
  if (env[TOKEN_ENV_VAR]) {
    return env[TOKEN_ENV_VAR];
  }
  return existsSync(AGENT_TOKEN_FILE_DEFAULT)
    ? readTokenFile(AGENT_TOKEN_FILE_DEFAULT) || undefined
    : undefined;
}

export function formatUsd(value: number): string {
  return `$${value.toFixed(USD_DECIMALS)}`.replace(".", ",");
}

const SOURCE_LABELS: Readonly<Record<string, string>> = {
  argument: "el argumento",
  project_file: "el fichero del proyecto",
  environment: "la variable de entorno",
  discovered: "se descubrirá el bucket de CDK de la cuenta",
  generated: "se generará uno nuevo",
  override: "el de --secret",
  project: "el del proyecto",
};

function sourceLabel(source: string): string {
  return SOURCE_LABELS[source] ?? source;
}

/** El plan en líneas de texto (sin secretos, sin AWS). */
export function describePlan(plan: AgentDeployPlan): string[] {
  const { scaling, cost } = plan;
  const lines = [
    `agente: ${plan.name} (imagen ${plan.template}, base ${plan.base}, ${plan.memoryMib} MiB)`,
    `región: ${plan.region ?? "la de la cadena de AWS"}`,
    `VMs: mínimo ${scaling.minVms}, máximo ${scaling.maxVms}, calientes ${scaling.warmVms}, sesiones ${scaling.sessions}`,
    `bucket del artefacto: ${sourceLabel(plan.bucketSource)}`,
    `access token: ${sourceLabel(plan.tokenSource)}`,
  ];
  for (const item of plan.secrets) {
    lines.push(`secreto de la pasarela ${item.gateway}: ${sourceLabel(item.source)}`);
  }
  lines.push(
    `coste de lista: ${formatUsd(cost.runningUsdPerHour)}/h por los VMs mínimos; plazas ` +
      `aparcadas ${formatUsd(cost.parkedUsdPerDay)}/día; imagen nueva ` +
      `${formatUsd(cost.imageBuildUsd)} y ${formatUsd(cost.imageStorageUsdPerWeek)}/semana`,
  );
  for (const note of plan.notes) {
    lines.push(`nota: ${note}`);
  }
  return lines;
}

/** Los campos escalares de un informe de detección como `detectado clave:
 * valor` (no la lista de evidencias ni ningún contenido de ficheros). */
export function detectionLines(report: unknown): string[] {
  if (typeof report !== "object" || report === null) {
    return [];
  }
  return Object.entries(report as Record<string, unknown>)
    .filter(
      ([, value]) =>
        (typeof value === "string" && value !== "") ||
        typeof value === "number" ||
        typeof value === "boolean",
    )
    .map(([key, value]) => `detectado ${key}: ${String(value)}`);
}
