/**
 * Política de sondeo de readiness y de reconexión (SPEC.md §5 y el contrato
 * M5): calendarios de `Health`, comprobaciones de `get-microvm`, el
 * presupuesto de reconexiones inútiles y la tabla de fallos. Sin I/O.
 */

import { ConnectError } from "@connectrpc/connect";
import {
  SandboxError,
  SandboxNotFoundError,
  SandboxNotReadyError,
  SandboxStateError,
} from "../errors.js";
import type { HealthResponse } from "../gen/rayito/v1/health_pb.js";
import { SUSPENDED_STATES, TERMINAL_STATES } from "../limits.js";
import type { SandboxHealth, SandboxInfo } from "../models.js";
import { isPhaseGate } from "../transport/errors.js";

export const CLOCK_OFFSET_WARN_MS = 5000;
export const MAX_FUTILE_RECONNECTS = 3;

export type Monotonic = () => number;
export type Random = () => number;

export interface PollOptions {
  readonly timeoutMs: number;
  readonly now?: Monotonic | undefined;
  readonly random?: Random | undefined;
}

/**
 * Calendario del sondeo de `Health`: 0,25 s doblando hasta 2 s, con una
 * comprobación de `get-microvm` cada 5 s para detectar `TERMINATING|TERMINATED`.
 * Los campos estáticos son mutables para que los tests acorten las esperas.
 */
export class ReadinessPoll {
  static initialDelayMs = 250;
  static maxDelayMs = 2000;
  static stateCheckIntervalMs = 5000;
  static maxRpcTimeoutMs = 5000;
  static minRpcTimeoutMs = 500;
  static jitter = 0;

  readonly #now: Monotonic;
  readonly #random: Random;
  readonly #started: number;
  readonly #deadline: number;
  #lastStateCheck: number;
  #delay: number;

  constructor(options: PollOptions) {
    this.#now = options.now ?? performance.now.bind(performance);
    this.#random = options.random ?? Math.random;
    const started = this.#now();
    this.#started = started;
    this.#deadline = started + options.timeoutMs;
    this.#lastStateCheck = started;
    this.#delay = this.timing().initialDelayMs;
  }

  protected timing(): {
    initialDelayMs: number;
    maxDelayMs: number;
    stateCheckIntervalMs: number;
    maxRpcTimeoutMs: number;
    minRpcTimeoutMs: number;
    jitter: number;
  } {
    return ReadinessPoll;
  }

