/**
 * `AgentFleet`: uno o varios MicroVMs que sirven un mismo agente, con las
 * sesiones repartidas entre ellos (ADR-029, D5). Espejo de
 * `rayito._agent._fleet_async`. Es la superficie de `deployAgent()`
 * (`deploy.ts`); el reparto, el id de sesión enrutable y la salud son lógica
 * pura de `fleet-base.ts`, y esta clase sólo hace la E/S.
 *
 * Sin servidor propio: el enrutado es del cliente. La flota vive en el proceso
 * que la usa; los VMs no dependen de él (se reconectan con
 * `AgentFleet.connect()` y se terminan solos por sus plazos). En un único
 * bucle de eventos no hace falta un lock: ninguna sección que toca el estado
 * de la flota espera (`await`) a medias.
 */

import {
  AgentError,
  CapacityError,
  InvalidArgumentError,
  SandboxError,
  SandboxNotFoundError,
} from "../errors.js";
import type { DynamoDbIndex } from "../index/dynamodb.js";
import { AGENT_SERVICE_READY_MARKER_PATH } from "../limits.js";
import type { PoolBackend } from "../pool/backend.js";
import type { PoolConfig } from "../pool/config.js";
import type { PoolStats } from "../pool/core.js";
import type { SandboxPoolOptions, TakeOptions } from "../pool/pool.js";
import type { ControlPlaneOptions } from "../sandbox/sandbox.js";
import {
  AgentDeployment,
  type AgentInvokeOptions,
  type DeploymentSandbox,
  labelsFor,
  parseLabels,
  ResultBuilder,
} from "./deployment.js";
import type { AgentEvent, AgentResult } from "./events.js";
import {
  AGENT_CAPACITY_CODE,
  AGENT_FLEET_BUSY_RETRY_MS,
  AGENT_FLEET_LIVE_STATES,
  AGENT_FLEET_PROBE_STATE,
  AGENT_FLEET_SESSION_LABEL,
  AGENT_FLEET_START_CONCURRENCY,
  AGENT_VM_LOST_CODE,
  capacityMessage,
  eventWithHandle,
  type FleetMemberStatus,
  type FleetSettings,
  type FleetStatus,
  fleetSettingsOf,
  fleetStatusOf,
  isLost,
  type Member,
  type MemberListing,
  markExtras,
  newMember,
  newReservation,
  ownedIds,
  pingIsFresh,
  planPlacement,
  poolConfigFor,
  recordBusy,
  recordPing,
  routeOf,
  selectMembers,
  sessionLabel,
  surplus,
  vmLostMessage,
  withHandle,
} from "./fleet-base.js";
import type { AgentProject } from "./project.js";
import {
  ProjectService,
  type ProjectServiceOptions,
  requestTimeoutMs,
  type ServiceSandbox,
} from "./project-service.js";
import {
  encodeHandle,
  replacementsNeeded,
  type ScalingOptions,
  SESSIONS_DEDICATED,
} from "./scaling-domain.js";

const NO_TOKEN_MESSAGE =
  "esta flota se reconectó sin access token: sólo puede listarse o terminarse; " +
  "pasa accessToken a AgentFleet.connect() para usarla";
const NO_PROJECT_MESSAGE =
  "esta flota se reconectó sin proyecto: no puede crear ni arrancar VMs; " +
  "pasa project a AgentFleet.connect()";

/** El `Sandbox` que la flota abre de un VM existente. */
export type FleetSandbox = ServiceSandbox & DeploymentSandbox;

/** Lo que la flota necesita de AWS para listar, abrir y terminar sus VMs: un
 * adaptador sobre `Sandbox` en producción y un doble en los tests. */
export interface FleetDirectory {
  listMembers(template: string): Promise<readonly MemberListing[]>;
  connect(sandboxId: string, accessToken: string): Promise<FleetSandbox>;
  kill(sandboxId: string): Promise<boolean>;
  findSession(template: string, label: string, index: DynamoDbIndex): Promise<string | undefined>;
}

/** El adaptador de producción de la flota: lista los VMs de la imagen
 * (`list-microvms` con el filtro de imagen) sin contar los que no son de un
 * agente (`selectMembers`: los `RUNNING` se sondean con un `Health`, sin token
 * ni despertar a nadie), conecta uno con el token de la flota y busca por
 * sesión en el índice opcional. */
export class SandboxDirectory implements FleetDirectory {
  readonly #connection: ControlPlaneOptions;

  constructor(connection: ControlPlaneOptions = {}) {
    this.#connection = connection;
  }

  async listMembers(template: string): Promise<readonly MemberListing[]> {
    const { Sandbox } = await import("../sandbox/sandbox.js");
    const listed: MemberListing[] = [];
    for await (const item of Sandbox.list({
      ...this.#connection,
      template,
      states: AGENT_FLEET_LIVE_STATES,
    })) {
      listed.push({ sandboxId: item.sandboxId, state: item.state, startedAt: item.startedAt });
    }
    const probed = [];
    for await (const item of Sandbox.list({
      ...this.#connection,
      template,
      states: [AGENT_FLEET_PROBE_STATE],
      metadata: {},
    })) {
      probed.push(item);
    }
    return selectMembers(listed, ownedIds(probed));
  }

