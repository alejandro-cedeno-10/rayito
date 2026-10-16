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
import type { PoolConfig } from "../pool/config.js";
import { MS_PER_SECOND } from "../units.js";
import type { DeploymentState } from "./deployment.js";
import type { AgentEvent, AgentResult } from "./events.js";
import type { AgentProject } from "./project.js";
import { ScalingOptions, SESSIONS_DEDICATED } from "./scaling-domain.js";

/** Prefijo de un id de sesión enrutable: `rs1.<sandbox_id>.<sesión interna>`.
 * Con más de un VM posible, `AgentResult.sessionId` lleva el VM de la sesión
 * y pasarlo de vuelta enruta sin consultar a nadie. */
export const AGENT_SESSION_HANDLE_PREFIX = "rs1";
export const HANDLE_SEPARATOR = ".";
const HANDLE_PARTS = 3;
/** Etiqueta del sandbox de un VM dedicado: el hash de la sesión que el
 * llamante eligió (`sessionIndex`). Nunca el id en claro: las etiquetas viajan
 * en el `runHookPayload`. */
export const AGENT_FLEET_SESSION_LABEL = "rayito.agent.session";
const SESSION_LABEL_HEX_CHARS = 32;
/** Cuánto vale un ping (o un `busy` visto) para elegir miembro. */
export const AGENT_FLEET_PING_TTL_MS = 5000;
/** Pings fallidos seguidos tras los que un miembro se da por perdido y se
 * sustituye. */
export const AGENT_FLEET_UNHEALTHY_PINGS = 3;
/** Espera entre dos intentos de colocar una sesión cuando la flota está llena. */
export const AGENT_FLEET_BUSY_RETRY_MS = 1000;
/** Plazo de la cola cliente; es el mismo valor por defecto que la cola del
 * host (`ServeOptions.queueTimeoutMs`). */
export const AGENT_FLEET_QUEUE_TIMEOUT_MS_DEFAULT = 30_000;
/** Cuántos VMs crea a la vez `start()` (y `scale()`). */
export const AGENT_FLEET_START_CONCURRENCY = 4;
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
/** Códigos nuevos de los errores existentes (ver `errors.ts`). */
export const AGENT_CAPACITY_CODE = "agent_capacity";
export const AGENT_VM_LOST_CODE = "agent_vm_lost";
const POOL_MIN_REMAINING_DEFAULT_MS = 3_600_000;
const POOL_MIN_REMAINING_FLOOR_MS = 60_000;

export type MemberHealth = DeploymentState | "unknown";

export interface SessionRoute {
  readonly sandboxId: string;
  readonly sessionId: string | undefined;
}

/** `rs1.<sandbox_id>.<sesión>`: el id de sesión que devuelve una flota
 * enrutable. */
export function encodeHandle(sandboxId: string, sessionId: string | undefined): string {
  if (sandboxId === "" || sandboxId.includes(HANDLE_SEPARATOR)) {
    throw new InvalidArgumentError("el id del sandbox no puede ser vacío ni llevar un punto");
  }
  return [AGENT_SESSION_HANDLE_PREFIX, sandboxId, sessionId ?? ""].join(HANDLE_SEPARATOR);
}

/** El destino de `value` si es un handle de la flota; `undefined` si es un id
 * cualquiera (el del llamante o el de un despliegue de un solo VM). La
 * sesión interna puede llevar puntos: sólo se parte dos veces. */
export function decodeHandle(value: string | undefined | null): SessionRoute | undefined {
  if (value === undefined || value === null) {
    return undefined;
  }
  const parts = value.split(HANDLE_SEPARATOR);
  const head = parts[0];
  const sandboxId = parts[1];
  if (parts.length < HANDLE_PARTS || head !== AGENT_SESSION_HANDLE_PREFIX || !sandboxId) {
    return undefined;
  }
  const session = parts.slice(HANDLE_PARTS - 1).join(HANDLE_SEPARATOR);
  return { sandboxId, sessionId: session === "" ? undefined : session };
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
}

