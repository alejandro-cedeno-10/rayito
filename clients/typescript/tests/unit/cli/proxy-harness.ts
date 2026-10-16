/**
 * Un proxy de verdad sobre sockets de loopback frente a un upstream falso sin
 * TLS (el conector se inyecta): lo que usan los tests del servidor y del caso
 * de uso de `rayito sandbox proxy`. Espejo de `_drive`, `_FakeConnector` y
 * `_start_proxy` de `clients/python/tests/unit/cli/test_proxy.py`.
 */

import { connect, createServer, type Server, type Socket } from "node:net";
import type { Duplex } from "node:stream";
import type { PortSpec } from "../../../src/aws/control-plane.js";
import { buildAccess } from "../../../src/cli/access.js";
import { ProxyListener } from "../../../src/cli/proxy-listener.js";
import {
  type ProxyTimeoutsSeconds,
  serveProxy,
  type UpstreamConnector,
} from "../../../src/cli/proxy-server.js";
import { CasePlane } from "./case-world.js";

export const SANDBOX_ID = "microvm-x";
export const ENDPOINT = "abc.example.on.aws";
export const JWE = "JWE-OPERATOR";
export const PROXY_PORT_VALUE = 8080;
const UPSTREAM_SETTLE_MS = 20;

export interface UpstreamScript {
  /** Lo que el upstream contesta en cuanto llega la cabecera. */
  readonly response?: string | Buffer;
  /** Cuánto espera antes de cerrar, para que se vea todo lo que el proxy le reenvíe. */
  readonly closeAfterMs?: number;
  /** Cierra sin contestar nada. */
  readonly silent?: boolean;
  /** Devuelve a cada trozo recibido lo que haga falta (un eco). */
  readonly echo?: boolean;
}

export const DEFAULT_UPSTREAM_RESPONSE = "HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok";

/** Un servidor TCP en loopback que graba todo lo que recibe y contesta según su guion. */
export class FakeUpstream {
  readonly #server: Server;
  readonly #script: UpstreamScript;
  readonly #chunks: Buffer[] = [];
  connections = 0;

  constructor(script: UpstreamScript = {}) {
    this.#script = script;
    this.#server = createServer({ allowHalfOpen: true }, (socket) => this.#onConnection(socket));
  }

