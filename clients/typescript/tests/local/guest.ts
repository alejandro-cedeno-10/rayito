/**
 * Plano de control local de los tests `local` (nunca se publica: vive en
 * `tests/`, fuera del paquete), espejo de `clients/python/tests/local/guest.py`.
 *
 * `LocalGuestControlPlane` es otro adaptador del puerto `ControlPlane`: un
 * decorador sobre el plano real (`LambdaMicrovmsControlPlane` contra Floci,
 * el emulador de AWS de `dev/local/compose.yaml`) que, donde AWS arrancaría un
 * MicroVM, apunta al contenedor `guest`, donde `rayd` corre como PID 1 sobre
 * la imagen de producto. Lo que Floci emula pasa por el adaptador real; lo que
 * no emula (el endpoint por VM, `create-microvm-auth-token`, suspend/resume)
 * lo sustituyen los hooks del guest, igual que los llama la plataforma
 * (`scripts/hooks-sim.py`, `AWS_API_NOTES.md` §8). Solo se activa cuando los
 * tests leen `RAYITO_LOCAL_GUEST`; el SDK no sabe que existe.
 */

import type {
  ControlPlane,
  ControlPlaneCallOptions,
  LaunchRequest,
  ListMicrovmsOptions,
  ListMicrovmsPageOptions,
  PortSpec,
} from "../../src/aws/control-plane.js";
import { InvalidArgumentError, SandboxError } from "../../src/errors.js";
import { HOOK_PATH_PREFIX } from "../../src/limits.js";
import {
  type MicrovmListPage,
  type SandboxInfo,
  type SandboxListItem,
  sandboxInfo,
} from "../../src/models.js";
import type { TransportSettings } from "../../src/transport/transport.js";

/** `host:puerto gRPC:puerto de hooks` del guest en el espacio de red del runner. */
export const LOCAL_GUEST_VAR = "RAYITO_LOCAL_GUEST";
/** El JWE lo valida el proxy de AWS, no `rayd`: en local basta un valor fijo que no es secreto. */
export const LOCAL_PROXY_TOKEN = "rayito-local-proxy-token";
/** `READY_RETRY_BUDGET_S` de `scripts/hooks-sim.py` más el rearranque del contenedor. */
export const READY_BUDGET_MS = 330_000;
export const READY_RETRY_INTERVAL_MS = 500;
/** `runTimeoutInSeconds` de la imagen (`DECLARED_TIMEOUT_S` de `scripts/hooks-sim.py`). */
export const HOOK_TIMEOUT_MS = 30_000;
/** Lo que tarda como mucho el `rayd` anterior en soltar el puerto tras `/terminate` (ADR-011). */
export const SHUTDOWN_BUDGET_MS = 15_000;
export const SHUTDOWN_POLL_MS = 50;
const HTTP_OK = 200;
/** `run_outcome` de `crates/rayd/src/hooks/mod.rs` cuando instala el token. */
const RUN_INSTALLED = "installed";
const SUSPENDED_STATE = "SUSPENDED";
const RUNNING_STATE = "RUNNING";

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Dónde escucha el `rayd` del guest. */
export class GuestAddress {
  constructor(
    readonly host: string,
    readonly grpcPort: number,
    readonly hooksPort: number,
  ) {}

  static parse(raw: string): GuestAddress {
    const parts = raw.trim().split(":");
    if (parts.length !== 3 || parts.some((part) => part.length === 0)) {
      throw new InvalidArgumentError(`${LOCAL_GUEST_VAR} debe ser host:puerto-grpc:puerto-hooks`);
    }
    const [host, grpcPort, hooksPort] = parts as [string, string, string];
    return new GuestAddress(host, Number(grpcPort), Number(hooksPort));
  }

  hookUrl(hook: string): string {
    return `http://${this.host}:${this.hooksPort}${HOOK_PATH_PREFIX}/${hook}`;
  }

  /** h2c por loopback: `openTransport` solo lo admite hacia loopback, que es lo que el runner comparte con el guest. */
  transport(): Partial<TransportSettings> {
    return { scheme: "http", port: this.grpcPort };
  }
}

/** Adaptador HTTP de los hooks del guest (`POST`, sólo `/run` lleva cuerpo). */
export class GuestHooks {
  constructor(private readonly address: GuestAddress) {}

  async post(hook: string, body?: Record<string, string>): Promise<[number, string]> {
    const response = await fetch(this.address.hookUrl(hook), {
      method: "POST",
      ...(body === undefined
        ? {}
        : { body: JSON.stringify(body), headers: { "content-type": "application/json" } }),
      signal: AbortSignal.timeout(HOOK_TIMEOUT_MS),
    });
    return [response.status, await response.text()];
  }

  /** `/ready` responde 503 mientras se calienta el kernel; la conexión falla mientras Docker rearranca el contenedor. */
  async waitReady(): Promise<void> {
    const deadline = performance.now() + READY_BUDGET_MS;
    for (;;) {
      let status = 0;
      try {
        [status] = await this.post("ready");
        if (status === HTTP_OK) {
          return;
        }
      } catch {
        status = 0;
      }
      if (performance.now() >= deadline) {
        throw new SandboxError(`el guest local no respondió /ready (último: ${status})`);
      }
      await sleep(READY_RETRY_INTERVAL_MS);
    }
  }

  /** Tras `/terminate`, hasta que el `rayd` anterior deja de responder. */
  async waitGone(): Promise<void> {
    const deadline = performance.now() + SHUTDOWN_BUDGET_MS;
    while (performance.now() < deadline) {
      try {
        await this.post("ready");
      } catch {
        return;
      }
      await sleep(SHUTDOWN_POLL_MS);
    }
  }

