/**
 * Política de egress en el guest (ADR-012), sin I/O: resolución de la forma
 * de E2B (selectores función, `allowInternetAccess: false`), comprobaciones
 * tempranas de forma (`rayd` sigue siendo la autoridad), conversión desde y
 * hacia el `.proto`, la puerta de `create()` que falla cerrado y la
 * traducción de errores de `NetworkService`. Espejo de
 * `rayito/_network_base.py`. Nada de aquí registra listas, direcciones de
 * proxy ni credenciales.
 */

import { isIP } from "node:net";
import { create } from "@bufbuild/protobuf";
import { Code } from "@connectrpc/connect";
import { InvalidArgumentError, UnimplementedError } from "../errors.js";
import {
  type EgressProxy as EgressProxyMessage,
  EgressProxySchema,
  EgressEnforcement as EnforcementMessage,
  type NetworkPolicy as NetworkPolicyMessage,
  NetworkPolicySchema,
  type NetworkState as NetworkStateMessage,
  type UpdateNetworkRequest,
  UpdateNetworkRequestSchema,
} from "../gen/rayito/v1/network_pb.js";
import {
  EGRESS_HOSTNAME_MAX_CHARS,
  EGRESS_MAX_ENTRIES_PER_LIST,
  EGRESS_MAX_HOSTNAME_ENTRIES,
  EGRESS_PROXY_CREDENTIAL_MAX_BYTES,
} from "../limits.js";
import type { Logger } from "../logger.js";
import {
  ALL_TRAFFIC,
  EgressEnforcement,
  type EgressProxyInput,
  type NetworkPolicyInput,
  type NetworkSelector,
  type NetworkSelectorContext,
  type NetworkState,
} from "../models.js";
import { asConnectError, translateRpcError } from "../transport/errors.js";

export const NETWORK_FEATURE = "network";
export const ALLOW_INTERNET_ACCESS_FEATURE = "allowInternetAccess: false";
export const UPDATE_NETWORK_FEATURE = "updateNetwork";
export const GET_NETWORK_FEATURE = "getNetwork";

export const EGRESS_UNAVAILABLE_REASON =
  "la imagen no aplica política de egress en el guest (Health.egress_enforcement=NONE): usa " +
  "una imagen M9 de rayito-base-caps (additionalOsCapabilities ALL) o, a nivel de plataforma, " +
  'Sandbox.create({ egress: ["<ConnectorArn de infra/egress-connector.yaml>"] }); el sandbox ' +
  "se ha terminado";
export const CAPS_REASON =
  "la imagen no tiene CAP_NET_ADMIN: la política de egress exige una imagen M9 de " +
  "rayito-base-caps (additionalOsCapabilities ALL)";
export const OLD_AGENT_REASON =
  "este rayd no tiene NetworkService: la política de egress exige una imagen M9 de rayito-base-caps";
export const ALLOW_ONLY_NOTICE =
  "allowOut sin denyOut no restringe nada: no se aplica política de egress en el guest";
export const POOL_NETWORK_MESSAGE =
  "create({ pool }) no admite network ni allowInternetAccess: false: las plazas de un " +
  "SandboxPool se lanzan sin política de egress";

const NETWORK_KEYS: ReadonlySet<string> = new Set(["allowOut", "denyOut", "egressProxy"]);
const EGRESS_PROXY_KEYS: ReadonlySet<string> = new Set(["address", "username", "password"]);
const IPV4_MAX_PREFIX = 32;
const IPV6_MAX_PREFIX = 128;

const ENFORCEMENT_FROM_MESSAGE: ReadonlyMap<EnforcementMessage, EgressEnforcement> = new Map([
  [EnforcementMessage.NONE, EgressEnforcement.NONE],
  [EnforcementMessage.GUEST_ROUTES, EgressEnforcement.GUEST_ROUTES],
  [EnforcementMessage.GUEST_ROUTES_AND_PROXY, EgressEnforcement.GUEST_ROUTES_AND_PROXY],
]);

const loggersNoticed = new WeakSet<Logger>();

/** La política ya resuelta: listas concretas (selectores evaluados) y el proxy validado en forma. */
export interface ResolvedNetworkPolicy {
  readonly allowOut: readonly string[];
  readonly denyOut: readonly string[];
  readonly egressProxy: EgressProxyInput | undefined;
}

export interface ResolveNetworkOptions {
  readonly allowInternetAccess?: boolean | undefined;
}

