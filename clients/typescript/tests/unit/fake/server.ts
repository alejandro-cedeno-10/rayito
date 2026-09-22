/**
 * El `rayd` falso completo: los cinco servicios servidos por `connectNodeAdapter`
 * sobre un `node:http2` en texto plano en `127.0.0.1:0`. Cuenta las sesiones
 * HTTP/2 (una por transporte del SDK), puede responder `HTTP 403` sin
 * `grpc-status` como el proxy de AWS (`forbidNext`) y orquesta un ciclo
 * suspend/resume con `suspendResume({ unavailableCalls })`.
 */

import http2 from "node:http2";
import type { AddressInfo } from "node:net";
import type { ConnectRouter } from "@connectrpc/connect";
import { connectNodeAdapter } from "@connectrpc/connect-node";
import { CodeService } from "../../../src/gen/rayito/v1/code_pb.js";
import { FilesystemService } from "../../../src/gen/rayito/v1/filesystem_pb.js";
import { HealthService } from "../../../src/gen/rayito/v1/health_pb.js";
import { ProcessService } from "../../../src/gen/rayito/v1/process_pb.js";
import { PtyService } from "../../../src/gen/rayito/v1/pty_pb.js";
import type { TransportSettings } from "../../../src/transport/transport.js";
import { FakeCodeService } from "./code.js";
import { installedTokenSha256 } from "./common.js";
import { FakeFilesystemService } from "./filesystem.js";
import { FakeHealth } from "./health.js";
import { FakeProcessService } from "./process.js";
import { FakePtyService } from "./pty.js";

export interface FakeRaydOptions {
  readonly accessToken?: string | undefined;
  /** El hash que `rayd` instalaría desde el `runHookPayload` (el pool sólo conoce el hash). */
  readonly tokenSha256?: string | undefined;
  readonly sandboxId?: string | undefined;
}

export interface SuspendResumeOptions {
  readonly unavailableCalls?: number;
  readonly clockOffsetMs?: number;
  readonly kernelStateLost?: boolean;
}

export class FakeRayd {
  readonly health: FakeHealth;
  readonly process: FakeProcessService;
  readonly filesystem: FakeFilesystemService;
  readonly code: FakeCodeService;
  readonly pty: FakePtyService;
  readonly host = "127.0.0.1";
  readonly port: number;
  readonly server: http2.Http2Server;
  sessions = 0;
  requests = 0;
  #forbidNext = 0;
  readonly #sessionSet = new Set<http2.ServerHttp2Session>();

  private constructor(
    server: http2.Http2Server,
    port: number,
    services: {
      health: FakeHealth;
      process: FakeProcessService;
      filesystem: FakeFilesystemService;
      code: FakeCodeService;
      pty: FakePtyService;
    },
  ) {
    this.server = server;
    this.port = port;
    this.health = services.health;
    this.process = services.process;
    this.filesystem = services.filesystem;
    this.code = services.code;
    this.pty = services.pty;
  }

