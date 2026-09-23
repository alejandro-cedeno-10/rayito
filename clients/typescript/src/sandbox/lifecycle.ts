/**
 * Reglas puras del plazo lógico que impone `rayd` (ADR-011), espejo en
 * milisegundos de `rayito._lifecycle_base`: el bloque `lifecycle` del
 * `runHookPayload`, el `maximumDurationInSeconds` y la `idlePolicy` de
 * `run-microvm`, la lectura del `LifecycleState`, lo que extiende `connect()`
 * y cuándo dispara el disparador de pausa. Sin I/O.
 *
 * `timeoutMs` es el plazo lógico y se puede mover (`setTimeout`,
 * `connect({ timeoutMs })`); `maxLifetimeMs` es el tope de la plataforma
 * (running + suspendido, como mucho 28 800 000 ms) y queda fijo en `create()`
 * porque `UpdateMicrovm` no existe. Sin ninguno de los dos no viaja bloque y
 * todo es exactamente como antes de M9.
 */

import { create } from "@bufbuild/protobuf";
import {
  InvalidArgumentError,
  LifecycleUnsupportedError,
  type SandboxErrorOptions,
  SandboxLifetimeError,
  SandboxStateError,
} from "../errors.js";
import {
  LifecyclePhase,
  type LifecycleState,
  type SetTimeoutRequest,
  SetTimeoutRequestSchema,
  TimeoutAction,
  type TimeoutMode,
} from "../gen/rayito/v1/lifecycle_pb.js";
import {
  LIFECYCLE_AUTO_RESUME_MIN_SECONDS,
  LIFECYCLE_CAP_MARGIN_SECONDS,
  LIFECYCLE_MIN_MAX_LIFETIME_SECONDS,
  LIFECYCLE_MIN_TIMEOUT_SECONDS,
  MAX_DURATION_SECONDS,
} from "../limits.js";
import {
  type IdlePolicy,
  type IdlePolicyInput,
  type LifecyclePhaseName,
  type SandboxLifecycle,
  validateIdlePolicy,
} from "../models.js";

export type OnTimeout = "kill" | "pause";

export const SANDBOX_TIMEOUT_DETAIL = "sandbox_timeout";
export const LIFECYCLE_UNMANAGED_DETAIL = "lifecycle_unmanaged";
export const BEYOND_CAP_PREFIX = "timeout beyond cap";
export const MAX_LIFETIME_MS = MAX_DURATION_SECONDS * 1000;
export const MIN_MAX_LIFETIME_MS = LIFECYCLE_MIN_MAX_LIFETIME_SECONDS * 1000;
export const CAP_MARGIN_MS = LIFECYCLE_CAP_MARGIN_SECONDS * 1000;
export const MIN_SET_TIMEOUT_MS = LIFECYCLE_MIN_TIMEOUT_SECONDS * 1000;
/** Holgura del disparador de pausa sobre el plazo que informó `rayd` (desfase de relojes). */
export const PAUSE_TRIGGER_SLACK_MS = 1000;
/** Lo que se deja antes del tope al reabrir un sandbox vencido sin `timeoutMs` explícito. */
export const REOPEN_CAP_SLACK_MS = 5000;

/** El bloque `lifecycle` del `runHookPayload`, ya validado. */
export interface LifecycleBlock {
  readonly timeoutS: number;
  readonly capS: number;
  readonly onTimeout: OnTimeout;
  readonly autoResume: boolean;
}

/** La forma exacta que valida `rayd`: las cuatro claves en snake_case. */
export interface LifecycleWire {
  readonly auto_resume: boolean;
  readonly cap_s: number;
  readonly on_timeout: OnTimeout;
  readonly timeout_s: number;
}

export function lifecycleBlockToWire(block: LifecycleBlock): LifecycleWire {
  return {
    auto_resume: block.autoResume,
    cap_s: block.capS,
    on_timeout: block.onTimeout,
    timeout_s: block.timeoutS,
  };
}

/** Lo que `create()` manda a `run-microvm` para el ciclo de vida del sandbox. */
export interface LifecyclePlan {
  readonly block: LifecycleBlock | undefined;
  readonly platformDurationSeconds: number;
  readonly idle: IdlePolicy | undefined;
}

export interface LifecycleInput {
  /** El plazo lógico ya validado y redondeado hacia arriba a segundos. */
  readonly timeoutSeconds: number;
  readonly maxLifetimeMs: number | undefined;
  readonly onTimeout: unknown;
  readonly idle: IdlePolicyInput | null | undefined;
}

