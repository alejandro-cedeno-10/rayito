/**
 * Plano de control falso en memoria para los tests del pool, como
 * `clients/python/tests/unit/fake_control_plane.py`: una máquina de estados
 * por MicroVM que sirve un número variable de llamadas. `runMicrovm` arranca
 * un `rayd` falso por VM (con el `token_sha256` del `runHookPayload` de esa
 * plaza y su propio `sandboxId` en `Health`), `suspend`/`resume`/`terminate`
 * mueven el estado, `listMicrovms` refleja el mapa con el filtro por defecto
 * del SDK y cada operación pasa por un `TokenBucket` real sobre un reloj
 * falso. Cada llamada queda en `calls` con su instante; `fail(operation,
 * index, outcome)` programa un fallo (un error o `false` para un conflicto)
 * en la llamada número `index` (desde 1) de esa operación; `hold(operation,
 * count)` bloquea las siguientes `count` llamadas hasta `release`.
 * `addListedSandbox` registra un MicroVM con su `rayd` sin lanzarlo, para los
 * listados con filtro de metadatos, que `listMicrovmsPage` sirve paginados.
 */

import { randomUUID } from "node:crypto";
import {
  type ControlPlane,
  LaunchRequest,
  type ListMicrovmsOptions,
  type ListMicrovmsPageOptions,
  type PortSpec,
  TokenBucket,
} from "../../../src/aws/control-plane.js";
import { SandboxNotFoundError } from "../../../src/errors.js";
import { API_TPS, TERMINAL_STATES } from "../../../src/limits.js";
import {
  type MicrovmListPage,
  type SandboxInfo,
  type SandboxListItem,
  sandboxInfo,
  sandboxListItem,
} from "../../../src/models.js";
import { encodeAccessToken } from "../../../src/payload.js";
import { installedTokenSha256 } from "./common.js";
import { ACCOUNT_ID, IMAGE_ARN, JWE, REGION } from "./control-plane.js";
import { FakeRayd } from "./server.js";

export type FailureOutcome = Error | false;

export const LISTED_IMAGE_ARN = IMAGE_ARN;
/** El token de los sandboxes de `addListedSandbox`: `Health` no lo pide; `MetricsHistory` sí. */
export const LISTED_ACCESS_TOKEN = encodeAccessToken(
  new TextEncoder().encode("listed-sandbox-access-token-32b!"),
);

export interface ListedSandboxOptions {
  readonly metadata?: Readonly<Record<string, string>>;
  readonly state?: string;
  readonly startedAt?: Date;
  readonly imageArn?: string;
  readonly accessToken?: string;
}

export interface FakeCall {
  readonly operation: string;
  readonly sandboxId: string | undefined;
  readonly at: number;
}

export interface FakeMicrovm {
  readonly sandboxId: string;
  state: string;
  readonly endpoint: string;
  readonly request: LaunchRequest;
  readonly startedAt: Date;
  readonly rayd: FakeRayd;
  readonly tokenSha256: string;
  stateReason: string | undefined;
}

/** Reloj manual en segundos para los token buckets: `sleep` lo avanza. */
export class FakeClock {
  now: number;
  readonly sleeps: number[] = [];

  constructor(start = 1_000_000) {
    this.now = start;
  }

  seconds = (): number => this.now;

  sleep = async (seconds: number): Promise<void> => {
    this.sleeps.push(seconds);
    this.now += seconds;
  };

  advance(seconds: number): void {
    this.now += seconds;
  }
}

class Gate {
  #remaining: number;
  readonly #released: Promise<void>;
  #release: () => void = () => undefined;

  constructor(count: number) {
    this.#remaining = count;
    this.#released = new Promise((resolve) => {
      this.#release = resolve;
    });
  }

  take(): Promise<void> | undefined {
    if (this.#remaining <= 0) {
      return undefined;
    }
    this.#remaining -= 1;
    return this.#released;
  }

  release(): void {
    this.#release();
  }
}

export class FakePoolControlPlane implements ControlPlane {
  readonly region = REGION;
  readonly clock: FakeClock;
  readonly microvms = new Map<string, FakeMicrovm>();
  readonly calls: FakeCall[] = [];
  readonly pageRequests: ListMicrovmsPageOptions[] = [];
  readonly #now: () => Date;
  readonly #buckets = new Map<string, TokenBucket>();
  readonly #failures = new Map<string, Map<number, FailureOutcome>>();
  readonly #gates = new Map<string, Gate>();
  readonly #counts = new Map<string, number>();
  #mints = 0;
  /** Máximo de VMs `RUNNING` (calentamientos en vuelo sin tomas) visto al aceptar un `runMicrovm`. */
  maxRunningAtLaunch = 0;

