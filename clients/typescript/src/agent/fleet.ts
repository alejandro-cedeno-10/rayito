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

import type { AwsClientSettings } from "../aws/control-plane.js";
import {
  AgentError,
  CapacityError,
  InvalidArgumentError,
  RateLimitError,
  SandboxError,
  SandboxNotFoundError,
  TimeoutError,
} from "../errors.js";
import type { DynamoDbIndex } from "../index/dynamodb.js";
import { AGENT_SERVICE_READY_MARKER_PATH } from "../limits.js";
import type { Logger } from "../logger.js";
import type { SandboxInfo } from "../models.js";
import type { PoolBackend } from "../pool/backend.js";
import type { PoolConfig } from "../pool/config.js";
import type { PoolStats } from "../pool/core.js";
import type { SandboxPoolOptions, TakeOptions } from "../pool/pool.js";
import { DEFAULT_REQUEST_TIMEOUT_MS } from "../sandbox/launch.js";
import { LifetimeBeyondCapError } from "../sandbox/lifecycle.js";
import type { ControlPlaneOptions, Sandbox } from "../sandbox/sandbox.js";
import type { SecretGateway } from "../secret-gateway/domain.js";
import {
  AgentDeployment,
  type AgentInputRequest,
  type AgentInvocation,
  type AgentInvokeOptions,
  type DeploymentSandbox,
  invocationOf,
  labelsFor,
  parseLabels,
  ResultBuilder,
  requirePromptOrInput,
} from "./deployment.js";
import type { AgentEvent, AgentResult } from "./events.js";
import {
  AGENT_CAPACITY_CODE,
  AGENT_FLEET_BUSY_RETRY_MS,
  AGENT_FLEET_LIVE_STATES,
  AGENT_FLEET_PROBE_CONCURRENCY,
  AGENT_FLEET_PROBE_STATE,
  AGENT_FLEET_SESSION_LABEL,
  AGENT_FLEET_START_CONCURRENCY,
  AGENT_VM_LOST_CODE,
  capacityMessage,
  correctedCap,
  eventWithHandle,
  type FleetLifetime,
  type FleetMemberStatus,
  type FleetSettings,
  type FleetStatus,
  fleetLifetime,
  fleetSettingsOf,
  fleetStatusOf,
  holdMs,
  isLost,
  lifetimeMs,
  listedCap,
  type Member,
  type MemberListing,
  type MemberProbe,
  mapBounded,
  markExtras,
  memberOf,
  newMember,
  newReservation,
  pingIsFresh,
  planPlacement,
  poolConfigFor,
  recordBusy,
  recordPing,
  restoreMs,
  routeOf,
  sandboxCap,
  selectProbed,
  sessionLabel,
  surplus,
  UNMANAGED_LIFETIME_MESSAGE,
  UNPROVEN_MEMBER_MESSAGE,
  UNREADABLE_MEMBER_MESSAGE,
  vmLostMessage,
  withHandle,
} from "./fleet-base.js";
import { type FleetRecipe, recipeOf, renewalOnConnect } from "./fleet-recipe.js";
import {
  defaultRecipeStore,
  type FleetRecipeStore,
  forgetRecipe,
  loadRecipe,
  RECIPE_SAVED,
  storeRecipe,
  warnRecipe,
} from "./fleet-recipe-aws.js";
import {
  type GatewayKeyRenewer,
  gatewayKeyRenewer,
  RENEW_NEEDS_BEDROCK,
  renewableGateways,
} from "./gateway-keys.js";
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
  "esta flota se reconectó sin proyecto ni receta: no puede crear ni arrancar VMs; " +
  "pasa project a AgentFleet.connect() o despliégala con rayito agent deploy";
const PARKED_SLOT_MESSAGE = "una plaza aparcada no pudo abrir las pasarelas del proyecto";

/** Una plaza aparcada que no pudo abrir las pasarelas del proyecto: la flota
 * ya la sacó y la sustituyó, y quien la eligió busca otra. */
class ParkedSlotError extends SandboxError {}

/** El `Sandbox` que la flota abre de un VM existente. */
export type FleetSandbox = ServiceSandbox & DeploymentSandbox;