  async start(): Promise<void> {
    await new Promise<void>((resolve) => this.#server.listen(0, "127.0.0.1", resolve));
  }

  get received(): Buffer {
    return Buffer.concat(this.#chunks);
  }

  get port(): number {
    return (this.#server.address() as { port: number }).port;
  }

  connector(): UpstreamConnector {
    return () =>
      new Promise<Duplex>((resolve, reject) => {
        const socket = connect({ host: "127.0.0.1", port: this.port, allowHalfOpen: true });
        socket.once("connect", () => resolve(socket));
        socket.once("error", reject);
      });
  }

  async close(): Promise<void> {
    await new Promise<void>((resolve) => this.#server.close(() => resolve()));
  }

  #onConnection(socket: Socket): void {
    this.connections += 1;
    let answered = false;
    socket.on("error", () => undefined);
    socket.on("data", (chunk) => {
      this.#chunks.push(chunk);
      if (this.#script.echo === true && answered) {
        socket.write(chunk);
      }
      if (!answered && this.received.includes("\r\n\r\n")) {
        answered = true;
        this.#answer(socket);
      }
    });
    socket.on("end", () => {
      if (this.#script.echo === true) {
        socket.end();
      }
    });
  }

  #answer(socket: Socket): void {
    if (this.#script.silent === true) {
      socket.end();
      return;
    }
    socket.write(this.#script.response ?? DEFAULT_UPSTREAM_RESPONSE);
    if (this.#script.echo === true) {
      return;
    }
    setTimeout(() => socket.end(), this.#script.closeAfterMs ?? UPSTREAM_SETTLE_MS);
  }
}

export interface ProxyWorldOptions {
  readonly upstream?: FakeUpstream;
  readonly connector?: UpstreamConnector;
  readonly jwe?: string | undefined;
  readonly bind?: string;
  readonly allowedHosts?: readonly string[];
  readonly allowedOrigins?: readonly string[];
  readonly maxConnections?: number;
  readonly timeouts?: ProxyTimeoutsSeconds;
}

/** Un proxy arrancado con su listener y la lista de las razones que informó. */
export class ProxyWorld {
  readonly reports: string[];
  readonly #abort: AbortController;
  readonly #listener: ProxyListener;
  readonly #served: Promise<void>;
  readonly #sockets = new Set<Socket>();

  private constructor(
    listener: ProxyListener,
    abort: AbortController,
    served: Promise<void>,
    reports: string[],
  ) {
    this.#listener = listener;
    this.#abort = abort;
    this.#served = served;
    this.reports = reports;
  }

  static async start(options: ProxyWorldOptions = {}): Promise<ProxyWorld> {
    const bind = options.bind ?? "127.0.0.1";
    const listener = await ProxyListener.bind(bind, 0);
    const access = buildAccess({
      bind,
      localPort: listener.port,
      sandboxId: SANDBOX_ID,
      allowedHosts: options.allowedHosts ?? [],
      allowedOrigins: options.allowedOrigins ?? [],
    });
    const connector =
      options.connector ??
      options.upstream?.connector() ??
      (() => Promise.reject(new Error("el upstream no debía usarse")));
    const reports: string[] = [];
    const abort = new AbortController();
    const served = serveProxy(
      listener,
      {
        spec: { endpoint: ENDPOINT, port: PROXY_PORT_VALUE, access },
        jweProvider: () => ("jwe" in options ? options.jwe : JWE),
        connector,
        report: (reason) => reports.push(reason),
        maxConnections: options.maxConnections ?? 1,
        ...(options.timeouts === undefined ? {} : { timeouts: options.timeouts }),
      },
      abort.signal,
    );
    return new ProxyWorld(listener, abort, served, reports);
  }

  get port(): number {
    return this.#listener.port;
  }

  /** `Host` con el puerto local, el que admite el proxy sin `--allowed-host`. */
  get localHost(): string {
    return `localhost:${this.port}`;
  }

  /** Abre una conexión, envía `raw` y cierra su lado de escritura; devuelve lo que llegue hasta el cierre. */
  async send(raw: string | Buffer): Promise<Buffer> {
    const socket = await this.open();
    const chunks: Buffer[] = [];
    const closed = new Promise<void>((resolve) => {
      socket.on("data", (chunk) => chunks.push(chunk));
      socket.on("close", () => resolve());
    });
    socket.write(typeof raw === "string" ? Buffer.from(raw, "latin1") : raw);
    socket.end();
    await closed;
    return Buffer.concat(chunks);
  }

  /** Una conexión que se queda abierta para hablar en los dos sentidos. */
  async open(): Promise<Socket> {
    const socket = connect({ host: "127.0.0.1", port: this.port, allowHalfOpen: true });
    socket.on("error", () => undefined);
    this.#sockets.add(socket);
    await new Promise<void>((resolve) => socket.once("connect", () => resolve()));
    return socket;
  }

  async stop(): Promise<void> {
    for (const socket of this.#sockets) {
      socket.destroy();
    }
    this.#abort.abort();
    await this.#served;
  }
}

/** Un puerto de loopback libre: se reserva y se suelta. */
export async function freePort(): Promise<number> {
  const listener = await ProxyListener.bind("127.0.0.1", 0);
  const { port } = listener;
  await listener.close();
  return port;
}

/** Un plano de control con un solo sandbox que cuenta las consultas y los JWE acuñados. */
export class MintingPlane extends CasePlane {
  readonly minted: Array<readonly PortSpec[]> = [];
  lookups = 0;

  constructor(state: string) {
    super({
      infos: [
        {
          id: SANDBOX_ID,
          state,
          image: "rayito-base",
          version: "1",
          startedAt: "2026-10-01T00:00:00Z",
        },
      ],
    });
  }

  override async getMicrovm(sandboxId: string) {
    this.lookups += 1;
    return super.getMicrovm(sandboxId);
  }

  override async createAuthToken(_sandboxId: string, ports: readonly PortSpec[]): Promise<string> {
    this.minted.push(ports);
    return `jwe-${this.minted.length}`;
  }
}