  async connect(sandboxId: string, accessToken: string): Promise<FleetSandbox> {
    const { Sandbox } = await import("../sandbox/sandbox.js");
    return (await Sandbox.connect(sandboxId, {
      ...this.#connection,
      accessToken,
    })) as unknown as FleetSandbox;
  }

  async kill(sandboxId: string): Promise<boolean> {
    const { Sandbox } = await import("../sandbox/sandbox.js");
    return Sandbox.kill(sandboxId, this.#connection);
  }

  async findSession(
    template: string,
    label: string,
    index: DynamoDbIndex,
  ): Promise<string | undefined> {
    const { Sandbox } = await import("../sandbox/sandbox.js");
    for await (const item of Sandbox.list({
      ...this.#connection,
      template,
      states: AGENT_FLEET_LIVE_STATES,
      metadata: { [AGENT_FLEET_SESSION_LABEL]: label },
      index,
    })) {
      return item.sandboxId;
    }
    return undefined;
  }
}

/** El pool de plazas calientes que la flota usa (`SandboxPool` o un doble). */
export interface FleetPool {
  start(): Promise<unknown>;
  take(options: TakeOptions): Promise<FleetSandbox>;
  stats(): Pick<PoolStats, "ready" | "slots">;
  close(options?: { readonly drain?: boolean | undefined }): Promise<void>;
  readonly config: { readonly size: number };
}

export type FleetPoolFactory = (
  config: PoolConfig,
  options: SandboxPoolOptions,
) => FleetPool | Promise<FleetPool>;

export interface AgentFleetOptions {
  readonly project?: AgentProject | undefined;
  /** El nombre de la imagen de la flota; por defecto el del proyecto. */
  readonly name?: string | undefined;
  /** El token de todos los VMs; sin él (una flota reconectada para listarla
   * o terminarla) no se puede abrir ninguno. */
  readonly accessToken?: string | undefined;
  readonly scaling?: ScalingOptions | undefined;
  readonly service?: ProjectService | undefined;
  readonly directory?: FleetDirectory | undefined;
  readonly poolFactory?: FleetPoolFactory | undefined;
  readonly poolBackend?: PoolBackend | undefined;
  /** Índice de sesiones de los VMs dedicados. */
  readonly sessionIndex?: DynamoDbIndex | undefined;
  readonly keepOnFailure?: boolean | undefined;
  /** Región, credenciales y plano de control, como en `Sandbox.create()`. */
  readonly connection?: ControlPlaneOptions | undefined;
  readonly serviceOptions?: ProjectServiceOptions | undefined;
  readonly fetch?: typeof fetch | undefined;
  readonly now?: (() => number) | undefined;
  readonly sleep?: ((ms: number) => Promise<void>) | undefined;
}

export type AgentFleetConnectOptions = Omit<AgentFleetOptions, "name">;

/** Una invocación en curso en un VM: el miembro, la sesión que ve el host y la
 * reserva de un VM dedicado hasta conocer la sesión real. */
interface Lease {
  readonly member: Member<AgentDeployment>;
  readonly inner: string | undefined;
  readonly reservation: string | undefined;
}

function defaultSleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** El stream de una invocación de la flota: los eventos del VM con el id de
 * sesión enrutable, y el VM liberado al cerrarlo. */
export class AgentFleetStream implements AsyncIterable<AgentEvent> {
  readonly #events: AsyncIterator<AgentEvent>;
  readonly #first: IteratorResult<AgentEvent>;
  readonly #inner: { readonly sessionId: string | undefined; readonly droppedLines: number };
  readonly #sandboxId: string;
  readonly #routable: boolean;
  readonly #release: (sessionId: string | undefined) => Promise<void>;
  readonly #close: () => Promise<void>;
  readonly #builder = new ResultBuilder();
  #released = false;
  #started = false;

  constructor(options: {
    readonly iterator: AsyncIterator<AgentEvent>;
    readonly first: IteratorResult<AgentEvent>;
    readonly inner: { readonly sessionId: string | undefined; readonly droppedLines: number };
    readonly sandboxId: string;
    readonly routable: boolean;
    readonly release: (sessionId: string | undefined) => Promise<void>;
    readonly close: () => Promise<void>;
  }) {
    this.#events = options.iterator;
    this.#first = options.first;
    this.#inner = options.inner;
    this.#sandboxId = options.sandboxId;
    this.#routable = options.routable;
    this.#release = options.release;
    this.#close = options.close;
  }

