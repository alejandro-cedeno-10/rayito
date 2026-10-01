/**
 * `CustomDomain` (m15-custom-domain, ADR-024): dominio propio sobre
 * CloudFront. Espejo de `rayito._custom_domain._service`. `deploy`/
 * `status`/`destroy` son una fachada fina sobre `OptionalStacks` (M15
 * foundations, ADR-016); `register`/`unregister`/`refresh`/`hostFor` son el
 * contrato propio de esta función.
 *
 * **Integración pendiente con `Sandbox.create({domain})`/`getHost()`/
 * `expose()`:** igual que en el SDK Python, `sandbox/sandbox.ts` sólo
 * expone kwargs, delegaciones y exports de M15 foundations
 * (`feature-options.ts`: `domain` sigue lanzando `UnimplementedError`
 * mientras esto no cambie). Ver el comentario equivalente en
 * `rayito._custom_domain._service` para el razonamiento completo.
 */

import type { AwsClientSettings } from "../aws/control-plane.js";
import { CustomDomainError, InvalidArgumentError } from "../errors.js";
import type { StackStatus } from "../stacks/model.js";
import type { StackProvisioner } from "../stacks/port.js";
import { DEFAULT_WAIT_TIMEOUT_MS, OptionalStacks } from "../stacks/service.js";
import {
  checkKvsValueSize,
  encodeRouteMetadata,
  kvsJsonKey,
  kvsMetaKey,
  routeHost,
  routeLabel,
  trafficTokenDigest,
  validatePublicDomain,
} from "./domain.js";
import { CloudFrontKvsWriter, type KeyValueStoreWriter, RESOURCE_NOT_FOUND_CODE } from "./kvs.js";

type Credentials = AwsClientSettings["credentials"];

/** Nombre fijo del componente `OptionalStack` que despliega esta función. */
export const STACK_COMPONENT = "custom-domain";
const KVS_ARN_OUTPUT_KEY = "KvsArn";

/** Reintentos acotados de "describe + puts encadenados" ante una carrera de
 * `ETag` con otro escritor de la misma ruta; espejo de
 * `_service.MAX_ETAG_CONFLICT_RETRIES`. */
const MAX_ETAG_CONFLICT_RETRIES = 3;

/** Códigos que `#writeRoute` trata como "el `ETag` ya no es el vigente,
 * reintenta desde `describe()`"; espejo de `_service._ETAG_CONFLICT_AWS_CODES`
 * (defensivo, sin confirmar contra una distribución real, D3). */
const ETAG_CONFLICT_AWS_CODES = ["ConflictException", "PreconditionFailedException"];

export interface CustomDomainOptions {
  readonly publicDomain: string;
  readonly stackName?: string | undefined;
  readonly kvsArn?: string | undefined;
  readonly region?: string | undefined;
  readonly credentials?: Credentials;
  readonly provisioner?: StackProvisioner;
  readonly kvsWriter?: KeyValueStoreWriter;
  readonly clock?: () => number;
}

export interface DeployCustomDomainOptions {
  readonly certificateArn: string;
  readonly tags?: Readonly<Record<string, string>>;
  readonly wait?: boolean;
  readonly waitTimeoutMs?: number;
}

export interface RegisterRouteOptions {
  readonly endpoint: string;
  readonly jwe: string;
  readonly trafficToken?: string | undefined;
  /** `true` para una ruta sin `trafficToken` a propósito; nunca el valor
   * por defecto implícito (SEC-T25). Omitirlo sin `trafficToken` lanza
   * `InvalidArgumentError` antes de tocar el KVS. */
  readonly public?: boolean | undefined;
  readonly ttlSeconds: number;
}

export interface CustomDomainRoute {
  readonly alias: string;
  readonly port: number;
  readonly publicDomain: string;
  /** Los metadatos que `register()` ya escribió en `m:<label>`; `refresh()`
   * los necesita de vuelta para reescribir esa clave con la misma
   * `endpoint`/hash y sólo `expiresAt` al día. */
  readonly endpoint: string;
  readonly trafficTokenSha256: string;
  readonly expiresAt: Date;
  /** `routeHost(alias, port, publicDomain)`, ya calculado. */
  readonly host: string;
}

function routeOf(
  alias: string,
  port: number,
  publicDomain: string,
  endpoint: string,
  trafficTokenSha256: string,
  expiresAt: Date,
): CustomDomainRoute {
  return {
    alias,
    port,
    publicDomain,
    endpoint,
    trafficTokenSha256,
    expiresAt,
    host: routeHost(alias, port, publicDomain),
  };
}

function resolveKvsArn(
  explicit: string | undefined,
  status: StackStatus | undefined,
): string | undefined {
  if (explicit) {
    return explicit;
  }
  return status?.outputs[KVS_ARN_OUTPUT_KEY];
}

