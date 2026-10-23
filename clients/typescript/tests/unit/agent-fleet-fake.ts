/**
 * Dobles de prueba de `AgentFleet` (ADR-029). Espejo de
 * `fake_agent_fleet.py`: un backend que simula los hosts de los VMs (salud,
 * ocupación, sesiones), un directorio con `create()`, `connect()`,
 * `listMembers()` y `kill()` que reparte sandboxes con id propio, un `fetch`
 * que contesta por VM y un pool de doble. Nunca tocan red, Connect ni AWS.
 */

import type { FleetDirectory, FleetSandbox } from "../../src/agent/fleet.js";
import { AgentFleet, type AgentFleetOptions, type FleetPool } from "../../src/agent/fleet.js";
import type { MemberListing } from "../../src/agent/fleet-base.js";
import type { SandboxFactory, ServiceSandbox } from "../../src/agent/project-service.js";
import { AGENT_SERVE_TAG, ProjectService } from "../../src/agent/project-service.js";
import type { ScalingOptions } from "../../src/agent/scaling-domain.js";
import { InvalidArgumentError } from "../../src/errors.js";
import { AGENT_SERVICE_READY_MARKER_PATH } from "../../src/limits.js";
import { HostAccess } from "../../src/models.js";
import { generateAccessToken } from "../../src/payload.js";
import type { PoolConfig } from "../../src/pool/config.js";
import { beyondCapError, CAP_MARGIN_MS } from "../../src/sandbox/lifecycle.js";
import type { SandboxCreateOptions } from "../../src/sandbox/sandbox.js";
import { gatewayStatus } from "./agent-fake.js";
import {
  ENDPOINT,
  FakeSandbox,
  project,
  type Scripted,
  scriptedFetch,
  sse,
} from "./agent-project-fake.js";

export const FLEET_TOKEN = generateAccessToken();
/** Ahora: el tope de un VM listado (`listedCap`) se compara con el reloj real. */
export const START = new Date(Math.floor(Date.now() / 1000) * 1000);
const MS_PER_SECOND = 1000;

export function sandboxIdOf(number: number): string {
  return `microvm-00000000-0000-0000-0000-${String(number).padStart(12, "0")}`;
}

/** El host de un VM: lo que contesta `/ping` y `/invocations`. */
export class FleetHost {
  ping = "Healthy";
  invokeStatus = 200;
  pingStatus = 200;
  readonly invocations: Record<string, unknown>[] = [];
  busyAnswers = 0;
  pings = 0;

  respond(method: string, path: string, body: string | undefined, accept: string): Scripted {
    if (method === "GET" && path === "/ping") {
      this.pings += 1;
      return { status: this.pingStatus, body: JSON.stringify({ status: this.ping }) };
    }
    const payload = JSON.parse(body ?? "{}") as Record<string, unknown>;
    this.invocations.push(payload);
    if (this.busyAnswers > 0) {
      this.busyAnswers -= 1;
      return { status: 429, body: "{}" };
    }
    if (this.invokeStatus !== 200) {
      return { status: this.invokeStatus, body: "{}" };
    }
    const session =
      (payload.session_id as string | undefined) ??
      `ses_${String(this.invocations.length).padStart(26, "0")}`;
    const text = `respuesta a ${String(payload.prompt)}`;
    if (accept === "text/event-stream") {
      return {
        status: 200,
        body: sse(
          ["session", { session_id: session }],
          ["step_started", { index: 1 }],
          ["text", { text }],
          ["done", { session_id: session }],
        ),
      };
    }
    return {
      status: 200,
      body: JSON.stringify({
        v: 1,
        text,
        session_id: session,
        usage: { input: 1, output: 1 },
        steps: 1,
        failed: null,
      }),
    };
  }
}

export class FleetBackend {
  readonly hosts = new Map<string, FleetHost>();

  host(sandboxId: string): FleetHost {
    let host = this.hosts.get(sandboxId);
    if (host === undefined) {
      host = new FleetHost();
      this.hosts.set(sandboxId, host);
    }
    return host;
  }

