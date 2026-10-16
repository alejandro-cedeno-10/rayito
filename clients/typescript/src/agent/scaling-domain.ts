/**
 * Dominio puro del escalado de un agente servido (`ScalingOptions`, ADR-029).
 * Espejo de `rayito._agent._scaling_domain`: los límites de concurrencia por
 * VM, el asa enrutable de una sesión, la colocación de una sesión nueva entre
 * los MicroVMs de la flota, la decisión de cola y el calendario de reducción.
 * No importa `@aws-sdk/*`, `@connectrpc/*` ni el código generado del proto, no
 * tiene reloj propio (el instante llega por parámetro) y no toca la red:
 * `AgentFleet` (la cáscara de I/O) le da los datos que midió y ejecuta lo que
 * decide. Los tiempos van en milisegundos.
 *
 * Los vectores compartidos con Python están en
 * `testdata/agent-fleet/routing.json`.
 */

import { InvalidArgumentError } from "../errors.js";
import {
  AGENT_FLEET_UNHEALTHY_PINGS,
  AGENT_SCALE_IN_AFTER_SECONDS_DEFAULT,
  AGENT_SCALE_IN_AFTER_SECONDS_MIN,
  AGENT_SCALING_MAX_VMS_DEFAULT,
  AGENT_SCALING_MIN_VMS_DEFAULT,
  AGENT_SCALING_WARM_VMS_DEFAULT,
  AGENT_SERVICE_QUEUE_MAX,
  AGENT_SERVICE_RESERVED_CONNECTIONS,
  AGENT_SESSION_HANDLE_PREFIX,
  BASELINE_MIB_PER_VCPU,
  MAX_CONCURRENT_CONNECTIONS_1_VCPU,
  MAX_DURATION_SECONDS,
  POOL_SIZE_MAX,
} from "../limits.js";
import { MS_PER_SECOND } from "../units.js";

/** Cómo se reparten las sesiones: compartidas (varias por VM, hasta
 * `ServeOptions.maxConcurrency`) o dedicadas (una sesión por MicroVM). */
export const SESSION_MODES = ["shared", "dedicated"] as const;
export type SessionMode = (typeof SESSION_MODES)[number];
export const SESSIONS_SHARED: SessionMode = "shared";
export const SESSIONS_DEDICATED: SessionMode = "dedicated";
export const DEFAULT_SESSION_MODE: SessionMode = SESSIONS_SHARED;
/** Separador del asa enrutable `rs1.<sandboxId>.<sessionId>`. */
export const HANDLE_SEPARATOR = ".";
/** Las decisiones de `placeSession` y de `decideQueue`. */
export const PLACEMENT_ROUTE = "route";
export const PLACEMENT_CLAIM = "claim";
export const PLACEMENT_CREATE = "create";
export const PLACEMENT_WAIT = "wait";
export const PLACEMENT_LOST = "lost";
export const QUEUE_ENQUEUE = "enqueue";
export const QUEUE_REJECT = "reject";
export const QUEUE_TIMEOUT = "timeout";

function isInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value);
}

/**
 * Tope de sesiones simultáneas por VM de `memoryMib`: las conexiones que
 * admite el MicroVM (`MAX_CONCURRENT_CONNECTIONS_1_VCPU` por vCPU de base,
 * `AWS_API_NOTES.md` cuotas) menos las que el servicio se reserva para
 * `/ping` y control. Por debajo de la memoria de una vCPU de base se cuenta
 * una vCPU, un valor conservador que la aceptación en AWS comprueba.
 */
export function maxSessionsForMemory(memoryMib: number): number {
  if (!isInteger(memoryMib) || memoryMib < 1) {
    throw new InvalidArgumentError("memoryMib debe ser un entero positivo");
  }
  const vcpus = Math.max(1, Math.floor(memoryMib / BASELINE_MIB_PER_VCPU));
  return MAX_CONCURRENT_CONNECTIONS_1_VCPU * vcpus - AGENT_SERVICE_RESERVED_CONNECTIONS;
}

function whole(value: unknown, label: string, minimum: number, maximum: number): number {
  if (!isInteger(value) || value < minimum || value > maximum) {
    throw new InvalidArgumentError(`${label} debe ser un entero en ${minimum}..${maximum}`);
  }
  return value;
}

