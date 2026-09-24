/**
 * Túnel HTTP `CONNECT` hacia un proxy corporativo (`proxy: "http://[u:p@]h:puerto"`)
 * para las sesiones HTTP/2 hacia el endpoint y para el plano de control. Sólo
 * built-ins de Node: el TLS sigue siendo `tls.connect` con `servername`, así
 * que la verificación del certificado no cambia. La URL del proxy y sus
 * credenciales nunca aparecen en un mensaje de error ni en un log.
 */

import { Agent, type AgentOptions, type RequestOptions } from "node:https";
import net from "node:net";
import { Duplex } from "node:stream";
import tls from "node:tls";
import { InvalidArgumentError, SandboxError } from "../errors.js";

export const PROXY_URL_MESSAGE = "proxy debe ser una URL http://host:puerto";
export const MAX_CONNECT_RESPONSE_BYTES = 16 * 1024;
const PROXY_URL = /^http:\/\/(?:([^@/?#]*)@)?(\[[^\]/?#]+\]|[^:@/?#[\]]+):(\d{1,5})\/?$/i;
const STATUS_LINE = /^HTTP\/1\.[01] (\d{3})/;
const HEADER_END = "\r\n\r\n";

/** El proxy ya validado: `authorization` es el valor de `Proxy-Authorization` si hubo userinfo. */
export interface ProxyEndpoint {
  readonly host: string;
  readonly port: number;
  readonly authorization: string | undefined;
}

function userinfoAuthorization(userinfo: string | undefined): string | undefined {
  if (userinfo === undefined || userinfo === "") {
    return undefined;
  }
  const colon = userinfo.indexOf(":");
  const user = colon < 0 ? userinfo : userinfo.slice(0, colon);
  const password = colon < 0 ? "" : userinfo.slice(colon + 1);
  const decoded = `${safeDecode(user)}:${safeDecode(password)}`;
  return `Basic ${Buffer.from(decoded, "utf8").toString("base64")}`;
}

function safeDecode(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    throw new InvalidArgumentError(PROXY_URL_MESSAGE);
  }
}

/**
 * `http://host:puerto` con userinfo opcional: el esquema tiene que ser
 * `http` (el proxy sólo habla `CONNECT`) y el puerto explícito. El mensaje de
 * error nunca repite la URL, que puede llevar credenciales.
 */
export function parseProxyUrl(proxy: unknown): ProxyEndpoint {
  if (typeof proxy !== "string") {
    throw new InvalidArgumentError(PROXY_URL_MESSAGE);
  }
  const match = PROXY_URL.exec(proxy.trim());
  if (match === null) {
    throw new InvalidArgumentError(PROXY_URL_MESSAGE);
  }
  const port = Number(match[3]);
  if (!Number.isInteger(port) || port < 1 || port > 65_535) {
    throw new InvalidArgumentError(PROXY_URL_MESSAGE);
  }
  const host = (match[2] as string).replace(/^\[(.*)\]$/, "$1");
  return Object.freeze({ host, port, authorization: userinfoAuthorization(match[1]) });
}

/** `undefined` pasa; cualquier otra cosa tiene que ser una URL de proxy válida. */
export function validateProxyUrl(proxy: unknown): string | undefined {
  if (proxy === undefined) {
    return undefined;
  }
  parseProxyUrl(proxy);
  return proxy as string;
}

export function formatAuthority(host: string, port: number): string {
  return net.isIPv6(host) ? `[${host}]:${port}` : `${host}:${port}`;
}

export function connectRequest(
  host: string,
  port: number,
  authorization: string | undefined,
): string {
  const authority = formatAuthority(host, port);
  const lines = [`CONNECT ${authority} HTTP/1.1`, `Host: ${authority}`];
  if (authorization !== undefined) {
    lines.push(`Proxy-Authorization: ${authorization}`);
  }
  return `${lines.join("\r\n")}${HEADER_END}`;
}

export function connectStatus(header: string): number | undefined {
  const match = STATUS_LINE.exec(header);
  return match === null ? undefined : Number(match[1]);
}

interface PendingWrite {
  readonly chunk: Buffer;
  readonly callback: (error?: Error | null) => void;
}

/**
 * Un `Duplex` que abre `net.connect` al proxy, manda `CONNECT host:puerto` y
 * retiene lo escrito hasta leer `HTTP/1.x 200`; después reenvía los bytes en
 * los dos sentidos. Cualquier otro status lo destruye con `SandboxError`
 * ("el proxy rechazó CONNECT con HTTP <status>").
 */
export class ProxyTunnelSocket extends Duplex {
  readonly #socket: net.Socket;
  readonly #pending: PendingWrite[] = [];
  #header = Buffer.alloc(0);
  #established = false;

  constructor(proxy: ProxyEndpoint, host: string, port: number) {
    super();
    this.#socket = net.connect(proxy.port, proxy.host);
    this.#socket.once("connect", () => {
      this.#socket.write(connectRequest(host, port, proxy.authorization));
    });
    this.#socket.on("data", (data: Buffer) => this.#onData(data));
    this.#socket.on("error", (error) => this.destroy(error));
    this.#socket.on("end", () => this.push(null));
    this.#socket.on("close", () => {
      if (!this.destroyed) {
        this.destroy();
      }
    });
  }

  get established(): boolean {
    return this.#established;
  }

  override _read(): void {
    this.#socket.resume();
  }

  override _write(
    chunk: Buffer,
    _encoding: BufferEncoding,
    callback: (error?: Error | null) => void,
  ): void {
    if (!this.#established) {
      this.#pending.push({ chunk, callback });
      return;
    }
    this.#socket.write(chunk, callback);
  }

  override _final(callback: (error?: Error | null) => void): void {
    this.#socket.end(callback);
  }

  override _destroy(error: Error | null, callback: (error?: Error | null) => void): void {
    this.#socket.destroy();
    for (const pending of this.#pending.splice(0)) {
      pending.callback(error ?? new SandboxError("túnel del proxy cerrado"));
    }
    callback(error);
  }

  setNoDelay(noDelay?: boolean): this {
    this.#socket.setNoDelay(noDelay);
    return this;
  }

  setKeepAlive(enable?: boolean, initialDelay?: number): this {
    this.#socket.setKeepAlive(enable, initialDelay);
    return this;
  }

  setTimeout(timeout: number, callback?: () => void): this {
    this.#socket.setTimeout(timeout, callback);
    return this;
  }

  ref(): this {
    this.#socket.ref();
    return this;
  }

  unref(): this {
    this.#socket.unref();
    return this;
  }

  #onData(data: Buffer): void {
    if (this.#established) {
      if (!this.push(data)) {
        this.#socket.pause();
      }
      return;
    }
    this.#header = Buffer.concat([this.#header, data]);
    const end = this.#header.indexOf(HEADER_END);
    if (end < 0) {
      if (this.#header.length > MAX_CONNECT_RESPONSE_BYTES) {
        this.destroy(new SandboxError("el proxy respondió a CONNECT sin cabeceras válidas"));
      }
      return;
    }
    const status = connectStatus(this.#header.subarray(0, end).toString("latin1"));
    const rest = this.#header.subarray(end + HEADER_END.length);
    this.#header = Buffer.alloc(0);
    if (status !== 200) {
      this.destroy(new SandboxError(`el proxy rechazó CONNECT con HTTP ${status ?? "inválido"}`));
      return;
    }
    this.#established = true;
    this.emit("tunnel");
    for (const pending of this.#pending.splice(0)) {
      this.#socket.write(pending.chunk, pending.callback);
    }
    if (rest.length > 0) {
      this.push(rest);
    }
  }
}

