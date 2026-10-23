/**
 * Lo que `AgentFleet` decide sin E/S (ADR-029, D5). Espejo de
 * `rayito._agent._fleet_base`: el id de sesión enrutable
 * (`rs1.<sandbox>.<sesión>`), el registro de cada miembro, la decisión de
 * dónde colocar una sesión, la salud por pings, el plazo de la cola y las
 * opciones del pool de plazas calientes. Nada aquí llama a AWS ni crea
 * clientes.
 */

import { createHash } from "node:crypto";
import { InvalidArgumentError } from "../errors.js";
import {
  AGENT_FLEET_PING_TTL_SECONDS,
  LIFECYCLE_MIN_TIMEOUT_SECONDS,
  MAX_DURATION_SECONDS,
} from "../limits.js";
import type { PoolConfig } from "../pool/config.js";
import {
  CAP_MARGIN_MS,
  defaultMaxLifetimeMs,
  type LifetimeBeyondCapError,
  type OnTimeout,
  REOPEN_CAP_SLACK_MS,
} from "../sandbox/lifecycle.js";
import { MS_PER_SECOND } from "../units.js";
import { type DeploymentState, parseLabels } from "./deployment.js";
import type { AgentEvent, AgentResult } from "./events.js";
import { type AgentProject, DEFAULT_QUEUE_TIMEOUT_MS } from "./project.js";
import {
  decodeHandle,
  encodeHandle,
  isUnhealthy,
  ScalingOptions,
  SESSIONS_DEDICATED,
  type SessionHandle,
} from "./scaling-domain.js";

/** Etiqueta del sandbox de un VM dedicado: el hash de la sesión que el
 * llamante eligió (`sessionIndex`). Nunca el id en claro: las etiquetas viajan
 * en el `runHookPayload`. */
export const AGENT_FLEET_SESSION_LABEL = "rayito.agent.session";
const SESSION_LABEL_HEX_CHARS = 32;
/** Cuánto vale un ping (o un `busy` visto) para elegir miembro. */
export const AGENT_FLEET_PING_TTL_MS = AGENT_FLEET_PING_TTL_SECONDS * MS_PER_SECOND;
/** Espera entre dos intentos de colocar una sesión cuando la flota está llena. */
export const AGENT_FLEET_BUSY_RETRY_MS = 1000;
/** Cuántos VMs crea a la vez `start()` (y `scale()`). */
export const AGENT_FLEET_START_CONCURRENCY = 4;
/** Cuántos VMs `RUNNING` sondea a la vez el listado de la flota (un
 * `get-microvm` más un `Health` sin token cada uno). Los resultados y los
 * avisos salen en el orden de `list-microvms`. */
export const AGENT_FLEET_PROBE_CONCURRENCY = 8;
/** Plazo para que las plazas calientes de un `deployAgent` queden aparcadas:
 * un `create()` más su `pause()` son decenas de segundos (`AWS_API_NOTES.md`,
 * Q155), con holgura para varias a la vez. */
export const AGENT_FLEET_WARM_READY_TIMEOUT_MS = 600_000;
export const AGENT_FLEET_WARM_POLL_MS = 2000;
/** Estados de `list-microvms` que cuentan como miembro de la flota. */
export const AGENT_FLEET_LIVE_STATES: readonly string[] = [
  "PENDING",
  "RUNNING",
  "SUSPENDING",
  "SUSPENDED",
];
/** Estado de `list-microvms` en el que se puede leer el agente del VM (sus
 * etiquetas) sin despertarlo. */
export const AGENT_FLEET_PROBE_STATE = "RUNNING";
/** Códigos nuevos de los errores existentes (ver `errors.ts`). */
export const AGENT_CAPACITY_CODE = "agent_capacity";
export const AGENT_VM_LOST_CODE = "agent_vm_lost";
const POOL_MIN_REMAINING_DEFAULT_MS = 3_600_000;
const POOL_MIN_REMAINING_FLOOR_MS = 60_000;