  get sessionId(): string | undefined {
    const inner = this.#inner.sessionId;
    if (inner === undefined || !this.#routable) {
      return inner;
    }
    return encodeHandle(this.#sandboxId, inner);
  }

  get droppedLines(): number {
    return this.#inner.droppedLines;
  }

  async *[Symbol.asyncIterator](): AsyncGenerator<AgentEvent> {
    if (this.#started) {
      return;
    }
    this.#started = true;
    try {
      let step = this.#first;
      while (!step.done) {
        this.#builder.add(step.value);
        yield this.#routable ? eventWithHandle(step.value, this.#sandboxId) : step.value;
        step = await this.#events.next();
      }
    } finally {
      await this.close();
    }
  }

  /** Consume lo que falte y devuelve el `AgentResult`; si el agente falló
   * lanza `AgentError`. */
  async result(): Promise<AgentResult> {
    for await (const event of this) {
      void event;
    }
    const result = this.#builder.result(this.droppedLines);
    return this.#routable ? withHandle(result, this.#sandboxId) : result;
  }

  async close(): Promise<void> {
    await this.#close();
    if (!this.#released) {
      this.#released = true;
      await this.#release(this.#inner.sessionId);
    }
  }
}

/**
 * Un agente servido por uno o varios MicroVMs (`deployAgent()`).
 *
 * `invoke()` y `stream()` colocan cada sesión nueva en un VM con cupo (con
 * `ScalingOptions.sessions: "dedicated"`, uno por sesión), crean uno si hace
 * falta y cabe, y esperan en una cola cliente hasta
 * `ServeOptions.queueTimeoutMs` si la flota está llena (`CapacityError` con
 * `code: "agent_capacity"` al agotarse). Con más de un VM posible
 * (`maxVms > 1`), el `sessionId` del resultado es un handle
 * `rs1.<sandbox>.<sesión>`: al volver a pasarlo, la llamada va derecha a ese
 * VM; si el VM ya no existe, `SandboxError` con `code: "agent_vm_lost"`. Con
 * `maxVms === 1` el id de sesión no cambia.
 *
 * Un id de sesión elegido por el llamante no dice en qué VM está. Con un VM
 * (o `shared` y un solo proceso) no importa; con `dedicated`, sólo el mismo
 * proceso lo recuerda, salvo con `sessionIndex: new DynamoDbIndex(...)`, que
 * escribe una fila por VM dedicado para encontrarlo desde cualquier proceso.
 * Sin índice, un id desconocido empieza un VM nuevo y el resultado trae el
 * handle con el que seguir.
 *
 * Los VMs de la flota son todos los MicroVMs no terminados de la imagen del
 * proyecto (un MicroVM no se puede etiquetar): una imagen, una flota. Los VMs
 * por encima de `minVms` se terminan solos tras `scaleInAfterMs` sin uso
 * (`setTimeout` de `rayd`, sin servidor propio).
 *
 * Coste y activación
 * -------------------
 * Activa: `deployAgent()` (`rayito agent deploy`) crea `minVms` VMs y
 *     `warmVms` plazas aparcadas; `invoke()` crea más, hasta `maxVms`, si las
 *     sesiones llenan los que hay. Construir o reconectar la flota no crea
 *     nada.
 * Recursos y llamadas AWS: un MicroVM por VM (`run-microvm`), el token del
 *     proxy (`CreateMicrovmAuthToken`, gratuito) y `list-microvms` al
 *     reconectar; con `warmVms`, plazas suspendidas del pool; con
 *     `sessionIndex`, una fila de DynamoDB por VM dedicado.
 * Coste aproximado: el del MicroVM por segundo mientras vive (2 GB: ≈ $0,14
 *     por hora en us-east-1, consultado 2026-10-06 en
 *     https://aws.amazon.com/lambda/pricing/), por cada VM; una plaza
 *     aparcada no factura cómputo pero sí almacenamiento de snapshot.
 * IAM: los de `Sandbox.create()`, `Sandbox.list()` y `Sandbox.connect()`
 *     (`RayitoSandboxRunner`) y `secretsmanager:GetSecretValue` por
 *     pasarela; con `sessionIndex`, los de `DynamoDbIndex`.
 * Cómo apagarla: `stop()` termina todos los VMs; `minVms: 1, maxVms: 1,
 *     warmVms: 0` (por defecto) es un solo VM; sin llamar a `deployAgent()`
 *     nada se crea.
 * Ejemplo:
 *   const fleet = await deployAgent("./mi-agente");
 *   (await fleet.invoke("Resume el README.")).text;
 *   await fleet.stop();
 */
export class AgentFleet {
  readonly #project: AgentProject | undefined;
  readonly #name: string;
  readonly #token: string | undefined;
  #settings: FleetSettings;
  readonly #service: ProjectService | undefined;
  readonly #directory: FleetDirectory;
  readonly #poolFactory: FleetPoolFactory | undefined;
  readonly #poolBackend: PoolBackend | undefined;
  readonly #index: DynamoDbIndex | undefined;
  readonly #keepOnFailure: boolean;
  readonly #connection: ControlPlaneOptions;
  readonly #fetch: typeof fetch | undefined;
  readonly #now: () => number;
  readonly #sleep: (ms: number) => Promise<void>;
  #members: Member<AgentDeployment>[] = [];
  #growing = 0;
  readonly #sessions = new Map<string, string>();
  #pool: FleetPool | undefined;

