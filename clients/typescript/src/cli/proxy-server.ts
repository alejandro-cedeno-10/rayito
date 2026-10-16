/**
 * El servidor de `rayito sandbox proxy` (ADR-027, fase 4): un proxy TCP
 * local, de un solo puerto, hacia el endpoint de un sandbox. Espejo del
 * servidor de `rayito.cli._proxy`. Sin coste de AWS más allá de `GetMicrovm`
 * y `CreateMicrovmAuthToken` (gratuitos): no hay ningún recurso nuevo ni
 * servidor propio de Rayito, y el JWE se acuña con el mismo `TokenRefresher`
 * del SDK (renovado a los 45 min, nunca por conexión).
 *
 * Contrato (AWS_API_NOTES.md §7):
 *
 * - El listener habla HTTP/1.1: por conexión lee sólo la cabecera de la
 *   petición (tope `PROXY_HEAD_MAX_BYTES`), la valida como HTTP/1.1
 *   estricto (si no lo es, `400` y cierra sin reenviar nada), comprueba
 *   `Host` (`421`) y `Origin` (`403`) contra `ProxyAccess`, quita cualquier
 *   `x-aws-proxy-*` del cliente, fija `Host: <endpoint>`, añade
 *   `X-aws-proxy-auth` y `X-aws-proxy-port` y fuerza `Connection: close`
 *   salvo en una petición de upgrade (RFC 9110 §7.8).
 * - **Un solo mensaje por conexión.** Sólo la primera petición de cada
 *   conexión se reescribe, así que sólo esa llega al upstream: su cuerpo se
 *   reenvía exactamente según `Content-Length` o el troceado `chunked`
 *   (una delimitación ambigua es `400`) y nada de lo que el cliente mande
 *   después (pipelining) se reenvía. En una petición de upgrade el proxy
 *   lee la cabecera de la respuesta: con `101` empieza la tubería en los dos
 *   sentidos; con cualquier otra respuesta final la reenvía con
 *   `Connection: close`, con su cuerpo delimitado, y cierra.
 * - Tiempos acotados (`limits.json`): la cabecera del cliente, la conexión
 *   TLS al endpoint y la cabecera de la respuesta a un upgrade. Sin JWE
 *   vigente o si la conexión al upstream falla o vence, `502` +
 *   `Connection: close` y una línea en stderr sin JWE, cabeceras ni ruta.
 * - Como mucho `--max-connections` conexiones reenviadas a la vez; la
 *   siguiente recibe `503` sin abrir el upstream.
 * - Nunca registra el JWE, las cabeceras, los cuerpos ni las rutas.
 */

import type { Duplex } from "node:stream";
import { type ConnectionOptions, connect as tlsConnect } from "node:tls";
import {
  ENDPOINT_TLS_PORT,
  PROXY_HEAD_MAX_BYTES,
  PROXY_HEAD_READ_TIMEOUT_SECONDS,
  PROXY_UPSTREAM_CONNECT_TIMEOUT_SECONDS,
  PROXY_UPSTREAM_RESPONSE_HEAD_TIMEOUT_SECONDS,
} from "../limits.js";
import { ALLOW_ORIGIN_OPTION, ALLOWED_HOST_OPTION, type ProxyAccess } from "./access.js";
import {
  type BodyFraming,
  CONTENT_LENGTH_HEADER_NAME,
  chunkSize,
  isInformational,
  MalformedFramingError,
  responseBodyFraming,
  responseStatus,
  SWITCHING_PROTOCOLS,
  TRANSFER_ENCODING_HEADER_NAME,
} from "./http-framing.js";
import {
  closingResponseHead,
  HEADER_TERMINATOR,
  HOST_HEADER_NAME,
  type HttpHead,
  MalformedHttpHeadError,
  ORIGIN_HEADER_NAME,
  parseHttpHead,
  rewriteHead,
} from "./http-head.js";
import type { ProxyListener } from "./proxy-listener.js";
import { MAX_CONNECTIONS_OPTION, type ProxySpec } from "./proxy-settings.js";
import {
  closeSocket,
  endWriting,
  isNetworkError,
  SocketReader,
  StreamClosedError,
  StreamLimitError,
  StreamTimeoutError,
  withTimeoutSeconds,
  writeAll,
} from "./socket-io.js";