export type MemberHealth = DeploymentState | "unknown";
/** De dónde sale el escalado de una flota: `scaling` explícito, el proyecto,
 * la receta guardada por `deployAgent()` o, sin ninguno, los valores por
 * defecto (que no dicen nada de la flota real). */
export const SCALING_SOURCES = ["explicit", "project", "recipe", "default"] as const;
export type ScalingSource = (typeof SCALING_SOURCES)[number];

/** El destino de `sessionId` si es un id de sesión enrutable de la flota;
 * `undefined` si es un id cualquiera (el del llamante o el de un despliegue de
 * un solo VM). */
export function routeOf(sessionId: string | undefined | null): SessionHandle | undefined {
  return sessionId === undefined || sessionId === null ? undefined : decodeHandle(sessionId);
}

/** El valor de `AGENT_FLEET_SESSION_LABEL` de una sesión elegida por el
 * llamante: un hash, para que el id no viaje en las etiquetas. */
export function sessionLabel(sessionId: string): string {
  return createHash("sha256")
    .update(sessionId, "utf8")
    .digest("hex")
    .slice(0, SESSION_LABEL_HEX_CHARS);
}

/** El resultado con el id de sesión enrutable de `sandboxId`. */
export function withHandle(result: AgentResult, sandboxId: string): AgentResult {
  if (!result.sessionId) {
    return result;
  }
  return Object.freeze({ ...result, sessionId: encodeHandle(sandboxId, result.sessionId) });
}

/** El evento con su id de sesión enrutable, si el evento lleva uno. */
export function eventWithHandle(event: AgentEvent, sandboxId: string): AgentEvent {
  if ((event.type === "done" || event.type === "agent_failed") && event.sessionId) {
    return Object.freeze({ ...event, sessionId: encodeHandle(sandboxId, event.sessionId) });
  }
  return event;
}

/** Un VM de la flota tal como lo ve este proceso. `deployment` es el cliente
 * (`undefined` hasta que se usa: abrir un VM suspendido lo reanuda);
 * `inflight` son las invocaciones en curso desde este proceso; `bound` las
 * sesiones (o reservas) de un VM dedicado; `busyUntil` hasta cuándo se da por
 * ocupado tras un 429 o un `HealthyBusy`; `extra` marca un VM por encima de
 * `minVms`, que se termina solo tras `scaleInAfterMs` sin uso. */
export interface Member<D = unknown> {
  readonly sandboxId: string;
  deployment: D | undefined;
  extra: boolean;
  inflight: number;
  readonly bound: Set<string>;
  health: MemberHealth;
  pingedAt: number | undefined;
  busyUntil: number;
  failedPings: number;
  /** El tope de `rayd` del VM (`raydCap`; `undefined` si no se conoce). */
  cap: Date | undefined;
  /** Si este proceso le acortó el plazo (`setTimeout` de reducción). */
  limited: boolean;
  /** Su `setTimeout` se rechazó por un argumento (un VM sin plazo lógico): no se reintenta. */
  lifetimeRejected: boolean;
  /** Si se sabe que el VM es de la flota (lo creó este proceso, sus etiquetas
   * lo prueban o la imagen sólo tiene agentes); uno sin probar nunca se
   * termina. */
  proven: boolean;
}

export function newMember<D>(
  sandboxId: string,
  deployment?: D,
  extra = false,
  cap?: Date,
): Member<D> {
  return {
    sandboxId,
    deployment,
    extra,
    inflight: 0,
    bound: new Set(),
    health: "unknown",
    pingedAt: undefined,
    busyUntil: 0,
    failedPings: 0,
    cap,
    limited: false,
    lifetimeRejected: false,
    proven: true,
  };
}

/** La acción al vencer de los VMs de una flota que puede crecer: `rayd`
 * termina el VM (ADR-011, `AWS_API_NOTES.md` Q63). Es lo que hace que un
 * `setTimeout` de reducción se acepte. */
