/**
 * Clasificación y traducción de los `ConnectError` que produce Connect-ES.
 *
 * Connect presenta una respuesta HTTP sin `grpc-status` como
 * `ConnectError("HTTP <status>", codeFromHttpStatus(status))`: 403 →
 * `PermissionDenied` (el proxy rechazó el JWE), 429/502/503/504 →
 * `Unavailable` (proxy o VM congelado). Un `RST_STREAM` o una sesión HTTP/2
 * caída llegan como `Aborted`, `Canceled` o `Internal` con `rawMessage`
 * `http/2 stream closed with error code …` o una causa Node (`ECONNRESET`,
 * `ERR_HTTP2_*`). La tabla de aquí es la verdad del contrato de reconexión.
 */

import { Code, ConnectError } from "@connectrpc/connect";
import {
  AuthenticationError,
  CapacityError,
  FileNotFoundError,
  InvalidArgumentError,
  NotFoundError,
  QuotaExceededError,
  RateLimitError,
  SandboxError,
  SandboxStateError,
  TimeoutError,
} from "../errors.js";

export const PROXY_FORBIDDEN_MESSAGE = "HTTP 403";
export const PHASE_GATE_DETAILS: ReadonlySet<string> = new Set(["suspending", "terminating"]);
export const KERNEL_GATE_PREFIX = "kernel not ready";
export const H2_CLOSED_PREFIX = "http/2 stream closed";
export const MISSING_STATUS_MESSAGE = "protocol error: missing status";
const RESET_NODE_CODES: ReadonlySet<string> = new Set(["ECONNRESET", "EPIPE"]);

export function asConnectError(error: unknown): ConnectError | undefined {
  return error instanceof ConnectError ? error : undefined;
}

/** Un 403 del proxy no lleva `grpc-status`: Connect lo presenta como `PermissionDenied "HTTP 403"`. */
export function isProxyForbidden(error: unknown): boolean {
  const connect = asConnectError(error);
  return (
    connect !== undefined &&
    connect.code === Code.PermissionDenied &&
    connect.rawMessage === PROXY_FORBIDDEN_MESSAGE
  );
}

/**
 * Lo que un sondeo de `Health` (arranque o reconexión, D14) trata como "aún
 * no": 502/503 del proxy y el kernel gate (`Unavailable`), el timeout del
 * propio sondeo (`DeadlineExceeded`) y cualquier corte por debajo de gRPC
 * (`Aborted`, `Canceled`/`Internal` con las marcas de reset HTTP/2). grpc-core
 * presenta todos esos cortes como `UNAVAILABLE`; Connect-ES los reparte en
 * varios `Code`, y el sondeo no debe abortar por uno de ellos.
 */
export function isNotYetReachable(error: unknown): boolean {
  const connect = asConnectError(error);
  return (
    connect !== undefined &&
    (connect.code === Code.Unavailable ||
      connect.code === Code.DeadlineExceeded ||
      isStreamReset(connect))
  );
}

/**
 * `rayd` responde `Unavailable` con `suspending`/`terminating` mientras
 * cambia de fase: el agente vive, no hay nada que sondear ni pid al que
 * reengancharse.
 */
export function isPhaseGate(error: unknown): boolean {
  const connect = asConnectError(error);
  return (
    connect !== undefined &&
    connect.code === Code.Unavailable &&
    PHASE_GATE_DETAILS.has(connect.rawMessage)
  );
}

/** `rayd` responde `Unavailable` con `kernel not ready: <motivo>` mientras el sidecar arranca. */
export function isKernelGate(error: unknown): boolean {
  const connect = asConnectError(error);
  return (
    connect !== undefined &&
    connect.code === Code.Unavailable &&
    connect.rawMessage.startsWith(KERNEL_GATE_PREFIX)
  );
}

/** Un `Canceled` cuyo origen es nuestro propio `AbortSignal`, no un `RST_STREAM` del proxy. */
export function isOwnAbort(error: unknown): boolean {
  const connect = asConnectError(error);
  return (
    connect !== undefined &&
    connect.code === Code.Canceled &&
    !connect.rawMessage.startsWith(H2_CLOSED_PREFIX)
  );
}

function causeChainHasNodeResetCode(error: ConnectError): boolean {
  let current: unknown = error.cause;
  for (let depth = 0; depth < 8 && typeof current === "object" && current !== null; depth += 1) {
    const code = (current as { code?: unknown }).code;
    if (typeof code === "string" && (code.startsWith("ERR_HTTP2_") || RESET_NODE_CODES.has(code))) {
      return true;
    }
    current = (current as { cause?: unknown }).cause;
  }
  return false;
}