const CRLF = "\r\n";
const PIPE_CHUNK_BYTES = 64 * 1024;
const LATIN1 = "latin1";

function closingResponse(statusLine: string): string {
  return `HTTP/1.1 ${statusLine}${CRLF}Connection: close${CRLF}Content-Length: 0${CRLF}${CRLF}`;
}

export const BAD_REQUEST_RESPONSE = closingResponse("400 Bad Request");
export const BAD_GATEWAY_RESPONSE = closingResponse("502 Bad Gateway");
/** `Host` ausente o fuera de `ProxyAccess` (RFC 9110 §15.5.20). */
export const MISDIRECTED_REQUEST_RESPONSE = closingResponse("421 Misdirected Request");
/** `Origin` presente y fuera de `ProxyAccess`. */
export const FORBIDDEN_RESPONSE = closingResponse("403 Forbidden");
/** Todas las conexiones de `--max-connections` ocupadas. */
export const SERVICE_UNAVAILABLE_RESPONSE = closingResponse("503 Service Unavailable");

const AMBIGUOUS_FRAMING_REPORT =
  "delimitación del cuerpo ambigua (Transfer-Encoding/Content-Length): 400";
const NO_JWE_REPORT = "sin JWE vigente (falla la renovación con CreateMicrovmAuthToken): 502";
const CONNECT_TIMEOUT_REPORT =
  "la conexión al upstream (endpoint del sandbox) no se abrió a tiempo: 502";
const CONNECT_FAILED_REPORT = "no se pudo conectar al upstream (endpoint del sandbox): 502";
const UPGRADE_RESPONSE_REPORT =
  "el upstream no respondió al upgrade con una cabecera HTTP/1.1 válida: 502";
const UNEXPECTED_REPORT = "error inesperado en una conexión";

/** Un socket duplex ya conectado hacia el upstream: TLS al endpoint, o loopback en los tests. */
export type UpstreamConnector = () => Promise<Duplex>;
export type UpstreamConnectorFactory = (endpoint: string) => UpstreamConnector;
export type JweProvider = () => string | undefined;

/** TLS al endpoint del sandbox, con verificación del certificado y SNI = `endpoint`. */
export function tlsUpstreamConnector(endpoint: string): UpstreamConnector {
  return () =>
    new Promise<Duplex>((resolve, reject) => {
      const options: ConnectionOptions & { readonly allowHalfOpen: boolean } = {
        host: endpoint,
        port: ENDPOINT_TLS_PORT,
        servername: endpoint,
        allowHalfOpen: true,
      };
      const socket = tlsConnect(options);
      socket.once("secureConnect", () => resolve(socket));
      socket.once("error", reject);
    });
}

export interface ProxyTimeoutsSeconds {
  readonly headRead: number;
  readonly upstreamConnect: number;
  readonly upstreamResponseHead: number;
}

export const DEFAULT_PROXY_TIMEOUTS: ProxyTimeoutsSeconds = {
  headRead: PROXY_HEAD_READ_TIMEOUT_SECONDS,
  upstreamConnect: PROXY_UPSTREAM_CONNECT_TIMEOUT_SECONDS,
  upstreamResponseHead: PROXY_UPSTREAM_RESPONSE_HEAD_TIMEOUT_SECONDS,
};

/** Las conexiones reenviadas a la vez; reservar y soltar son síncronos, sin carreras. */
export class ConnectionSlots {
  #free: number;

  constructor(capacity: number) {
    this.#free = capacity;
  }

  tryAcquire(): boolean {
    if (this.#free === 0) {
      return false;
    }
    this.#free -= 1;
    return true;
  }

  release(): void {
    this.#free += 1;
  }
}

export interface ConnectionDeps {
  readonly spec: Pick<ProxySpec, "endpoint" | "port" | "access">;
  readonly jweProvider: JweProvider;
  readonly connector: UpstreamConnector;
  readonly slots: ConnectionSlots;
  /** Una línea sin JWE, cabeceras, cuerpo ni ruta: sólo el motivo fijo. */
  readonly report: (reason: string) => void;
  readonly timeouts?: ProxyTimeoutsSeconds;
}