const SCALE_IN_AFTER_MS_DEFAULT = AGENT_SCALE_IN_AFTER_SECONDS_DEFAULT * MS_PER_SECOND;
const SCALE_IN_AFTER_MS_MIN = AGENT_SCALE_IN_AFTER_SECONDS_MIN * MS_PER_SECOND;
const SCALE_IN_AFTER_MS_MAX = MAX_DURATION_SECONDS * MS_PER_SECOND;

export interface ScalingOptionsInput {
  readonly minVms?: number | undefined;
  readonly maxVms?: number | undefined;
  readonly warmVms?: number | undefined;
  readonly sessions?: SessionMode | undefined;
  readonly scaleInAfterMs?: number | undefined;
}

/**
 * Cuántos MicroVMs sirven el agente y cómo se reparten las sesiones.
 * `minVms` corren siempre, `warmVms` esperan suspendidos (una sesión nueva los
 * reanuda en vez de crear un VM) y `maxVms` acota el total; con
 * `sessions: "dedicated"` cada sesión tiene su propio MicroVM. Los VMs por
 * encima de `minVms` se terminan tras `scaleInAfterMs` sin uso.
 *
 * Los valores por defecto son los de un solo VM: nada cambia para quien no
 * pasa `ScalingOptions`.
 */
export class ScalingOptions {
  readonly minVms: number;
  readonly maxVms: number;
  readonly warmVms: number;
  readonly sessions: SessionMode;
  readonly scaleInAfterMs: number;

  constructor(options: ScalingOptionsInput = {}) {
    if (typeof options !== "object" || options === null) {
      throw new InvalidArgumentError("ScalingOptions necesita un objeto de opciones");
    }
    this.minVms = whole(
      options.minVms ?? AGENT_SCALING_MIN_VMS_DEFAULT,
      "ScalingOptions.minVms",
      0,
      POOL_SIZE_MAX,
    );
    this.maxVms = whole(
      options.maxVms ?? AGENT_SCALING_MAX_VMS_DEFAULT,
      "ScalingOptions.maxVms",
      1,
      POOL_SIZE_MAX,
    );
    this.warmVms = whole(
      options.warmVms ?? AGENT_SCALING_WARM_VMS_DEFAULT,
      "ScalingOptions.warmVms",
      0,
      POOL_SIZE_MAX,
    );
    if (this.minVms > this.maxVms) {
      throw new InvalidArgumentError("ScalingOptions.minVms no puede superar a maxVms");
    }
    if (this.warmVms > this.maxVms - this.minVms) {
      throw new InvalidArgumentError("ScalingOptions.warmVms no puede superar a maxVms - minVms");
    }
    const sessions = options.sessions ?? DEFAULT_SESSION_MODE;
    if (!(SESSION_MODES as readonly string[]).includes(sessions)) {
      throw new InvalidArgumentError(
        `ScalingOptions.sessions debe ser uno de ${SESSION_MODES.join(", ")}`,
      );
    }
    this.sessions = sessions;
    const scaleInAfterMs = options.scaleInAfterMs ?? SCALE_IN_AFTER_MS_DEFAULT;
    if (
      typeof scaleInAfterMs !== "number" ||
      !(scaleInAfterMs >= SCALE_IN_AFTER_MS_MIN) ||
      scaleInAfterMs > SCALE_IN_AFTER_MS_MAX
    ) {
      throw new InvalidArgumentError(
        `ScalingOptions.scaleInAfterMs debe estar en [${SCALE_IN_AFTER_MS_MIN}, ${SCALE_IN_AFTER_MS_MAX}]`,
      );
    }
    this.scaleInAfterMs = scaleInAfterMs;
  }

  /** Con más de un VM posible, `AgentResult.sessionId` es un asa que nombra
   * el VM (`rs1.<sandboxId>.<sessionId>`); con uno solo, el id de sesión
   * queda como siempre. */
  get routesByHandle(): boolean {
    return this.maxVms > 1;
  }
}

/** El asa enrutable de una sesión: el MicroVM que la sirve y el id de sesión
 * que ve el host. */
export interface SessionHandle {
  readonly sandboxId: string;
  readonly sessionId: string;
}

const HANDLE_PREFIX = `${AGENT_SESSION_HANDLE_PREFIX}${HANDLE_SEPARATOR}`;

