/**
 * Dominio puro de `gateways` (ADR-023, m15-secrets-gateway): qué es un
 * `SecretGateway` válido y cómo se ve su estado una vez `rayd` lo aplica.
 * Espejo de `rayito._secret_gateway._domain`. Nada de AWS ni gRPC:
 * `section.ts` (adaptador) resuelve cada cabecera contra `SecretCache` y
 * llama a `ConfigureSandbox`; este módulo sólo valida la forma de lo que
 * el llamante escribió.
 *
 * Los límites mirroran uno a uno los de `rayd_core::secret_gateway::route`
 * (mismo valor, mismo porqué); viven aquí, no en un límite negociado con
 * el guest, por lo que no pasan por `limits.json`/`limits.ts`.
 */

import { InvalidArgumentError } from "../errors.js";
import type { SecretLike } from "../secrets/names.js";

// -- rayd_core::secret_gateway::route ---------------------------------------
export const MAX_ROUTE_NAME_LEN = 64;
export const MAX_ROUTES_PER_GATEWAY = 8;
export const MAX_HEADERS_PER_ROUTE = 16;
export const MAX_ALLOW_RULES_PER_ROUTE = 32;
/** 10 peticiones/segundo: un tope conservador para un agente que llama a
 * una única API externa de forma interactiva. */
export const DEFAULT_RATE_PER_MINUTE = 600;
export const MIN_RATE_PER_MINUTE = 1;
export const MAX_RATE_PER_MINUTE = 6_000;
export const WILDCARD_SUFFIX = "/*";
export const LOOPBACK_HOST = "127.0.0.1";