export const AGENT_FLEET_ON_TIMEOUT = "kill" satisfies OnTimeout;
/** Margen bajo el tope de `rayd` al acotar la vida de un VM (desfase de
 * relojes): el mismo que deja `connect()` al reabrir un sandbox
 * (`REOPEN_CAP_SLACK_MS`). */
export const AGENT_FLEET_CAP_SAFETY_MS = REOPEN_CAP_SLACK_MS;
/** Lo que se loguea una vez por VM cuando `rayd` rechaza acotar su vida. */
export const UNMANAGED_LIFETIME_MESSAGE =
  "un VM de la flota no tiene plazo lógico (se creó antes de 0.17 o con maxVms igual a " +
  "minVms): no se reducirá solo; termínalo con stop() o vuelve a desplegar";

/** Las opciones de vida con las que se crea cada VM de una flota que puede
 * crecer (y cada plaza caliente): el plazo lógico en modo `kill` y su tope.
 * Sin ellas `rayd` rechaza el `setTimeout` que termina un VM sobrante. */
export interface FleetLifetime {
  readonly maxLifetimeMs: number;
  readonly onTimeout: typeof AGENT_FLEET_ON_TIMEOUT;
}

/** Si la flota puede tener VMs por encima de `minVms`. */
export function scalesIn(scaling: ScalingOptions): boolean {
  return scaling.maxVms > scaling.minVms;
}

/** La vida de los VMs de la flota: `undefined` si nunca puede haber un VM
 * sobrante (`maxVms === minVms`), y así se crean byte a byte como en 0.16.
 * Si puede, `serve.timeoutMs` sigue siendo la vida de un VM base (ahora como
 * plazo lógico) y el tope es el de `Sandbox.create()` por defecto,
 * `timeout + 60 s` (como mucho 28 800 s). */
export function fleetLifetime(
  project: AgentProject,
  scaling: ScalingOptions,
): FleetLifetime | undefined {
  if (!scalesIn(scaling)) {
    return undefined;
  }
  const timeoutSeconds = Math.ceil(project.serve.timeoutMs / MS_PER_SECOND);
  return Object.freeze({
    maxLifetimeMs: defaultMaxLifetimeMs(timeoutSeconds),
    onTimeout: AGENT_FLEET_ON_TIMEOUT,
  });
}

/** El tope que impone `rayd` a partir del de la plataforma: `rayd` lo fija en
 * `/run + maxLifetime - 60 s` (`CAP_MARGIN_MS`, `CAP_MARGIN` de `rayd-core`)
 * y rechaza cualquier plazo posterior. `/run` llega unos segundos después
 * del `startedAt` de la plataforma, así que contar desde `startedAt` (o desde
 * antes de `create()`) se queda del lado seguro. */
export function raydCap(platformCap: Date): Date {
  return new Date(platformCap.getTime() - CAP_MARGIN_MS);
}

/** Los milisegundos (segundos enteros) que se piden en `setTimeout`:
 * `desiredMs` redondeado hacia arriba al segundo (o, con `undefined`, todo
 * lo que queda) y nunca más allá del tope de `rayd` (`cap`, ver `raydCap`)
 * menos `AGENT_FLEET_CAP_SAFETY_MS`, porque `rayd` rechaza un plazo más allá
 * de su tope. `undefined` si no hay
 * nada que pedir: el tope está demasiado cerca o, sin `desiredMs`, no se
 * conoce. */
export function lifetimeMs(
  desiredMs: number | undefined,
  cap: Date | undefined,
  nowMs: number,
): number | undefined {
  const desiredSeconds = desiredMs === undefined ? undefined : Math.ceil(desiredMs / MS_PER_SECOND);
  if (cap === undefined) {
    return desiredSeconds === undefined ? undefined : desiredSeconds * MS_PER_SECOND;
  }
  const remaining =
    Math.floor((cap.getTime() - nowMs) / MS_PER_SECOND) - AGENT_FLEET_CAP_SAFETY_MS / MS_PER_SECOND;
  if (remaining < LIFECYCLE_MIN_TIMEOUT_SECONDS) {
    return undefined;
  }
  const seconds = desiredSeconds === undefined ? remaining : Math.min(desiredSeconds, remaining);
  return seconds * MS_PER_SECOND;
}