  static async start(options: FakeRaydOptions): Promise<FakeRayd> {
    const tokenSha256 = options.tokenSha256 ?? installedTokenSha256(options.accessToken ?? "");
    const health = new FakeHealth(tokenSha256);
    if (options.sandboxId !== undefined) {
      health.sandboxId = options.sandboxId;
    }
    const process = new FakeProcessService(tokenSha256);
    const filesystem = new FakeFilesystemService(tokenSha256);
    const code = new FakeCodeService(tokenSha256);
    const pty = new FakePtyService(tokenSha256, process);
    const routes = (router: ConnectRouter) => {
      router.service(HealthService, {
        health: (request, context) => health.health(request, context),
        metrics: (request, context) => health.metrics(request, context),
      });
      router.service(ProcessService, {
        start: (request, context) => process.start(request, context),
        connect: (request, context) => process.connect(request, context),
        sendInput: (request, context) => process.sendInput(request, context),
        closeStdin: (request, context) => process.closeStdin(request, context),
        sendSignal: (request, context) => process.sendSignal(request, context),
        list: (request, context) => process.list(request, context),
      });
      router.service(FilesystemService, {
        read: (request, context) => filesystem.read(request, context),
        write: (requests, context) => filesystem.write(requests, context),
        stat: (request, context) => filesystem.stat(request, context),
        listDir: (request, context) => filesystem.listDir(request, context),
        makeDir: (request, context) => filesystem.makeDir(request, context),
        move: (request, context) => filesystem.move(request, context),
        remove: (request, context) => filesystem.remove(request, context),
        watchDir: (request, context) => filesystem.watchDir(request, context),
        checkpoint: (request, context) => filesystem.checkpoint(request, context),
        restore: (request, context) => filesystem.restore(request, context),
      });
      router.service(CodeService, {
        createContext: (request, context) => code.createContext(request, context),
        execute: (request, context) => code.execute(request, context),
        reattach: (request, context) => code.reattach(request, context),
        listContexts: (request, context) => code.listContexts(request, context),
        destroyContext: (request, context) => code.destroyContext(request, context),
        restartContext: (request, context) => code.restartContext(request, context),
      });
      router.service(PtyService, {
        create: (request, context) => pty.create(request, context),
        connect: (request, context) => pty.connect(request, context),
        sendInput: (request, context) => pty.sendInput(request, context),
        resize: (request, context) => pty.resize(request, context),
        kill: (request, context) => pty.kill(request, context),
      });
    };
    const adapter = connectNodeAdapter({ routes });
    let rayd: FakeRayd | undefined;
    const server = http2.createServer((request, response) => {
      const current = rayd as FakeRayd;
      current.requests += 1;
      if (current.takeForbidden()) {
        response.writeHead(403, { "content-type": "text/plain" });
        response.end("Forbidden");
        return;
      }
      adapter(request, response);
    });
    server.on("session", (session) => {
      const current = rayd as FakeRayd;
      current.sessions += 1;
      current.trackSession(session);
    });
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    rayd = new FakeRayd(server, (server.address() as AddressInfo).port, {
      health,
      process,
      filesystem,
      code,
      pty,
    });
    return rayd;
  }

  get transport(): Partial<TransportSettings> {
    return { scheme: "http", port: this.port, pingIdleConnection: false };
  }

  /** Las siguientes `count` requests reciben `HTTP 403` sin `grpc-status`, como el proxy con un JWE caducado. */
  forbidNext(count: number): void {
    this.#forbidNext = count;
  }

  takeForbidden(): boolean {
    if (this.#forbidNext <= 0) {
      return false;
    }
    this.#forbidNext -= 1;
    return true;
  }

  trackSession(session: http2.ServerHttp2Session): void {
    this.#sessionSet.add(session);
    session.on("close", () => this.#sessionSet.delete(session));
  }

  /** `/suspend`: cada servicio cierra sus streams con su forma y rechaza los nuevos con el phase gate. */
  suspend(): void {
    this.process.suspend();
    this.pty.suspend();
    this.filesystem.suspend();
    this.code.suspend();
  }

  /** `/resume`: la generación avanza y la fase vuelve a aceptar streams. */
  resume(options: { clockOffsetMs?: number; kernelStateLost?: boolean } = {}): void {
    this.health.resumeGeneration += 1;
    this.health.clockOffsetMs = options.clockOffsetMs ?? 0;
    this.health.kernelStateLost = options.kernelStateLost ?? false;
    this.process.resume();
    this.filesystem.resume();
    this.code.resume();
  }

  /** Un ciclo suspend/resume visto por el cliente: streams cerrados, `Health` `Unavailable` N veces, generación nueva. */
  suspendResume(options: SuspendResumeOptions = {}): void {
    this.suspend();
    this.health.unavailableCalls = options.unavailableCalls ?? 0;
    this.resume(options);
  }

  async close(): Promise<void> {
    for (const session of this.#sessionSet) {
      session.destroy();
    }
    await new Promise<void>((resolve) => this.server.close(() => resolve()));
  }
}

export async function startFakeRayd(options: FakeRaydOptions): Promise<FakeRayd> {
  return FakeRayd.start(options);
}