  constructor(options: AgentFleetOptions & { readonly name?: string | undefined }) {
    if (options.project === undefined && options.name === undefined) {
      throw new InvalidArgumentError("AgentFleet necesita project o name");
    }
    this.#project = options.project;
    this.#name = options.name ?? options.project?.template ?? options.project?.name ?? "";
    this.#token = options.accessToken;
    this.#settings = fleetSettingsOf(options.project, options.scaling);
    this.#connection = options.connection ?? {};
    this.#fetch = options.fetch;
    this.#service =
      options.service ??
      (options.project === undefined
        ? undefined
        : new ProjectService(options.project, {
            ...options.serviceOptions,
            ...(options.fetch === undefined ? {} : { fetch: options.fetch }),
          }));
    this.#directory = options.directory ?? new SandboxDirectory(this.#connection);
    this.#poolFactory = options.poolFactory;
    this.#poolBackend = options.poolBackend;
    this.#index = options.sessionIndex;
    this.#keepOnFailure = options.keepOnFailure ?? false;
    this.#now = options.now ?? Date.now;
    this.#sleep = options.sleep ?? defaultSleep;
  }

  /** Reconstruye la flota de la imagen `name` en cualquier proceso: todos sus
   * MicroVMs no terminados, sin reanudar ninguno (un VM se abre al usarlo).
   * Sin `project` la flota enruta y consulta pero no puede crear VMs; sin
   * `accessToken`, sólo puede listarse o terminarse. */
  static async connect(name: string, options: AgentFleetConnectOptions = {}): Promise<AgentFleet> {
    const fleet = new AgentFleet({ ...options, name });
    await fleet.refreshMembers();
    return fleet;
  }

  get name(): string {
    return this.#name;
  }

  /** El access token de todos los VMs de la flota: guárdalo (nunca lo
   * imprimas ni lo versiones) para `AgentFleet.connect()` desde otro proceso.
   * Una flota reconectada sin token no lo tiene: `InvalidArgumentError`. */
  get accessToken(): string {
    return this.#requireToken();
  }

  get scaling(): ScalingOptions {
    return this.#settings.scaling;
  }

  /** Los clientes de los VMs que este proceso ya abrió. */
  get deployments(): readonly AgentDeployment[] {
    return this.#members.flatMap((member) =>
      member.deployment === undefined ? [] : [member.deployment],
    );
  }

  get sandboxIds(): readonly string[] {
    return this.#members.map((member) => member.sandboxId);
  }

  get urls(): readonly string[] {
    return this.deployments.map((deployment) => deployment.url);
  }

  // ------------------------------------------------------------ lifecycle