/** Los milisegundos que devuelven toda su vida a un VM que este proceso
 * acortó y ahora es base: todo lo que queda hasta el tope de `rayd` si se
 * conoce y, si no, `MAX_DURATION_SECONDS` (el máximo de la plataforma), que
 * `rayd` rechaza informando su tope (`LifetimeBeyondCapError`) para
 * reintentar con él (`correctedCap`). `undefined` si el tope está demasiado
 * cerca: el VM termina antes de todos modos. Espejo de `restore_seconds`. */
export function restoreMs(cap: Date | undefined, nowMs: number): number | undefined {
  return cap === undefined
    ? MAX_DURATION_SECONDS * MS_PER_SECOND
    : lifetimeMs(undefined, cap, nowMs);
}

/** El plazo de un VM sobrante mientras atiende una invocación: nunca menos
 * que lo que puede durar la petición, para que la reducción no lo termine a
 * media llamada. */
export function holdMs(scaling: ScalingOptions, requestTimeoutMs: number | undefined): number {
  return Math.max(scaling.scaleInAfterMs, requestTimeoutMs ?? 0);
}

/** El tope de `rayd` que informó al rechazar un plazo más allá de él, si
 * adelanta el que la flota estimaba (`current`): con él se reintenta una vez.
 * `undefined` si el rechazo no trae tope o no corrige nada (no hay nada que
 * reintentar). */
export function correctedCap(
  error: LifetimeBeyondCapError,
  current: Date | undefined,
): Date | undefined {
  if (error.capUnixMs === undefined) {
    return undefined;
  }
  if (current !== undefined && error.capUnixMs >= current.getTime()) {
    return undefined;
  }
  return new Date(error.capUnixMs);
}

/** El tope de `rayd` de un VM que arrancó en `startedAt` (el listado, o el
 * instante previo a `create()` para uno que la flota crea), si la flota sabe
 * con qué vida se crean sus VMs. */
export function listedCap(startedAt: Date, lifetime: FleetLifetime | undefined): Date | undefined {
  return lifetime === undefined
    ? undefined
    : raydCap(new Date(startedAt.getTime() + lifetime.maxLifetimeMs));
}

/** El tope de `rayd` de un sandbox abierto, sin llamar a AWS: el que informó
 * el propio agente (`info.lifecycle.cap`) o, si no se leyó, el de la
 * plataforma (`info.platformExpiresAt`) pasado por `raydCap`. `undefined` si
 * el objeto no trae ninguno. */
export function sandboxCap(sandbox: unknown): Date | undefined {
  const info = (
    sandbox as {
      readonly info?: {
        readonly platformExpiresAt?: unknown;
        readonly lifecycle?: { readonly cap?: unknown } | undefined;
      };
    }
  ).info;
  const reported = info?.lifecycle?.cap;
  if (reported instanceof Date) {
    return reported;
  }
  return info?.platformExpiresAt instanceof Date ? raydCap(info.platformExpiresAt) : undefined;
}

/** Un VM de la imagen de la flota según `list-microvms`. */
export interface MemberListing {
  readonly sandboxId: string;
  readonly state: string;
  readonly startedAt: Date;
  /** `false` si el listado no puede probar que el VM es de la flota. */
  readonly proven?: boolean | undefined;
  /** La versión de la imagen con la que arrancó el VM. */
  readonly templateVersion?: string | undefined;
}

/** La versión de imagen del miembro más reciente que la lleva: con la que una
 * flota reconectada sin receta crea sus VMs nuevos, en vez de la más reciente
 * de la imagen. */