  /** Un `fetch` que contesta según el VM del que sale el host de la URL. */
  readonly fetch: typeof fetch = async (input, init) => {
    const url = new URL(String(input));
    const host = this.host(url.hostname.split(".")[0] ?? "");
    const headers = (init?.headers ?? {}) as Record<string, string>;
    const response = host.respond(
      init?.method ?? "GET",
      url.pathname,
      init?.body === undefined ? undefined : String(init.body),
      headers.Accept ?? "",
    );
    return scriptedFetch(response).fetch(input, init);
  };
}

/** Un sandbox de doble con su propio id y su host. `served` dice si el host del
 * agente arrancó (una plaza aparcada aún no). `managed` dice si se creó con
 * plazo lógico (`maxLifetimeMs`/`onTimeout`): sin él, `setTimeout` falla como
 * en `rayd` (`lifecycle_unmanaged`). `maxLifetimeMs` fija, como en `rayd`, un
 * tope en `arranque + maxLifetimeMs - 60 s`: un `setTimeout` que pase de él se
 * rechaza con el tope (`timeout beyond cap`). `info` expone el tope de la
 * plataforma como `SandboxInfo.platformExpiresAt`. */
export class FleetFakeSandbox extends FakeSandbox {
  override readonly sandboxId: string;
  readonly number: number;
  served: boolean;
  state: string;
  managed: boolean;
  maxLifetimeMs: number | undefined;
  readonly startedAt = new Date();
  readonly timeouts: number[] = [];
  readonly beyondCap: number[] = [];
  killError: Error | undefined;
  /** Las pasarelas que la flota abrió en esta plaza (`openGateways`). */
  readonly configured: string[][] = [];
  configureError: Error | undefined;

  constructor(
    number: number,
    options: {
      metadata?: Record<string, string>;
      served?: boolean;
      state?: string;
      managed?: boolean;
      maxLifetimeMs?: number | undefined;
    } = {},
  ) {
    super({ metadata: options.metadata ?? {}, accessToken: FLEET_TOKEN });
    this.number = number;
    this.managed = options.managed ?? true;
    this.maxLifetimeMs = options.maxLifetimeMs;
    this.sandboxId = sandboxIdOf(number);
    this.served = options.served ?? true;
    this.state = options.state ?? "RUNNING";
  }

  override readonly files = {
    writeFiles: async (): Promise<void> => undefined,
    exists: async (path: string): Promise<boolean> =>
      path === AGENT_SERVICE_READY_MARKER_PATH ? this.served : false,
    read: async (): Promise<string> => "",
  };

  override readonly commands = {
    run: async (_cmd: string, options: Record<string, unknown>) => {
      if (options.tag === AGENT_SERVE_TAG) {
        this.served = true;
      }
      return this.handle;
    },
  };

  override async getHost(port: number): Promise<HostAccess> {
    return new HostAccess(`${this.sandboxId}.${ENDPOINT}`, port, () => this.token.jwe);
  }

  override async kill(): Promise<boolean> {
    this.kills += 1;
    this.state = "TERMINATED";
    if (this.killError !== undefined) {
      throw this.killError;
    }
    return true;
  }

  get info(): { readonly platformExpiresAt: Date | undefined } {
    return {
      platformExpiresAt:
        this.maxLifetimeMs === undefined
          ? undefined
          : new Date(this.startedAt.getTime() + this.maxLifetimeMs),
    };
  }

  /** El tope de `rayd`: el de la plataforma menos 60 s. */
  get cap(): Date | undefined {
    const platform = this.info.platformExpiresAt;
    return platform === undefined ? undefined : new Date(platform.getTime() - CAP_MARGIN_MS);
  }

  override async setTimeout(timeoutMs: number): Promise<void> {
    if (!this.managed) {
      throw new InvalidArgumentError("setTimeout no aplica: el sandbox no tiene plazo");
    }
    const cap = this.cap;
    if (cap !== undefined && Date.now() + timeoutMs > cap.getTime()) {
      this.beyondCap.push(timeoutMs / MS_PER_SECOND);
      throw beyondCapError(timeoutMs, cap.getTime());
    }
    this.timeouts.push(timeoutMs / MS_PER_SECOND);
  }