  constructor(options: { readonly clock?: FakeClock; readonly now?: () => Date } = {}) {
    this.clock = options.clock ?? new FakeClock();
    this.#now = options.now ?? (() => new Date());
    for (const [operation, rate] of Object.entries(API_TPS)) {
      this.#buckets.set(
        operation,
        new TokenBucket(rate, { now: this.clock.seconds, sleep: this.clock.sleep }),
      );
    }
  }

  // ------------------------------------------------------------- scripting

  fail(operation: string, index: number, outcome: FailureOutcome): void {
    let failures = this.#failures.get(operation);
    if (failures === undefined) {
      failures = new Map();
      this.#failures.set(operation, failures);
    }
    failures.set(index, outcome);
  }

  hold(operation: string, count: number): void {
    this.#gates.set(operation, new Gate(count));
  }

  release(operation: string): void {
    this.#gates.get(operation)?.release();
  }

  releaseAll(): void {
    for (const gate of this.#gates.values()) {
      gate.release();
    }
  }

  setState(sandboxId: string, state: string, reason?: string): void {
    const vm = this.#require(sandboxId);
    vm.state = state;
    vm.stateReason = reason;
  }

  callsTo(operation: string): FakeCall[] {
    return this.calls.filter((call) => call.operation === operation);
  }

  timestamps(operation: string): number[] {
    return this.callsTo(operation).map((call) => call.at);
  }

  opsFor(sandboxId: string, since = 0): string[] {
    return this.calls
      .slice(since)
      .filter((call) => call.sandboxId === sandboxId)
      .map((call) => call.operation);
  }

  get liveIds(): string[] {
    return [...this.microvms.values()]
      .filter((vm) => !TERMINAL_STATES.has(vm.state))
      .map((vm) => vm.sandboxId);
  }

  async close(): Promise<void> {
    this.releaseAll();
    for (const vm of this.microvms.values()) {
      await vm.rayd.close();
    }
  }

  // ------------------------------------------------------------ ControlPlane

  async resolveTemplateArn(template: string): Promise<string> {
    return template.startsWith("arn:")
      ? template
      : `arn:aws:lambda:${REGION}:${ACCOUNT_ID}:microvm-image:${template}`;
  }

  async runMicrovm(request: LaunchRequest): Promise<SandboxInfo> {
    await this.#enter("RunMicrovm", undefined);
    const running = [...this.microvms.values()].filter((vm) => vm.state === "RUNNING").length + 1;
    this.maxRunningAtLaunch = Math.max(this.maxRunningAtLaunch, running);
    const payload = JSON.parse(request.runHookPayload) as { token_sha256: string };
    const sandboxId = `microvm-${randomUUID()}`;
    const rayd = await FakeRayd.start({ tokenSha256: payload.token_sha256, sandboxId });
    const vm: FakeMicrovm = {
      sandboxId,
      state: "RUNNING",
      endpoint: `${rayd.host}:${rayd.port}`,
      request,
      startedAt: this.#now(),
      rayd,
      tokenSha256: payload.token_sha256,
      stateReason: undefined,
    };
    this.microvms.set(sandboxId, vm);
    return this.#info(vm);
  }

  async getMicrovm(sandboxId: string): Promise<SandboxInfo> {
    await this.#enter("GetMicrovm", sandboxId);
    return this.#info(this.#require(sandboxId));
  }

  async *listMicrovms(options: ListMicrovmsOptions = {}): AsyncIterable<SandboxListItem> {
    await this.#enter("ListMicrovms", undefined);
    const wanted = options.states === undefined ? undefined : new Set(options.states);
    for (const vm of [...this.microvms.values()]) {
      if (options.imageArn !== undefined && vm.request.imageArn !== options.imageArn) {
        continue;
      }
      const keep = wanted === undefined ? !TERMINAL_STATES.has(vm.state) : wanted.has(vm.state);
      if (keep) {
        yield this.#listItem(vm);
      }
    }
  }

  /** El mapa en orden de inserción, en páginas de `maxResults`; filtra por imagen como AWS, nunca por estado. */
  async listMicrovmsPage(options: ListMicrovmsPageOptions): Promise<MicrovmListPage> {
    await this.#enter("ListMicrovms", undefined);
    this.pageRequests.push(options);
    const matching = [...this.microvms.values()].filter(
      (vm) => options.imageArn === undefined || vm.request.imageArn === options.imageArn,
    );
    const start = options.nextToken === undefined ? 0 : Number(options.nextToken);
    const end = start + options.maxResults;
    return {
      items: matching.slice(start, end).map((vm) => this.#listItem(vm)),
      nextToken: end < matching.length ? String(end) : undefined,
    };
  }

  /**
   * Un MicroVM ya arrancado con su propio `rayd` falso, sin pasar por
   * `runMicrovm` (ni por su bucket): `metadata` es lo que su `Health`
   * devuelve.
   */
  async addListedSandbox(options: ListedSandboxOptions = {}): Promise<FakeMicrovm> {
    const sandboxId = `microvm-${randomUUID()}`;
    const tokenSha256 = installedTokenSha256(options.accessToken ?? LISTED_ACCESS_TOKEN);
    const rayd = await FakeRayd.start({ tokenSha256, sandboxId });
    rayd.health.metadata = { ...(options.metadata ?? {}) };
    const vm: FakeMicrovm = {
      sandboxId,
      state: options.state ?? "RUNNING",
      endpoint: `${rayd.host}:${rayd.port}`,
      request: new LaunchRequest({
        imageArn: options.imageArn ?? LISTED_IMAGE_ARN,
        maximumDurationSeconds: 3600,
        runHookPayload: "{}",
        clientToken: randomUUID(),
        logging: { disabled: {} },
      }),
      startedAt: options.startedAt ?? this.#now(),
      rayd,
      tokenSha256,
      stateReason: undefined,
    };
    this.microvms.set(sandboxId, vm);
    return vm;
  }

  async terminateMicrovm(sandboxId: string): Promise<boolean> {
    if ((await this.#enter("TerminateMicrovm", sandboxId)) === false) {
      return false;
    }
    const vm = this.microvms.get(sandboxId);
    if (vm === undefined) {
      return false;
    }
    vm.state = "TERMINATED";
    vm.stateReason = vm.stateReason ?? "Success.";
    return true;
  }

  async suspendMicrovm(sandboxId: string): Promise<boolean> {
    if ((await this.#enter("SuspendMicrovm", sandboxId)) === false) {
      return false;
    }
    const vm = this.#require(sandboxId);
    if (vm.state !== "RUNNING") {
      return false;
    }
    vm.state = "SUSPENDED";
    return true;
  }

  async resumeMicrovm(sandboxId: string): Promise<boolean> {
    if ((await this.#enter("ResumeMicrovm", sandboxId)) === false) {
      return false;
    }
    const vm = this.#require(sandboxId);
    if (vm.state !== "SUSPENDED") {
      return false;
    }
    vm.state = "RUNNING";
    return true;
  }

  async createAuthToken(sandboxId: string, _ports: readonly PortSpec[]): Promise<string> {
    await this.#enter("CreateMicrovmAuthToken", sandboxId);
    this.#require(sandboxId);
    this.#mints += 1;
    return `${JWE}.${this.#mints}`;
  }

  // --------------------------------------------------------------- internals

  async #enter(
    operation: string,
    sandboxId: string | undefined,
  ): Promise<FailureOutcome | undefined> {
    await this.#buckets.get(operation)?.acquire();
    const index = (this.#counts.get(operation) ?? 0) + 1;
    this.#counts.set(operation, index);
    this.calls.push({ operation, sandboxId, at: this.clock.seconds() });
    const failures = this.#failures.get(operation);
    const outcome = failures?.get(index);
    failures?.delete(index);
    const gate = this.#gates.get(operation)?.take();
    if (gate !== undefined) {
      await gate;
    }
    if (outcome instanceof Error) {
      throw outcome;
    }
    return outcome;
  }

  #require(sandboxId: string): FakeMicrovm {
    const vm = this.microvms.get(sandboxId);
    if (vm === undefined) {
      throw new SandboxNotFoundError(`MicroVM ${sandboxId} no existe`);
    }
    return vm;
  }

  #listItem(vm: FakeMicrovm): SandboxListItem {
    return sandboxListItem({
      sandboxId: vm.sandboxId,
      state: vm.state,
      template: vm.request.imageArn,
      templateVersion: vm.request.imageVersion ?? "1.0",
      startedAt: vm.startedAt,
    });
  }

  #info(vm: FakeMicrovm): SandboxInfo {
    return sandboxInfo({
      sandboxId: vm.sandboxId,
      state: vm.state,
      endpoint: vm.endpoint,
      template: vm.request.imageArn,
      templateVersion: vm.request.imageVersion ?? "1.0",
      startedAt: vm.startedAt,
      maximumDurationSeconds: vm.request.maximumDurationSeconds,
      stateReason: vm.stateReason,
      idle: vm.request.idle,
      executionRoleArn: vm.request.executionRoleArn,
    });
  }
}

/** Cuántas llamadas caben como mucho en una ventana deslizante de `window` s. */
export function maxCallsInWindow(timestamps: readonly number[], window: number): number {
  const ordered = [...timestamps].sort((a, b) => a - b);
  let best = 0;
  let start = 0;
  for (let end = 0; end < ordered.length; end += 1) {
    while ((ordered[end] as number) - (ordered[start] as number) >= window) {
      start += 1;
    }
    best = Math.max(best, end - start + 1);
  }
  return best;
}