  elapsedMs(): number {
    return Math.max(0, this.#now() - this.#started);
  }

  remainingMs(): number {
    return Math.max(0, this.#deadline - this.#now());
  }

  timedOut(): boolean {
    return this.remainingMs() <= 0;
  }

  rpcTimeoutMs(): number {
    const timing = this.timing();
    return Math.ceil(
      Math.min(timing.maxRpcTimeoutMs, Math.max(timing.minRpcTimeoutMs, this.remainingMs())),
    );
  }

  shouldCheckState(): boolean {
    const now = this.#now();
    if (now - this.#lastStateCheck < this.timing().stateCheckIntervalMs) {
      return false;
    }
    this.#lastStateCheck = now;
    return true;
  }

  nextDelayMs(): number {
    const delay = Math.min(this.#jittered(this.#delay), this.remainingMs());
    this.#delay = Math.min(this.#delay * 2, this.timing().maxDelayMs);
    return Math.max(0, delay);
  }

  #jittered(delay: number): number {
    const jitter = this.timing().jitter;
    if (jitter === 0) {
      return delay;
    }
    return delay * (1 + jitter * (2 * this.#random() - 1));
  }
}

/**
 * Calendario de la reconexión tras un corte: 0,5 s doblando hasta 4 s con
 * ±25 % de jitter (varios handles de un proceso no sondean al unísono),
 * `get-microvm` cada 5 s, y un tope de `reconnectTimeoutMs` (60 s por
 * defecto: los 30 s de `resumeTimeoutInSeconds` de la imagen más 30).
 */
export class ReconnectPoll extends ReadinessPoll {
  static override initialDelayMs = 500;
  static override maxDelayMs = 4000;
  static override stateCheckIntervalMs = 5000;
  static override maxRpcTimeoutMs = 5000;
  static override minRpcTimeoutMs = 500;
  static override jitter = 0.25;

  protected override timing(): {
    initialDelayMs: number;
    maxDelayMs: number;
    stateCheckIntervalMs: number;
    maxRpcTimeoutMs: number;
    minRpcTimeoutMs: number;
    jitter: number;
  } {
    return ReconnectPoll;
  }
}

/**
 * Resultado de `Sandbox.reconnect`: si el agente volvió a responder, si la
 * generación de resume cambió (los streams del servidor se perdieron) y, si
 * no volvió, el error con el que debe fallar el caller.
 */
export interface ReconnectOutcome {
  readonly resumed: boolean;
  readonly generationChanged: boolean;
  readonly resumeGeneration: number;
  readonly error?: Error | undefined;
}

/**
 * Cuenta las reconexiones seguidas que no vieron una generación nueva: un
 * stream que el proxy corta una y otra vez con el agente vivo no se reabre
 * para siempre; a la cuarta el corte se clasifica como en M2.
 */
export class ReconnectBudget {
  futile = 0;

  allows(outcome: ReconnectOutcome): boolean {
    if (outcome.generationChanged) {
      this.futile = 0;
      return true;
    }
    this.futile += 1;
    return this.futile <= MAX_FUTILE_RECONNECTS;
  }
}

/**
 * `suspend-microvm` es idempotente: sobre un VM `SUSPENDED` responde 200
 * (medido 2026-09-16, nunca `ConflictException`), así que "no estaba
 * `RUNNING`" se decide leyendo `get-microvm` antes de llamar.
 */
export function alreadySuspended(info: SandboxInfo): boolean {
  return SUSPENDED_STATES.has(info.state);
}

/**
 * Un corte causado por un `/suspend` (final en-stream `suspending` o el phase
 * gate `Unavailable suspending`): sólo cuenta como reconectado un `Health` con
 * una generación de resume nueva, porque el agente sigue respondiendo unos
 * cientos de ms mientras el VM se congela.
 */
export function isSuspendingReason(reason: unknown): boolean {
  if (reason instanceof ConnectError) {
    return isPhaseGate(reason);
  }
  return reason instanceof SandboxStateError;
}

export interface ReconnectFailureOptions {
  readonly info?: SandboxInfo | undefined;
  readonly timeoutMs?: number | undefined;
  readonly wake?: boolean | undefined;
}

/**
 * Por qué el sondeo de reconexión se detiene: un estado terminal de
 * `get-microvm`, `SUSPENDED` sin auto-resume o el deadline agotado.
 * `undefined` significa seguir sondeando. `SUSPENDED` sin auto-resume sólo
 * detiene a un caller con `wake` (sus sondas de `Health` no van a despertar
 * nada); un handle dormido espera al `resume()` sea cual sea la política.
 */
export function reconnectFailure(
  reason: unknown,
  options: ReconnectFailureOptions = {},
): Error | undefined {
  const wake = options.wake ?? true;
  const info = options.info;
  if (info !== undefined) {
    if (TERMINAL_STATES.has(info.state)) {
      return chained(terminalStateError(info), reason);
    }
    if (wake && info.state === "SUSPENDED" && !(info.idle?.autoResume ?? false)) {
      return chained(
        new SandboxStateError(
          `el sandbox ${info.sandboxId} está suspendido sin auto-resume; llama a resume() para reanudarlo`,
        ),
        reason,
      );
    }
    return undefined;
  }
  if (options.timeoutMs === undefined) {
    return undefined;
  }
  const seconds = formatSeconds(options.timeoutMs);
  if (isSuspendingReason(reason)) {
    const phase = reason instanceof ConnectError ? reason.rawMessage : "suspending";
    return chained(
      new SandboxStateError(`el sandbox está ${phase} y no volvió a responder en ${seconds} s`),
      reason,
    );
  }
  return chained(new SandboxError(`el sandbox no volvió a responder en ${seconds} s`), reason);
}

export function formatSeconds(ms: number): string {
  return String(Number((ms / 1000).toFixed(3)));
}

function chained<T extends Error>(error: T, cause: unknown): T {
  if (cause !== undefined && (error as { cause?: unknown }).cause === undefined) {
    Object.defineProperty(error, "cause", { value: cause, writable: true, configurable: true });
  }
  return error;
}

export function healthFromProto(response: HealthResponse): SandboxHealth {
  return Object.freeze({
    agentReady: Boolean(response.agentReady),
    kernelReady: Boolean(response.kernelReady),
    agentVersion: String(response.agentVersion),
    uptimeMs: Number(response.uptimeMs),
    sandboxId: String(response.sandboxId),
    resumeGeneration: Number(response.resumeGeneration),
    clockOffsetMs: Number(response.clockOffsetMs),
    kernelStateLost: Boolean(response.kernelStateLost),
  });
}

/**
 * `Health` cuenta como "volvió" cuando el agente y el kernel están listos;
 * tras un `/suspend` además hace falta una generación nueva.
 */
export function healthReconnected(
  response: HealthResponse | undefined,
  options: { readonly seenGeneration: number; readonly suspending: boolean },
): boolean {
  if (response === undefined || !(response.agentReady && response.kernelReady)) {
    return false;
  }
  return !options.suspending || Number(response.resumeGeneration) !== options.seenGeneration;
}

export function notReadyError(
  info: SandboxInfo | undefined,
  options: { readonly readyTimeoutMs: number; readonly terminated: boolean },
): SandboxNotReadyError {
  const state = info?.state;
  const reason = info?.stateReason;
  const detail = info === undefined ? "" : ` (state=${state}, stateReason=${reason})`;
  const outcome = options.terminated ? "; el MicroVM fue terminado" : "";
  return new SandboxNotReadyError(
    `el agente no respondió a Health en ${formatSeconds(options.readyTimeoutMs)} s${detail}${outcome}`,
    { state, stateReason: reason },
  );
}

/** Un MicroVM `TERMINATING|TERMINATED` ya no existe para el SDK. */
export function terminalStateError(info: SandboxInfo): SandboxNotFoundError {
  return new SandboxNotFoundError(
    `el sandbox ${info.sandboxId} está ${info.state}: ${info.stateReason ?? "sin stateReason"}`,
  );
}

export function terminatedDuringBootError(info: SandboxInfo): SandboxNotReadyError {
  return new SandboxNotReadyError(
    `el MicroVM pasó a ${info.state} antes de estar listo: ${info.stateReason ?? "sin stateReason"}`,
    { state: info.state, stateReason: info.stateReason },
  );
}

export function closedDuringReconnect(sandboxId: string): SandboxError {
  return new SandboxError(`el sandbox ${sandboxId} fue cerrado durante la reconexión`);
}