export function selectorContext(): NetworkSelectorContext {
  return Object.freeze({ allTraffic: ALL_TRAFFIC, rules: new Map<string, readonly unknown[]>() });
}

/** Una lista se copia; una función se llama con `context`. Lo que no sea una lista de strings es `InvalidArgumentError`. */
export function resolveSelector(
  selector: NetworkSelector | undefined,
  field: string,
  context: NetworkSelectorContext,
): string[] {
  if (selector === undefined || selector === null) {
    return [];
  }
  const resolved: unknown = typeof selector === "function" ? selector(context) : selector;
  if (!Array.isArray(resolved)) {
    throw new InvalidArgumentError(
      `${field} debe ser una lista de strings o una función que la devuelva`,
    );
  }
  return resolved.map((entry: unknown, index) => {
    if (typeof entry !== "string") {
      throw new InvalidArgumentError(`${field}[${index}] debe ser un string`);
    }
    return entry;
  });
}

/**
 * Semántica de `update_network` de E2B: lo omitido queda vacío.
 * `allowInternetAccess: false` añade `ALL_TRAFFIC` a `denyOut` si falta;
 * `true` o `undefined` dejan las listas como vienen.
 */
export function resolveNetwork(
  network: NetworkPolicyInput | undefined,
  options: ResolveNetworkOptions = {},
): ResolvedNetworkPolicy {
  const allowInternetAccess = validatedAllowInternetAccess(options.allowInternetAccess);
  const input = networkObject(network);
  const context = selectorContext();
  const allowOut = resolveSelector(input.allowOut, "allowOut", context);
  const denyOut = resolveSelector(input.denyOut, "denyOut", context);
  return Object.freeze({
    allowOut: Object.freeze(allowOut),
    denyOut: Object.freeze(allowInternetAccess === false ? withAllTraffic(denyOut) : denyOut),
    egressProxy: resolveEgressProxy(input.egressProxy),
  });
}

function validatedAllowInternetAccess(value: unknown): boolean | undefined {
  if (value !== undefined && typeof value !== "boolean") {
    throw new InvalidArgumentError(
      `allowInternetAccess debe ser un boolean, recibido ${typeof value}`,
    );
  }
  return value;
}

function networkObject(network: unknown): NetworkPolicyInput {
  if (network === undefined || network === null) {
    return {};
  }
  if (typeof network !== "object" || Array.isArray(network)) {
    throw new InvalidArgumentError(
      "network debe ser un objeto con allowOut, denyOut o egressProxy",
    );
  }
  rejectUnknownKeys(network, NETWORK_KEYS, "network");
  return network as NetworkPolicyInput;
}

function resolveEgressProxy(value: unknown): EgressProxyInput | undefined {
  if (value === undefined || value === null) {
    return undefined;
  }
  if (typeof value !== "object" || Array.isArray(value)) {
    throw new InvalidArgumentError(
      "egressProxy debe ser un objeto con address y, opcionalmente, username y password",
    );
  }
  rejectUnknownKeys(value, EGRESS_PROXY_KEYS, "egressProxy");
  const proxy = value as Readonly<Record<string, unknown>>;
  const address = requiredString(proxy.address, "egressProxy.address");
  const username = optionalString(proxy.username, "egressProxy.username");
  const password = optionalString(proxy.password, "egressProxy.password");
  return Object.freeze({
    address,
    ...(username === undefined ? {} : { username }),
    ...(password === undefined ? {} : { password }),
  });
}

function rejectUnknownKeys(value: object, known: ReadonlySet<string>, field: string): void {
  for (const key of Object.keys(value)) {
    if (!known.has(key)) {
      throw new InvalidArgumentError(`${field}: clave desconocida '${key}'`);
    }
  }
}

function requiredString(value: unknown, field: string): string {
  if (typeof value !== "string") {
    throw new InvalidArgumentError(`${field} debe ser un string`);
  }
  return value;
}

function optionalString(value: unknown, field: string): string | undefined {
  return value === undefined || value === null ? undefined : requiredString(value, field);
}

function withAllTraffic(entries: readonly string[]): string[] {
  return entries.includes(ALL_TRAFFIC) ? [...entries] : [...entries, ALL_TRAFFIC];
}

/** Espejo del modo de `rayd`: sólo `denyOut` o un proxy del operador restringen algo. */
export function requiresEnforcement(policy: ResolvedNetworkPolicy): boolean {
  return policy.denyOut.length > 0 || policy.egressProxy !== undefined;
}