/** Rellena `suspendedDurationSeconds` con `durationSeconds − maxIdleSeconds`. */
export function resolveIdlePolicy(
  idle: IdlePolicyInput | null | undefined,
  durationSeconds: number,
): IdlePolicy | undefined {
  if (idle === null) {
    return undefined;
  }
  const validated = validateIdlePolicy(idle ?? {});
  if (validated.maxIdleSeconds >= durationSeconds) {
    throw new InvalidArgumentError(
      `idle.maxIdleSeconds=${validated.maxIdleSeconds} debe ser menor que el timeout de ${durationSeconds} s`,
    );
  }
  if (validated.suspendedDurationSeconds !== undefined) {
    return validated;
  }
  return Object.freeze({
    maxIdleSeconds: validated.maxIdleSeconds,
    suspendedDurationSeconds: durationSeconds - validated.maxIdleSeconds,
    autoResume: validated.autoResume,
  });
}

/**
 * Sin `maxLifetimeMs` ni `onTimeout`: ningún bloque, `maximumDurationInSeconds
 * = timeout` y la idle de siempre (ADR-007). Con cualquiera de los dos:
 * `onTimeout` por defecto `"kill"`, el tope es `maxLifetimeMs` y el bloque
 * viaja. En `"pause"` la idle de la plataforma siempre auto-reanuda (la
 * suspensión por idle es la que pausa sin cliente) y el `autoResume` lógico
 * es el de `idle`; en `"kill"` la idle se resuelve contra el tope y el
 * `autoResume` lógico es `false`.
 */
export function resolveLifecycle(input: LifecycleInput): LifecyclePlan {
  if (input.maxLifetimeMs === undefined && input.onTimeout === undefined) {
    return Object.freeze({
      block: undefined,
      platformDurationSeconds: input.timeoutSeconds,
      idle: resolveIdlePolicy(input.idle, input.timeoutSeconds),
    });
  }
  const onTimeout = validateOnTimeout(input.onTimeout ?? "kill");
  const capS = resolveMaxLifetimeSeconds(input);
  if (onTimeout === "pause") {
    return pausePlan(input.timeoutSeconds, capS, input.idle);
  }
  return Object.freeze({
    block: Object.freeze({
      timeoutS: input.timeoutSeconds,
      capS,
      onTimeout,
      autoResume: false,
    }),
    platformDurationSeconds: capS,
    idle: resolveIdlePolicy(input.idle, capS),
  });
}

export function validateOnTimeout(onTimeout: unknown): OnTimeout {
  if (onTimeout === "kill" || onTimeout === "pause") {
    return onTimeout;
  }
  throw new InvalidArgumentError(
    `onTimeout debe ser 'kill' o 'pause', recibido ${JSON.stringify(onTimeout)}`,
  );
}

function resolveMaxLifetimeSeconds(input: LifecycleInput): number {
  const maxLifetimeMs = validateMaxLifetimeMs(
    input.maxLifetimeMs ?? defaultMaxLifetimeMs(input.timeoutSeconds),
  );
  if (input.timeoutSeconds * 1000 > maxLifetimeMs) {
    throw new InvalidArgumentError(
      `timeoutMs (${input.timeoutSeconds * 1000}) no puede superar maxLifetimeMs (${maxLifetimeMs})`,
    );
  }
  return maxLifetimeMs / 1000;
}

/** `min(max(timeout + 60 s, 120 s), 28 800 s)`: 60 s de margen para el `/run` más un minuto de vida útil. */
export function defaultMaxLifetimeMs(timeoutSeconds: number): number {
  const withMargin = timeoutSeconds * 1000 + CAP_MARGIN_MS;
  return Math.min(Math.max(withMargin, MIN_MAX_LIFETIME_MS), MAX_LIFETIME_MS);
}

export function validateMaxLifetimeMs(maxLifetimeMs: unknown): number {
  if (typeof maxLifetimeMs !== "number" || !Number.isInteger(maxLifetimeMs)) {
    throw new InvalidArgumentError(
      `maxLifetimeMs debe ser un entero en milisegundos, recibido ${String(maxLifetimeMs)}`,
    );
  }
  if (maxLifetimeMs > MAX_LIFETIME_MS) {
    throw new SandboxLifetimeError(
      `maxLifetimeMs=${maxLifetimeMs} supera el tope de ${MAX_DURATION_SECONDS} s ` +
        `(${MAX_LIFETIME_MS} ms, running + suspendido): la vida de un MicroVM no se puede ` +
        "extender después; reincarnate() sigue con los ficheros en un sandbox nuevo",
    );
  }
  if (maxLifetimeMs < MIN_MAX_LIFETIME_MS) {
    throw new InvalidArgumentError(
      `maxLifetimeMs debe ser >= ${MIN_MAX_LIFETIME_MS} (60 s de margen del /run más 60 s ` +
        `de vida), recibido ${maxLifetimeMs}`,
    );
  }
  if (maxLifetimeMs % 1000 !== 0) {
    throw new InvalidArgumentError(
      `maxLifetimeMs debe ser un múltiplo de 1000 (maximumDurationInSeconds), recibido ${maxLifetimeMs}`,
    );
  }
  return maxLifetimeMs;
}