  /** `/ready` y `/run` hasta que un `rayd` recién arrancado instale el token; uno ya usado se recicla con `/terminate`. */
  async run(microvmId: string, payload: string): Promise<void> {
    const deadline = performance.now() + READY_BUDGET_MS;
    for (;;) {
      await this.waitReady();
      const reply = await this.call("run", { microvmId, runHookPayload: payload });
      if (reply.includes(RUN_INSTALLED)) {
        return;
      }
      if (performance.now() >= deadline) {
        throw new SandboxError("el guest local no instaló el token de /run");
      }
      await this.call("terminate");
      await this.waitGone();
    }
  }

  async call(hook: string, body?: Record<string, string>): Promise<string> {
    const [status, text] = await this.post(hook, body);
    if (status !== HTTP_OK) {
      throw new SandboxError(`el hook /${hook} del guest local respondió ${status}`);
    }
    return text;
  }
}

/**
 * `ControlPlane` que lanza sobre Floci y ejecuta en el guest local. Un guest
 * aloja un sandbox a la vez: `runMicrovm` termina el anterior si un test lo
 * dejó vivo, y `/terminate` hace salir a `rayd`, que Docker rearranca limpio.
 */
export class LocalGuestControlPlane implements ControlPlane {
  #live: string | undefined;
  readonly #suspended = new Set<string>();
  #queue: Promise<unknown> = Promise.resolve();

  constructor(
    private readonly inner: ControlPlane,
    private readonly address: GuestAddress,
    private readonly hooks: GuestHooks = new GuestHooks(address),
  ) {}

  get region(): string {
    return this.inner.region;
  }

  get awsClientSettings() {
    return this.inner.awsClientSettings;
  }

  get liveSandboxId(): string | undefined {
    return this.#live;
  }

  resolveTemplateArn(template: string, options?: ControlPlaneCallOptions): Promise<string> {
    return this.inner.resolveTemplateArn(template, options);
  }

  runMicrovm(request: LaunchRequest): Promise<SandboxInfo> {
    return this.#serialized(async () => {
      if (this.#live !== undefined) {
        await this.#terminateGuest(this.#live);
      }
      const info = await this.inner.runMicrovm(request);
      await this.hooks.run(info.sandboxId, request.runHookPayload);
      this.#live = info.sandboxId;
      return this.#localize(info);
    });
  }

  async getMicrovm(sandboxId: string, options?: ControlPlaneCallOptions): Promise<SandboxInfo> {
    return this.#localize(await this.inner.getMicrovm(sandboxId, options));
  }

  async *listMicrovms(options: ListMicrovmsOptions = {}): AsyncIterable<SandboxListItem> {
    const { states, ...unfiltered } = options;
    const wanted = states === undefined ? undefined : new Set(states);
    for await (const item of this.inner.listMicrovms(unfiltered)) {
      const local = this.#localizeItem(item);
      if (wanted === undefined || wanted.has(local.state)) {
        yield local;
      }
    }
  }

  async listMicrovmsPage(options: ListMicrovmsPageOptions): Promise<MicrovmListPage> {
    const page = await this.inner.listMicrovmsPage(options);
    return { ...page, items: page.items.map((item) => this.#localizeItem(item)) };
  }

  async terminateMicrovm(sandboxId: string): Promise<boolean> {
    await this.#serialized(async () => {
      if (sandboxId === this.#live) {
        await this.#terminateGuest(sandboxId);
      }
    });
    return this.inner.terminateMicrovm(sandboxId);
  }

  suspendMicrovm(sandboxId: string): Promise<boolean> {
    return this.#serialized(async () => {
      if (sandboxId !== this.#live || this.#suspended.has(sandboxId)) {
        return false;
      }
      await this.hooks.call("suspend");
      this.#suspended.add(sandboxId);
      return true;
    });
  }

  resumeMicrovm(sandboxId: string): Promise<boolean> {
    return this.#serialized(async () => {
      if (!this.#suspended.has(sandboxId)) {
        return false;
      }
      await this.hooks.call("resume");
      this.#suspended.delete(sandboxId);
      return true;
    });
  }

  async createAuthToken(_sandboxId: string, ports: readonly PortSpec[]): Promise<string> {
    if (ports.length === 0) {
      throw new InvalidArgumentError("allowedPorts necesita al menos un puerto");
    }
    return LOCAL_PROXY_TOKEN;
  }

  getMicrovmImageVersion(
    imageArn: string,
    imageVersion: string,
    options?: ControlPlaneCallOptions,
  ): Promise<number> {
    return this.inner.getMicrovmImageVersion(imageArn, imageVersion, options);
  }

  /** Las operaciones que tocan el guest, de una en una (el guest aloja un sandbox). */
  #serialized<T>(work: () => Promise<T>): Promise<T> {
    const next = this.#queue.then(work, work);
    this.#queue = next.catch(() => undefined);
    return next;
  }

  async #terminateGuest(sandboxId: string): Promise<void> {
    await this.hooks.call("terminate");
    await this.hooks.waitGone();
    this.#suspended.delete(sandboxId);
    this.#live = undefined;
  }

  #localState(sandboxId: string, state: string): string {
    return this.#suspended.has(sandboxId) && state === RUNNING_STATE ? SUSPENDED_STATE : state;
  }

  #localize(info: SandboxInfo): SandboxInfo {
    return sandboxInfo({
      ...info,
      endpoint: this.address.host,
      state: this.#localState(info.sandboxId, info.state),
    });
  }

  #localizeItem(item: SandboxListItem): SandboxListItem {
    return { ...item, state: this.#localState(item.sandboxId, item.state) };
  }
}
