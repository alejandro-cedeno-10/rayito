/**
 * El socket local de `rayito sandbox proxy`, reservado ANTES de tocar AWS
 * (`GetMicrovm`, `CreateMicrovmAuthToken`): un `--local-port` ocupado o un
 * `--bind` inválido falla aquí, limpio (`InvalidArgumentError` con el puerto,
 * que la CLI muestra sin traza), sin haber gastado ninguna llamada. Las
 * conexiones que lleguen mientras se resuelve el endpoint esperan en una cola
 * hasta que `accept` instala el manejador, como el backlog de `listen()`.
 */

import { createServer, type Server, type Socket } from "node:net";
import { InvalidArgumentError } from "../errors.js";

export type ConnectionHandler = (socket: Socket) => void;

export class ProxyListener {
  readonly #server: Server;
  readonly #queued: Socket[] = [];
  #handler: ConnectionHandler | undefined;

  private constructor(server: Server) {
    this.#server = server;
    server.on("connection", (socket) => this.#onConnection(socket));
  }

  /** Reserva `bind:localPort` (`0` elige un puerto libre); `InvalidArgumentError` si no se puede. */
  static async bind(bind: string, localPort: number): Promise<ProxyListener> {
    const server = createServer({ allowHalfOpen: true });
    const listener = new ProxyListener(server);
    await new Promise<void>((resolve, reject) => {
      server.once("error", (error: NodeJS.ErrnoException) => {
        reject(
          new InvalidArgumentError(
            `no se puede escuchar en ${bind} con --local-port ${localPort}: ${error.code ?? "error"}`,
          ),
        );
      });
      server.listen({ host: bind, port: localPort }, resolve);
    });
    server.removeAllListeners("error");
    return listener;
  }

  get port(): number {
    const address = this.#server.address();
    return typeof address === "object" && address !== null ? address.port : 0;
  }

  /** Instala el manejador y le entrega las conexiones que ya esperaban. */
  accept(handler: ConnectionHandler, onFailure: (error: Error) => void): void {
    this.#server.on("error", onFailure);
    this.#handler = handler;
    for (const socket of this.#queued.splice(0)) {
      handler(socket);
    }
  }

  close(): Promise<void> {
    for (const socket of this.#queued.splice(0)) {
      socket.destroy();
    }
    return new Promise<void>((resolve) => {
      if (!this.#server.listening) {
        resolve();
        return;
      }
      this.#server.close(() => resolve());
    });
  }

  #onConnection(socket: Socket): void {
    socket.on("error", () => undefined);
    if (this.#handler === undefined) {
      this.#queued.push(socket);
      return;
    }
    this.#handler(socket);
  }
}