export function listedImageVersion(items: readonly MemberListing[]): string | undefined {
  let newest: MemberListing | undefined;
  for (const item of items) {
    if (item.templateVersion && (newest === undefined || item.startedAt > newest.startedAt)) {
      newest = item;
    }
  }
  return newest?.templateVersion;
}

/** Los ids de los VMs sondeados cuyas etiquetas son las de un despliegue de
 * agente de Rayito (`parseLabels`): los únicos que se sabe que son de una
 * flota. */
export function ownedIds(
  items: Iterable<{
    readonly sandboxId: string;
    readonly metadata?: Readonly<Record<string, string>> | undefined;
  }>,
): Set<string> {
  const owned = new Set<string>();
  for (const item of items) {
    try {
      parseLabels(item.metadata);
    } catch (error) {
      if (error instanceof InvalidArgumentError) {
        continue;
      }
      throw error;
    }
    owned.add(item.sandboxId);
  }
  return owned;
}

/** Los VMs de una imagen que cuentan como miembros de la flota. Un VM
 * `RUNNING` lo es sólo si sus etiquetas lo prueban (`owned`); los que no están
 * `RUNNING` no se pueden sondear sin despertarlos, así que cuentan salvo que
 * la imagen ya haya demostrado tener VMs que no son de un agente: entonces
 * ninguno se da por miembro, para no terminar sandboxes ajenos.
 *
 * `unreadable` son los VMs `RUNNING` cuya sonda falló o que dejaron de estar
 * `RUNNING` al sondearlos: la plataforma aún los lista, pero su `rayd` ya no
 * responde (un VM que se está terminando al reducir la flota). No cuentan como
 * miembros ni prueban que la imagen sea compartida: se tratan como VMs que
 * terminan. Como tampoco prueban lo contrario (uno podría ser un sandbox
 * ajeno), con alguno los miembros que no están `RUNNING` salen con
 * `proven: false`: cuentan para el cupo, pero la flota nunca los termina. */
export function selectMembers(
  items: readonly MemberListing[],
  owned: ReadonlySet<string>,
  unreadable: ReadonlySet<string> = new Set(),
): MemberListing[] {
  const sharedImage = items.some(
    (item) =>
      item.state === AGENT_FLEET_PROBE_STATE &&
      !owned.has(item.sandboxId) &&
      !unreadable.has(item.sandboxId),
  );
  const proven = unreadable.size === 0;
  const selected: MemberListing[] = [];
  for (const item of items) {
    if (item.state === AGENT_FLEET_PROBE_STATE) {
      if (owned.has(item.sandboxId)) {
        selected.push(item);
      }
    } else if (!sharedImage) {
      selected.push({ ...item, proven });
    }
  }
  return selected;
}

/** La sonda de un VM `RUNNING` del listado: su información si sigue `RUNNING`
 * (`undefined` si no) y, si la sonda falló, la clase del error. */
export interface MemberProbe {
  readonly sandboxId: string;
  readonly info:
    | {
        readonly sandboxId: string;
        readonly metadata?: Readonly<Record<string, string>> | undefined;
      }
    | undefined;
  readonly error?: string | undefined;
}

/** `selectMembers` con las sondas de los VMs `RUNNING`: las que trajeron
 * información prueban (o no) las etiquetas; las demás son `unreadable`. */
export function selectProbed(
  items: readonly MemberListing[],
  probes: readonly MemberProbe[],
): MemberListing[] {
  const readable = probes.flatMap((probe) => (probe.info === undefined ? [] : [probe.info]));
  const unreadable = new Set(
    probes.filter((probe) => probe.info === undefined).map((probe) => probe.sandboxId),
  );
  return selectMembers(items, ownedIds(readable), unreadable);
}

/** El miembro de un VM listado: el que este proceso ya conocía (que pasa a
 * probado si el listado lo prueba) o uno nuevo con la prueba del listado. */
export function memberOf<D>(
  known: Member<D> | undefined,
  item: MemberListing,
  cap: Date | undefined,
): Member<D> {
  const proven = item.proven !== false;
  if (known === undefined) {
    return { ...newMember<D>(item.sandboxId, undefined, false, cap), proven };
  }
  known.proven = known.proven || proven;
  return known;
}

