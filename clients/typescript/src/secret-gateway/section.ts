/**
 * Adaptador de `gateways` sobre `ConfigureSandbox` (ADR-023): la única
 * parte de esta función que toca AWS (`SecretCache.get`, en `fill()`,
 * justo antes de cada `Configure`) o construye un mensaje de protobuf.
 * `sandbox/sandbox.ts` es quien de verdad llama al RPC; este módulo sólo
 * construye la sección que le pasa y el objeto `sbx.gateways` que recibe
 * de vuelta.
 */

import { create } from "@bufbuild/protobuf";
import type { ConfigureSection } from "../configure-base.js";
import type { ConfigureRequest } from "../gen/rayito/v1/configure_pb.js";
import {
  SecretGatewayAllowRuleSchema,
  SecretGatewayConfigSchema,
  SecretGatewayRouteSchema,
  type SecretGatewayStatus,
} from "../gen/rayito/v1/secret_gateway_pb.js";
import type { SecretCache } from "../secrets/cache.js";
import { GatewayStatus, type SecretGateway } from "./domain.js";

export const REQUIRED_FLAG = "secretGateway" as const;
export const SECTION_NAME = "secret_gateway";

/** Implementa `ConfigureSection` (`configure-base.ts`) para `gateways`. */
export class GatewaySection implements ConfigureSection {
  readonly section = SECTION_NAME;
  readonly requiredFlag = REQUIRED_FLAG;

  constructor(
    private readonly gateways: Readonly<Record<string, SecretGateway>>,
    private readonly cache: SecretCache,
  ) {}

  /** Resuelve cada cabecera (`SecretCache.get`: un acierto no llama a AWS)
   * y rellena `request.secretGateway`. El valor resuelto sólo vive en este
   * mensaje, que sale por el canal autenticado de `ConfigureSandbox`. */
  async fill(request: ConfigureRequest): Promise<void> {
    const routes = await Promise.all(
      Object.entries(this.gateways).map(async ([name, gateway]) => {
        const headerEntries = await Promise.all(
          Object.entries(gateway.headers).map(
            async ([header, secret]) => [header, await this.cache.get(secret)] as const,
          ),
        );
        return create(SecretGatewayRouteSchema, {
          name,
          upstream: gateway.upstream,
          headers: Object.fromEntries(headerEntries),
          allow: gateway.allow.map((rule) =>
            create(SecretGatewayAllowRuleSchema, { method: rule.method, path: rule.path }),
          ),
          ratePerMinute: gateway.ratePerMinute,
        });
      }),
    );
    request.secretGateway = create(SecretGatewayConfigSchema, { routes });
  }
}

/**
 * Lo que `planFeatures` pone en `FeaturePlan.configureSections` por cada
 * `gateways`: un `ConfigureSection` todavía sin resolver, a la espera de
 * la `SecretCache` que `Sandbox.create()` ya calcula para `secrets` (la
 * misma, nunca una segunda). Fija la convención que sigue cualquier
 * entrada de `configureSections` que necesite algo resuelto más tarde que
 * `planFeatures`: un invocable de un solo argumento, la `SecretCache`
 * resuelta, que devuelve el `ConfigureSection` de verdad.
 */
export class GatewaySectionFactory {
  constructor(private readonly gateways: Readonly<Record<string, SecretGateway>>) {}

  build(cache: SecretCache): GatewaySection {
    return new GatewaySection(this.gateways, cache);
  }
}

export function gatewayStatusesFromProto(
  status: SecretGatewayStatus,
): Readonly<Record<string, GatewayStatus>> {
  const result: Record<string, GatewayStatus> = {};
  for (const route of status.routes) {
    result[route.name] = new GatewayStatus(route.port, route.lastErrorClass || undefined);
  }
  return result;
}

export type GatewayRefresher = () => Promise<Readonly<Record<string, GatewayStatus>>>;

/**
 * `sbx.gateways`: un mapeo de sólo lectura `nombre -> GatewayStatus`. Sin
 * `gateways` es siempre un `GatewayHandle` vacío cuyo `refresh()` no hace
 * nada, así que llamarlo nunca es una rama especial. Con `gateways`,
 * `refresh()` vuelve a resolver cada cabecera (una `SecretCache` que ya
 * venció la relee) y manda un `Configure` nuevo — la forma de rotar un
 * secreto sin recrear el sandbox.
 */
export class GatewayHandle {
  #statuses: Readonly<Record<string, GatewayStatus>>;
  readonly #refresher: GatewayRefresher | undefined;

  constructor(statuses: Readonly<Record<string, GatewayStatus>>, refresher?: GatewayRefresher) {
    this.#statuses = statuses;
    this.#refresher = refresher;
  }

  get(name: string): GatewayStatus | undefined {
    return this.#statuses[name];
  }

  get size(): number {
    return Object.keys(this.#statuses).length;
  }

  entries(): readonly (readonly [string, GatewayStatus])[] {
    return Object.entries(this.#statuses);
  }

  async refresh(): Promise<void> {
    if (this.#refresher !== undefined) {
      this.#statuses = await this.#refresher();
    }
  }
}

export const EMPTY_GATEWAYS: GatewayHandle = new GatewayHandle({});
