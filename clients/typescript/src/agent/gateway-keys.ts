/**
 * Renovación opcional de la clave de corta duración de Bedrock de las
 * pasarelas de una flota (`renewGatewayKeys`, ADR-034 D7, design D6). Espejo
 * de `rayito._agent._gateway_keys`.
 *
 * La decisión es pura (`testdata/fleet-recipe/renewal.json`): una pasarela con
 * preset `bedrock` se renueva si este proceso no la renovó en los últimos
 * `gatewayKeyRenewAfterSeconds` y su secreto tampoco cambió en ese plazo
 * (`DescribeSecret.LastChangedDate`). Renovar es firmar en local una clave con
 * las credenciales del llamante (`@aws/bedrock-token-generator`, sin llamada a
 * AWS) y escribir `Bearer <clave>` en el secreto (`PutSecretValue`). Ni la
 * clave ni el nombre del secreto aparecen en logs, errores ni en la receta.
 */

import { randomUUID } from "node:crypto";
import type { AwsClientSettings } from "../aws/control-plane.js";
import { clientConfig } from "../aws/control-plane.js";
import { awsCode } from "../aws/optional-client.js";
import { sanitizeAwsError } from "../aws/sanitize.js";
import { InvalidArgumentError, SecretError } from "../errors.js";
import { GATEWAY_KEY_EXPIRY_SECONDS, GATEWAY_KEY_RENEW_AFTER_SECONDS } from "../limits.js";
import { loadOptionalPeer } from "../optional.js";
import { DEFAULT_SECRET_PREFIX, resolveSecretId, type SecretLike } from "../secrets/names.js";
import { MS_PER_SECOND } from "../units.js";
import { AWS_REGION_PATTERN } from "./domain.js";
import type { AgentProject } from "./project.js";

/** El preset de las pasarelas cuya clave se puede renovar. */
export const BEDROCK_PRESET = "bedrock";
export const RENEW_NEEDS_BEDROCK = "renew_gateway_keys necesita una pasarela con preset bedrock";
/** Escribir la clave de Bedrock en un secreto que también lee una pasarela
 * hacia otro upstream la enviaría allí: no se renueva. */
export const RENEW_SHARED_SECRET =
  "renew_gateway_keys no renueva la pasarela '{gateway}': su secreto lo usa también " +
  "la pasarela '{other}', que no es de Bedrock";
/** El valor que espera la pasarela `bedrock`: `Bearer <clave>`. */
export const BEARER_PREFIX = "Bearer ";
/** El peer opcional del generador oficial de AWS. */
export const TOKEN_GENERATOR_PEER = "@aws/bedrock-token-generator";
const SECRETS_MANAGER_PEER = "@aws-sdk/client-secrets-manager";
const RENEWAL_FEATURE = "renewGatewayKeys";
const BEDROCK_RUNTIME_HOST_PREFIX = "bedrock-runtime.";
const AMAZONAWS_HOST_SUFFIX = ".amazonaws.com";
/** El código propio de un permiso que falta, con el nombre de la pasarela. */
export const PERMISSION_DENIED_CODE = "permission_denied";
const ACCESS_DENIED_PREFIX = "AccessDenied";

type Credentials = AwsClientSettings["credentials"];

/** Genera la clave de una región. */
export type TokenProvider = (region: string) => Promise<string>;

/** Una pasarela `bedrock` de la flota: su nombre, el `SecretId` que lee en
 * tiempo de ejecución y la región de su upstream. */
export interface RenewableGateway {
  readonly name: string;
  readonly secretId: string;
  readonly region: string;
}

/** Las dos operaciones de Secrets Manager de la renovación. */
export interface SecretsWriter {
  describeSecret(input: { SecretId: string }): Promise<{ LastChangedDate?: Date | undefined }>;
  putSecretValue(input: {
    SecretId: string;
    SecretString: string;
    ClientRequestToken: string;
  }): Promise<unknown>;
}