/**
 * Coste y activación
 * -------------------
 * Activa: `new CustomDomain({publicDomain}).deploy({certificateArn})`.
 * Recursos y llamadas AWS: una distribución CloudFront, su CloudFront
 *     Function de enrutado y un KeyValueStore (`infra/custom-domain.yaml`).
 *     En uso, `register`/`unregister`/`refresh` llaman a
 *     `DescribeKeyValueStore`/`PutKey`/`DeleteKey`. No hay refresher
 *     automático en este cambio: llama a `refresh()` tú mismo antes de que
 *     caduque el JWE de una ruta (DOM-7).
 * Coste aproximado: CloudFront ~$0,085/GB + $0,0075/10 000 peticiones HTTPS
 *     (salida, us-east-1); CloudFront Functions ~$0,10 por 1 000 000 de
 *     invocaciones (una por petición); KeyValueStore $0 en reposo, ~$0,50
 *     por 1 000 000 de lecturas (las de la Function) y ~$5 por 1 000 000 de
 *     llamadas de gestión (PutKey/DeleteKey de register/unregister/refresh)
 *     — tres líneas separadas, no una cifra combinada (cifras de lista
 *     desde su lanzamiento, reconfirmar en la etapa de aceptación AWS).
 * IAM: `cloudformation:*Stack*` (vía `OptionalStacks`);
 *     `cloudfront-keyvaluestore:DescribeKeyValueStore/PutKey/DeleteKey`.
 * Cómo apagarla: no llames a `deploy()`; `destroy()` borra la distribución,
 *     la Function y el KVS.
 * Ejemplo:
 *     const domain = new CustomDomain({publicDomain: "sbx.example.com"});
 *     await domain.deploy({certificateArn: "arn:aws:acm:us-east-1:...:certificate/..."});
 *     const route = await domain.register("ws-7", 8000, {
 *       endpoint: sbx.endpoint, jwe, trafficToken: randomToken, ttlSeconds: 2400,
 *     });
 *     route.host; // via hostFor("ws-7", 8000)
 */
export class CustomDomain {
  readonly publicDomain: string;
  readonly #stackName: string | undefined;
  readonly #explicitKvsArn: string | undefined;
  readonly #stacks: OptionalStacks;
  readonly #kvs: KeyValueStoreWriter;
  readonly #clock: () => number;
  #cachedKvsArn: string | undefined;