function pausePlan(
  timeoutSeconds: number,
  capS: number,
  idle: IdlePolicyInput | null | undefined,
): LifecyclePlan {
  if (idle === null) {
    throw new InvalidArgumentError(
      "onTimeout 'pause' necesita idle (p. ej. { maxIdleSeconds: 300, autoResume: true }): " +
        "sin cliente, la suspensión la hace la política de idle de la plataforma",
    );
  }
  const validated = validateIdlePolicy(idle ?? {});
  if (validated.suspendedDurationSeconds !== undefined) {
    throw new InvalidArgumentError(
      "con onTimeout 'pause' no se pasa idle.suspendedDurationSeconds: vale siempre " +
        "maxLifetime − maxIdleSeconds",
    );
  }
  if (validated.maxIdleSeconds >= capS) {
    throw new InvalidArgumentError(
      `idle.maxIdleSeconds=${validated.maxIdleSeconds} debe ser menor que maxLifetime de ${capS} s`,
    );
  }
  return Object.freeze({
    block: Object.freeze({
      timeoutS: timeoutSeconds,
      capS,
      onTimeout: "pause" as const,
      autoResume: validated.autoResume,
    }),
    platformDurationSeconds: capS,
    idle: Object.freeze({
      maxIdleSeconds: validated.maxIdleSeconds,
      suspendedDurationSeconds: capS - validated.maxIdleSeconds,
      autoResume: true,
    }),
  });
}

// ------------------------------------------------------------------ reading

/** `undefined` cuando `Health` no trae `lifecycle`: la puerta de un agente anterior a M9. */
export function lifecycleFromProto(
  state: LifecycleState | undefined,
): SandboxLifecycle | undefined {
  if (state === undefined) {
    return undefined;
  }
  return Object.freeze({
    phase: phaseName(state.phase),
    deadline: wallInstant(state.deadlineUnixMs),
    cap: wallInstant(state.capUnixMs),
    timeoutMs: Number(state.timeoutMs),
    onTimeout: onTimeoutName(state.onTimeout),
    autoResume: state.autoResume,
    extensions: state.extensions,
  });
}

function phaseName(phase: LifecyclePhase): LifecyclePhaseName {
  switch (phase) {
    case LifecyclePhase.ACTIVE:
      return "active";
    case LifecyclePhase.RESUME_GRACE:
      return "resumeGrace";
    case LifecyclePhase.EXPIRED:
      return "expired";
    default:
      return "unmanaged";
  }
}

function onTimeoutName(action: TimeoutAction): OnTimeout | undefined {
  switch (action) {
    case TimeoutAction.KILL:
      return "kill";
    case TimeoutAction.PAUSE:
      return "pause";
    default:
      return undefined;
  }
}

function wallInstant(unixMs: bigint): Date | undefined {
  return unixMs > 0n ? new Date(Number(unixMs)) : undefined;
}

// ------------------------------------------------------------ moving it

/** `timeoutMs` de `setTimeout`/`connect`: entero, >= 1000; por encima de 28 800 000 nunca cabe bajo el tope. */
export function validateSetTimeoutMs(timeoutMs: unknown): number {
  if (typeof timeoutMs !== "number" || !Number.isInteger(timeoutMs)) {
    throw new InvalidArgumentError(
      `timeoutMs debe ser un entero en milisegundos, recibido ${String(timeoutMs)}`,
    );
  }
  if (timeoutMs < MIN_SET_TIMEOUT_MS) {
    throw new InvalidArgumentError(
      `timeoutMs debe ser >= ${MIN_SET_TIMEOUT_MS}, recibido ${timeoutMs}`,
    );
  }
  if (timeoutMs > MAX_LIFETIME_MS) {
    throw beyondCapError(timeoutMs, undefined);
  }
  return timeoutMs;
}

/** `timeoutMs` opcional de `connect()`: se valida antes de tocar AWS. */
export function optionalSetTimeoutMs(timeoutMs: unknown): number | undefined {
  return timeoutMs === undefined ? undefined : validateSetTimeoutMs(timeoutMs);
}