  /** Crea `minVms` VMs (todo o nada: si uno falla, termina los que creó) y
   * arranca el pool de plazas calientes. Lo llama `deployAgent()`. */
  async start(): Promise<this> {
    this.#requireProject();
    await this.#startPool();
    const missing = replacementsNeeded(this.#members.length, this.scaling);
    const created: Member<AgentDeployment>[] = [];
    const errors: unknown[] = [];
    let next = 0;
    const worker = async (): Promise<void> => {
      while (next < missing) {
        next += 1;
        try {
          created.push(await this.#createMember(false));
        } catch (error) {
          errors.push(error);
        }
      }
    };
    await Promise.all(
      Array.from({ length: Math.min(AGENT_FLEET_START_CONCURRENCY, missing) }, worker),
    );
    if (errors.length > 0) {
      if (!this.#keepOnFailure) {
        await this.#discard(created);
      }
      await this.#closePool(true);
      throw errors[0];
    }
    this.#members.push(...created);
    markExtras(this.#members, this.scaling);
    return this;
  }

  /** Vuelve a leer qué VMs de la imagen siguen vivos (`list-microvms`): añade
   * los nuevos, olvida los que ya no están y deja fuera las plazas aparcadas
   * del pool de este proceso. */
  async refreshMembers(): Promise<void> {
    const parked = this.#parkedIds();
    const listing = [...(await this.#directory.listMembers(this.#name))].sort(
      (a, b) => a.startedAt.getTime() - b.startedAt.getTime(),
    );
    const known = new Map(this.#members.map((member) => [member.sandboxId, member]));
    this.#members = listing
      .filter((item) => !parked.has(item.sandboxId))
      .map((item) => known.get(item.sandboxId) ?? newMember<AgentDeployment>(item.sandboxId));
    markExtras(this.#members, this.scaling);
  }

  /** Deja la flota dentro de `ScalingOptions`: olvida los VMs perdidos, crea
   * los que falten hasta `minVms` y termina los que sobren por encima de
   * `maxVms` (sólo los libres). */
  async reconcile(): Promise<void> {
    await this.refreshMembers();
    for (const member of this.#members.filter((candidate) => isLost(candidate))) {
      await this.#replace(member);
    }
    await this.#trim();
    await this.#fillToMinimum();
    await this.#resizePool();
  }

  /** Cambia los límites de la flota (se validan como `ScalingOptions`) y la
   * reconcilia: crea hasta `minVms`, termina los sobrantes y ajusta las
   * plazas calientes. */
  async scale(
    limits: {
      readonly minVms?: number | undefined;
      readonly maxVms?: number | undefined;
      readonly warmVms?: number | undefined;
    } = {},
  ): Promise<void> {
    const { ScalingOptions: Scaling } = await import("./scaling-domain.js");
    const current = this.scaling;
    this.#settings = {
      ...this.#settings,
      scaling: new Scaling({
        minVms: limits.minVms ?? current.minVms,
        maxVms: limits.maxVms ?? current.maxVms,
        warmVms: limits.warmVms ?? current.warmVms,
        sessions: current.sessions,
        scaleInAfterMs: current.scaleInAfterMs,
      }),
    };
    await this.reconcile();
  }

  /** Para el relleno de plazas calientes de este proceso y deja los VMs
   * vivos: un proceso de un solo uso (`rayito agent deploy`) termina así sin
   * tirar lo que creó. Las plazas aparcadas requieren un backend de pool
   * persistente (`poolBackend`). */
  async detach(): Promise<void> {
    await this.#closePool(this.#poolBackend === undefined);
  }

  /** Termina todos los VMs de la flota y las plazas del pool. Sigue aunque
   * uno falle y, al final, lanza el primer error. */
  async stop(): Promise<void> {
    const errors: unknown[] = [];
    await this.#closePool(true, errors);
    const members = this.#members;
    this.#members = [];
    for (const member of members) {
      try {
        await this.#terminate(member);
      } catch (error) {
        errors.push(error);
      }
    }
    if (errors.length > 0) {
      throw errors[0];
    }
  }

  /** Estado de la flota: pregunta a los VMs que este proceso ya abrió
   * (`GET /ping`) y deja como `unknown` los demás. */
  async status(): Promise<FleetStatus> {
    const statuses: FleetMemberStatus[] = [];
    for (const member of [...this.#members]) {
      if (member.deployment !== undefined) {
        await this.#probe(member, true);
      }
      statuses.push({
        sandboxId: member.sandboxId,
        state: member.health,
        extra: member.extra,
        inflight: member.inflight,
      });
    }
    const warmReady = this.#pool?.stats().ready ?? 0;
    return fleetStatusOf(this.#name, statuses, this.scaling, warmReady);
  }

  /** Espera a que las plazas calientes queden aparcadas; `false` si se agota
   * `timeoutMs` (la flota funciona igual: las plazas son una optimización de
   * latencia). */
  async waitWarm(timeoutMs: number, pollMs: number): Promise<boolean> {
    const pool = this.#pool;
    if (pool === undefined) {
      return true;
    }
    const deadline = this.#now() + timeoutMs;
    while (pool.stats().ready < this.scaling.warmVms) {
      if (this.#now() >= deadline) {
        return false;
      }
      await this.#sleep(pollMs);
    }
    return true;
  }

  // --------------------------------------------------------- invocations

  /** Una invocación sin streaming: el `AgentResult`, con el id de sesión
   * enrutable si la flota puede tener varios VMs. */
  async invoke(prompt: string, options: AgentInvokeOptions = {}): Promise<AgentResult> {
    const { sessionId, ...rest } = options;
    const [lease, result] = await this.#execute(sessionId, (deployment, inner) =>
      deployment.invoke(prompt, { ...rest, sessionId: inner }),
    );
    await this.#release(lease, result.sessionId);
    return this.#routable ? withHandle(result, lease.member.sandboxId) : result;
  }

  /** Como `invoke()`, con los eventos a medida que llegan. La petición se hace
   * aquí (un 429 se reintenta) y el VM queda ocupado hasta cerrar el stream. */
  async stream(prompt: string, options: AgentInvokeOptions = {}): Promise<AgentFleetStream> {
    const { sessionId, ...rest } = options;
    const [lease, opened] = await this.#execute(sessionId, async (deployment, inner) => {
      const stream = deployment.stream(prompt, { ...rest, sessionId: inner });
      const iterator = stream[Symbol.asyncIterator]();
      const first = await iterator.next();
      return { stream, iterator, first };
    });
    return new AgentFleetStream({
      iterator: opened.iterator,
      first: opened.first,
      inner: {
        get sessionId() {
          return opened.stream.sessionId;
        },
        get droppedLines() {
          return opened.stream.droppedLines;
        },
      },
      sandboxId: lease.member.sandboxId,
      routable: this.#routable,
      release: (inner) => this.#release(lease, inner),
      close: () => opened.stream.close(),
    });
  }

  // -------------------------------------------------------------- internals

  get #routable(): boolean {
    return this.scaling.routesByHandle;
  }

  #requireToken(): string {
    if (this.#token === undefined) {
      throw new InvalidArgumentError(NO_TOKEN_MESSAGE);
    }
    return this.#token;
  }

  #requireProject(): AgentProject {
    if (this.#project === undefined) {
      throw new InvalidArgumentError(NO_PROJECT_MESSAGE);
    }
    return this.#project;
  }

  /** Coloca la sesión, ejecuta `operation` en su VM y devuelve el
   * arrendamiento (con la invocación en curso anotada) y el resultado. Un 429
   * del host marca el VM ocupado y reintenta (en otro VM si la sesión es
   * nueva) hasta el plazo de la cola; un VM que ya no existe es
   * `agent_vm_lost`. */
  async #execute<T>(
    sessionId: string | undefined,
    operation: (deployment: AgentDeployment, inner: string | undefined) => Promise<T>,
  ): Promise<[Lease, T]> {
    const deadline = this.#now() + this.#settings.queueTimeoutMs;
    const routed = routeOf(sessionId) !== undefined;
    for (;;) {
      const lease = await this.#lease(sessionId, deadline);
      try {
        const deployment = await this.#openOrConnect(lease.member);
        return [lease, await operation(deployment, lease.inner)];
      } catch (error) {
        this.#unlease(lease);
        if (error instanceof AgentError && error.reason === "busy") {
          await this.#waitForCapacity(lease.member, deadline, routed);
          continue;
        }
        if (error instanceof SandboxNotFoundError) {
          await this.#replace(lease.member);
          throw new SandboxError(vmLostMessage(), { code: AGENT_VM_LOST_CODE });
        }
        if (error instanceof SandboxError) {
          await this.#noteTransportFailure(lease.member);
        }
        throw error;
      }
    }
  }