  override asService(): ServiceSandbox {
    return this as unknown as ServiceSandbox;
  }

  asFleet(): FleetSandbox {
    return this as unknown as FleetSandbox;
  }
}

export interface CreatedCall {
  readonly options: SandboxCreateOptions;
}

/** El `Sandbox` de doble: `create()` reparte sandboxes numerados, `connect()`
 * devuelve uno existente, `listMembers()` los vivos de la imagen y `kill()`
 * termina uno por id. Lo que cada llamada recibió queda anotado. */
export class FleetWorld implements SandboxFactory, FleetDirectory {
  readonly sandboxes = new Map<string, FleetFakeSandbox>();
  readonly created: CreatedCall[] = [];
  readonly connected: string[] = [];
  readonly sessionHits = new Map<string, string>();
  readonly foundSessions: string[] = [];
  /** Los VMs que el listado no puede probar que sean de la flota. */
  readonly unproven = new Set<string>();
  /** La versión de imagen con la que arrancó cada VM. */
  readonly versions = new Map<string, string>();
  createError: Error | undefined;
  #next = 1;

  /** Un VM que ya existía antes de la flota (reconexión, plaza aparcada). */
  add(
    options: {
      served?: boolean;
      state?: string;
      managed?: boolean;
      maxLifetimeMs?: number | undefined;
    } = {},
  ): FleetFakeSandbox {
    const sandbox = new FleetFakeSandbox(this.#next, {
      served: options.served ?? true,
      state: options.state ?? "RUNNING",
      managed: options.managed ?? true,
      maxLifetimeMs: options.maxLifetimeMs,
      metadata: { "rayito.agent.deployment": "support-bot", "rayito.agent.port": "8088" },
    });
    this.#next += 1;
    this.sandboxes.set(sandbox.sandboxId, sandbox);
    return sandbox;
  }

  async create(options: SandboxCreateOptions): Promise<ServiceSandbox> {
    this.created.push({ options });
    if (this.createError !== undefined) {
      throw this.createError;
    }
    const managed = options.maxLifetimeMs !== undefined || options.onTimeout !== undefined;
    const sandbox = this.add({ served: false, managed, maxLifetimeMs: options.maxLifetimeMs });
    Object.assign(sandbox.metadata, options.metadata ?? {});
    return sandbox.asService();
  }

  async listMembers(): Promise<readonly MemberListing[]> {
    return [...this.sandboxes.values()]
      .filter((sandbox) => sandbox.state !== "TERMINATED")
      .map((sandbox) => ({
        sandboxId: sandbox.sandboxId,
        state: sandbox.state,
        startedAt: new Date(START.getTime() + sandbox.number * MS_PER_SECOND),
        proven: !this.unproven.has(sandbox.sandboxId),
        templateVersion: this.versions.get(sandbox.sandboxId),
      }));
  }

  async connect(sandboxId: string): Promise<FleetSandbox> {
    this.connected.push(sandboxId);
    const sandbox = this.sandboxes.get(sandboxId);
    if (sandbox === undefined) {
      throw new Error("sandbox desconocido");
    }
    sandbox.state = "RUNNING";
    return sandbox.asFleet();
  }

  async kill(sandboxId: string): Promise<boolean> {
    const sandbox = this.sandboxes.get(sandboxId);
    if (sandbox === undefined) {
      return false;
    }
    await sandbox.kill();
    return true;
  }

  async findSession(_template: string, label: string): Promise<string | undefined> {
    this.foundSessions.push(label);
    return this.sessionHits.get(label);
  }

  /** `ConfigureSandbox` de doble: anota las pasarelas que la flota abre en una
   * plaza aparcada o falla si se le pide. */
  async openGateways(
    sandbox: FleetSandbox,
    gateways: Readonly<Record<string, unknown>>,
  ): Promise<void> {
    const fake = this.sandboxes.get(sandbox.sandboxId);
    if (fake?.configureError !== undefined) {
      throw fake.configureError;
    }
    fake?.configured.push(Object.keys(gateways));
  }

  get live(): FleetFakeSandbox[] {
    return [...this.sandboxes.values()].filter((sandbox) => sandbox.state !== "TERMINATED");
  }
}