/** Lo que la flota necesita de AWS para listar, abrir y terminar sus VMs: un
 * adaptador sobre `Sandbox` en producción y un doble en los tests. */
export interface FleetDirectory {
  listMembers(template: string): Promise<readonly MemberListing[]>;
  connect(sandboxId: string, accessToken: string): Promise<FleetSandbox>;
  kill(sandboxId: string): Promise<boolean>;
  findSession(template: string, label: string, index: DynamoDbIndex): Promise<string | undefined>;
  /** Abre `gateways` en un VM ya conectado (una plaza aparcada) con el mismo
   * camino que `SandboxPool.take({ gateways })`, sin terminarlo si falla. */
  openGateways(
    sandbox: FleetSandbox,
    gateways: Readonly<Record<string, SecretGateway>>,
    options: { readonly timeoutMs: number },
  ): Promise<void>;
}

/** La sonda de un VM `RUNNING` que usa `SandboxDirectory`: `get-microvm` más
 * un `Health` sin token, con los metadatos del agente si está listo. */
export type MemberInfoProbe = (
  sandboxId: string,
  connection: ControlPlaneOptions,
) => Promise<Pick<SandboxInfo, "sandboxId" | "state" | "metadata">>;

/** Opciones de `SandboxDirectory`: el logger de la flota y, en tests, la
 * sonda de cada VM. */
export interface SandboxDirectoryOptions {
  readonly logger?: Logger | undefined;
  readonly probeInfo?: MemberInfoProbe | undefined;
}

/** Los errores de la sonda que no dicen nada del VM sino del plano de control
 * (una limitación o un plazo de `get-microvm`): hacen fallar el listado en vez
 * de dejar fuera a un miembro sano. */
function isControlPlaneListingError(error: SandboxError): boolean {
  return error instanceof RateLimitError || error instanceof TimeoutError;
}

async function probeMemberInfo(
  sandboxId: string,
  connection: ControlPlaneOptions,
): Promise<SandboxInfo> {
  const { probeSandboxInfo } = await import("../sandbox/info-probe.js");
  return probeSandboxInfo(sandboxId, connection);
}

/** El adaptador de producción de la flota: lista los VMs de la imagen
 * (`list-microvms` con el filtro de imagen) sin contar los que no son de un
 * agente (`selectMembers`: los `RUNNING` se sondean uno a uno con un `Health`,
 * sin token ni despertar a nadie), conecta uno con el token de la flota y busca
 * por sesión en el índice opcional. Un VM cuya sonda falla (la plataforma aún
 * lo lista `RUNNING` pero su `rayd` ya salió) queda fuera como un VM que
 * termina, sin tumbar el listado. */
export class SandboxDirectory implements FleetDirectory {
  readonly #connection: ControlPlaneOptions;
  readonly #logger: Logger | undefined;
  readonly #probeInfo: MemberInfoProbe;

