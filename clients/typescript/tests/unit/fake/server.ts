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
import { LifecyclePhase, LifecycleService } from "../../../src/gen/rayito/v1/lifecycle_pb.js";
import { NetworkService } from "../../../src/gen/rayito/v1/network_pb.js";
import { ProcessService } from "../../../src/gen/rayito/v1/process_pb.js";
import { PtyService } from "../../../src/gen/rayito/v1/pty_pb.js";
import type { TransportSettings } from "../../../src/transport/transport.js";
import { FakeCodeService } from "./code.js";
import { installedTokenSha256 } from "./common.js";
import { FakeFilesystemService } from "./filesystem.js";
import { FakeHealth } from "./health.js";
import { FakeLifecycleService } from "./lifecycle.js";
import { FakeNetworkService } from "./network.js";
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

/** Lo único que `rayd` admite con el plazo vencido (`RESUME_GRACE`/`EXPIRED`). */
export const TIMEOUT_GATE_ADMITTED: ReadonlySet<string> = new Set([
  "/rayito.v1.HealthService/Health",
  "/rayito.v1.LifecycleService/SetTimeout",
]);

/** Las fases en las que la puerta `"phase"` rechaza: las de `rayd` con el plazo vencido. */
const TIMEOUT_GATE_PHASES: ReadonlySet<LifecyclePhase> = new Set([
  LifecyclePhase.EXPIRED,
  LifecyclePhase.RESUME_GRACE,
]);

export class FakeRayd {
  readonly health: FakeHealth;
  readonly process: FakeProcessService;
  readonly filesystem: FakeFilesystemService;
  readonly code: FakeCodeService;
  readonly pty: FakePtyService;
  readonly network: FakeNetworkService;
  readonly lifecycle: FakeLifecycleService;
  readonly host = "127.0.0.1";
  readonly port: number;
  readonly server: http2.Http2Server;
  sessions = 0;
  requests = 0;
  /** Rutas (`/rayito.v1.Servicio/Metodo`) que nunca responden: para probar que un `signal` corta la llamada. */
  readonly held = new Set<string>();
  /**
   * La puerta de `rayd` con el plazo vencido: todo lo no admitido recibe
   * `FAILED_PRECONDITION sandbox_timeout`; con `"phase"`, sólo mientras el
   * `LifecycleState` de `Health` está `EXPIRED` o `RESUME_GRACE`, como `rayd`.
   */
  timeoutGate: boolean | "phase" = false;
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
      network: FakeNetworkService;
      lifecycle: FakeLifecycleService;
    },
  ) {
    this.server = server;
    this.port = port;
    this.health = services.health;
    this.process = services.process;
    this.filesystem = services.filesystem;
    this.code = services.code;
    this.pty = services.pty;
    this.network = services.network;
    this.lifecycle = services.lifecycle;
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
    const network = new FakeNetworkService(tokenSha256, health);
    const lifecycle = new FakeLifecycleService(tokenSha256, health);
    const routes = (router: ConnectRouter) => {
      router.service(HealthService, {
        health: (request, context) => health.health(request, context),
        metrics: (request, context) => health.metrics(request, context),
        metricsHistory: (request, context) => health.metricsHistory(request, context),
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
        startImport: (request, context) => filesystem.startImport(request, context),
        startExport: (request, context) => filesystem.startExport(request, context),
        getTransfer: (request, context) => filesystem.getTransfer(request, context),
        watchTransfer: (request, context) => filesystem.watchTransfer(request, context),
        cancelTransfer: (request, context) => filesystem.cancelTransfer(request, context),
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
      router.service(NetworkService, {
        updateNetwork: (request, context) => network.updateNetwork(request, context),
        getNetwork: (request, context) => network.getNetwork(request, context),
      });
      router.service(LifecycleService, {
        setTimeout: (request, context) => lifecycle.setTimeout(request, context),
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
      if (current.held.has(request.url ?? "")) {
        return;
      }
      if (current.gates(request.url)) {
        answerSandboxTimeout(request, response);
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
      network,
      lifecycle,
    });
    return rayd;
  }

  gates(path: string): boolean {
    if (this.timeoutGate === false || TIMEOUT_GATE_ADMITTED.has(path)) {
      return false;
    }
    const phase = this.health.lifecycle?.phase ?? LifecyclePhase.ACTIVE;
    return this.timeoutGate === true || TIMEOUT_GATE_PHASES.has(phase);
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

/** Respuesta gRPC trailers-only tras drenar el cuerpo, como la capa de `rayd`. */
function answerSandboxTimeout(
  request: http2.Http2ServerRequest,
  response: http2.Http2ServerResponse,
): void {
  request.resume();
  response.writeHead(200, {
    "content-type": "application/grpc+proto",
    "grpc-status": "9",
    "grpc-message": "sandbox_timeout",
  });
  response.end();
}

export async function startFakeRayd(options: FakeRaydOptions): Promise<FakeRayd> {
  return FakeRayd.start(options);
}