export function isEmptyPolicy(policy: ResolvedNetworkPolicy): boolean {
  return (
    policy.allowOut.length === 0 && policy.denyOut.length === 0 && policy.egressProxy === undefined
  );
}

/** `allowInternetAccess: false` cuando la política salió sólo de ese flag; si no, `network`. */
export function egressFeature(
  policy: ResolvedNetworkPolicy,
  allowInternetAccess: boolean | undefined,
): string {
  const onlyTheFlag =
    allowInternetAccess === false &&
    policy.allowOut.length === 0 &&
    policy.egressProxy === undefined &&
    policy.denyOut.length === 1 &&
    policy.denyOut[0] === ALL_TRAFFIC;
  return onlyTheFlag ? ALLOW_INTERNET_ACCESS_FEATURE : NETWORK_FEATURE;
}

/** Un pool lanza sus plazas sin política: `create({ pool })` con una política no vacía se rechaza. */
export function rejectNetworkWithPool(policy: ResolvedNetworkPolicy): void {
  if (!isEmptyPolicy(policy)) {
    throw new InvalidArgumentError(POOL_NETWORK_MESSAGE);
  }
}

/**
 * Comprobaciones tempranas con los límites de `limits.json`: tamaño de las
 * listas, entradas vacías, nombres de host en `denyOut`, cuántos nombres y
 * de qué longitud, bytes de las credenciales y una contraseña sin usuario.
 * Los mensajes nombran la lista y el índice, nunca la entrada.
 */
export function validatePolicyShape(policy: ResolvedNetworkPolicy): void {
  validateList(policy.allowOut, "allowOut");
  validateList(policy.denyOut, "denyOut");
  validateDenyEntries(policy.denyOut);
  validateHostnameEntries(policy.allowOut);
  if (policy.egressProxy !== undefined) {
    validateEgressProxy(policy.egressProxy);
  }
}

function validateList(entries: readonly string[], field: string): void {
  if (entries.length > EGRESS_MAX_ENTRIES_PER_LIST) {
    throw new InvalidArgumentError(
      `${field}: máximo ${EGRESS_MAX_ENTRIES_PER_LIST} entradas, recibidas ${entries.length}`,
    );
  }
  entries.forEach((entry, index) => {
    if (entry.trim().length === 0) {
      throw new InvalidArgumentError(`${field}[${index}]: entrada vacía`);
    }
  });
}

function validateDenyEntries(entries: readonly string[]): void {
  entries.forEach((entry, index) => {
    if (!isNetworkEntry(entry)) {
      throw new InvalidArgumentError(
        `denyOut[${index}]: no es un CIDR ni una IP (los nombres de host no se admiten en denyOut)`,
      );
    }
  });
}

function validateHostnameEntries(entries: readonly string[]): void {
  let hostnames = 0;
  entries.forEach((entry, index) => {
    if (isNetworkEntry(entry)) {
      return;
    }
    hostnames += 1;
    if (hostnameLength(entry) > EGRESS_HOSTNAME_MAX_CHARS) {
      throw new InvalidArgumentError(
        `allowOut[${index}]: un nombre de host tiene como máximo ${EGRESS_HOSTNAME_MAX_CHARS} caracteres`,
      );
    }
  });
  if (hostnames > EGRESS_MAX_HOSTNAME_ENTRIES) {
    throw new InvalidArgumentError(
      `allowOut: máximo ${EGRESS_MAX_HOSTNAME_ENTRIES} nombres de host, recibidos ${hostnames}`,
    );
  }
}

/** Sin el `*.` inicial ni el punto final: nunca rechaza en cliente lo que `rayd` aceptaría. */
function hostnameLength(entry: string): number {
  return entry.replace(/^\*\./, "").replace(/\.$/, "").length;
}

/** Una IP o un CIDR `dirección/prefijo` con el prefijo dentro de su familia. */
export function isNetworkEntry(entry: string): boolean {
  const slash = entry.indexOf("/");
  const address = slash < 0 ? entry : entry.slice(0, slash);
  const family = isIP(address);
  if (family === 0) {
    return false;
  }
  if (slash < 0) {
    return true;
  }
  const prefix = entry.slice(slash + 1);
  if (!/^\d{1,3}$/.test(prefix)) {
    return false;
  }
  return Number(prefix) <= (family === 4 ? IPV4_MAX_PREFIX : IPV6_MAX_PREFIX);
}