  constructor(connection: ControlPlaneOptions = {}, options: SandboxDirectoryOptions = {}) {
    this.#connection = connection;
    this.#logger = options.logger;
    this.#probeInfo = options.probeInfo ?? probeMemberInfo;
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
    const running = listed.filter((item) => item.state === AGENT_FLEET_PROBE_STATE);
    const probes = await mapBounded(running, AGENT_FLEET_PROBE_CONCURRENCY, (item) =>
      this.#probe(item.sandboxId),
    );
    for (const probe of probes) {
      if (probe.error !== undefined) {
        this.#logger?.warn?.(UNREADABLE_MEMBER_MESSAGE, {
          sandboxId: probe.sandboxId,
          error: probe.error,
        });
      }
    }
    return selectProbed(listed, probes);
  }

  /** La sonda de un VM `RUNNING`: su información con metadatos, o sin ella si
   * su `Health` falla (con la clase del error), ya no existe o ya no está
   * `RUNNING`. Una limitación o un plazo de `get-microvm` sí hacen fallar el
   * listado. */
  async #probe(sandboxId: string): Promise<MemberProbe> {
    let info: Pick<SandboxInfo, "sandboxId" | "state" | "metadata">;
    try {
      info = await this.#probeInfo(sandboxId, this.#connection);
    } catch (error) {
      if (!(error instanceof SandboxError) || isControlPlaneListingError(error)) {
        throw error;
      }
      return { sandboxId, info: undefined, error: error.name };
    }
    return { sandboxId, info: info.state === AGENT_FLEET_PROBE_STATE ? info : undefined };
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

  async openGateways(
    sandbox: FleetSandbox,
    gateways: Readonly<Record<string, SecretGateway>>,
    options: { readonly timeoutMs: number },
  ): Promise<void> {
    const { applyGateways } = await import("../pool/pool.js");
    await applyGateways(sandbox as unknown as Sandbox, gateways, options);
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
  /** Credenciales de las llamadas de la receta y de la renovación de claves;
   * por defecto la cadena por defecto. */
  readonly credentials?: AwsClientSettings["credentials"] | undefined;
  /** Dónde avisa la flota (los códigos de la receta); en silencio por
   * defecto. */
  readonly logger?: Logger | undefined;
  /**
   * Renueva la clave de corta duración de Bedrock de las pasarelas `bedrock`
   * antes de crear cada VM o abrir cada plaza (por defecto `false`).
   *
   * Coste y activación
   * -------------------
   * Activa: `renewGatewayKeys: true` (`--renew-gateway-keys`); necesita los
   *     peers opcionales `@aws/bedrock-token-generator` y
   *     `@aws-sdk/client-secrets-manager`.
   * Recursos y llamadas AWS: la firma es local; `DescribeSecret` y, como
   *     mucho una vez cada `gatewayKeyRenewAfterSeconds`, `PutSecretValue`
   *     sobre el secreto de la pasarela.
   * Coste aproximado: $0,05 por 10 000 llamadas a Secrets Manager (us-east-1,
   *     consultado 2026-10-22 en https://aws.amazon.com/secrets-manager/pricing/).
   * IAM: `secretsmanager:DescribeSecret`, `secretsmanager:PutSecretValue` y
   *     `bedrock:CallWithBearerToken` en el principal que firma.
   * Cómo apagarla: no la pases (por defecto `false`).
   * Ejemplo:
   *   await AgentFleet.connect("my-agent", { accessToken, renewGatewayKeys: true });
   */
  readonly renewGatewayKeys?: boolean | undefined;
  /** La receta que leyó `connect()`; sólo la pasa `connect()`. */
  readonly recipe?: FleetRecipe | undefined;
  /** Dónde vive la receta (por defecto, Parameter Store). */
  readonly recipeStore?: FleetRecipeStore | undefined;
  /** Un renovador de claves propio, p. ej. en tests. */
  readonly keyRenewer?: Pick<GatewayKeyRenewer, "renew"> | undefined;
}

export interface AgentFleetConnectOptions extends Omit<AgentFleetOptions, "name" | "recipe"> {
  /** Sin `project`, lee la receta que guardó `deployAgent()` (por defecto
   * `true`); con `false` la flota es la de 0.16. */
  readonly useRecipe?: boolean | undefined;
}