export function newMember<D>(sandboxId: string, deployment?: D, extra = false): Member<D> {
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
  };
}

/** Un VM de la imagen de la flota según `list-microvms`. */
export interface MemberListing {
  readonly sandboxId: string;
  readonly state: string;
  readonly startedAt: Date;
}

export interface FleetMemberStatus {
  readonly sandboxId: string;
  readonly state: MemberHealth;
  readonly extra: boolean;
  readonly inflight: number;
}

/** El estado de una flota: sus miembros, cuántos están sanos u ocupados y los
 * límites de `ScalingOptions`. Un miembro que este proceso aún no abrió figura
 * como `unknown`: preguntarle lo reanudaría. */
export interface FleetStatus {
  readonly name: string;
  readonly members: readonly FleetMemberStatus[];
  readonly healthy: number;
  readonly busy: number;
  readonly minVms: number;
  readonly maxVms: number;
  readonly warmVms: number;
  readonly warmReady: number;
}

export function fleetStatusOf(
  name: string,
  members: readonly FleetMemberStatus[],
  scaling: ScalingOptions,
  warmReady: number,
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
  return member.failedPings >= AGENT_FLEET_UNHEALTHY_PINGS;
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

/** Cuántos VMs faltan para llegar a `minVms`. */
export function deficit(memberCount: number, scaling: ScalingOptions): number {
  return Math.max(0, scaling.minVms - memberCount);
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
 * opciones de lanzamiento que un VM servido (imagen, vida, idle, red) y el
 * token único de la flota. Una plaza se recicla cuando le queda menos vida de
 * la que `minRemainingMs` exige: por defecto una hora, o lo que deje el plazo
 * del servicio. */
export function poolConfigFor(
  project: AgentProject,
  scaling: ScalingOptions,
  accessToken: string,
  metadata: Readonly<Record<string, string>>,
): PoolConfig {
  const timeoutMs = Math.ceil(project.serve.timeoutMs / MS_PER_SECOND) * MS_PER_SECOND;
  return {
    size: scaling.warmVms,
    template: project.template ?? project.name,
    timeoutMs,
    minRemainingMs: Math.min(
      POOL_MIN_REMAINING_DEFAULT_MS,
      timeoutMs - POOL_MIN_REMAINING_FLOOR_MS,
    ),
    ...(project.serve.idle === null || project.serve.idle === undefined
      ? {}
      : { idle: project.serve.idle }),
    metadata,
    ...(project.egress === undefined ? {} : { egress: project.egress }),
    allowInternetAccess: project.allowInternetAccess,
    accessToken,
  };
}

/** Lo que la flota lee de un `AgentProject`: el escalado, el plazo de la cola
 * y la concurrencia por VM. */
export interface FleetSettings {
  readonly scaling: ScalingOptions;
  readonly queueTimeoutMs: number;
  readonly maxConcurrency: number;
}

/** Un único sitio donde la flota mira el proyecto: un `scaling` explícito
 * gana al del proyecto (`AgentProject.scaling`), y la cola cliente comparte el
 * plazo de la cola del host. Una flota reconectada sin proyecto usa los
 * valores por defecto. */
export function fleetSettingsOf(
  project: AgentProject | undefined,
  scaling?: ScalingOptions | undefined,
): FleetSettings {
  if (project === undefined) {
    return {
      scaling: scaling ?? new ScalingOptions(),
      queueTimeoutMs: AGENT_FLEET_QUEUE_TIMEOUT_MS_DEFAULT,
      maxConcurrency: 1,
    };
  }
  const extras = project as unknown as { scaling?: ScalingOptions };
  const serve = project.serve as unknown as { queueTimeoutMs?: number };
  return {
    scaling: scaling ?? extras.scaling ?? new ScalingOptions(),
    queueTimeoutMs: serve.queueTimeoutMs ?? AGENT_FLEET_QUEUE_TIMEOUT_MS_DEFAULT,
    maxConcurrency: project.serve.maxConcurrency,
  };
}