/** Lo que corta un reenvío a medias: el otro lado cerró, un mensaje mal delimitado o un plazo vencido. */
function isRelayFailure(error: unknown): boolean {
  return (
    error instanceof StreamClosedError ||
    error instanceof StreamLimitError ||
    error instanceof StreamTimeoutError ||
    error instanceof MalformedFramingError ||
    error instanceof MalformedHttpHeadError ||
    isNetworkError(error)
  );
}

async function replyAndClose(socket: Duplex, response: string): Promise<void> {
  try {
    await writeAll(socket, response);
  } catch (error) {
    if (!isRelayFailure(error)) {
      throw error;
    }
  }
  await closeSocket(socket);
}

/** La respuesta de rechazo, o `undefined` si `Host` y `Origin` están en `access`. */
function accessRejection(
  head: HttpHead,
  access: ProxyAccess,
  report: (reason: string) => void,
): string | undefined {
  if (!access.admitsHost(head.header(HOST_HEADER_NAME))) {
    report(`Host ausente o no permitido (añádelo con ${ALLOWED_HOST_OPTION}): 421`);
    return MISDIRECTED_REQUEST_RESPONSE;
  }
  if (!access.admitsOrigin(head.header(ORIGIN_HEADER_NAME))) {
    report(`Origin no permitido (añádelo con ${ALLOW_ORIGIN_OPTION}): 403`);
    return FORBIDDEN_RESPONSE;
  }
  return undefined;
}

async function pump(reader: SocketReader, socket: Duplex): Promise<void> {
  try {
    for (;;) {
      const chunk = await reader.read(PIPE_CHUNK_BYTES);
      if (chunk.length === 0) {
        return;
      }
      await writeAll(socket, chunk);
    }
  } catch (error) {
    if (!isRelayFailure(error)) {
      throw error;
    }
  } finally {
    endWriting(socket);
  }
}

async function copyExactly(reader: SocketReader, socket: Duplex, length: number): Promise<void> {
  let remaining = length;
  while (remaining > 0) {
    const chunk = await reader.read(Math.min(remaining, PIPE_CHUNK_BYTES));
    if (chunk.length === 0) {
      throw new StreamClosedError();
    }
    await writeAll(socket, chunk);
    remaining -= chunk.length;
  }
}

async function copyUntilEof(reader: SocketReader, socket: Duplex): Promise<void> {
  for (;;) {
    const chunk = await reader.read(PIPE_CHUNK_BYTES);
    if (chunk.length === 0) {
      return;
    }
    await writeAll(socket, chunk);
  }
}

/** Un cuerpo `chunked` (RFC 9112 §7.1) tal cual, hasta el trozo de tamaño 0 y la línea vacía tras los `trailer`; ni un byte más. */
async function relayChunked(reader: SocketReader, socket: Duplex): Promise<void> {
  for (;;) {
    const sizeLine = await reader.readUntil(CRLF, PROXY_HEAD_MAX_BYTES);
    const size = chunkSize(sizeLine.toString(LATIN1));
    await writeAll(socket, sizeLine);
    if (size === 0) {
      break;
    }
    await copyExactly(reader, socket, size);
    const terminator = await reader.readExactly(CRLF.length);
    if (terminator.toString(LATIN1) !== CRLF) {
      throw new MalformedFramingError("trozo sin CRLF final");
    }
    await writeAll(socket, terminator);
  }
  for (;;) {
    const trailer = await reader.readUntil(CRLF, PROXY_HEAD_MAX_BYTES);
    await writeAll(socket, trailer);
    if (trailer.toString(LATIN1) === CRLF) {
      return;
    }
  }
}

function relayBody(reader: SocketReader, socket: Duplex, framing: BodyFraming): Promise<void> {
  switch (framing.kind) {
    case "length":
      return copyExactly(reader, socket, framing.length);
    case "chunked":
      return relayChunked(reader, socket);
    case "until-close":
      return copyUntilEof(reader, socket);
    case "none":
      return Promise.resolve();
  }
}

interface Peers {
  readonly client: Duplex;
  readonly clientReader: SocketReader;
  readonly upstream: Duplex;
  readonly upstreamReader: SocketReader;
}

/**
 * Respuesta a una petición de upgrade: los `1xx` informativos pasan tal
 * cual; con `101` empieza la tubería en los dos sentidos; cualquier otra
 * respuesta final se reenvía con `Connection: close` y su cuerpo delimitado,
 * y la conexión se cierra (nada más del cliente llega al upstream). Sin una
 * cabecera de respuesta válida a tiempo, `502`.
 */