export interface AgentFleetStopOptions {
  /** Conserva la receta de la flota (por defecto se borra si este proceso la
   * guardó o la leyó). */
  readonly keepRecipe?: boolean | undefined;
}

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
 *     pasarela; con `sessionIndex`, los de `DynamoDbIndex`. Leer y borrar la
 *     receta (`connect(nombre)` sin `project`, `stop()`), los de
 *     `SsmRecipeStore`; con `renewGatewayKeys`, los de `GatewayKeyRenewer`.
 * Cómo apagarla: `stop()` termina todos los VMs; `minVms: 1, maxVms: 1,
 *     warmVms: 0` (por defecto) es un solo VM; sin llamar a `deployAgent()`
 *     nada se crea. `connect(nombre, { useRecipe: false })` no lee la receta
 *     y `stop({ keepRecipe: true })` no la borra; sin `renewGatewayKeys` no
 *     se renueva ninguna clave.
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
  readonly #credentials: AwsClientSettings["credentials"] | undefined;
  readonly #logger: Logger | undefined;
  #renewKeys: boolean;
  #renewer: Pick<GatewayKeyRenewer, "renew"> | undefined;
  #renewing: Promise<void> | undefined;
  #recipeStore: FleetRecipeStore | undefined;
  #recipeKnown: boolean;
  #members: Member<AgentDeployment>[] = [];
  #growing = 0;
  /** La cola de `setTimeout` de cada VM (`#lifetimeTurn`), por id. */
  readonly #lifetimeTurns = new Map<string, Promise<void>>();
  readonly #sessions = new Map<string, string>();
  #pool: FleetPool | undefined;

  constructor(options: AgentFleetOptions & { readonly name?: string | undefined }) {
    if (options.project === undefined && options.name === undefined) {
      throw new InvalidArgumentError("AgentFleet necesita project o name");
    }
    this.#project = options.project;
    this.#name = options.name ?? options.project?.template ?? options.project?.name ?? "";
    this.#token = options.accessToken;
    this.#settings = fleetSettingsOf(options.project, options.scaling, options.recipe?.scaling);
    this.#connection = options.connection ?? {};
    this.#fetch = options.fetch;
    const imageVersion = options.recipe?.imageVersion;
    this.#service =
      options.service ??
      (options.project === undefined
        ? undefined
        : new ProjectService(options.project, {
            ...options.serviceOptions,
            ...(options.fetch === undefined ? {} : { fetch: options.fetch }),
            ...(imageVersion === undefined ? {} : { imageVersion }),
          }));
    this.#credentials = options.credentials;
    this.#logger = options.logger;
    this.#renewKeys = options.renewGatewayKeys ?? false;
    if (this.#renewKeys) {
      if (options.project === undefined) {
        throw new InvalidArgumentError(RENEW_NEEDS_BEDROCK);
      }
      renewableGateways(options.project);
    }
    this.#renewer = this.#renewKeys ? options.keyRenewer : undefined;
    this.#recipeStore = options.recipeStore;
    this.#recipeKnown = options.recipe !== undefined;
    this.#directory =
      options.directory ?? new SandboxDirectory(this.#connection, { logger: options.logger });
    this.#poolFactory = options.poolFactory;
    this.#poolBackend = options.poolBackend;
    this.#index = options.sessionIndex;
    this.#keepOnFailure = options.keepOnFailure ?? false;
    this.#now = options.now ?? Date.now;
    this.#sleep = options.sleep ?? defaultSleep;
  }

  /** Reconstruye la flota de la imagen `name` en cualquier proceso: todos sus
   * MicroVMs no terminados, sin reanudar ninguno (un VM se abre al usarlo).
   *
   * Sin `project` y con `useRecipe` (por defecto) lee la receta que guardó
   * `deployAgent()` (un `GetParameter`): con ella la flota crea VMs iguales a
   * los del despliegue (proyecto tras las banderas, versión de la imagen y
   * escalado) y `status()` da su escalado real. Un `scaling` explícito gana a
   * la receta. Las claves de Bedrock sólo se renuevan con
   * `renewGatewayKeys: true`: si la receta lo pedía y no lo pasas, la flota
   * no renueva y avisa `recipe_renewal_ignored`. Sin
   * receta (o con `project`, o `useRecipe: false`), la flota es la de 0.16:
   * sin `project` enruta y consulta pero no puede crear VMs; sin
   * `accessToken`, sólo puede listarse o terminarse. */
  static async connect(name: string, options: AgentFleetConnectOptions = {}): Promise<AgentFleet> {
    const { useRecipe, ...rest } = options;
    let project = options.project;
    let recipe: FleetRecipe | undefined;
    let recipeStore = options.recipeStore;
    if (project === undefined && useRecipe !== false) {
      recipeStore ??= defaultRecipeStore({
        region: options.connection?.region,
        credentials: options.credentials,
      });
      const loaded = await loadRecipe(recipeStore, name, options.logger);
      project = loaded?.project;
      recipe = loaded?.recipe;
    }
    const renewal = renewalOnConnect(recipe, options.renewGatewayKeys);
    if (renewal.warning !== undefined) {
      warnRecipe(options.logger, renewal.warning);
    }
    const fleet = new AgentFleet({
      ...rest,
      name,
      project,
      recipe,
      recipeStore,
      renewGatewayKeys: renewal.renew,
    });
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
    const lifetime = this.#lifetime();
    this.#members = listing
      .filter((item) => !parked.has(item.sandboxId))
      .map((item) =>
        memberOf<AgentDeployment>(
          known.get(item.sandboxId),
          item,
          listedCap(item.startedAt, lifetime),
        ),
      );
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
      scalingSource: "explicit",
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

  /** Termina todos los VMs de la flota y las plazas del pool y, si este
   * proceso guardó o leyó su receta, la borra (`keepRecipe: true` la
   * conserva). Sigue aunque uno falle y, al final, lanza el primer error.
   * Devuelve cuántos VMs terminó: uno sin probar (`Member.proven`) no se
   * termina y no cuenta. */
  async stop(options: AgentFleetStopOptions = {}): Promise<number> {
    const errors: unknown[] = [];
    await this.#closePool(true, errors);
    const members = this.#members;
    this.#members = [];
    let stopped = 0;
    for (const member of members) {
      try {
        stopped += (await this.#terminate(member)) ? 1 : 0;
      } catch (error) {
        errors.push(error);
      }
    }
    if (this.#recipeKnown && options.keepRecipe !== true) {
      try {
        await forgetRecipe(this.#recipes(), this.#name, this.#logger);
        this.#recipeKnown = false;
      } catch (error) {
        errors.push(error);
      }
    }
    if (errors.length > 0) {
      throw errors[0];
    }
    return stopped;
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
    return fleetStatusOf(
      this.#name,
      statuses,
      this.scaling,
      warmReady,
      this.#settings.scalingSource,
    );
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
   * enrutable si la flota puede tener varios VMs. Con `{ input }` en lugar del
   * prompt, `result.output` trae la salida del grafo; se comprueba antes de
   * reservar un VM. */
  invoke(prompt: string, options?: AgentInvokeOptions): Promise<AgentResult>;
  invoke(request: AgentInputRequest, options?: AgentInvokeOptions): Promise<AgentResult>;
  invoke(request: AgentInvocation, options?: AgentInvokeOptions): Promise<AgentResult>;
  async invoke(request: AgentInvocation, options: AgentInvokeOptions = {}): Promise<AgentResult> {
    const checked = invocationOf(request);
    requirePromptOrInput(checked.prompt, checked.input);
    const { sessionId, ...rest } = options;
    const [lease, result] = await this.#execute(sessionId, (deployment, inner) =>
      deployment.invoke(request, { ...rest, sessionId: inner }),
    );
    await this.#release(lease, result.sessionId);
    return this.#routable ? withHandle(result, lease.member.sandboxId) : result;
  }

  /** Como `invoke()`, con los eventos a medida que llegan. La petición se hace
   * aquí (un 429 se reintenta) y el VM queda ocupado hasta cerrar el stream. */
  stream(prompt: string, options?: AgentInvokeOptions): Promise<AgentFleetStream>;
  stream(request: AgentInputRequest, options?: AgentInvokeOptions): Promise<AgentFleetStream>;
  stream(request: AgentInvocation, options?: AgentInvokeOptions): Promise<AgentFleetStream>;
  async stream(
    request: AgentInvocation,
    options: AgentInvokeOptions = {},
  ): Promise<AgentFleetStream> {
    const checked = invocationOf(request);
    requirePromptOrInput(checked.prompt, checked.input);
    const { sessionId, ...rest } = options;
    const [lease, opened] = await this.#execute(sessionId, async (deployment, inner) => {
      const stream = deployment.stream(request, { ...rest, sessionId: inner });
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
        await this.#holdLifetime(lease.member);
        return [lease, await operation(deployment, lease.inner)];
      } catch (error) {
        if (error instanceof SandboxNotFoundError) {
          this.#unlease(lease);
          await this.#replace(lease.member);
          throw new SandboxError(vmLostMessage(), { code: AGENT_VM_LOST_CODE });
        }
        await this.#release(lease, undefined);
        if (error instanceof AgentError && error.reason === "busy") {
          await this.#waitForCapacity(lease.member, deadline, routed);
          continue;
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
    await this.#renewGatewayKeys();
    const label = this.#indexLabel(sessionId);
    const lifetime = this.#lifetime();
    let deployment: AgentDeployment;
    let cap: Date | undefined;
    if (extra && this.#pool !== undefined && label === undefined) {
      const sandbox = await this.#pool.take({ gateways: { ...project.gateways } });
      cap = sandboxCap(sandbox);
      try {
        deployment = await service.serve(sandbox as unknown as ServiceSandbox);
      } catch (error) {
        await this.#killQuietly(sandbox);
        throw error;
      }
    } else {
      cap = listedCap(new Date(), lifetime);
      deployment = await service.deploy({
        ...this.#connection,
        accessToken: this.#requireToken(),
        keepOnFailure: this.#keepOnFailure,
        ...(label === undefined ? {} : { metadata: { [AGENT_FLEET_SESSION_LABEL]: label } }),
        ...(label === undefined || this.#index === undefined ? {} : { index: this.#index }),
        ...(lifetime === undefined ? {} : { lifetime }),
      });
    }
    const member = newMember(deployment.sandboxId, deployment, extra, cap);
    await this.#limitLifetime(member);
    return member;
  }

  /** Con qué vida se crean los VMs de la flota (`fleetLifetime`): ninguna
   * propia si la flota no puede crecer o no tiene proyecto. */
  #lifetime(): FleetLifetime | undefined {
    const project = this.#project;
    return project === undefined ? undefined : fleetLifetime(project, this.scaling);
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

  /** A un VM por encima de `minVms` sin ninguna invocación en curso se le
   * acota la vida: `rayd` lo termina solo tras `scaleInAfterMs` sin uso. Se
   * llama al crearlo, al abrirlo y al soltar cada invocación (también la que
   * falla). Se comprueba que siga libre dentro del turno del VM
   * (`#lifetimeTurn`): si otra invocación lo tomó entre medias, su plazo de
   * invocación manda y no se acorta. */
  async #limitLifetime(member: Member<AgentDeployment>): Promise<void> {
    if (!member.extra) {
      return;
    }
    await this.#lifetimeTurn(member, async () => {
      if (member.inflight === 0) {
        await this.#setLifetime(member, this.scaling.scaleInAfterMs);
      }
    });
  }

  /** Antes de una invocación: a un VM sobrante se le da al menos lo que puede
   * durar la petición (`holdMs`), y a uno que este proceso acortó y ahora es
   * base (`markExtras` lo ascendió) se le devuelve toda la vida hasta su
   * tope. Va en el turno del VM, después del arrendamiento: un acortamiento
   * de otra invocación que se suelta ya no puede llegar detrás. */
  async #holdLifetime(member: Member<AgentDeployment>): Promise<void> {
    await this.#lifetimeTurn(member, async () => {
      if (member.extra) {
        const timeout = this.#project === undefined ? undefined : requestTimeoutMs(this.#project);
        await this.#setLifetime(member, holdMs(this.scaling, timeout));
      } else if (member.limited) {
        await this.#setLifetime(member, undefined);
      }
    });
  }

  /** Ordena los `setTimeout` de un VM en una cola: el de soltar una
   * invocación y el de empezar otra nunca se cruzan en vuelo, así que el
   * último que llega a `rayd` es siempre el de la última decisión. */
  async #lifetimeTurn(member: Member, change: () => Promise<void>): Promise<void> {
    const id = member.sandboxId;
    const turn = (this.#lifetimeTurns.get(id) ?? Promise.resolve()).then(change);
    const settled = turn.catch(() => undefined);
    this.#lifetimeTurns.set(id, settled);
    try {
      await turn;
    } finally {
      if (this.#lifetimeTurns.get(id) === settled) {
        this.#lifetimeTurns.delete(id);
      }
    }
  }

  /** Un `setTimeout` acotado al tope de `rayd` del VM (`lifetimeMs`). Sin
   * `desiredMs` pide todo lo que queda hasta el tope (`restoreMs`: sin tope
   * conocido, el máximo de la plataforma, que `rayd` rechaza informando el
   * suyo) y deja el VM sin acortar; si no hay nada que pedir o `rayd` no
   * informa su tope, el VM deja de contar como acortado, porque no queda nada
   * que devolverle.
   * Si `rayd` responde que el plazo pasa de su tope (la estimación de la flota
   * era tardía), se corrige `member.cap` con el tope que informa y se
   * reintenta una vez; nunca se marca el VM. Cualquier otro rechazo por
   * argumento es un VM creado sin plazo lógico (antes de 0.17 o con
   * `maxVms === minVms`): se avisa una vez y no se reintenta. */
  async #setLifetime(
    member: Member<AgentDeployment>,
    desiredMs: number | undefined,
    retry = true,
  ): Promise<void> {
    const deployment = member.deployment;
    if (deployment === undefined || member.lifetimeRejected) {
      return;
    }
    const timeoutMs =
      desiredMs === undefined
        ? restoreMs(member.cap, Date.now())
        : lifetimeMs(desiredMs, member.cap, Date.now());
    if (timeoutMs === undefined) {
      if (desiredMs === undefined) {
        member.limited = false;
      }
      return;
    }
    try {
      await deployment.setTimeout(timeoutMs);
    } catch (error) {
      if (error instanceof LifetimeBeyondCapError) {
        const cap = retry ? correctedCap(error, member.cap) : undefined;
        if (cap === undefined) {
          this.#logger?.debug?.("el plazo de un VM de la flota pasaba de su tope; no se reintenta");
          if (desiredMs === undefined) {
            member.limited = false;
          }
          return;
        }
        member.cap = cap;
        await this.#setLifetime(member, desiredMs, false);
        return;
      }
      if (error instanceof InvalidArgumentError) {
        member.lifetimeRejected = true;
        this.#logger?.warn?.(UNMANAGED_LIFETIME_MESSAGE);
        return;
      }
      if (error instanceof SandboxError) {
        this.#logger?.warn?.("no se pudo acotar la vida de un VM sobrante");
        return;
      }
      throw error;
    }
    member.limited = desiredMs !== undefined;
  }

  /** El cliente del VM: el que ya hay o, la primera vez, conecta con el token
   * de la flota y, si el VM no tiene servicio (una plaza aparcada), lo
   * arranca. */
  async #openOrConnect(member: Member<AgentDeployment>): Promise<AgentDeployment> {
    if (member.deployment !== undefined) {
      return member.deployment;
    }
    const sandbox = await this.#directory.connect(member.sandboxId, this.#requireToken());
    const deployment = await this.#serving(member, sandbox);
    member.deployment = deployment;
    member.cap = sandboxCap(sandbox) ?? member.cap;
    await this.#limitLifetime(member);
    return deployment;
  }

  async #serving(member: Member<AgentDeployment>, sandbox: FleetSandbox): Promise<AgentDeployment> {
    if (await sandbox.files.exists(AGENT_SERVICE_READY_MARKER_PATH)) {
      return this.#deploymentOf(sandbox);
    }
    if (this.#service === undefined) {
      throw new InvalidArgumentError(NO_PROJECT_MESSAGE);
    }
    await this.#openParked(member, sandbox);
    return this.#service.serve(sandbox);
  }

  /** Una plaza aparcada (de este proceso o de otro) nace sin pasarelas: se
   * abren las del proyecto (o de la receta) antes de arrancar el host, con el
   * mismo camino que `SandboxPool.take({ gateways })`. Si no se pueden abrir,
   * la plaza se sustituye y quien la eligió busca otra. */
  async #openParked(member: Member<AgentDeployment>, sandbox: FleetSandbox): Promise<void> {
    const project = this.#project;
    if (project === undefined || Object.keys(project.gateways).length === 0) {
      return;
    }
    await this.#renewGatewayKeys();
    try {
      await this.#directory.openGateways(sandbox, project.gateways, {
        timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
      });
    } catch (error) {
      this.#logger?.warn?.("una plaza aparcada no pudo abrir sus pasarelas: se sustituye");
      await this.#replace(member);
      throw new ParkedSlotError(PARKED_SLOT_MESSAGE, { cause: error });
    }
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
      if (error instanceof ParkedSlotError) {
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
   * sesión real y, si era la última invocación en curso de un VM sobrante, su
   * plazo vuelve a `scaleInAfterMs`. Mientras quede otra en curso, sigue el
   * plazo de invocación (`holdMs`): acortarlo la cortaría a media llamada. */
  async #release(lease: Lease, innerSession: string | undefined): Promise<void> {
    if (this.#unlease(lease, innerSession)) {
      await this.#limitLifetime(lease.member);
    }
  }

  /** Quita la invocación en curso del VM; devuelve si el VM se quedó sin
   * ninguna. La sesión real de una reserva dedicada (`keep`) se recuerda para
   * su VM: con `maxVms === 1` el id de sesión no lleva asa, y sin recordarlo
   * la siguiente llamada de esa sesión no encontraría su VM y esperaría a uno
   * libre hasta `CapacityError`. */
  #unlease(lease: Lease, keep?: string): boolean {
    const member = lease.member;
    member.inflight = Math.max(0, member.inflight - 1);
    if (lease.reservation !== undefined) {
      member.bound.delete(lease.reservation);
      if (keep !== undefined) {
        member.bound.add(keep);
        this.#remember(keep, member);
      }
    }
    return member.inflight === 0;
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
      (member) => member.inflight === 0 && member.bound.size === 0 && member.proven,
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
   * matarlo. Un miembro sin probar (`Member.proven`) no se termina: podría ser
   * un sandbox ajeno de la misma imagen, y se deja a su propio plazo y a su
   * política de idle con un aviso. Devuelve si lo terminó. */
  async #terminate(member: Member<AgentDeployment>): Promise<boolean> {
    if (!member.proven) {
      this.#logger?.warn?.(UNPROVEN_MEMBER_MESSAGE, { sandboxId: member.sandboxId });
      return false;
    }
    if (member.deployment !== undefined) {
      await member.deployment.stop();
    } else {
      await this.#directory.kill(member.sandboxId);
    }
    return true;
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

  // ------------------------------------------------------- recipe and keys

  #recipes(): FleetRecipeStore {
    this.#recipeStore ??= defaultRecipeStore({
      region: this.#connection.region,
      credentials: this.#credentials,
    });
    return this.#recipeStore;
  }

  /** Guarda la receta de la flota recién arrancada (sólo lo llama
   * `deployAgent()`): `saved` o el código del aviso, nunca un error. */
  async saveRecipeForDeploy(): Promise<string> {
    const project = this.#requireProject();
    const recipe = recipeOf(project, {
      name: this.#name,
      imageVersion: this.#service?.imageVersion,
      scaling: this.scaling,
      renewGatewayKeys: this.#renewKeys,
    });
    const outcome = await storeRecipe(this.#recipes(), recipe, this.#logger);
    if (outcome === RECIPE_SAVED) {
      this.#recipeKnown = true;
    }
    return outcome;
  }

  /** Con `renewGatewayKeys`, renueva las claves que lo necesiten y, si
   * escribió alguna, refresca las pasarelas de los VMs abiertos. El
   * renovador (y sus peers opcionales) se carga en el primer uso, y una
   * renovación en curso se comparte: los VMs que arrancan en paralelo
   * escriben como mucho una clave por ventana. */
  async #renewGatewayKeys(): Promise<void> {
    if (!this.#renewKeys) {
      return;
    }
    this.#renewing ??= this.#renewOnce().finally(() => {
      this.#renewing = undefined;
    });
    await this.#renewing;
  }

  /** La renovación del despliegue (sólo la llama `deployAgent()`, antes de
   * `start()`): con `renewGatewayKeys`, escribe siempre una clave nueva en
   * cada pasarela `bedrock`, aunque el secreto acabe de cambiar. */
  async renewGatewayKeysForDeploy(): Promise<void> {
    if (!this.#renewKeys) {
      return;
    }
    await this.#renewing;
    await this.#renewOnce(true);
  }

  /** Sólo se activa con la opción explícita de quien llama (nunca desde la
   * receta), así que un error de la renovación se propaga. */
  async #renewOnce(force = false): Promise<void> {
    this.#renewer ??= await gatewayKeyRenewer(this.#requireProject(), {
      region: this.#connection.region,
      credentials: this.#credentials,
    });
    const renewed = force
      ? await this.#renewer.renew({ force: true })
      : await this.#renewer.renew();
    if (!renewed) {
      return;
    }
    for (const deployment of this.deployments) {
      try {
        await deployment.refreshGateways();
      } catch (error) {
        if (!(error instanceof SandboxError)) {
          throw error;
        }
        this.#logger?.warn?.("no se pudo refrescar la pasarela de un VM de la flota");
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
    const config = poolConfigFor(
      project,
      scaling,
      this.#requireToken(),
      this.#poolMetadata(),
      this.#service?.imageVersion,
    );
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