  /** Tras un 429: el VM cuenta como ocupado un intento; vencido el plazo,
   * `CapacityError`. Una sesión nueva vuelve a la cola, que espera si no hay
   * otro VM; una con handle sólo puede ir a su VM, así que espera aquí. */
  async #waitForCapacity(member: Member, deadline: number, routed: boolean): Promise<void> {
    recordBusy(member, this.#now());
    if (this.#now() >= deadline) {
      throw new CapacityError(capacityMessage(this.scaling), { code: AGENT_CAPACITY_CODE });
    }
    if (routed) {
      await this.#sleep(AGENT_FLEET_BUSY_RETRY_MS);
    }
  }

  async #lease(sessionId: string | undefined, deadline: number): Promise<Lease> {
    const route = routeOf(sessionId);
    if (route !== undefined) {
      return {
        member: await this.#routeMember(route.sandboxId),
        inner: route.sessionId,
        reservation: undefined,
      };
    }
    const [member, reservation] = await this.#place(sessionId, deadline);
    return { member, inner: sessionId, reservation };
  }

  async #routeMember(sandboxId: string): Promise<Member<AgentDeployment>> {
    let member = this.#find(sandboxId);
    if (member === undefined) {
      await this.refreshMembers();
      member = this.#find(sandboxId);
    }
    if (member === undefined) {
      throw new SandboxError(vmLostMessage(), { code: AGENT_VM_LOST_CODE });
    }
    member.inflight += 1;
    return member;
  }

  #find(sandboxId: string): Member<AgentDeployment> | undefined {
    return this.#members.find((member) => member.sandboxId === sandboxId);
  }

  /** La cola cliente: el VM de una sesión conocida, el primer VM libre, uno
   * nuevo si cabe, o espera hasta el plazo de la cola y entonces
   * `CapacityError`. */
  async #place(
    sessionId: string | undefined,
    deadline: number,
  ): Promise<[Member<AgentDeployment>, string | undefined]> {
    const settings = this.#settings;
    for (;;) {
      const known = await this.#knownSessionMember(sessionId);
      if (known !== undefined) {
        return [known, undefined];
      }
      const plan = planPlacement(
        this.#members,
        this.#growing,
        settings.scaling,
        settings.maxConcurrency,
        this.#now(),
      );
      const chosen = plan.member as Member<AgentDeployment> | undefined;
      const reservation = plan.kind === "use" ? this.#reserve(chosen) : undefined;
      if (plan.kind === "grow") {
        this.#growing += 1;
      }
      if (plan.kind === "use" && chosen !== undefined) {
        if (await this.#probe(chosen)) {
          this.#remember(sessionId, chosen);
          return [chosen, reservation];
        }
        this.#unreserve(chosen, reservation);
        continue;
      }
      if (plan.kind === "grow") {
        return this.#growFor(sessionId);
      }
      if (this.#now() >= deadline) {
        throw new CapacityError(capacityMessage(settings.scaling), { code: AGENT_CAPACITY_CODE });
      }
      await this.#sleep(AGENT_FLEET_BUSY_RETRY_MS);
    }
  }

  #reserve(member: Member | undefined): string | undefined {
    if (member === undefined) {
      return undefined;
    }
    member.inflight += 1;
    if (this.scaling.sessions !== SESSIONS_DEDICATED) {
      return undefined;
    }
    const reservation = newReservation();
    member.bound.add(reservation);
    return reservation;
  }

  #unreserve(member: Member, reservation: string | undefined): void {
    member.inflight -= 1;
    if (reservation !== undefined) {
      member.bound.delete(reservation);
    }
  }

  #remember(sessionId: string | undefined, member: Member): void {
    if (sessionId !== undefined && this.scaling.sessions === SESSIONS_DEDICATED) {
      this.#sessions.set(sessionId, member.sandboxId);
    }
  }

  /** El VM dedicado de una sesión que el llamante eligió: el que este proceso
   * recuerda o, con `sessionIndex`, el que el índice encuentra. */
  async #knownSessionMember(
    sessionId: string | undefined,
  ): Promise<Member<AgentDeployment> | undefined> {
    if (sessionId === undefined || this.scaling.sessions !== SESSIONS_DEDICATED) {
      return undefined;
    }
    let sandboxId = this.#sessions.get(sessionId);
    if (sandboxId === undefined && this.#index !== undefined) {
      sandboxId = await this.#directory.findSession(
        this.#name,
        sessionLabel(sessionId),
        this.#index,
      );
    }
    if (sandboxId === undefined) {
      return undefined;
    }
    const member = await this.#routeMember(sandboxId);
    this.#remember(sessionId, member);
    return member;
  }

  /** Un VM nuevo para la sesión: ocupado desde el primer instante (una
   * reserva si la flota es dedicada) para que ninguna otra se cuele. */
  async #growFor(
    sessionId: string | undefined,
  ): Promise<[Member<AgentDeployment>, string | undefined]> {
    const dedicated = this.scaling.sessions === SESSIONS_DEDICATED;
    let member: Member<AgentDeployment>;
    try {
      member = await this.#createMember(true, dedicated ? sessionId : undefined);
    } finally {
      this.#growing -= 1;
    }
    const reservation = dedicated ? newReservation() : undefined;
    member.inflight += 1;
    if (reservation !== undefined) {
      member.bound.add(reservation);
    }
    this.#members.push(member);
    markExtras(this.#members, this.scaling);
    this.#remember(sessionId, member);
    return [member, reservation];
  }

  /** Un VM nuevo con servicio: si crece la flota (`extra`) y la sesión no
   * necesita etiqueta propia, de una plaza caliente; si no, con `create()`
   * (los VMs base no gastan las plazas). */
  async #createMember(extra: boolean, sessionId?: string): Promise<Member<AgentDeployment>> {
    const project = this.#requireProject();
    const service = this.#service;
    if (service === undefined) {
      throw new InvalidArgumentError(NO_PROJECT_MESSAGE);
    }
    const label = this.#indexLabel(sessionId);
    let deployment: AgentDeployment;
    if (extra && this.#pool !== undefined && label === undefined) {
      const sandbox = await this.#pool.take({ gateways: { ...project.gateways } });
      try {
        deployment = await service.serve(sandbox as unknown as ServiceSandbox);
      } catch (error) {
        await this.#killQuietly(sandbox);
        throw error;
      }
    } else {
      deployment = await service.deploy({
        ...this.#connection,
        accessToken: this.#requireToken(),
        keepOnFailure: this.#keepOnFailure,
        ...(label === undefined ? {} : { metadata: { [AGENT_FLEET_SESSION_LABEL]: label } }),
        ...(label === undefined || this.#index === undefined ? {} : { index: this.#index }),
      });
    }
    const member = newMember(deployment.sandboxId, deployment, extra);
    await this.#limitLifetime(member);
    return member;
  }

  #indexLabel(sessionId: string | undefined): string | undefined {
    if (sessionId === undefined || this.#index === undefined) {
      return undefined;
    }
    return sessionLabel(sessionId);
  }

  async #killQuietly(sandbox: { kill(): Promise<boolean> }): Promise<void> {
    try {
      await sandbox.kill();
    } catch {
      return;
    }
  }

  /** A un VM por encima de `minVms` se le acota la vida: `rayd` lo termina
   * solo tras `scaleInAfterMs`, y cada uso la renueva. */
  async #limitLifetime(member: Member<AgentDeployment>): Promise<void> {
    if (!member.extra || member.deployment === undefined) {
      return;
    }
    try {
      await member.deployment.setTimeout(this.scaling.scaleInAfterMs);
    } catch (error) {
      if (!(error instanceof SandboxError)) {
        throw error;
      }
    }
  }

  /** El cliente del VM: el que ya hay o, la primera vez, conecta con el token
   * de la flota y, si el VM no tiene servicio (una plaza aparcada), lo
   * arranca. */
  async #openOrConnect(member: Member<AgentDeployment>): Promise<AgentDeployment> {
    if (member.deployment !== undefined) {
      return member.deployment;
    }
    const sandbox = await this.#directory.connect(member.sandboxId, this.#requireToken());
    const deployment = await this.#serving(sandbox);
    member.deployment = deployment;
    await this.#limitLifetime(member);
    return deployment;
  }

  async #serving(sandbox: FleetSandbox): Promise<AgentDeployment> {
    if (await sandbox.files.exists(AGENT_SERVICE_READY_MARKER_PATH)) {
      return this.#deploymentOf(sandbox);
    }
    if (this.#service === undefined) {
      throw new InvalidArgumentError(NO_PROJECT_MESSAGE);
    }
    return this.#service.serve(sandbox);
  }

  async #deploymentOf(sandbox: FleetSandbox): Promise<AgentDeployment> {
    const labels = parseLabels(sandbox.metadata);
    return new AgentDeployment({
      sandbox,
      port: labels.port,
      hostAccess: await sandbox.getHost(labels.port),
      project: labels.project,
      raw: labels.raw,
      ...(this.#project === undefined ? {} : { requestTimeoutMs: requestTimeoutMs(this.#project) }),
      ...(this.#fetch === undefined ? {} : { fetch: this.#fetch }),
    });
  }

  /** Si el VM puede atender una sesión: lo abre si hace falta y, si su último
   * ping caducó, le pregunta por `GET /ping`. Un VM perdido se sustituye. */
  async #probe(member: Member<AgentDeployment>, force = false): Promise<boolean> {
    let deployment: AgentDeployment;
    try {
      deployment = await this.#openOrConnect(member);
    } catch (error) {
      if (error instanceof SandboxNotFoundError) {
        this.#drop(member);
        return false;
      }
      throw error;
    }
    if (!force && pingIsFresh(member, this.#now())) {
      return member.health !== "unavailable";
    }
    const status = await deployment.status();
    recordPing(member, status.state, this.#now());
    if (isLost(member)) {
      await this.#replace(member);
      return false;
    }
    return status.state === "healthy";
  }

  /** Un fallo de transporte no basta para dar un VM por perdido: se pregunta a
   * `/ping` y sólo tras `AGENT_FLEET_UNHEALTHY_PINGS` fallos seguidos se
   * sustituye (la sesión de ese VM se pierde). */
  async #noteTransportFailure(member: Member<AgentDeployment>): Promise<void> {
    const deployment = member.deployment;
    if (deployment === undefined) {
      return;
    }
    const status = await deployment.status();
    recordPing(member, status.state, this.#now());
    if (isLost(member)) {
      await this.#replace(member);
    }
  }

  /** Suelta la invocación en curso: la reserva de un VM dedicado pasa a ser la
   * sesión real y a un VM sobrante se le renueva la vida. */
  async #release(lease: Lease, innerSession: string | undefined): Promise<void> {
    this.#unlease(lease, innerSession);
    if (innerSession !== undefined) {
      await this.#limitLifetime(lease.member);
    }
  }

  #unlease(lease: Lease, keep?: string): void {
    const member = lease.member;
    member.inflight = Math.max(0, member.inflight - 1);
    if (lease.reservation !== undefined) {
      member.bound.delete(lease.reservation);
      if (keep !== undefined) {
        member.bound.add(keep);
      }
    }
  }

  #drop(member: Member): void {
    this.#members = this.#members.filter((candidate) => candidate !== member);
    markExtras(this.#members, this.scaling);
  }

  /** Termina un VM perdido, lo saca de la flota y, si baja de `minVms`, crea
   * otro. */
  async #replace(member: Member<AgentDeployment>): Promise<void> {
    this.#drop(member);
    try {
      await this.#terminate(member);
    } catch {
      // Un VM perdido puede no responder a su propio cierre.
    }
    await this.#fillToMinimum();
  }

  async #fillToMinimum(): Promise<void> {
    if (this.#project === undefined) {
      return;
    }
    for (
      let missing = replacementsNeeded(this.#members.length, this.scaling);
      missing > 0;
      missing -= 1
    ) {
      this.#members.push(await this.#createMember(false));
      markExtras(this.#members, this.scaling);
    }
  }

  async #trim(): Promise<void> {
    const excess = surplus(this.#members, this.scaling).filter(
      (member) => member.inflight === 0 && member.bound.size === 0,
    ) as Member<AgentDeployment>[];
    for (const member of excess) {
      this.#drop(member);
      try {
        await this.#terminate(member);
      } catch {
        // Un VM sobrante que no se puede terminar acabará solo por su plazo.
      }
    }
  }

  /** Termina el VM sin abrirlo: abrir uno suspendido lo reanudaría sólo para
   * matarlo. */
  async #terminate(member: Member<AgentDeployment>): Promise<void> {
    if (member.deployment !== undefined) {
      await member.deployment.stop();
    } else {
      await this.#directory.kill(member.sandboxId);
    }
  }

  async #discard(members: readonly Member<AgentDeployment>[]): Promise<void> {
    for (const member of members) {
      try {
        await this.#terminate(member);
      } catch {
        // Un VM de un arranque fallido que no se puede terminar acabará por su plazo.
      }
    }
  }

  // -------------------------------------------------------------- warm pool

  #poolMetadata(): Readonly<Record<string, string>> {
    const project = this.#requireProject();
    return labelsFor(project.name, project.serve.port, { raw: false });
  }

  async #startPool(): Promise<void> {
    const scaling = this.scaling;
    if (scaling.warmVms === 0 || this.#pool !== undefined) {
      return;
    }
    const project = this.#requireProject();
    const config = poolConfigFor(project, scaling, this.#requireToken(), this.#poolMetadata());
    const factory = this.#poolFactory ?? defaultPool;
    const pool = await factory(config, {
      ...this.#connection,
      ...(this.#poolBackend === undefined ? {} : { backend: this.#poolBackend }),
    });
    await pool.start();
    this.#pool = pool;
  }

  async #resizePool(): Promise<void> {
    if (this.#pool !== undefined && this.#pool.config.size === this.scaling.warmVms) {
      return;
    }
    await this.#closePool(true);
    await this.#startPool();
  }

  async #closePool(drain: boolean, errors?: unknown[]): Promise<void> {
    const pool = this.#pool;
    this.#pool = undefined;
    if (pool === undefined) {
      return;
    }
    try {
      await pool.close({ drain });
    } catch (error) {
      errors?.push(error);
    }
  }

  #parkedIds(): Set<string> {
    return new Set((this.#pool?.stats().slots ?? []).map((slot) => slot.sandboxId));
  }
}

const defaultPool: FleetPoolFactory = async (config, options) => {
  const { SandboxPool } = await import("../pool/pool.js");
  return new SandboxPool(config, options) as unknown as FleetPool;
};