/** Lo que se loguea (sólo el id) cuando la flota no termina un VM porque no
 * pudo probar que es suyo: lo deja a su propio plazo y a su política de idle. */
export const UNPROVEN_MEMBER_MESSAGE =
  "no se termina un VM: no se pudo probar que sea de la flota; se deja a su propio plazo";

/** Ejecuta `work` sobre cada elemento con como mucho `limit` (al menos uno)
 * a la vez y devuelve los resultados en el orden de `items`. Si uno falla, no
 * empieza ninguno más y rechaza con ese error. */
export async function mapBounded<T, R>(
  items: readonly T[],
  limit: number,
  work: (item: T) => Promise<R>,
): Promise<R[]> {
  const results: R[] = new Array(items.length);
  let next = 0;
  let failed = false;
  const lane = async (): Promise<void> => {
    while (!failed && next < items.length) {
      const position = next;
      next += 1;
      try {
        results[position] = await work(items[position] as T);
      } catch (error) {
        failed = true;
        throw error;
      }
    }
  };
  await Promise.all(
    Array.from(
      { length: Math.min(Number.isFinite(limit) ? Math.max(limit, 1) : 1, items.length) },
      lane,
    ),
  );
  return results;
}

/** Lo que se loguea (sólo el id y la clase del error) cuando la sonda de un VM
 * `RUNNING` falla al listar la flota: se trata como un VM que termina. */
export const UNREADABLE_MEMBER_MESSAGE =
  "un VM de la flota no respondió a Health al listarla: se trata como un VM que termina";

export interface FleetMemberStatus {
  readonly sandboxId: string;
  readonly state: MemberHealth;
  readonly extra: boolean;
  readonly inflight: number;
}

/** El estado de una flota: sus miembros, cuántos están sanos u ocupados y los
 * límites de `ScalingOptions`. Un miembro que este proceso aún no abrió figura
 * como `unknown`: preguntarle lo reanudaría. `scalingSource` dice de dónde
 * salen los límites: con `default` (sin proyecto ni receta) no describen la
 * flota real. */
export interface FleetStatus {
  readonly name: string;
  readonly members: readonly FleetMemberStatus[];
  readonly healthy: number;
  readonly busy: number;
  readonly minVms: number;
  readonly maxVms: number;
  readonly warmVms: number;
  readonly warmReady: number;
  readonly scalingSource: ScalingSource;
}

export function fleetStatusOf(
  name: string,
  members: readonly FleetMemberStatus[],
  scaling: ScalingOptions,
  warmReady: number,
  scalingSource: ScalingSource = "default",
): FleetStatus {
  return Object.freeze({
    name,
    members: Object.freeze([...members]),
    healthy: members.filter((m) => m.state === "healthy" || m.state === "busy").length,
    busy: members.filter((m) => m.state === "busy").length,
    minVms: scaling.minVms,
    maxVms: scaling.maxVms,
    warmVms: scaling.warmVms,
    warmReady,
    scalingSource,
  });
}

export type PlacementKind = "use" | "grow" | "wait";

/** La decisión de `planPlacement`: usar un miembro, crear uno o esperar. */
export interface Placement {
  readonly kind: PlacementKind;
  readonly member: Member | undefined;
}

export function pingIsFresh(member: Member, now: number): boolean {
  return member.pingedAt !== undefined && now - member.pingedAt < AGENT_FLEET_PING_TTL_MS;
}

/** Anota un ping: un `unavailable` suma al contador de pings fallidos, un
 * `busy` da al miembro por ocupado hasta que el ping caduque y cualquier otra
 * respuesta lo pone todo a cero. */
export function recordPing(member: Member, state: DeploymentState, now: number): void {
  member.health = state;
  member.pingedAt = now;
  member.failedPings = state === "unavailable" ? member.failedPings + 1 : 0;
  member.busyUntil = state === "busy" ? now + AGENT_FLEET_PING_TTL_MS : 0;
}