/** `rs1.<sandboxId>.<sessionId>`. El id del sandbox no lleva `.`. */
export function encodeHandle(sandboxId: string, sessionId: string): string {
  if (sandboxId === "" || sandboxId.includes(HANDLE_SEPARATOR)) {
    throw new InvalidArgumentError("el id del sandbox de un asa no puede ser vacío ni llevar '.'");
  }
  if (sessionId === "") {
    throw new InvalidArgumentError("el id de sesión de un asa no puede ser vacío");
  }
  return `${HANDLE_PREFIX}${sandboxId}${HANDLE_SEPARATOR}${sessionId}`;
}

/** El asa de `value`, o `undefined` si es un id de sesión sin enrutar. */
export function decodeHandle(value: string): SessionHandle | undefined {
  if (!value.startsWith(HANDLE_PREFIX)) {
    return undefined;
  }
  const rest = value.slice(HANDLE_PREFIX.length);
  const split = rest.indexOf(HANDLE_SEPARATOR);
  if (split <= 0) {
    return undefined;
  }
  const sessionId = rest.slice(split + HANDLE_SEPARATOR.length);
  if (sessionId === "") {
    return undefined;
  }
  return { sandboxId: rest.slice(0, split), sessionId };
}

/** Un id de sesión que elige quien llama: no puede empezar por el prefijo
 * reservado de las asas si no es un asa completa. */
export function validateCallerSessionId(value: string): string {
  if (value === "") {
    throw new InvalidArgumentError("sessionId no puede ser vacío");
  }
  if (value.startsWith(HANDLE_PREFIX) && decodeHandle(value) === undefined) {
    throw new InvalidArgumentError(
      `sessionId no puede empezar por '${HANDLE_PREFIX}' salvo que sea un asa completa`,
    );
  }
  return value;
}

/**
 * Lo que la flota sabe de un MicroVM al colocar una sesión: `active` llamadas
 * en curso, `served` sesiones distintas que ya ha servido, si responde
 * (`healthy`), si su `/ping` dice `HealthyBusy` (`busy`) y si es una plaza
 * templada suspendida (`warm`) que aún no sirve.
 */
export interface VmView {
  readonly sandboxId: string;
  readonly active?: number | undefined;
  readonly served?: number | undefined;
  readonly healthy?: boolean | undefined;
  readonly busy?: boolean | undefined;
  readonly warm?: boolean | undefined;
}

/** Dónde va una sesión: `route` a un VM en marcha (`sandboxId`), `claim` una
 * plaza templada, `create` un VM nuevo, `wait` (la flota está llena) o `lost`
 * (el asa nombra un VM que ya no está o no responde). */
export interface Placement {
  readonly action: string;
  readonly sandboxId: string | undefined;
}

function isHealthy(vm: VmView): boolean {
  return vm.healthy ?? true;
}

function byId(left: VmView, right: VmView): number {
  return left.sandboxId < right.sandboxId ? -1 : left.sandboxId > right.sandboxId ? 1 : 0;
}

function freeVm(
  candidates: readonly VmView[],
  scaling: ScalingOptions,
  maxConcurrency: number,
): VmView | undefined {
  const eligible =
    scaling.sessions === SESSIONS_DEDICATED
      ? candidates.filter((vm) => (vm.served ?? 0) === 0 && (vm.active ?? 0) === 0)
      : candidates.filter((vm) => !(vm.busy ?? false) && (vm.active ?? 0) < maxConcurrency);
  return [...eligible].sort(
    (left, right) => (left.active ?? 0) - (right.active ?? 0) || byId(left, right),
  )[0];
}

function placementForHandle(handle: SessionHandle, vms: readonly VmView[]): Placement {
  const vm = vms.find(
    (candidate) => candidate.sandboxId === handle.sandboxId && isHealthy(candidate),
  );
  return vm === undefined
    ? { action: PLACEMENT_LOST, sandboxId: handle.sandboxId }
    : { action: PLACEMENT_ROUTE, sandboxId: vm.sandboxId };
}

/**
 * Decide dónde va una sesión. Con un asa va a su VM (o es `lost`). Sin asa, en
 * modo compartido va al VM sano con menos llamadas y hueco; en modo dedicado, a
 * un VM sin sesiones servidas. Si no hay, reanuda una plaza templada, crea un
 * VM si el total no llega a `maxVms` o espera.
 */
