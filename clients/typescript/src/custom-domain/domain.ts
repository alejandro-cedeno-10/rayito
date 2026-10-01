/**
 * Dominio puro de `m15-custom-domain` (ADR-024). Espejo de
 * `rayito._custom_domain._domain`: el formato de hostname
 * `{puerto}-{alias}.{dominio}`, las dos claves por ruta del
 * `KeyValueStore` de CloudFront y el contrato de su valor. Nada aquí
 * importa un SDK de AWS.
 *
 * Por qué dos claves por ruta (`j:<label>`/`m:<label>`): un JWE de
 * `create-microvm-auth-token` mide 823 B (DOM-1); sumarle el endpoint y el
 * hash del `traffic_token` pasaría del límite de 1 KiB por valor de
 * CloudFront KeyValueStore (`AWS_API_NOTES.md` §29).
 */

import { createHash } from "node:crypto";
import { CustomDomainError, InvalidArgumentError } from "../errors.js";
import { RESERVED_PORTS } from "../limits.js";

/** Límite real de un valor de CloudFront KeyValueStore (`AWS_API_NOTES.md` §29). */
export const MAX_KVS_VALUE_BYTES = 1024;

const MAX_TCP_PORT = 65535;
const DNS_LABEL_RE = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/;
/** Nunca empieza ni termina en guion, como cualquier etiqueta DNS; defensa
 * en profundidad, no una necesidad de `route_label` en sí (ver la
 * contraparte en Python). */
const ALIAS_RE = /^[a-z0-9]([a-z0-9-]{0,53}[a-z0-9])?$/;
const MAX_HOST_LENGTH = 253;

export function validatePublicDomain(value: string): string {
  const labels = value.split(".");
  if (value === "" || labels.some((label) => !DNS_LABEL_RE.test(label))) {
    throw new InvalidArgumentError(`publicDomain inválido: ${JSON.stringify(value)}`);
  }
  return value;
}

export function validateAlias(value: string): string {
  if (!ALIAS_RE.test(value)) {
    throw new InvalidArgumentError(`alias de ruta inválido: ${JSON.stringify(value)}`);
  }
  return value;
}

export function validateRoutePort(port: number): number {
  if (!Number.isInteger(port) || port < 1 || port > MAX_TCP_PORT) {
    throw new InvalidArgumentError(`puerto de ruta fuera de rango: ${port}`);
  }
  if ((RESERVED_PORTS as readonly number[]).includes(port)) {
    throw new InvalidArgumentError(
      `puerto de ruta reservado: ${port} (reservedPorts: ${RESERVED_PORTS.join(", ")})`,
    );
  }
  return port;
}

export function routeLabel(alias: string, port: number): string {
  validateAlias(alias);
  validateRoutePort(port);
  const label = `${port}-${alias}`;
  if (!DNS_LABEL_RE.test(label)) {
    throw new InvalidArgumentError(`etiqueta de ruta inválida: ${JSON.stringify(label)}`);
  }
  return label;
}

export function routeHost(alias: string, port: number, publicDomain: string): string {
  const label = routeLabel(alias, port);
  validatePublicDomain(publicDomain);
  const host = `${label}.${publicDomain}`;
  if (host.length > MAX_HOST_LENGTH) {
    throw new InvalidArgumentError(`hostname de ruta demasiado largo (${host.length} car.)`);
  }
  return host;
}

export function kvsJsonKey(label: string): string {
  return `j:${label}`;
}

export function kvsMetaKey(label: string): string {
  return `m:${label}`;
}

export function trafficTokenDigest(trafficToken: string | undefined): string {
  if (trafficToken === undefined) {
    return "";
  }
  return createHash("sha256").update(trafficToken, "utf8").digest("hex");
}

export interface RouteMetadata {
  readonly endpoint: string;
  readonly trafficTokenSha256: string;
  readonly expiresAt: number;
}

export function checkKvsValueSize(value: string): void {
  const size = Buffer.byteLength(value, "utf8");
  if (size > MAX_KVS_VALUE_BYTES) {
    throw new CustomDomainError(
      `valor de KeyValueStore de ${size} B supera el límite de ${MAX_KVS_VALUE_BYTES} B (kvs_value_too_large)`,
    );
  }
}

export function encodeRouteMetadata(metadata: RouteMetadata): string {
  const payload = { e: metadata.endpoint, t: metadata.trafficTokenSha256, x: metadata.expiresAt };
  const encoded = JSON.stringify(payload);
  checkKvsValueSize(encoded);
  return encoded;
}

export function decodeRouteMetadata(raw: string): RouteMetadata {
  const payload = JSON.parse(raw) as { e: string; t: string; x: number };
  return { endpoint: payload.e, trafficTokenSha256: payload.t, expiresAt: payload.x };
}