export function setTimeoutRequest(mode: TimeoutMode, timeoutMs: number): SetTimeoutRequest {
  return create(SetTimeoutRequestSchema, { timeoutMs: BigInt(timeoutMs), mode });
}

/**
 * `Sandbox.setTimeout(sandboxId)` nunca despierta un sandbox suspendido: el
 * propio `SetTimeout` sería la petición que lo reanuda.
 */
export function suspendedSetTimeoutError(sandboxId: string): SandboxStateError {
  return new SandboxStateError(
    `el sandbox ${sandboxId} está suspendido: connect() lo reanuda y acepta timeoutMs`,
  );
}

/**
 * El `SetTimeout(AT_LEAST)` que manda `connect()` tras la readiness, o
 * `undefined` si no manda ninguno. Con `requestedMs` extiende (nunca acorta);
 * sin él sólo reabre un sandbox `resumeGrace`/`expired` con su propio
 * timeout, recortado para que quepa bajo el tope. Un agente anterior a M9 o
 * un sandbox `unmanaged` no admiten `requestedMs`.
 */
export function connectExtension(
  lifecycle: SandboxLifecycle | undefined,
  requestedMs: number | undefined,
  nowUnixMs: number,
): number | undefined {
  const requested = requestedMs === undefined ? undefined : validateSetTimeoutMs(requestedMs);
  if (lifecycle === undefined) {
    if (requested !== undefined) {
      throw new LifecycleUnsupportedError(
        "el agente del sandbox no impone el timeout del servidor (imagen anterior a M9): " +
          "connect({ timeoutMs }) no puede extender nada; publica una imagen M9",
      );
    }
    return undefined;
  }
  if (lifecycle.phase === "unmanaged") {
    if (requested !== undefined) {
      throw unmanagedLifecycleError();
    }
    return undefined;
  }
  if (requested !== undefined) {
    return requested;
  }
  if (lifecycle.phase === "active") {
    return undefined;
  }
  return reopenTimeoutMs(lifecycle, nowUnixMs);
}

function reopenTimeoutMs(lifecycle: SandboxLifecycle, nowUnixMs: number): number {
  const capMs = lifecycle.cap?.getTime() ?? 0;
  const timeoutMs = Math.floor(
    Math.min(lifecycle.timeoutMs, capMs - nowUnixMs - REOPEN_CAP_SLACK_MS),
  );
  if (timeoutMs < MIN_SET_TIMEOUT_MS) {
    throw new SandboxLifetimeError(
      `el sandbox llegó al tope de su maxLifetimeMs (${isoOrUnknown(lifecycle.cap)}): no ` +
        "queda vida que reabrir; usa reincarnate() para seguir con los ficheros en un sandbox nuevo",
    );
  }
  return timeoutMs;
}

/**
 * `setTimeout` más allá del tope: el mensaje nombra `maxLifetimeMs`, las
 * 28 800 s y `reincarnate()`, que es lo único que llega más lejos.
 */
export function beyondCapError(
  timeoutMs: number,
  capUnixMs: number | undefined,
  options: SandboxErrorOptions = {},
): InvalidArgumentError {
  const cap = capUnixMs === undefined ? undefined : new Date(capUnixMs);
  return new InvalidArgumentError(
    `setTimeout(${timeoutMs} ms) supera el maxLifetimeMs del sandbox (tope a las ` +
      `${isoOrUnknown(cap)}; maxLifetimeMs se fija en create() y como mucho vale ` +
      `${MAX_DURATION_SECONDS} s): la vida de la plataforma no se puede extender (no existe ` +
      "UpdateMicrovm); usa reincarnate() para seguir con los ficheros en un sandbox nuevo",
    options,
  );
}

/** `setTimeout`/`connect({ timeoutMs })` sobre un sandbox creado sin plazo lógico. */
export function unmanagedLifecycleError(options: SandboxErrorOptions = {}): InvalidArgumentError {
  return new InvalidArgumentError(
    "el sandbox se creó sin maxLifetimeMs ni onTimeout: su vida es la de la plataforma " +
      "(maximumDurationInSeconds, fija desde create()) y no tiene un plazo movible; " +
      "crea el sandbox con maxLifetimeMs u onTimeout",
    options,
  );
}

/** `SetTimeout` respondió `UNIMPLEMENTED`: el agente es anterior a M9. */
export function setTimeoutUnsupportedError(
  options: SandboxErrorOptions = {},
): LifecycleUnsupportedError {
  return new LifecycleUnsupportedError(
    "el agente del sandbox no implementa SetTimeout (imagen anterior a M9): publica una imagen M9",
    options,
  );
}

