/**
 * Piezas compartidas por los servicios del `rayd` falso: verificación de las
 * cabeceras del proxy y del `x-access-token` (como `rayd`: sha256 de los
 * bytes decodificados), colas asíncronas para los suscriptores de un stream y
 * el ring con `seq` que reproduce `Connect(from_seq)`.
 */

import { createHash } from "node:crypto";
import { Code, ConnectError, type HandlerContext } from "@connectrpc/connect";
import { InvalidArgumentError } from "../../../src/errors.js";
import { buildRunHookPayload, decodeAccessToken } from "../../../src/payload.js";
import {
  ACCESS_TOKEN_HEADER,
  PROXY_AUTH_HEADER,
  PROXY_FORCE_H2_HEADER,
  PROXY_PORT_HEADER,
} from "../../../src/transport/headers.js";

export const STEP_MS = 20;
export const KNOWN_DIRECTORIES: ReadonlySet<string> = new Set(["/", "/tmp", "/home/user"]);
export const DEFAULT_USERNAME = "user";
export const DEFAULT_CWD = "/home/user";

export type HeaderMap = Readonly<Record<string, string>>;

/** Lo que `rayd` instala desde `runHookPayload`: el campo `token_sha256`. */
export function installedTokenSha256(accessToken: string): string {
  const payload = JSON.parse(buildRunHookPayload({ accessToken })) as { token_sha256: string };
  return payload.token_sha256;
}

/** `sha256(base64url_decode(x-access-token))`, como `rayd`; `undefined` si falta o no decodifica. */
export function presentedTokenSha256(headers: HeaderMap): string | undefined {
  const presented = headers[ACCESS_TOKEN_HEADER];
  if (presented === undefined) {
    return undefined;
  }
  try {
    return createHash("sha256").update(decodeAccessToken(presented)).digest("hex");
  } catch (error) {
    if (error instanceof InvalidArgumentError) {
      return undefined;
    }
    throw error;
  }
}

export function headerMap(context: HandlerContext): HeaderMap {
  const map: Record<string, string> = {};
  context.requestHeader.forEach((value, key) => {
    map[key.toLowerCase()] = value;
  });
  return map;
}

/** Las cuatro cabeceras del proxy en cada request (el proxy real las exige). */
export function assertProxyHeaders(headers: HeaderMap): void {
  const problems: string[] = [];
  if (!headers[PROXY_AUTH_HEADER]) {
    problems.push(`${PROXY_AUTH_HEADER} ausente`);
  }
  if (headers[PROXY_PORT_HEADER] !== "8080") {
    problems.push(`${PROXY_PORT_HEADER}=${headers[PROXY_PORT_HEADER] ?? "ausente"}`);
  }
  if (headers[PROXY_FORCE_H2_HEADER] !== "true") {
    problems.push(`${PROXY_FORCE_H2_HEADER}=${headers[PROXY_FORCE_H2_HEADER] ?? "ausente"}`);
  }
  if (problems.length > 0) {
    throw new ConnectError(`cabeceras del proxy: ${problems.join(", ")}`, Code.Internal);
  }
}

const GRPC_TIMEOUT_UNITS: Readonly<Record<string, number>> = {
  H: 3_600_000,
  M: 60_000,
  S: 1000,
  m: 1,
  u: 0.001,
  n: 0.000001,
};

/** El deadline que envió el cliente, leído de `grpc-timeout` (no del reloj del servidor). */
export function deadlineFromHeaders(headers: HeaderMap): number | undefined {
  const raw = headers["grpc-timeout"];
  if (raw === undefined) {
    return undefined;
  }
  const match = /^(\d+)([HMSmun])$/.exec(raw);
  if (match === null) {
    return undefined;
  }
  return Number(match[1]) * (GRPC_TIMEOUT_UNITS[match[2] as string] ?? 1);
}

export function requireAccessToken(headers: HeaderMap, tokenSha256: string): void {
  if (presentedTokenSha256(headers) !== tokenSha256) {
    throw new ConnectError("x-access-token ausente o inválido", Code.Unauthenticated);
  }
}

export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Un `Event` de asyncio: `wait(ms)` resuelve `true` si se disparó antes del plazo. */
export class Signal {
  #fired = false;
  #waiters: Array<() => void> = [];

  get isSet(): boolean {
    return this.#fired;
  }

  set(): void {
    this.#fired = true;
    const waiters = this.#waiters;
    this.#waiters = [];
    for (const waiter of waiters) {
      waiter();
    }
  }

  wait(ms: number): Promise<boolean> {
    if (this.#fired) {
      return Promise.resolve(true);
    }
    return new Promise((resolve) => {
      const timer = setTimeout(() => resolve(false), ms);
      this.#waiters.push(() => {
        clearTimeout(timer);
        resolve(true);
      });
    });
  }
}

/** Cola asíncrona de un suscriptor: `next(signal)` devuelve `undefined` cuando el cliente abortó. */
export class AsyncQueue<T> {
  readonly #items: T[] = [];
  #waiter: ((item: T | undefined) => void) | undefined;

  push(item: T): void {
    if (this.#waiter !== undefined) {
      const waiter = this.#waiter;
      this.#waiter = undefined;
      waiter(item);
      return;
    }
    this.#items.push(item);
  }

  get size(): number {
    return this.#items.length;
  }

  next(signal: AbortSignal): Promise<T | undefined> {
    const queued = this.#items.shift();
    if (queued !== undefined) {
      return Promise.resolve(queued);
    }
    if (signal.aborted) {
      return Promise.resolve(undefined);
    }
    return new Promise((resolve) => {
      const onAbort = () => {
        this.#waiter = undefined;
        resolve(undefined);
      };
      signal.addEventListener("abort", onAbort, { once: true });
      this.#waiter = (item) => {
        signal.removeEventListener("abort", onAbort);
        resolve(item);
      };
    });
  }
}

export class ReplayOutOfRange extends Error {
  readonly oldest: number;
  readonly nextSeq: number;

  constructor(oldest: number, nextSeq: number) {
    super(`from_seq fuera de rango: oldest=${oldest} next=${nextSeq}`);
    this.oldest = oldest;
    this.nextSeq = nextSeq;
  }
}

/** Un `StreamEnd` en la cola de un suscriptor aborta su stream con ese status. */
export class StreamEnd {
  readonly code: Code;
  readonly message: string;

  constructor(code: Code, message: string) {
    this.code = code;
    this.message = message;
  }
}

export function abortWith(item: StreamEnd): never {
  throw new ConnectError(item.message, item.code);
}

/** `record[key].push(item)` creando la lista la primera vez. */
export function record<T>(store: Record<string, T[]>, key: string, item: T): void {
  const list = store[key];
  if (list === undefined) {
    store[key] = [item];
    return;
  }
  list.push(item);
}

export function chunks(payload: Uint8Array, size: number): Uint8Array[] {
  const parts: Uint8Array[] = [];
  for (let offset = 0; offset < payload.length; offset += size) {
    parts.push(payload.subarray(offset, offset + size));
  }
  return parts;
}

export const encoder = new TextEncoder();

export function bytes(text: string): Uint8Array {
  return encoder.encode(text);
}