/** Anota un 429 del host: el miembro cuenta como ocupado hasta el siguiente
 * intento de la cola (`AGENT_FLEET_BUSY_RETRY_MS`). */
export function recordBusy(member: Member, now: number): void {
  member.health = "busy";
  member.busyUntil = now + AGENT_FLEET_BUSY_RETRY_MS;
}

export function isLost(member: Member): boolean {
  return isUnhealthy(member.failedPings);
}

/** Si `member` puede aceptar una sesión nueva según lo que este proceso sabe.
 * Un `unavailable` reciente nunca; uno viejo se vuelve a sondear, y tras
 * `AGENT_FLEET_UNHEALTHY_PINGS` fallos seguidos se sustituye. Dedicado: sin
 * ninguna sesión asignada. Compartido: con cupo (`inflight < maxConcurrency`)
 * y sin `busyUntil` por delante. */
export function isFree(
  member: Member,
  scaling: ScalingOptions,
  maxConcurrency: number,
  now: number,
): boolean {
  if (member.health === "unavailable" && pingIsFresh(member, now)) {
    return false;
  }
  if (scaling.sessions === SESSIONS_DEDICATED) {
    return member.bound.size === 0;
  }
  return member.inflight < maxConcurrency && now >= member.busyUntil;
}

/** El miembro libre con menos invocaciones en curso (el primero en caso de
 * empate: así los VMs se llenan por orden y los sobrantes se apagan). */
export function chooseMember(
  members: readonly Member[],
  scaling: ScalingOptions,
  maxConcurrency: number,
  now: number,
): Member | undefined {
  let best: Member | undefined;
  for (const member of members) {
    if (
      isFree(member, scaling, maxConcurrency, now) &&
      (!best || member.inflight < best.inflight)
    ) {
      best = member;
    }
  }
  return best;
}

/** Dónde va una sesión nueva: en un miembro libre; si no hay y la flota puede
 * crecer (contando los VMs que ya se están creando), en uno nuevo; si no, a
 * esperar. */
export function planPlacement(
  members: readonly Member[],
  growing: number,
  scaling: ScalingOptions,
  maxConcurrency: number,
  now: number,
): Placement {
  const member = chooseMember(members, scaling, maxConcurrency, now);
  if (member !== undefined) {
    return { kind: "use", member };
  }
  if (members.length + growing < scaling.maxVms) {
    return { kind: "grow", member: undefined };
  }
  return { kind: "wait", member: undefined };
}

let reservationCounter = 0;

/** La reserva con la que una sesión nueva ocupa un VM dedicado hasta conocer
 * su id. */
export function newReservation(): string {
  reservationCounter += 1;
  return `reserva-${Date.now().toString(36)}-${reservationCounter}`;
}

/** Los miembros que sobran por encima de `maxVms`, los más nuevos primero. */
export function surplus(members: readonly Member[], scaling: ScalingOptions): Member[] {
  const excess = members.length - scaling.maxVms;
  return excess > 0 ? [...members].reverse().slice(0, excess) : [];
}

/** Marca como `extra` los miembros más allá de los `minVms` primeros. */
export function markExtras(members: readonly Member[], scaling: ScalingOptions): void {
  members.forEach((member, position) => {
    member.extra = position >= scaling.minVms;
  });
}

export function capacityMessage(scaling: ScalingOptions): string {
  const kind = scaling.sessions === SESSIONS_DEDICATED ? "dedicadas" : "compartidas";
  return (
    `la flota no tiene cupo: sus ${scaling.maxVms} VMs están ocupados ` +
    `(sesiones ${kind}) y se agotó el plazo de la cola`
  );
}

export function vmLostMessage(): string {
  return "el VM de esta sesión ya no existe: la sesión se perdió";
}