  constructor(options: CustomDomainOptions) {
    this.publicDomain = validatePublicDomain(options.publicDomain);
    this.#stackName = options.stackName;
    this.#explicitKvsArn = options.kvsArn;
    this.#stacks = new OptionalStacks({
      ...(options.region !== undefined ? { region: options.region } : {}),
      ...(options.credentials !== undefined ? { credentials: options.credentials } : {}),
      ...(options.provisioner !== undefined ? { provisioner: options.provisioner } : {}),
    });
    this.#kvs =
      options.kvsWriter ??
      new CloudFrontKvsWriter({
        ...(options.region !== undefined ? { region: options.region } : {}),
        ...(options.credentials !== undefined ? { credentials: options.credentials } : {}),
      });
    this.#clock = options.clock ?? (() => Date.now() / 1000);
  }

  #stackNameOption(): { readonly stackName?: string } {
    return this.#stackName !== undefined ? { stackName: this.#stackName } : {};
  }

  async deploy(options: DeployCustomDomainOptions): Promise<StackStatus> {
    const status = await this.#stacks.deploy(STACK_COMPONENT, {
      ...this.#stackNameOption(),
      parameters: { PublicDomain: this.publicDomain, CertificateArn: options.certificateArn },
      ...(options.tags !== undefined ? { tags: options.tags } : {}),
      ...(options.wait !== undefined ? { wait: options.wait } : {}),
      waitTimeoutMs: options.waitTimeoutMs ?? DEFAULT_WAIT_TIMEOUT_MS,
    });
    this.#cachedKvsArn = resolveKvsArn(undefined, status);
    return status;
  }

  async status(): Promise<StackStatus | undefined> {
    const status = await this.#stacks.status(STACK_COMPONENT, { ...this.#stackNameOption() });
    this.#cachedKvsArn = resolveKvsArn(undefined, status);
    return status;
  }

  async destroy(
    options: { readonly wait?: boolean; readonly waitTimeoutMs?: number } = {},
  ): Promise<void> {
    await this.#stacks.destroy(STACK_COMPONENT, { ...this.#stackNameOption(), ...options });
    this.#cachedKvsArn = undefined;
  }

  kvsArn(): string {
    const resolved = resolveKvsArn(this.#explicitKvsArn, undefined) ?? this.#cachedKvsArn;
    if (resolved === undefined) {
      throw new CustomDomainError(
        "no se conoce el KvsArn de la pila: pasa kvsArn al construir CustomDomain o llama a status()/deploy() primero",
      );
    }
    return resolved;
  }

  hostFor(alias: string, port: number): string {
    return routeHost(alias, port, this.publicDomain);
  }

  async #deleteBestEffort(kvsArn: string, key: string): Promise<void> {
    try {
      const etag = await this.#kvs.describe(kvsArn);
      await this.#kvs.delete(kvsArn, key, etag);
    } catch {
      // best-effort: la excepción original de quien llamó a #writeRoute ya
      // se está propagando, ésta no debe taparla.
    }
  }

  /**
   * Escribe `writes` (clave, valor) encadenando el `ETag` de un
   * `describe()` inicial. Si una escritura después de la primera falla,
   * borra en reversa (best-effort) las que sí se aplicaron antes de
   * relanzar, para no dejar la ruta a medias. Reintenta la secuencia
   * completa desde `describe()` hasta `MAX_ETAG_CONFLICT_RETRIES` veces si
   * AWS rechaza el `ETag` encadenado por una carrera con otro escritor.
   */
  async #writeRoute(
    kvsArn: string,
    writes: ReadonlyArray<readonly [string, string]>,
  ): Promise<void> {
    let lastError: CustomDomainError | undefined;
    for (let attempt = 0; attempt < MAX_ETAG_CONFLICT_RETRIES; attempt += 1) {
      let etag = await this.#kvs.describe(kvsArn);
      const written: string[] = [];
      try {
        for (const [key, value] of writes) {
          etag = await this.#kvs.put(kvsArn, key, value, etag);
          written.push(key);
        }
        return;
      } catch (error) {
        for (const key of written.reverse()) {
          await this.#deleteBestEffort(kvsArn, key);
        }
        if (
          !(error instanceof CustomDomainError) ||
          !ETAG_CONFLICT_AWS_CODES.includes(error.awsCode ?? "")
        ) {
          throw error;
        }
        lastError = error;
      }
    }
    // El bucle sólo sale por aquí tras agotar los reintentos, siempre
    // después de haber asignado `lastError` al menos una vez.
    throw lastError ?? new CustomDomainError("_writeRoute: reintentos de ETag agotados");
  }

  async register(
    alias: string,
    port: number,
    options: RegisterRouteOptions,
  ): Promise<CustomDomainRoute> {
    if (options.public !== true && options.trafficToken === undefined) {
      throw new InvalidArgumentError(
        "register() necesita trafficToken (o public: true para una ruta pública a propósito: " +
          "nunca es el valor por defecto implícito)",
      );
    }
    if (!(options.ttlSeconds > 0)) {
      throw new InvalidArgumentError(`ttlSeconds debe ser positivo: ${options.ttlSeconds}`);
    }
    checkKvsValueSize(options.jwe);
    const label = routeLabel(alias, port);
    const expiresAtSeconds = Math.floor(this.#clock()) + options.ttlSeconds;
    const trafficTokenSha256 = trafficTokenDigest(options.trafficToken);
    const metadata = encodeRouteMetadata({
      endpoint: options.endpoint,
      trafficTokenSha256,
      expiresAt: expiresAtSeconds,
    });
    await this.#writeRoute(this.kvsArn(), [
      [kvsJsonKey(label), options.jwe],
      [kvsMetaKey(label), metadata],
    ]);
    return routeOf(
      alias,
      port,
      this.publicDomain,
      options.endpoint,
      trafficTokenSha256,
      new Date(expiresAtSeconds * 1000),
    );
  }

  async refresh(
    route: CustomDomainRoute,
    options: { readonly jwe: string; readonly ttlSeconds: number },
  ): Promise<CustomDomainRoute> {
    if (!(options.ttlSeconds > 0)) {
      throw new InvalidArgumentError(`ttlSeconds debe ser positivo: ${options.ttlSeconds}`);
    }
    checkKvsValueSize(options.jwe);
    const label = routeLabel(route.alias, route.port);
    const expiresAtSeconds = Math.floor(this.#clock()) + options.ttlSeconds;
    const metadata = encodeRouteMetadata({
      endpoint: route.endpoint,
      trafficTokenSha256: route.trafficTokenSha256,
      expiresAt: expiresAtSeconds,
    });
    await this.#writeRoute(this.kvsArn(), [
      [kvsJsonKey(label), options.jwe],
      [kvsMetaKey(label), metadata],
    ]);
    return routeOf(
      route.alias,
      route.port,
      route.publicDomain,
      route.endpoint,
      route.trafficTokenSha256,
      new Date(expiresAtSeconds * 1000),
    );
  }

  async unregister(alias: string, port: number): Promise<void> {
    const label = routeLabel(alias, port);
    const kvsArn = this.kvsArn();
    let etag = await this.#kvs.describe(kvsArn);
    for (const key of [kvsJsonKey(label), kvsMetaKey(label)]) {
      try {
        etag = await this.#kvs.delete(kvsArn, key, etag);
      } catch (error) {
        if (!(error instanceof CustomDomainError) || error.awsCode !== RESOURCE_NOT_FOUND_CODE) {
          throw error;
        }
        etag = await this.#kvs.describe(kvsArn);
      }
    }
  }
}