/** TLS (`h2` por ALPN) sobre el túnel: el `createConnection` de `Http2SessionManager`. */
export function tunnelledTlsConnection(
  proxy: ProxyEndpoint,
  host: string,
  port: number,
  alpn: readonly string[] = ["h2"],
  tlsOptions: tls.ConnectionOptions = {},
): tls.TLSSocket {
  return tls.connect({
    ...tlsOptions,
    socket: new ProxyTunnelSocket(proxy, host, port),
    servername: net.isIP(host) === 0 ? host : undefined,
    ALPNProtocols: [...alpn],
  });
}

/**
 * `https.Agent` del plano de control detrás del proxy: cada conexión es el
 * mismo túnel `CONNECT` seguido de `tls.connect`, con las opciones TLS que
 * pase el SDK de AWS.
 */
export class ProxyTunnelAgent extends Agent {
  readonly #proxy: ProxyEndpoint;

  constructor(proxy: string | ProxyEndpoint, options: AgentOptions = {}) {
    super({ keepAlive: true, ...options });
    this.#proxy = typeof proxy === "string" ? parseProxyUrl(proxy) : proxy;
  }

  /** Devuelve el socket en el acto: el `Agent` de Node no espera al callback si hay valor de retorno. */
  override createConnection(options: RequestOptions): Duplex {
    const host = String(options.hostname ?? options.host ?? "localhost").replace(
      /^\[(.*)\]$/,
      "$1",
    );
    const port = Number(options.port ?? 443);
    return tls.connect({
      ...(options as tls.ConnectionOptions),
      host,
      port,
      socket: new ProxyTunnelSocket(this.#proxy, host, port),
      servername: options.servername ?? (net.isIP(host) === 0 ? host : undefined),
    });
  }
}