/** La región de un upstream `https://bedrock-runtime.<región>.amazonaws.com`;
 * `undefined` para cualquier otro. La región tiene que ser una región de AWS
 * (`AWS_REGION_PATTERN`): un host como `bedrock-runtime.x.s3.amazonaws.com`
 * no es Bedrock y no se renueva. */
export function gatewayRegion(upstream: string): string | undefined {
  let host: string;
  try {
    host = new URL(upstream).hostname;
  } catch {
    return undefined;
  }
  if (!host.startsWith(BEDROCK_RUNTIME_HOST_PREFIX) || !host.endsWith(AMAZONAWS_HOST_SUFFIX)) {
    return undefined;
  }
  const region = host.slice(BEDROCK_RUNTIME_HOST_PREFIX.length, -AMAZONAWS_HOST_SUFFIX.length);
  return AWS_REGION_PATTERN.test(region) ? region : undefined;
}

/** Si hay que preguntar a AWS: este proceso no la renovó nunca, o hace al
 * menos `gatewayKeyRenewAfterSeconds`. */
export function shouldDescribe(processRenewedAgoMs: number | undefined): boolean {
  return (
    processRenewedAgoMs === undefined ||
    processRenewedAgoMs >= GATEWAY_KEY_RENEW_AFTER_SECONDS * MS_PER_SECOND
  );
}

/** Si hay que escribir una clave nueva: el secreto cambió hace al menos
 * `gatewayKeyRenewAfterSeconds`. */
export function shouldRenew(secretChangedAgoMs: number): boolean {
  return secretChangedAgoMs >= GATEWAY_KEY_RENEW_AFTER_SECONDS * MS_PER_SECOND;
}

export function bearerValue(token: string): string {
  return `${BEARER_PREFIX}${token}`;
}

/** Las pasarelas `bedrock` del proyecto (`gatewayPresets`). Sin ninguna,
 * `InvalidArgumentError`: la opción no tiene nada que renovar. Si el secreto
 * de una lo lee también una pasarela cuyo upstream no es Bedrock,
 * `InvalidArgumentError` (`RENEW_SHARED_SECRET`): la clave nueva se enviaría
 * a ese otro upstream. */
export function renewableGateways(project: AgentProject): readonly RenewableGateway[] {
  const found: RenewableGateway[] = [];
  for (const [name, preset] of Object.entries(project.gatewayPresets).sort(([a], [b]) =>
    a < b ? -1 : a > b ? 1 : 0,
  )) {
    const gateway = project.gateways[name];
    if (preset !== BEDROCK_PRESET || gateway === undefined) {
      continue;
    }
    const region = gatewayRegion(gateway.upstream);
    const secret = Object.values(gateway.headers)[0];
    if (region === undefined || secret === undefined) {
      continue;
    }
    found.push({ name, secretId: secretIdOf(secret), region });
  }
  if (found.length === 0) {
    throw new InvalidArgumentError(RENEW_NEEDS_BEDROCK);
  }
  refuseSharedSecrets(project, found);
  return found;
}

/** Falla si una pasarela con upstream que no es Bedrock lee el secreto de una
 * pasarela que se renovaría. El mensaje nombra las pasarelas, nunca el
 * secreto. */
function refuseSharedSecrets(project: AgentProject, renewable: readonly RenewableGateway[]): void {
  for (const [other, gateway] of Object.entries(project.gateways).sort(([a], [b]) =>
    a < b ? -1 : a > b ? 1 : 0,
  )) {
    if (gatewayRegion(gateway.upstream) !== undefined) {
      continue;
    }
    const secrets = new Set(Object.values(gateway.headers).map(secretIdOf));
    const shared = renewable.find((candidate) => secrets.has(candidate.secretId));
    if (shared !== undefined) {
      throw new InvalidArgumentError(
        RENEW_SHARED_SECRET.replace("{gateway}", shared.name).replace("{other}", other),
      );
    }
  }
}