export class FakeClock {
  now = 1000;
  readonly sleeps: number[] = [];
  readonly clock = (): number => this.now;
  readonly sleep = async (ms: number): Promise<void> => {
    this.sleeps.push(ms);
    this.now += ms;
  };
}

/** Un `SandboxPool` de doble: `take()` entrega un sandbox aparcado (o crea
 * uno) con las pasarelas pedidas; `stats()` informa de las plazas. */
export class FakePool implements FleetPool {
  readonly config: PoolConfig;
  started = false;
  readonly closed: boolean[] = [];
  readonly taken: Record<string, unknown>[] = [];
  readonly parked: FleetFakeSandbox[];
  readonly #world: FleetWorld;
  readonly #managed: boolean;
  readonly #maxLifetimeMs: number | undefined;

  constructor(config: PoolConfig, world: FleetWorld) {
    this.config = config;
    this.#world = world;
    this.#managed = config.onTimeout !== undefined;
    this.#maxLifetimeMs = config.maxLifetimeMs;
    this.parked = Array.from({ length: config.size }, () =>
      world.add({
        served: false,
        state: "SUSPENDED",
        managed: this.#managed,
        maxLifetimeMs: this.#maxLifetimeMs,
      }),
    );
  }

  async start(): Promise<unknown> {
    this.started = true;
    return this;
  }

  async take(options: Record<string, unknown>): Promise<FleetSandbox> {
    this.taken.push(options);
    const sandbox =
      this.parked.shift() ??
      this.#world.add({
        served: false,
        managed: this.#managed,
        maxLifetimeMs: this.#maxLifetimeMs,
      });
    sandbox.state = "RUNNING";
    return sandbox.asFleet();
  }

  stats() {
    return {
      ready: this.parked.length,
      slots: this.parked.map((sandbox) => ({
        sandboxId: sandbox.sandboxId,
        state: "ready" as const,
        startedAt: START,
        expiresAt: START,
        parkedAt: START,
      })),
    };
  }

  async close(options: { readonly drain?: boolean | undefined } = {}): Promise<void> {
    const drain = options.drain ?? true;
    this.closed.push(drain);
    if (drain) {
      for (const sandbox of this.parked) {
        await sandbox.kill();
      }
    }
  }
}

/** La flota y sus dobles juntos, con la API que usan los tests. */
export class FleetHarness {
  readonly world = new FleetWorld();
  readonly backend = new FleetBackend();
  readonly clock = new FakeClock();
  readonly pools: FakePool[] = [];

  service(the = project()): ProjectService {
    return new ProjectService(the, {
      sandboxFactory: this.world,
      fetch: this.backend.fetch,
      sleep: this.clock.sleep,
      now: this.clock.clock,
    });
  }

  fleet(
    scaling?: ScalingOptions,
    options: Partial<AgentFleetOptions> & { name?: string } = {},
  ): AgentFleet {
    const the = options.project === undefined && options.name !== undefined ? undefined : project();
    return new AgentFleet({
      project: the,
      accessToken: FLEET_TOKEN,
      scaling,
      service: the === undefined ? undefined : this.service(the),
      directory: this.world,
      poolFactory: (config) => {
        const pool = new FakePool(config, this.world);
        this.pools.push(pool);
        return pool;
      },
      fetch: this.backend.fetch,
      now: this.clock.clock,
      sleep: this.clock.sleep,
      ...options,
    });
  }

  async started(
    scaling?: ScalingOptions,
    options: Partial<AgentFleetOptions> & { name?: string } = {},
  ): Promise<AgentFleet> {
    const fleet = this.fleet(scaling, options);
    await fleet.start();
    return fleet;
  }

  host(number: number): FleetHost {
    return this.backend.host(sandboxIdOf(number));
  }
}

export { gatewayStatus };