const ROUTE_NAME_PATTERN = new RegExp(`^[a-z0-9-]{1,${MAX_ROUTE_NAME_LEN}}$`);
const VALID_METHODS = new Set(["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]);

export interface AllowRule {
  readonly method: string;
  readonly path: string;
}

export interface SecretGatewayOptions {
  readonly upstream: string;
  readonly headers: Readonly<Record<string, SecretLike>>;
  readonly allow: readonly (readonly [string, string])[];
  readonly ratePerMinute?: number | undefined;
}

/**
 * Una entrada de `gateways: { nombre: new SecretGateway(...) }` en
 * `Sandbox.create()`: un único `upstream` fijo al que `rayd` reenvía,
 * inyectando en cada petición permitida el valor de cada cabecera de
 * `headers` (resuelto de Secrets Manager por su nombre, nunca aquí) y
 * rechazando lo que `allow` no cubre o exceda `ratePerMinute`.
 *
 * Coste y activación
 * -------------------
 * Activa: `gateways: { nombre: new SecretGateway(...) }` en
 *     `Sandbox.create()`; sin él, `rayd` no abre ningún listener de loopback
 *     y el SDK no hace ninguna llamada a `ConfigureSandbox` ni a Secrets
 *     Manager.
 * Recursos y llamadas AWS: `secretsmanager:GetSecretValue` una vez por
 *     cabecera y TTL de la `SecretCache` que ya usa `secrets`; ningún
 *     recurso nuevo (reutiliza `infra/secrets-access.yaml`).
 * Coste aproximado: el de `SecretCache` ($0,05 por 10 000 llamadas,
 *     us-east-1, 2026-09-30) más el del secreto en sí si no existía ya.
 * IAM: `secretsmanager:GetSecretValue` en las credenciales del llamante
 *     (no el execution role: la pasarela corre en `rayd`).
 * Cómo apagarla: no pases `gateways` (por defecto `undefined`).
 * Ejemplo:
 *     const sbx = await Sandbox.create({
 *       gateways: {
 *         anthropic: new SecretGateway({
 *           upstream: "https://api.anthropic.com",
 *           headers: { "x-api-key": "anthropic" },
 *           allow: [["POST", "/v1/messages"]],
 *           ratePerMinute: 600,
 *         }),
 *       },
 *     });
 *     const url = sbx.gateways.get("anthropic")?.url;
 */
export class SecretGateway {
  readonly upstream: string;
  readonly headers: Readonly<Record<string, SecretLike>>;
  readonly allow: readonly AllowRule[];
  readonly ratePerMinute: number;

  constructor(options: SecretGatewayOptions) {
    validateUpstream(options.upstream);
    validateHeaders(options.headers);
    const allow = options.allow.map(([method, path]) => ({ method, path }));
    validateAllow(allow);
    const ratePerMinute = options.ratePerMinute ?? 0;
    validateRate(ratePerMinute);
    this.upstream = options.upstream;
    this.headers = options.headers;
    this.allow = allow;
    this.ratePerMinute = ratePerMinute;
  }
}

function validateUpstream(upstream: string): void {
  if (typeof upstream !== "string" || !upstream.startsWith("https://")) {
    throw new InvalidArgumentError(
      "SecretGateway.upstream debe ser 'https://host', sin ruta ni query",
    );
  }
  const rest = upstream.slice("https://".length);
  if (rest === "" || rest.includes("/") || rest.includes("?") || rest.includes("#")) {
    throw new InvalidArgumentError(
      "SecretGateway.upstream no admite ruta, query ni fragmento: sólo esquema y host",
    );
  }
}

function validateHeaders(headers: Readonly<Record<string, SecretLike>>): void {
  const count = Object.keys(headers ?? {}).length;
  if (
    headers === null ||
    typeof headers !== "object" ||
    count < 1 ||
    count > MAX_HEADERS_PER_ROUTE
  ) {
    throw new InvalidArgumentError(
      `SecretGateway.headers debe tener entre 1 y ${MAX_HEADERS_PER_ROUTE} entradas`,
    );
  }
}

function validateAllow(allow: readonly AllowRule[]): void {
  if (allow.length === 0) {
    throw new InvalidArgumentError(
      "SecretGateway.allow no puede estar vacío: ninguna petición se reenviaría nunca",
    );
  }
  if (allow.length > MAX_ALLOW_RULES_PER_ROUTE) {
    throw new InvalidArgumentError(
      `SecretGateway.allow admite como mucho ${MAX_ALLOW_RULES_PER_ROUTE} reglas`,
    );
  }
  for (const rule of allow) {
    if (!VALID_METHODS.has(rule.method)) {
      throw new InvalidArgumentError(
        `método no soportado en allow: ${JSON.stringify(rule.method)}`,
      );
    }
    if (typeof rule.path !== "string" || !rule.path.startsWith("/")) {
      throw new InvalidArgumentError("una ruta de allow debe ser absoluta (empezar por '/')");
    }
  }
}

function validateRate(ratePerMinute: number): void {
  if (
    ratePerMinute !== 0 &&
    (ratePerMinute < MIN_RATE_PER_MINUTE || ratePerMinute > MAX_RATE_PER_MINUTE)
  ) {
    throw new InvalidArgumentError(
      `SecretGateway.ratePerMinute debe ser 0 (usa el valor por defecto) o estar en ` +
        `${MIN_RATE_PER_MINUTE}..${MAX_RATE_PER_MINUTE}`,
    );
  }
}

/** 1-64 `[a-z0-9-]`, como `rayd_core::secret_gateway::route`; es la clave
 * pública de `sbx.gateways[name]`, así que nunca es secreta. */
export function validateRouteName(name: string): string {
  if (typeof name !== "string" || !ROUTE_NAME_PATTERN.test(name)) {
    throw new InvalidArgumentError(
      `nombre de gateway inválido: 1-${MAX_ROUTE_NAME_LEN} caracteres [a-z0-9-]`,
    );
  }
  return name;
}

export function validateGateways(
  gateways: Readonly<Record<string, SecretGateway>>,
): Readonly<Record<string, SecretGateway>> {
  const entries = Object.entries(gateways ?? {});
  if (gateways === null || typeof gateways !== "object" || entries.length === 0) {
    throw new InvalidArgumentError(
      "gateways debe ser un objeto no vacío { nombre: SecretGateway }",
    );
  }
  if (entries.length > MAX_ROUTES_PER_GATEWAY) {
    throw new InvalidArgumentError(`como mucho ${MAX_ROUTES_PER_GATEWAY} gateways por sandbox`);
  }
  for (const [name, gateway] of entries) {
    validateRouteName(name);
    if (!(gateway instanceof SecretGateway)) {
      throw new InvalidArgumentError("cada valor de gateways debe ser un SecretGateway");
    }
  }
  return gateways;
}

/** Espejo de `SecretGatewayRouteStatus`: el estado de una ruta tras el
 * `Configure` (o `ConfigureStatus`) más reciente. */
export class GatewayStatus {
  readonly port: number;
  readonly lastErrorClass: string | undefined;

  constructor(port: number, lastErrorClass: string | undefined = undefined) {
    this.port = port;
    this.lastErrorClass = lastErrorClass;
  }

  /** `"http://127.0.0.1:<port>"`: no es secreto. */
  get url(): string {
    return `http://${LOOPBACK_HOST}:${this.port}`;
  }
}
