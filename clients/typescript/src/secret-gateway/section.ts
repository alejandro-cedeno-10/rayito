/**
 * Adaptador de `gateways` sobre `ConfigureSandbox` (ADR-023): la única
 * parte de esta función que toca AWS (`SecretCache.get`, en `fill()`,
 * justo antes de cada `Configure`) o construye un mensaje de protobuf.
 * `sandbox/sandbox.ts` es quien de verdad llama al RPC; este módulo sólo
 * construye la sección que le pasa y el objeto `sbx.gateways` que recibe
 * de vuelta.
 */

import { create } from "@bufbuild/protobuf";
import {
  type AgentFeatures,
  type ConfigureSectionFactory,
  ImmediateSection,
  type PostApplySection,
  type SectionApplied,
} from "../configure/base.js";
import type { ConfigureRequest, ConfigureStatusResponse } from "../gen/rayito/v1/configure_pb.js";
import {
  SecretGatewayAllowRuleSchema,
  SecretGatewayConfigSchema,
  SecretGatewayRouteSchema,
  type SecretGatewayStatus,
  SecretGatewayStatusSchema,
} from "../gen/rayito/v1/secret_gateway_pb.js";
import type { SecretCache } from "../secrets/cache.js";
import { GatewayStatus, type SecretGateway } from "./domain.js";

export const REQUIRED_FLAG = "secretGateway" as const;
export const SECTION_NAME = "secret_gateway";

/** Implementa `PostApplySection` (`configure/base.ts`) para `gateways`. */
export class GatewaySection extends ImmediateSection implements PostApplySection {
  readonly section = SECTION_NAME;
  readonly requiredFlag = REQUIRED_FLAG;

  constructor(
    private readonly gateways: Readonly<Record<string, SecretGateway>>,
    private readonly cache: SecretCache,
  ) {
    super();
  }

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

  /**
   * Descarta de `cache` cada secreto que estas rutas inyectan, para que el
   * próximo `fill()` los relea de Secrets Manager aunque su TTL
   * (`SecretCache.ttlSeconds`, 300 s por defecto) no haya vencido: sin
   * esto, `sbx.gateways.refresh()` justo después de `SecretStore.update()`
   * volvería a mandar el valor viejo.
   */
  forgetCachedValues(): void {
    for (const gateway of Object.values(this.gateways)) {
      for (const secret of Object.values(gateway.headers)) {
        this.cache.invalidate(secret);
      }
    }
  }

  /**
   * `PostApplySection`: el `sbx.gateways` que `create()`/`take()` guardan.
   * Su `refresh()` olvida los valores en caché y vuelve a mandar esta
   * sección (`applied.reapply`), así que siempre empuja la versión actual
   * de cada secreto.
   */
  afterApply(applied: SectionApplied): GatewayHandle {
    return new GatewayHandle(statusesOf(applied.status), async () => {
      this.forgetCachedValues();
      return statusesOf(await applied.reapply());
    });
  }
}

function statusesOf(status: ConfigureStatusResponse): Readonly<Record<string, GatewayStatus>> {
  return gatewayStatusesFromProto(status.secretGateway ?? create(SecretGatewayStatusSchema, {}));
}

/**
 * Lo que `planFeatures` pone en `FeaturePlan.configureSections` por cada
 * `gateways`: un `ConfigureSection` todavía sin resolver, a la espera de
 * la `SecretCache` que `Sandbox.create()` ya calcula para `secrets` (la
 * misma, nunca una segunda): un `ConfigureSectionFactory`
 * (`configure/base.ts`) que `resolveSections` construye justo antes de la
 * llamada a `Configure`.
 */
export class GatewaySectionFactory implements ConfigureSectionFactory {
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
 * `refresh()` relee cada cabecera de Secrets Manager (aunque la
 * `SecretCache` no haya vencido) y manda un `Configure` nuevo — la forma
 * de rotar un secreto sin recrear el sandbox. Cada ruta conserva su
 * puerto; si `rayd` rechaza la sección, lanza y el estado anterior sigue
 * en pie.
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

/** Relee `ConfigureStatus` sin mandar ningún `Configure`: el `refresh()` de
 * un `sbx.gateways` recuperado por `connect()`. */
export type StatusReader = () => Promise<ConfigureStatusResponse>;

/** `connect()` sólo pregunta por las pasarelas a un agente que anuncia
 * `ConfigureService` y la función `secretGateway`: sobre uno anterior,
 * `sbx.gateways` queda vacío sin ninguna llamada extra. Espejo de
 * `gateways_recoverable` de Python. */
export function gatewaysRecoverable(features: AgentFeatures | undefined): boolean {
  return features?.configure === true && features.secretGateway;
}

/**
 * El `sbx.gateways` que `connect()` reconstruye desde el `ConfigureStatus`
 * de un sandbox en marcha: sólo nombre, puerto y último error de cada ruta,
 * porque `rayd` nunca devuelve el upstream, las cabeceras ni sus valores.
 * Sin la definición original no hay nada que rotar, así que su `refresh()`
 * sólo relee el estado; rotar un secreto sigue siendo cosa del proceso que
 * pasó `gateways` (o de un `reincarnate()`). `EMPTY_GATEWAYS` si el sandbox
 * no tiene pasarelas. Espejo de `recovered_gateways` de Python.
 */
export function recoveredGateways(
  status: ConfigureStatusResponse,
  reader?: StatusReader,
): GatewayHandle {
  const statuses = statusesOf(status);
  if (Object.keys(statuses).length === 0) {
    return EMPTY_GATEWAYS;
  }
  return new GatewayHandle(
    statuses,
    reader === undefined ? undefined : async () => statusesOf(await reader()),
  );
}