/**
 * Un stream cortado por debajo de gRPC: `Unavailable` que no es ninguna de
 * las dos puertas de `rayd` (proxy 429/502/503/504, `REFUSED_STREAM`,
 * `ECONNREFUSED`, `ETIMEDOUT`), `Aborted` (`ECONNRESET`, stream destruido),
 * `Canceled` por un `RST_STREAM CANCEL` del proxy, o `Internal` con las marcas
 * de reset HTTP/2. Se clasifica sondeando `Health`.
 */
export function isStreamReset(error: unknown): boolean {
  const connect = asConnectError(error);
  if (connect === undefined) {
    return false;
  }
  switch (connect.code) {
    case Code.Unavailable:
      return !(isPhaseGate(connect) || isKernelGate(connect));
    case Code.Aborted:
      return true;
    case Code.Canceled:
      return connect.rawMessage.startsWith(H2_CLOSED_PREFIX);
    case Code.Internal:
      return (
        connect.rawMessage.startsWith(H2_CLOSED_PREFIX) ||
        connect.rawMessage === MISSING_STATUS_MESSAGE ||
        causeChainHasNodeResetCode(connect)
      );
    default:
      return false;
  }
}

/**
 * Lo que dispara el contrato de reconexión: un reset por debajo de gRPC o el
 * phase gate `suspending`/`terminating`. Nunca `DeadlineExceeded` (el caller
 * eligió ese plazo), nunca el kernel gate (el agente vive), nunca un 403 del
 * proxy (se reacuña) ni un `Canceled` propio.
 */
export function isReconnectable(error: unknown): boolean {
  return isStreamReset(error) || isPhaseGate(error);
}

export interface TranslateOptions {
  readonly filesystem?: boolean | undefined;
}

/**
 * Tabla unaria. `Canceled` sólo lo produce el propio cliente (cerrar el
 * stream), nunca un timeout, por eso no es `TimeoutError`.
 */
export function translateRpcError(error: unknown, options: TranslateOptions = {}): Error {
  const connect = asConnectError(error);
  if (connect === undefined) {
    return error instanceof Error ? error : new SandboxError(String(error));
  }
  const own = ownErrorInCause(connect);
  if (own !== undefined) {
    return own;
  }
  const code = connect.code;
  const message = connect.rawMessage;
  const base = { grpcCode: code, cause: connect };
  if (isPhaseGate(connect)) {
    return new SandboxStateError(
      `el sandbox está ${message}; reintenta cuando vuelva a RUNNING`,
      base,
    );
  }
  if (isKernelGate(connect)) {
    return new SandboxError(`el kernel no está listo: ${message}`, base);
  }
  switch (code) {
    case Code.InvalidArgument:
    case Code.FailedPrecondition:
    case Code.Unimplemented:
      return new InvalidArgumentError(message, base);
    case Code.Unauthenticated:
      return new AuthenticationError(message, { grpcCode: code, cause: connect });
    case Code.PermissionDenied:
      return new AuthenticationError(message, {
        grpcCode: code,
        proxyRejected: isProxyForbidden(connect),
        cause: connect,
      });
    case Code.NotFound:
      return options.filesystem
        ? new FileNotFoundError(message, base)
        : new NotFoundError(message, base);
    case Code.OutOfRange:
      return new NotFoundError(message, base);
    case Code.ResourceExhausted:
      return new RateLimitError(message, base);
    case Code.DeadlineExceeded:
      return new TimeoutError(message, base);
    case Code.Canceled:
      return new SandboxError(`llamada cancelada por el cliente: ${message}`, base);
    default:
      return new SandboxError(message, base);
  }
}

/** Connect envuelve lo que lanza un interceptor en `ConnectError(Unknown)`; nuestros errores salen tal cual. */
function ownErrorInCause(error: ConnectError): Error | undefined {
  const cause = error.cause;
  if (
    cause instanceof SandboxError ||
    cause instanceof AuthenticationError ||
    cause instanceof QuotaExceededError ||
    cause instanceof CapacityError
  ) {
    return cause;
  }
  return undefined;
}

/** Mapa de `StreamError.code` (conjunto cerrado de `common.proto`). */
export function translateStreamError(
  code: string,
  message: string,
  options: TranslateOptions = {},
): Error {
  switch (code) {
    case "not_found":
      return options.filesystem ? new FileNotFoundError(message) : new NotFoundError(message);
    case "permission_denied":
      return new AuthenticationError(message);
    case "deadline_exceeded":
      return new TimeoutError(message);
    case "unimplemented":
    case "invalid_argument":
      return new InvalidArgumentError(message);
    case "suspending":
      return new SandboxStateError(message);
    case "output_truncated":
      return new SandboxError(`output_truncated: ${message}`);
    default:
      return new SandboxError(`${code}: ${message}`);
  }
}