function validateEgressProxy(proxy: EgressProxyInput): void {
  if (proxy.address.trim().length === 0) {
    throw new InvalidArgumentError(
      "egressProxy.address: obligatorio (host:puerto o [IPv6]:puerto)",
    );
  }
  if (proxy.password !== undefined && proxy.username === undefined) {
    throw new InvalidArgumentError("egressProxy.password exige egressProxy.username");
  }
  validateCredential(proxy.username, "egressProxy.username");
  validateCredential(proxy.password, "egressProxy.password");
}

function validateCredential(value: string | undefined, field: string): void {
  if (value === undefined) {
    return;
  }
  const bytes = Buffer.byteLength(value, "utf8");
  if (bytes === 0 || bytes > EGRESS_PROXY_CREDENTIAL_MAX_BYTES) {
    throw new InvalidArgumentError(
      `${field}: entre 1 y ${EGRESS_PROXY_CREDENTIAL_MAX_BYTES} bytes, recibidos ${bytes}`,
    );
  }
}

export function policyToProto(policy: ResolvedNetworkPolicy): NetworkPolicyMessage {
  return create(NetworkPolicySchema, {
    allowOut: [...policy.allowOut],
    denyOut: [...policy.denyOut],
    ...(policy.egressProxy === undefined
      ? {}
      : { egressProxy: egressProxyToProto(policy.egressProxy) }),
  });
}

function egressProxyToProto(proxy: EgressProxyInput): EgressProxyMessage {
  return create(EgressProxySchema, {
    address: proxy.address,
    ...(proxy.username === undefined ? {} : { username: proxy.username }),
    ...(proxy.password === undefined ? {} : { password: proxy.password }),
  });
}

export function updateNetworkRequest(policy: ResolvedNetworkPolicy): UpdateNetworkRequest {
  return create(UpdateNetworkRequestSchema, { policy: policyToProto(policy) });
}

/** Un valor desconocido del enum (agente más nuevo) cuenta como `unspecified`: la puerta falla cerrado. */
export function enforcementFromProto(value: EnforcementMessage): EgressEnforcement {
  return ENFORCEMENT_FROM_MESSAGE.get(value) ?? EgressEnforcement.UNSPECIFIED;
}

export function stateFromProto(message: NetworkStateMessage): NetworkState {
  return Object.freeze({
    allowOut: Object.freeze([...message.allowOut]),
    denyOut: Object.freeze([...message.denyOut]),
    egressProxyConfigured: message.egressProxyConfigured,
    enforcement: enforcementFromProto(message.enforcement),
    localProxyPort: message.localProxyPort === 0 ? undefined : message.localProxyPort,
  });
}

export function isEnforced(enforcement: EgressEnforcement): boolean {
  return enforcement !== EgressEnforcement.UNSPECIFIED && enforcement !== EgressEnforcement.NONE;
}

/** El error de la puerta de `create()` cuando el guest no aplica la política (`unspecified` o `none`). */
export function egressGateError(
  sandboxId: string,
  enforcement: EgressEnforcement,
  feature: string,
): UnimplementedError | undefined {
  if (isEnforced(enforcement)) {
    return undefined;
  }
  return new UnimplementedError(
    feature,
    `${EGRESS_UNAVAILABLE_REASON} (${sandboxId}, egressEnforcement=${enforcement})`,
  );
}

/**
 * `FailedPrecondition` (imagen sin `CAP_NET_ADMIN`) y `Unimplemented`
 * (agente anterior a M9) son `UnimplementedError`; el resto sigue la tabla
 * unaria (`InvalidArgument` → `InvalidArgumentError`, `Internal` →
 * `SandboxError`).
 */
export function networkRpcError(error: unknown, feature: string): Error {
  const code = asConnectError(error)?.code;
  if (code === Code.FailedPrecondition) {
    return new UnimplementedError(feature, CAPS_REASON);
  }
  if (code === Code.Unimplemented) {
    return new UnimplementedError(feature, OLD_AGENT_REASON);
  }
  return translateRpcError(error);
}

/** Un aviso por logger cuando `allowOut` llega sin `denyOut` ni proxy: no restringe nada. */
export function logAllowOnlyNotice(
  policy: ResolvedNetworkPolicy,
  logger: Logger | undefined,
): void {
  if (logger === undefined || loggersNoticed.has(logger)) {
    return;
  }
  if (policy.allowOut.length === 0 || requiresEnforcement(policy)) {
    return;
  }
  loggersNoticed.add(logger);
  logger.info?.(ALLOW_ONLY_NOTICE);
}
