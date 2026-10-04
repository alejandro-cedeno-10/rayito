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

/** La etiqueta comodín del alias por defecto de la distribución
 * (`*.<PublicDomain>`, `infra/custom-domain.yaml`). Espejo de
 * `_domain.WILDCARD_LABEL`. */
export const WILDCARD_LABEL = "*";

/** Cuota por defecto de nombres alternativos (CNAMEs) por distribución
 * CloudFront (100,
 * https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/cloudfront-limits.html).
 * Espejo de `_domain.MAX_ALTERNATE_DOMAIN_NAMES`. */
export const MAX_ALTERNATE_DOMAIN_NAMES = 100;

/** Separador de un parámetro `CommaDelimitedList` de CloudFormation
 * (`AlternateDomainNames`). Espejo de `_domain.CFN_LIST_SEPARATOR`. */
export const CFN_LIST_SEPARATOR = ",";

/**
 * Los nombres alternativos explícitos de la distribución, en lugar del
 * comodín por defecto `*.<publicDomain>`. Cada uno es `*.<publicDomain>` o
 * `<etiqueta>.<publicDomain>` con UNA sola etiqueta DNS delante (en
 * minúsculas): la Function enruta por la primera etiqueta del host. Lo
 * normal es pasar `hostFor(alias, port)` de rutas ya conocidas: sirve
 * cuando otra distribución ya tiene `*.<publicDomain>` o para una prueba
 * aislada. Espejo de `_domain.validate_alternate_domain_names`.
 */
export function validateAlternateDomainNames(
  names: readonly string[],
  publicDomain: string,
): readonly string[] {
  validatePublicDomain(publicDomain);
  if (names.length === 0) {
    throw new InvalidArgumentError(
      "alternateDomainNames debe ser una lista no vacía de hostnames (omítelo para " +
        `usar el comodín ${WILDCARD_LABEL}.${publicDomain})`,
    );
  }
  if (names.length > MAX_ALTERNATE_DOMAIN_NAMES) {
    throw new InvalidArgumentError(
      `demasiados nombres alternativos (${names.length}): CloudFront admite ` +
        `${MAX_ALTERNATE_DOMAIN_NAMES} por distribución`,
    );
  }
  const suffix = `.${publicDomain}`;
  for (const name of names) {
    const label = name.endsWith(suffix) ? name.slice(0, -suffix.length) : undefined;
    if (label === undefined || !(label === WILDCARD_LABEL || DNS_LABEL_RE.test(label))) {
      throw new InvalidArgumentError(
        `nombre alternativo inválido: ${JSON.stringify(name)} (debe ser <etiqueta>${suffix} o ` +
          `${WILDCARD_LABEL}${suffix})`,
      );
    }
  }
  if (new Set(names).size !== names.length) {
    throw new InvalidArgumentError(`nombres alternativos repetidos: ${JSON.stringify(names)}`);
  }
  return [...names];
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