function secretIdOf(secret: SecretLike): string {
  return resolveSecretId(typeof secret === "string" ? secret : secret.name, DEFAULT_SECRET_PREFIX);
}

/**
 * Renueva, como mucho una vez por ventana, la clave de corta duración de cada
 * pasarela `bedrock` de una flota.
 *
 * Coste y activación
 * -------------------
 * Activa: `renewGatewayKeys: true` en `AgentFleet`, `AgentFleet.connect` o
 *     `AgentDeployOptions` (`--renew-gateway-keys`). Apagada por defecto: sin
 *     ella no se carga el generador ni se llama a Secrets Manager.
 * Recursos y llamadas AWS: la firma de la clave es local (SigV4, sin
 *     llamada); `DescribeSecret` antes de crear un VM o abrir una plaza (salvo
 *     si este proceso ya renovó en la ventana) y `PutSecretValue` como mucho
 *     una vez cada `gatewayKeyRenewAfterSeconds` por pasarela.
 * Coste aproximado: $0,05 por 10 000 llamadas a Secrets Manager; la clave no
 *     cuesta (us-east-1, consultado 2026-10-22 en
 *     https://aws.amazon.com/secrets-manager/pricing/).
 * IAM: `secretsmanager:DescribeSecret` y `secretsmanager:PutSecretValue` sobre
 *     el secreto de la pasarela, y `bedrock:CallWithBearerToken` en el
 *     principal que firma.
 * Cómo apagarla: no pases `renewGatewayKeys` (por defecto `false`).
 * Ejemplo:
 *   const fleet = await AgentFleet.connect("my-agent", { accessToken, renewGatewayKeys: true });
 */
export class GatewayKeyRenewer {
  readonly #gateways: readonly RenewableGateway[];
  readonly #tokenProvider: TokenProvider;
  readonly #secrets: SecretsWriter;
  readonly #now: () => number;
  readonly #renewedAt = new Map<string, number>();

  constructor(
    gateways: readonly RenewableGateway[],
    options: {
      readonly tokenProvider: TokenProvider;
      readonly secrets: SecretsWriter;
      readonly now?: (() => number) | undefined;
    },
  ) {
    this.#gateways = gateways;
    this.#tokenProvider = options.tokenProvider;
    this.#secrets = options.secrets;
    this.#now = options.now ?? Date.now;
  }

  /** Renueva las pasarelas que lo necesitan; `true` si escribió alguna clave
   * (quien llama refresca las pasarelas de los VMs abiertos). Con `force` (el
   * despliegue que pidió la renovación) escribe una clave nueva en cada
   * pasarela sin mirar cuándo cambió el secreto: un secreto recién creado con
   * un valor marcador también la recibe. Sin `force` (las renovaciones de
   * cada VM nuevo) se respeta la ventana de `gatewayKeyRenewAfterSeconds`. */
  async renew(options: { readonly force?: boolean | undefined } = {}): Promise<boolean> {
    let renewed = false;
    for (const gateway of this.#gateways) {
      renewed = (await this.#renewOne(gateway, options.force === true)) || renewed;
    }
    return renewed;
  }

  async #renewOne(gateway: RenewableGateway, force: boolean): Promise<boolean> {
    if (force) {
      await this.#write(gateway);
      return true;
    }
    const last = this.#renewedAt.get(gateway.name);
    if (!shouldDescribe(last === undefined ? undefined : this.#now() - last)) {
      return false;
    }
    const described = await this.#call(gateway, "describeSecret", () =>
      this.#secrets.describeSecret({ SecretId: gateway.secretId }),
    );
    const changed = described.LastChangedDate;
    if (changed instanceof Date && !shouldRenew(this.#now() - changed.getTime())) {
      this.#renewedAt.set(gateway.name, this.#now());
      return false;
    }
    await this.#write(gateway);
    return true;
  }

  async #write(gateway: RenewableGateway): Promise<void> {
    const token = await this.#tokenProvider(gateway.region);
    await this.#call(gateway, "putSecretValue", () =>
      this.#secrets.putSecretValue({
        SecretId: gateway.secretId,
        SecretString: bearerValue(token),
        ClientRequestToken: randomUUID(),
      }),
    );
    this.#renewedAt.set(gateway.name, this.#now());
  }

  async #call<T>(
    gateway: RenewableGateway,
    operation: "describeSecret" | "putSecretValue",
    run: () => Promise<T>,
  ): Promise<T> {
    try {
      return await run();
    } catch (error) {
      throw renewalError(gateway, operation, error);
    }
  }
}