async function exchangeUpgrade(
  head: HttpHead,
  requestBody: Promise<void>,
  peers: Peers,
  deps: ConnectionDeps,
): Promise<void> {
  const timeouts = deps.timeouts ?? DEFAULT_PROXY_TIMEOUTS;
  for (;;) {
    let raw: string;
    let response: HttpHead;
    let status: number;
    try {
      raw = (
        await withTimeoutSeconds(
          peers.upstreamReader.readUntil(HEADER_TERMINATOR, PROXY_HEAD_MAX_BYTES),
          timeouts.upstreamResponseHead,
        )
      ).toString(LATIN1);
      response = parseHttpHead(raw);
      status = responseStatus(response.requestLine);
    } catch (error) {
      if (!isRelayFailure(error)) {
        throw error;
      }
      deps.report(UPGRADE_RESPONSE_REPORT);
      await writeAll(peers.client, BAD_GATEWAY_RESPONSE);
      return;
    }
    if (status === SWITCHING_PROTOCOLS) {
      await writeAll(peers.client, raw);
      await requestBody;
      await Promise.all([
        pump(peers.clientReader, peers.upstream),
        pump(peers.upstreamReader, peers.client),
      ]);
      return;
    }
    if (isInformational(status)) {
      await writeAll(peers.client, raw);
      continue;
    }
    await writeAll(peers.client, closingResponseHead(response));
    await relayBody(
      peers.upstreamReader,
      peers.client,
      responseBodyFraming(status, {
        requestMethod: head.method,
        transferEncodings: response.headerValues(TRANSFER_ENCODING_HEADER_NAME),
        contentLengths: response.headerValues(CONTENT_LENGTH_HEADER_NAME),
      }),
    );
    return;
  }
}

async function connectUpstream(deps: ConnectionDeps): Promise<Duplex> {
  const timeouts = deps.timeouts ?? DEFAULT_PROXY_TIMEOUTS;
  const attempt = deps.connector();
  try {
    return await withTimeoutSeconds(attempt, timeouts.upstreamConnect);
  } catch (error) {
    attempt.then(
      (late) => late.destroy(),
      () => undefined,
    );
    throw error;
  }
}

/**
 * Reenvía la primera petición: la cabecera reescrita y su cuerpo exacto
 * (`framing`); después ya no lee del cliente, así que una segunda petición
 * en la misma conexión nunca llega al upstream. Sin upgrade, la respuesta
 * pasa tal cual hasta que el upstream cierra (le llegó `Connection:
 * close`); con upgrade, `exchangeUpgrade`.
 */
async function forward(
  head: HttpHead,
  framing: BodyFraming,
  client: Duplex,
  clientReader: SocketReader,
  deps: ConnectionDeps,
): Promise<void> {
  const jwe = deps.jweProvider();
  if (jwe === undefined) {
    deps.report(NO_JWE_REPORT);
    await replyAndClose(client, BAD_GATEWAY_RESPONSE);
    return;
  }
  const upstreamHead = rewriteHead(head, {
    endpoint: deps.spec.endpoint,
    jwe,
    port: deps.spec.port,
  });
  let upstream: Duplex;
  try {
    upstream = await connectUpstream(deps);
  } catch (error) {
    deps.report(
      error instanceof StreamTimeoutError ? CONNECT_TIMEOUT_REPORT : CONNECT_FAILED_REPORT,
    );
    await replyAndClose(client, BAD_GATEWAY_RESPONSE);
    return;
  }
  const peers: Peers = {
    client,
    clientReader,
    upstream,
    upstreamReader: new SocketReader(upstream),
  };
  await relayExchange(head, framing, upstreamHead, peers, deps);
}