/** La configuración del pool de plazas calientes de una flota: las mismas
 * opciones de lanzamiento que un VM servido (imagen, vida, idle, red, y el
 * plazo lógico de `fleetLifetime`) y el token único de la flota. Una plaza se recicla cuando le queda menos vida de
 * la que `minRemainingMs` exige: por defecto una hora, o lo que deje el plazo
 * del servicio. `templateVersion` fija la versión de la imagen (la que eligió
 * el build del despliegue); `undefined` usa la más reciente activa. */
export function poolConfigFor(
  project: AgentProject,
  scaling: ScalingOptions,
  accessToken: string,
  metadata: Readonly<Record<string, string>>,
  templateVersion?: string | undefined,
): PoolConfig {
  const timeoutMs = Math.ceil(project.serve.timeoutMs / MS_PER_SECOND) * MS_PER_SECOND;
  const lifetime = fleetLifetime(project, scaling);
  return {
    size: scaling.warmVms,
    template: project.template ?? project.name,
    ...(templateVersion === undefined ? {} : { templateVersion }),
    timeoutMs,
    minRemainingMs: Math.min(
      POOL_MIN_REMAINING_DEFAULT_MS,
      timeoutMs - POOL_MIN_REMAINING_FLOOR_MS,
    ),
    ...(project.serve.idle === null || project.serve.idle === undefined
      ? {}
      : { idle: project.serve.idle }),
    metadata,
    ...(Object.keys(project.env).length === 0 ? {} : { envs: project.env }),
    ...(project.egress === undefined ? {} : { egress: project.egress }),
    ...(project.network === undefined ? {} : { network: project.network }),
    allowInternetAccess: project.allowInternetAccess,
    accessToken,
    ...(lifetime === undefined
      ? {}
      : { maxLifetimeMs: lifetime.maxLifetimeMs, onTimeout: lifetime.onTimeout }),
  };
}

/** Lo que la flota lee de un `AgentProject` (o de su receta): el escalado y
 * de dónde sale, el plazo de la cola y la concurrencia por VM. */
export interface FleetSettings {
  readonly scaling: ScalingOptions;
  readonly queueTimeoutMs: number;
  readonly maxConcurrency: number;
  readonly scalingSource: ScalingSource;
}

/** De dónde sale el escalado: lo explícito gana a la receta, y la receta (que
 * guarda las banderas del despliegue) al proyecto. */
export function scalingSourceOf(
  project: AgentProject | undefined,
  scaling: ScalingOptions | undefined,
  recipeScaling: ScalingOptions | undefined,
): ScalingSource {
  if (scaling !== undefined) {
    return "explicit";
  }
  if (recipeScaling !== undefined) {
    return "recipe";
  }
  return project === undefined ? "default" : "project";
}

/** Un único sitio donde la flota mira el proyecto: un `scaling` explícito
 * gana al de la receta (`recipeScaling`) y éste al del proyecto
 * (`AgentProject.scaling`), y la cola cliente comparte el plazo de la cola del
 * host. Una flota reconectada sin proyecto ni receta usa los valores por
 * defecto. */
export function fleetSettingsOf(
  project: AgentProject | undefined,
  scaling?: ScalingOptions | undefined,
  recipeScaling?: ScalingOptions | undefined,
): FleetSettings {
  const scalingSource = scalingSourceOf(project, scaling, recipeScaling);
  const chosen = scaling ?? recipeScaling;
  if (project === undefined) {
    return {
      scaling: chosen ?? new ScalingOptions(),
      queueTimeoutMs: DEFAULT_QUEUE_TIMEOUT_MS,
      maxConcurrency: 1,
      scalingSource,
    };
  }
  const extras = project as unknown as { scaling?: ScalingOptions };
  const serve = project.serve as unknown as { queueTimeoutMs?: number };
  return {
    scaling: chosen ?? extras.scaling ?? new ScalingOptions(),
    queueTimeoutMs: serve.queueTimeoutMs ?? DEFAULT_QUEUE_TIMEOUT_MS,
    maxConcurrency: project.serve.maxConcurrency,
    scalingSource,
  };
}