/** Por código: un permiso que falta es `permission_denied` y nombra la
 * pasarela, nunca el secreto. */
function renewalError(
  gateway: RenewableGateway,
  operation: "describeSecret" | "putSecretValue",
  error: unknown,
): Error {
  const code = awsCode(error);
  const action =
    operation === "putSecretValue"
      ? "secretsmanager:PutSecretValue"
      : "secretsmanager:DescribeSecret";
  const cause = sanitizeAwsError(error, { includeMessage: false });
  if (code?.startsWith(ACCESS_DENIED_PREFIX)) {
    return new SecretError(
      `sin permiso ${action} para renovar la clave de la pasarela '${gateway.name}'`,
      { awsCode: code, code: PERMISSION_DENIED_CODE, cause },
    );
  }
  return new SecretError(
    `no se pudo renovar la clave de la pasarela '${gateway.name}' (${action}: ${code ?? "error"})`,
    { awsCode: code, cause },
  );
}

interface TokenGeneratorModule {
  getToken(config: {
    readonly credentials: NonNullable<Credentials> | (() => Promise<unknown>);
    readonly region: string;
    readonly expiresInSeconds: number;
  }): Promise<string>;
}

interface SecretsManagerModule {
  readonly SecretsManagerClient: new (
    config: object,
  ) => {
    send(command: unknown): Promise<unknown>;
    readonly config: { readonly credentials: () => Promise<unknown> };
  };
  readonly DescribeSecretCommand: new (input: object) => unknown;
  readonly PutSecretValueCommand: new (input: object) => unknown;
}

/** El renovador de producción de `project`: valida que haya una pasarela
 * `bedrock` y carga el generador y el cliente de Secrets Manager (sin llamar
 * a AWS). Sin credenciales explícitas firma con las del cliente (la cadena
 * por defecto). */
export async function gatewayKeyRenewer(
  project: AgentProject,
  options: {
    readonly region?: string | undefined;
    readonly credentials?: Credentials | undefined;
  },
): Promise<GatewayKeyRenewer> {
  const gateways = renewableGateways(project);
  const generator = await loadOptionalPeer<TokenGeneratorModule>(
    TOKEN_GENERATOR_PEER,
    RENEWAL_FEATURE,
  );
  const sdk = await loadOptionalPeer<SecretsManagerModule>(SECRETS_MANAGER_PEER, RENEWAL_FEATURE);
  const region = options.region ?? process.env.AWS_REGION ?? process.env.AWS_DEFAULT_REGION;
  if (!region) {
    throw new InvalidArgumentError("falta la región: pasa `region` o define AWS_REGION");
  }
  const client = new sdk.SecretsManagerClient(
    clientConfig(
      region,
      options.credentials === undefined ? {} : { credentials: options.credentials },
    ),
  );
  const credentials = options.credentials ?? client.config.credentials;
  return new GatewayKeyRenewer(gateways, {
    tokenProvider: (gatewayRegionName) =>
      generator.getToken({
        credentials,
        region: gatewayRegionName,
        expiresInSeconds: GATEWAY_KEY_EXPIRY_SECONDS,
      }),
    secrets: {
      describeSecret: (input) =>
        client.send(new sdk.DescribeSecretCommand(input)) as Promise<{
          LastChangedDate?: Date | undefined;
        }>,
      putSecretValue: (input) => client.send(new sdk.PutSecretValueCommand(input)),
    },
  });
}