/** La puerta de `create()`: se pidió un plazo lógico y `Health` no trae `lifecycle`. */
export function olderAgentError(template: string, agentVersion: string): LifecycleUnsupportedError {
  return new LifecycleUnsupportedError(
    `la imagen ${template} (agentVersion ${agentVersion}) no impone el timeout del servidor: ` +
      "publica una imagen M9 o crea el sandbox sin maxLifetimeMs ni onTimeout",
  );
}

/**
 * Si `getInfo()` debe releer `Health` (el `deadline_may_have_moved` de
 * Python): sólo un sandbox `RUNNING` con plazo lógico gestionado, porque otro
 * cliente o `rayd` al reanudarse pueden haberlo movido. Los metadatos y los
 * hechos del guest quedan fijos en `/run`, y cada `Health` es tráfico de
 * entrada que reinicia el contador de idle de la plataforma: sondear
 * `getInfo()` sin plazo gestionado nunca debe impedir la auto-suspensión.
 */
export function deadlineMayHaveMoved(
  state: string,
  lifecycle: SandboxLifecycle | undefined,
): boolean {
  return state === "RUNNING" && lifecycle !== undefined && lifecycle.phase !== "unmanaged";
}

/**
 * Cuándo debe mirar el SDK si suspender un sandbox `pause`: al plazo + 1 s
 * cuando está `active`, ya cuando está `expired`; nunca en `kill`,
 * `unmanaged` ni `resumeGrace` (ahí manda `rayd`).
 */
export function pauseTriggerDelayMs(
  lifecycle: SandboxLifecycle | undefined,
  nowUnixMs: number,
): number | undefined {
  if (lifecycle?.onTimeout !== "pause") {
    return undefined;
  }
  if (lifecycle.phase === "expired") {
    return 0;
  }
  if (lifecycle.phase !== "active" || lifecycle.deadline === undefined) {
    return undefined;
  }
  return Math.max(0, lifecycle.deadline.getTime() - nowUnixMs) + PAUSE_TRIGGER_SLACK_MS;
}

/** Lo que `autoResumeReopenMs` necesita saber de la suspensión por el plazo. */
export interface DeadlinePauseResume {
  /** `resumeGeneration` cuando este cliente suspendió el sandbox por su plazo. */
  readonly pausedGeneration: number | undefined;
  /** `resumeGeneration` que reporta ahora `Health`. */
  readonly generation: number;
  readonly nowUnixMs: number;
}

/**
 * El `SetTimeout(EXACT)` que reabre un sandbox `pause` con `autoResume` que
 * este cliente suspendió por su plazo y que ya se reanudó (la generación
 * avanzó) pero sigue `expired`. `rayd` sólo aplica la regla de E2B
 * (`max(timeout, 5 min)`, acotada al tope) si su vigilante vio una
 * congelación de al menos 2 s; tras una suspensión real más corta el SDK la
 * aplica con el token de acceso, acotada al tope menos 5 s. `undefined` si
 * no toca o si no queda ni 1 s hasta el tope.
 */
export function autoResumeReopenMs(
  lifecycle: SandboxLifecycle | undefined,
  resume: DeadlinePauseResume,
): number | undefined {
  if (resume.pausedGeneration === undefined || resume.generation <= resume.pausedGeneration) {
    return undefined;
  }
  if (lifecycle?.phase !== "expired") {
    return undefined;
  }
  if (lifecycle.onTimeout !== "pause" || !lifecycle.autoResume) {
    return undefined;
  }
  const capMs = lifecycle.cap?.getTime() ?? 0;
  const timeoutMs = Math.floor(
    Math.min(
      Math.max(lifecycle.timeoutMs, LIFECYCLE_AUTO_RESUME_MIN_SECONDS * 1000),
      capMs - resume.nowUnixMs - REOPEN_CAP_SLACK_MS,
    ),
  );
  return timeoutMs < MIN_SET_TIMEOUT_MS ? undefined : timeoutMs;
}

/** `cap_unix_ms=<n>` del detalle de `INVALID_ARGUMENT "timeout beyond cap; cap_unix_ms=<n>"`. */
export function capFromDetail(detail: string): number | undefined {
  const match = /cap_unix_ms=(-?\d+)/.exec(detail);
  return match === null ? undefined : Number(match[1]);
}

function isoOrUnknown(instant: Date | undefined): string {
  return instant === undefined ? "desconocido" : instant.toISOString();
}