export function placeSession(
  sessionId: string | undefined,
  vms: readonly VmView[],
  scaling: ScalingOptions,
  options: { readonly maxConcurrency: number },
): Placement {
  const handle = sessionId === undefined ? undefined : decodeHandle(sessionId);
  if (handle !== undefined) {
    return placementForHandle(handle, vms);
  }
  const candidates = vms.filter((vm) => isHealthy(vm) && !(vm.warm ?? false));
  const chosen = freeVm(candidates, scaling, options.maxConcurrency);
  if (chosen !== undefined) {
    return { action: PLACEMENT_ROUTE, sandboxId: chosen.sandboxId };
  }
  const warm = vms.filter((vm) => (vm.warm ?? false) && isHealthy(vm)).sort(byId)[0];
  if (warm !== undefined) {
    return { action: PLACEMENT_CLAIM, sandboxId: warm.sandboxId };
  }
  if (vms.length < scaling.maxVms) {
    return { action: PLACEMENT_CREATE, sandboxId: undefined };
  }
  return { action: PLACEMENT_WAIT, sandboxId: undefined };
}

/** Qué hace el cliente con una llamada que no cabe: `enqueue` (espera
 * `waitMs` y reintenta), `reject` (la cola está llena o desactivada) o
 * `timeout` (esperó todo `queueTimeoutMs`). */
export interface QueueDecision {
  readonly action: string;
  readonly waitMs: number;
}

/**
 * La decisión de cola de una llamada que recibió 429. Con `queueMax === 0` (el
 * valor por defecto) se rechaza enseguida, como en 0.14; con cola, espera lo
 * que dice el servicio (`retryAfterMs`) sin pasar de lo que queda del plazo.
 */
export function decideQueue(options: {
  readonly waiting: number;
  readonly elapsedMs: number;
  readonly queueMax: number;
  readonly queueTimeoutMs: number;
  readonly retryAfterMs: number;
}): QueueDecision {
  whole(options.queueMax, "queueMax", 0, AGENT_SERVICE_QUEUE_MAX);
  if (options.queueMax === 0 || options.waiting >= options.queueMax) {
    return { action: QUEUE_REJECT, waitMs: 0 };
  }
  const remaining = options.queueTimeoutMs - options.elapsedMs;
  if (remaining <= 0) {
    return { action: QUEUE_TIMEOUT, waitMs: 0 };
  }
  return { action: QUEUE_ENQUEUE, waitMs: Math.min(options.retryAfterMs, remaining) };
}

/** El uso de un VM en marcha para calcular la reducción: cuándo se creó y
 * cuándo se usó por última vez (milisegundos desde la época). */
export interface VmUsage {
  readonly sandboxId: string;
  readonly createdAtMs: number;
  readonly lastUsedAtMs: number;
}

/**
 * Plazo de terminación de cada VM por encima de `minVms`: su último uso más
 * `scaleInAfterMs`. Los `minVms` más antiguos son la base y no tienen plazo.
 */
export function scaleInDeadlines(
  usages: readonly VmUsage[],
  scaling: ScalingOptions,
): Readonly<Record<string, number>> {
  const ordered = [...usages].sort(
    (left, right) =>
      left.createdAtMs - right.createdAtMs ||
      (left.sandboxId < right.sandboxId ? -1 : left.sandboxId > right.sandboxId ? 1 : 0),
  );
  const deadlines: Record<string, number> = {};
  for (const usage of ordered.slice(scaling.minVms)) {
    deadlines[usage.sandboxId] = usage.lastUsedAtMs + scaling.scaleInAfterMs;
  }
  return deadlines;
}

/** Los VMs cuyo plazo de reducción ya pasó en `nowMs`, por plazo. */
export function expiredVms(
  deadlines: Readonly<Record<string, number>>,
  nowMs: number,
): readonly string[] {
  return Object.entries(deadlines)
    .filter(([, deadline]) => deadline <= nowMs)
    .sort(([leftId, left], [rightId, right]) => left - right || (leftId < rightId ? -1 : 1))
    .map(([sandboxId]) => sandboxId);
}

/** Un VM se da por perdido tras `AGENT_FLEET_UNHEALTHY_PINGS` pings seguidos
 * sin respuesta. */
export function isUnhealthy(failedPings: number): boolean {
  return failedPings >= AGENT_FLEET_UNHEALTHY_PINGS;
}

/** Cuántos VMs crear para volver a `minVms` tras perder alguno. */
export function replacementsNeeded(healthyRunning: number, scaling: ScalingOptions): number {
  return Math.max(0, scaling.minVms - healthyRunning);
}