async function relayExchange(
  head: HttpHead,
  framing: BodyFraming,
  upstreamHead: string,
  peers: Peers,
  deps: ConnectionDeps,
): Promise<void> {
  let unexpected: unknown;
  const abort = (): void => {
    peers.upstream.destroy();
    peers.client.destroy();
  };
  const trackBody = (error: unknown): void => {
    if (!isRelayFailure(error)) {
      unexpected = error;
    }
    abort();
  };
  let requestBody: Promise<void> = Promise.resolve();
  try {
    await writeAll(peers.upstream, upstreamHead);
    requestBody = relayBody(peers.clientReader, peers.upstream, framing).catch(trackBody);
    if (head.isUpgrade()) {
      await exchangeUpgrade(head, requestBody, peers, deps);
    } else {
      await pump(peers.upstreamReader, peers.client);
    }
  } catch (error) {
    if (!isRelayFailure(error)) {
      unexpected = error;
    }
  } finally {
    await Promise.all([closeSocket(peers.upstream), closeSocket(peers.client)]);
    await requestBody;
  }
  if (unexpected !== undefined) {
    throw unexpected;
  }
}

async function readRequestHead(
  client: Duplex,
  clientReader: SocketReader,
  deps: ConnectionDeps,
): Promise<HttpHead | undefined> {
  const timeouts = deps.timeouts ?? DEFAULT_PROXY_TIMEOUTS;
  let raw: Buffer;
  try {
    raw = await withTimeoutSeconds(
      clientReader.readUntil(HEADER_TERMINATOR, PROXY_HEAD_MAX_BYTES),
      timeouts.headRead,
    );
  } catch (error) {
    if (!isRelayFailure(error)) {
      throw error;
    }
    await closeSocket(client);
    return undefined;
  }
  try {
    return parseHttpHead(raw.toString(LATIN1));
  } catch (error) {
    if (!(error instanceof MalformedHttpHeadError)) {
      throw error;
    }
    await replyAndClose(client, BAD_REQUEST_RESPONSE);
    return undefined;
  }
}

function framingOrUndefined(head: HttpHead): BodyFraming | undefined {
  try {
    return head.bodyFraming();
  } catch (error) {
    if (error instanceof MalformedFramingError) {
      return undefined;
    }
    throw error;
  }
}

/**
 * Una conexión de cliente: comprueba `Host` (`421`) y `Origin` (`403`)
 * contra `access` y la delimitación del cuerpo (`400`), reserva una de las
 * `slots` (`503` si no queda), y sólo entonces reenvía la primera petición
 * reescrita. Nunca registra nada de lo que pasa por ella.
 */
export async function handleConnection(client: Duplex, deps: ConnectionDeps): Promise<void> {
  const clientReader = new SocketReader(client);
  const head = await readRequestHead(client, clientReader, deps);
  if (head === undefined) {
    return;
  }
  const rejection = accessRejection(head, deps.spec.access, deps.report);
  if (rejection !== undefined) {
    await replyAndClose(client, rejection);
    return;
  }
  const framing = framingOrUndefined(head);
  if (framing === undefined) {
    deps.report(AMBIGUOUS_FRAMING_REPORT);
    await replyAndClose(client, BAD_REQUEST_RESPONSE);
    return;
  }
  if (!deps.slots.tryAcquire()) {
    deps.report(`${MAX_CONNECTIONS_OPTION} alcanzado: 503`);
    await replyAndClose(client, SERVICE_UNAVAILABLE_RESPONSE);
    return;
  }
  try {
    await forward(head, framing, client, clientReader, deps);
  } finally {
    deps.slots.release();
  }
}

/**
 * Sirve el listener (ya reservado, antes de cualquier llamada a AWS) hasta
 * que `signal` se cancele (Ctrl-C): entonces deja de aceptar y corta las
 * conexiones vivas.
 */
export function serveProxy(
  listener: ProxyListener,
  deps: Omit<ConnectionDeps, "slots"> & { readonly maxConnections: number },
  signal: AbortSignal,
): Promise<void> {
  const slots = new ConnectionSlots(deps.maxConnections);
  const active = new Set<Duplex>();
  return new Promise<void>((resolve, reject) => {
    const stop = (): void => {
      void listener.close();
      for (const socket of active) {
        socket.destroy();
      }
      resolve();
    };
    listener.accept(
      (socket) => {
        active.add(socket);
        socket.once("close", () => active.delete(socket));
        handleConnection(socket, { ...deps, slots }).catch(() => {
          deps.report(UNEXPECTED_REPORT);
          socket.destroy();
        });
      },
      (error) => {
        stop();
        reject(error);
      },
    );
    if (signal.aborted) {
      stop();
      return;
    }
    signal.addEventListener("abort", stop, { once: true });
  });
}
