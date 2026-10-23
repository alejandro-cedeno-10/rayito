/**
 * Los valores por defecto de AWS de `deployAgent()` (ADR-029, D7). Espejo de
 * `rayito._agent._aws_resolve`: la región, el bucket del artefacto, la imagen
 * base, el secreto del modelo y el access token. Cada uno se resuelve en el
 * mismo orden (argumento explícito, fichero de proyecto, variable de entorno,
 * descubrimiento) y ninguno crea nada en silencio: el bucket nunca se crea
 * (`CreateBucket` no se llama), un secreto sólo se crea con
 * `createSecretFromEnv: true`, y una imagen base ausente se publica sólo si el
 * llamante pasó quien la publique.
 *
 * Este módulo reparte la decisión (puras: `resolveBucket`, `resolveToken`,
 * `secretNameFor`, `secretValueFromEnv`) y la E/S sobre AWS (`AwsResolver`:
 * `sts`, `s3` y la imagen base) y sobre `SecretStore`. Construir el resolvedor
 * no llama a AWS: los clientes son perezosos.
 */

import {
  ARTIFACT_STACK_NAME,
  artifactStackBucket,
  BUCKET_ENV_VAR,
  CDK_BOOTSTRAP_QUALIFIER,
  cdkBucketName,
  withBaseImageHint,
  withSetupHint,
} from "../account-setup/domain.js";
import { clientConfig } from "../aws/control-plane.js";
import { awsCode } from "../aws/optional-client.js";
import { sanitizeAwsError } from "../aws/sanitize.js";
import { InvalidArgumentError, StackError } from "../errors.js";
import { accountImageArn } from "../images/gateway.js";
import { generateAccessToken } from "../payload.js";
import { ACCESS_TOKEN_ENV_VAR } from "../sandbox/launch.js";
import { SecretGateway } from "../secret-gateway/domain.js";
import { DEFAULT_SECRET_PREFIX, SecretRef } from "../secrets/names.js";
import { CloudFormationProvisioner } from "../stacks/cloudformation.js";
import type { StackProvisioner } from "../stacks/port.js";
import { AwsBuildClients } from "../templates/build.js";
import type { AgentProject } from "./project.js";

/** El mensaje cuando ni el argumento ni el entorno dan la región de AWS. */
export const NO_REGION_MESSAGE =
  "no hay región de AWS: pasa region (--region) o exporta AWS_REGION";
/** El prefijo con el que la pasarela lee su secreto en tiempo de ejecución
 * (la `SecretCache` compartida de `secrets`/`gateways`): `--create-secret`
 * comprueba y crea con el mismo, para que el nombre sea exactamente el que se
 * leerá. */
export const GATEWAY_SECRET_PREFIX = DEFAULT_SECRET_PREFIX;
export const BEARER_PREFIX = "Bearer ";
export const AUTHORIZATION_HEADER = "authorization";
/** Códigos de `HeadBucket` que dan el bucket por inexistente o ajeno: sin
 * `s3:ListBucket` S3 contesta 403 a un HEAD, y un bucket que no se puede leer
 * tampoco sirve para el artefacto. */
export const BUCKET_MISSING_CODES: ReadonlySet<string> = new Set([
  "404",
  "NoSuchBucket",
  "NotFound",
  "403",
  "Forbidden",
]);
/** La variable de entorno estándar de cada pasarela de modelo
 * (`testdata/agent-deploy/secret-env.json`, la misma tabla en Python). */
export const GATEWAY_SECRET_ENV_VARS: Readonly<Record<string, string>> = Object.freeze({
  bedrock: "AWS_BEARER_TOKEN_BEDROCK",
  openai: "OPENAI_API_KEY",
  anthropic: "ANTHROPIC_API_KEY",
  gemini: "GEMINI_API_KEY",
  azure_openai: "AZURE_OPENAI_API_KEY",
  openrouter: "OPENROUTER_API_KEY",
  groq: "GROQ_API_KEY",
  mistral: "MISTRAL_API_KEY",
  deepseek: "DEEPSEEK_API_KEY",
  xai: "XAI_API_KEY",
  litellm: "LITELLM_API_KEY",
});

/** `BUCKET_ENV_VAR` (el bucket de artefactos, el mismo de `rayito image publish`) y las reglas del bucket del bootstrap de CDK viven en `account-setup/domain.ts`. */
export { BUCKET_ENV_VAR, CDK_BOOTSTRAP_QUALIFIER, cdkBucketName };

/** Códigos de acceso denegado de CloudFormation (`AccessDenied`) y de la forma `*Exception`. */
const ACCESS_DENIED_CODES: ReadonlySet<string> = new Set(["AccessDenied", "AccessDeniedException"]);

/** Un `StackError` de `describe` cuya causa es un acceso denegado de AWS. */
function isAccessDenied(error: unknown): boolean {
  if (!(error instanceof StackError)) {
    return false;
  }
  const cause = error.cause as { name?: unknown; code?: unknown } | undefined;
  return [cause?.name, cause?.code].some(
    (code) => typeof code === "string" && ACCESS_DENIED_CODES.has(code),
  );
}

export type BucketSource = "argument" | "project_file" | "environment" | "discovered";

export interface BucketChoice {
  readonly bucket: string;
  readonly source: BucketSource;
}

export type Environ = Readonly<Record<string, string | undefined>>;

/** Lo que `deployAgent()` pregunta a AWS antes de crear nada: la región
 * efectiva, la cuenta (`GetCallerIdentity`), si un bucket existe
 * (`HeadBucket`), el bucket de la pila `rayito-m0-artifacts` que creó
 * `rayito setup` (`DescribeStacks`) y si una imagen existe
 * (`GetMicrovmImage`). */
export interface DeployAws {
  resolvedRegion(): string;
  accountId(): Promise<string>;
  bucketExists(bucket: string): Promise<boolean>;
  artifactStackBucket(): Promise<string | undefined>;
  imageExists(name: string): Promise<boolean>;
}

/** Lo que `deployAgent()` usa de `SecretStore`. */
export interface SecretStorePort {
  exists(name: string): Promise<boolean>;
  create(name: string, value: string): Promise<unknown>;
}

/** Las pasarelas del proyecto con el nombre de secreto que se usará, y las
 * pasarelas cuyo secreto creó esta resolución (para avisar del coste; nunca el
 * nombre del secreto). */
export interface ResolvedSecrets {
  readonly gateways: Readonly<Record<string, SecretGateway>>;
  readonly created: readonly string[];
}

// ------------------------------------------------------------------- puras

/** El nombre convencional del secreto de la pasarela `gateway` del proyecto
 * `project`, bajo el prefijo `rayito/` con el que la pasarela lo lee y
 * `SecretStore` lo crea (el secreto queda en
 * `rayito/agents/<proyecto>/<pasarela>`). Es el mismo que escribe la detección
 * (`GATEWAY_SECRET_NAME_TEMPLATE` de `detect-project.ts`). */
export function secretNameFor(project: string, gateway: string): string {
  return `agents/${project}/${gateway}`;
}

/** El bucket del artefacto: argumento > `[deploy].bucket` del fichero de
 * proyecto > `RAYITO_BUCKET` > el bucket de activos del bootstrap de CDK de la
 * cuenta y la región (si existe) > el de la pila `rayito-m0-artifacts` que creó
 * `rayito setup` (si existe). Sin ninguno, `InvalidArgumentError` con las
 * opciones; nunca se crea un bucket. */
export async function resolveBucket(
  explicit: string | undefined,
  projectFile: string | undefined,
  environ: Environ,
  discover: () => Promise<string | undefined>,
): Promise<BucketChoice> {
  if (explicit) {
    return { bucket: explicit, source: "argument" };
  }
  if (projectFile) {
    return { bucket: projectFile, source: "project_file" };
  }
  const fromEnvironment = environ[BUCKET_ENV_VAR];
  if (fromEnvironment) {
    return { bucket: fromEnvironment, source: "environment" };
  }
  const discovered = await discover();
  if (discovered) {
    return { bucket: discovered, source: "discovered" };
  }
  throw new InvalidArgumentError(
    withSetupHint(
      "no hay bucket para el artefacto de la imagen: pasa bucket (--bucket), " +
        `[deploy].bucket en el fichero del proyecto o exporta ${BUCKET_ENV_VAR}; ` +
        "Rayito no crea buckets",
    ),
  );
}

/** El access token de la flota: argumento > `RAYITO_ACCESS_TOKEN` > uno nuevo
 * (que quien despliega debe guardar: `AgentFleet.accessToken`). */
export function resolveToken(explicit: string | undefined, environ: Environ): string {
  return explicit || environ[ACCESS_TOKEN_ENV_VAR] || generateAccessToken();
}

/** La variable de entorno estándar de la pasarela `gateway`: la de su
 * `preset` (`[gateways.<nombre>] preset`, p. ej. `bedrock` para una pasarela
 * llamada `model`) y, si el preset no tiene una (`custom`) o no se conoce, la
 * del nombre de la pasarela, como en 0.16. */
export function gatewayEnvVar(gateway: string, preset?: string): string | undefined {
  const byPreset = preset ? GATEWAY_SECRET_ENV_VARS[preset] : undefined;
  return byPreset ?? GATEWAY_SECRET_ENV_VARS[gateway];
}

/** El valor que se guardaría en el secreto de `gateway` a partir de su
 * variable de entorno estándar (`gatewayEnvVar`): `Bearer <clave>` para la
 * cabecera `authorization` (salvo que ya lo traiga) y la clave tal cual para
 * las demás. `undefined` si la variable no está o está vacía. */
export function secretValueFromEnv(
  gateway: string,
  header: string,
  environ: Environ,
  options: { readonly preset?: string | undefined } = {},
): string | undefined {
  const variable = gatewayEnvVar(gateway, options.preset);
  const value = variable === undefined ? "" : (environ[variable] ?? "");
  if (value === "") {
    return undefined;
  }
  if (header.toLowerCase() === AUTHORIZATION_HEADER && !value.startsWith(BEARER_PREFIX)) {
    return BEARER_PREFIX + value;
  }
  return value;
}

function secretNameOf(value: string | SecretRef): string {
  return typeof value === "string" ? value : value.name;
}

function renamed(value: string | SecretRef, name: string): string | SecretRef {
  if (typeof value === "string") {
    return name;
  }
  return new SecretRef(name, {
    versionId: value.versionId,
    versionStage: value.versionStage,
  });
}

function missingSecretMessage(gateway: string, preset: string | undefined): string {
  const variable = gatewayEnvVar(gateway, preset);
  const creation =
    variable === undefined
      ? "crea el secreto tú mismo con SecretStore"
      : `--create-secret (createSecretFromEnv: true) lo crea desde ${variable}`;
  return (
    `falta el secreto de la pasarela '${gateway}': pasa uno que ya exista con ` +
    `--secret ${gateway}=<nombre> (secretNames), o ${creation}`
  );
}

function missingValueMessage(gateway: string, preset: string | undefined): string {
  const variable = gatewayEnvVar(gateway, preset);
  return variable === undefined
    ? `no se puede crear el secreto de la pasarela '${gateway}' desde el entorno`
    : `no se puede crear el secreto de la pasarela '${gateway}': la variable ${variable} no está definida`;
}

function overrideTarget(gateway: string, definition: SecretGateway): string {
  const headers = Object.keys(definition.headers);
  const [header] = headers;
  if (headers.length !== 1 || header === undefined) {
    throw new InvalidArgumentError(
      `la pasarela '${gateway}' lleva varias cabeceras: --secret no puede elegir cuál`,
    );
  }
  return header;
}

function withHeaders(
  definition: SecretGateway,
  headers: Readonly<Record<string, string | SecretRef>>,
): SecretGateway {
  return new SecretGateway({
    upstream: definition.upstream,
    headers,
    allow: definition.allow.map((rule) => [rule.method, rule.path] as const),
    ratePerMinute: definition.ratePerMinute,
  });
}

/** Las pasarelas con el nombre de secreto de `secretNames` (pasarela →
 * nombre) en lugar del que traía el proyecto. Una pasarela que no existe es un
 * error, no se ignora. */
export function applySecretNames(
  gateways: Readonly<Record<string, SecretGateway>>,
  secretNames: Readonly<Record<string, string>>,
): Record<string, SecretGateway> {
  const unknown = Object.keys(secretNames)
    .filter((gateway) => !(gateway in gateways))
    .sort();
  if (unknown.length > 0) {
    throw new InvalidArgumentError(
      `secretNames nombra pasarelas que el proyecto no tiene: ${unknown.join(", ")}`,
    );
  }
  const result: Record<string, SecretGateway> = { ...gateways };
  for (const [gateway, name] of Object.entries(secretNames)) {
    const original = gateways[gateway] as SecretGateway;
    const header = overrideTarget(gateway, original);
    const current = original.headers[header] as string | SecretRef;
    result[gateway] = withHeaders(original, { [header]: renamed(current, name) });
  }
  return result;
}

// ------------------------------------------------------------- con E/S

/** El nombre con el prefijo `rayito/` duplicado que 0.16 escribía en la
 * pasarela detectada (`rayito/agents/<proyecto>/<proveedor>` relativo a
 * `rayito/`) si `name` es el nombre convencional de esa pasarela; `undefined`
 * para cualquier otro nombre, que nunca se reinterpreta. */
export function legacySecretName(
  project: string,
  gateway: string,
  name: string,
): string | undefined {
  return name === secretNameFor(project, gateway) ? GATEWAY_SECRET_PREFIX + name : undefined;
}

/** `name` si el secreto existe (`DescribeSecret`); si no, el nombre de 0.16
 * (`legacySecretName`) si ése existe, para que un despliegue de 0.16 hecho con
 * `--create-secret` siga usando su secreto sin crear otro. */
async function existingName(
  project: AgentProject,
  gateway: string,
  name: string,
  store: SecretStorePort,
): Promise<string | undefined> {
  if (await store.exists(name)) {
    return name;
  }
  const legacy = legacySecretName(project.name, gateway, name);
  return legacy !== undefined && (await store.exists(legacy)) ? legacy : undefined;
}

/** Comprueba que existe el secreto de cada pasarela del proyecto
 * (`DescribeSecret`) y, sólo con `createFromEnv`, crea el que falte con el
 * valor de la variable estándar del preset de la pasarela (o de su nombre:
 * `gatewayEnvVar`) (`CreateSecret`). `store` usa el mismo prefijo con el que
 * la pasarela lee el secreto (`rayito/`), así que el nombre que se comprueba y
 * se crea es exactamente el que se leerá. El valor sale de `environ` a Secrets
 * Manager y no se escribe en disco ni en logs; los errores nombran la
 * pasarela, nunca el secreto. */
export async function ensureModelSecrets(
  project: AgentProject,
  options: {
    readonly secretNames: Readonly<Record<string, string>>;
    readonly createFromEnv: boolean;
    readonly store: SecretStorePort;
    readonly environ: Environ;
  },
): Promise<ResolvedSecrets> {
  const gateways = applySecretNames(project.gateways, options.secretNames);
  const created: string[] = [];
  for (const [gateway, definition] of Object.entries(gateways)) {
    const preset = project.gatewayPresets[gateway];
    const headers: Record<string, string | SecretRef> = { ...definition.headers };
    let renamedAny = false;
    for (const [header, reference] of Object.entries(definition.headers)) {
      const name = secretNameOf(reference);
      const existing = await existingName(project, gateway, name, options.store);
      if (existing !== undefined) {
        if (existing !== name) {
          headers[header] = renamed(reference, existing);
          renamedAny = true;
        }
        continue;
      }
      if (!options.createFromEnv) {
        throw new InvalidArgumentError(missingSecretMessage(gateway, preset));
      }
      const value = secretValueFromEnv(gateway, header, options.environ, { preset });
      if (value === undefined) {
        throw new InvalidArgumentError(missingValueMessage(gateway, preset));
      }
      await options.store.create(name, value);
      created.push(gateway);
    }
    if (renamedAny) {
      gateways[gateway] = withHeaders(definition, headers);
    }
  }
  return { gateways: Object.freeze(gateways), created };
}

/** El adaptador de `DeployAws` sobre el SDK de AWS v3. Los clientes `sts`,
 * `s3` y `lambda-microvms` se crean en la primera llamada que los necesita. */
export class AwsResolver implements DeployAws {
  readonly #region: string | undefined;
  readonly #credentials: ConstructorParameters<typeof AwsBuildClients>[1];
  readonly #stacks: Pick<StackProvisioner, "describe"> | undefined;
  #build: AwsBuildClients | undefined;

  constructor(options: {
    readonly region?: string | undefined;
    readonly credentials?: ConstructorParameters<typeof AwsBuildClients>[1];
    /** Quien lee las pilas, para tests; por defecto CloudFormation. */
    readonly stacks?: Pick<StackProvisioner, "describe">;
  }) {
    this.#region = options.region;
    this.#credentials = options.credentials;
    this.#stacks = options.stacks;
  }

  resolvedRegion(): string {
    const resolved = this.#region ?? process.env.AWS_REGION ?? process.env.AWS_DEFAULT_REGION;
    if (!resolved) {
      throw new InvalidArgumentError(NO_REGION_MESSAGE);
    }
    return resolved;
  }

  #clients(): AwsBuildClients {
    this.#build ??= new AwsBuildClients(this.resolvedRegion(), this.#credentials);
    return this.#build;
  }

  accountId(): Promise<string> {
    return this.#clients().accountId();
  }

  async bucketExists(bucket: string): Promise<boolean> {
    const sdk = await import("@aws-sdk/client-s3");
    const client = new sdk.S3Client(
      clientConfig(
        this.resolvedRegion(),
        this.#credentials === undefined ? {} : { credentials: this.#credentials },
      ),
    );
    try {
      await client.send(
        new sdk.HeadBucketCommand({ Bucket: bucket, ExpectedBucketOwner: await this.accountId() }),
      );
    } catch (error) {
      const code = awsCode(error);
      if (code !== undefined && BUCKET_MISSING_CODES.has(code)) {
        return false;
      }
      throw sanitizeAwsError(error, { includeMessage: true });
    }
    return true;
  }

  /** `DescribeStacks` de `rayito-m0-artifacts`; sólo lectura, nunca crea la pila.
   * Sin `cloudformation:DescribeStacks` es como si no hubiera pila: quien no
   * tenía bucket sigue recibiendo el mismo error de siempre. */
  async artifactStackBucket(): Promise<string | undefined> {
    const stacks =
      this.#stacks ??
      new CloudFormationProvisioner({
        region: this.resolvedRegion(),
        credentials: this.#credentials,
      });
    try {
      const status = await stacks.describe(ARTIFACT_STACK_NAME);
      return artifactStackBucket({ status: status?.state, outputs: status?.outputs ?? {} });
    } catch (error) {
      if (isAccessDenied(error)) {
        return undefined;
      }
      throw error;
    }
  }

  async imageExists(name: string): Promise<boolean> {
    const arn = accountImageArn(this.resolvedRegion(), await this.accountId(), name);
    return (await this.#clients().getMicrovmImage(arn)) !== undefined;
  }
}

/** El bucket de activos del bootstrap de CDK de la cuenta y la región, si
 * existe (`GetCallerIdentity` y `HeadBucket`), y si no el de la pila
 * `rayito-m0-artifacts` de `rayito setup` (`DescribeStacks`); nunca se crea. */
export async function discoverBucket(aws: DeployAws, region: string): Promise<string | undefined> {
  const candidate = cdkBucketName(await aws.accountId(), region);
  if (await aws.bucketExists(candidate)) {
    return candidate;
  }
  return aws.artifactStackBucket();
}

/** Lo que se añade al error de una imagen base que falta cuando la imagen del
 * proyecto ya existe: reutilizarla no necesita la base. */
export function reuseTemplateHint(template: string): string {
  return `; o reutiliza la imagen '${template}' ya construida sin build: --no-build (buildImage: false)`;
}

/** Si la imagen base existe. Si no, la publica con `publisher` (que el
 * llamante dio y que costó una confirmación suya) y vuelve a comprobar; sin
 * `publisher` o con `ensure: false` es `InvalidArgumentError` con el comando
 * que la publica. Si `template` (la imagen del proyecto) ya existe, el mensaje
 * dice además cómo reutilizarla sin construir. Devuelve si hubo que
 * publicarla. */
export async function ensureBaseImage(
  aws: DeployAws,
  base: string,
  publisher: (() => Promise<void>) | undefined,
  options: { readonly ensure: boolean; readonly template?: string | undefined },
): Promise<boolean> {
  if (await aws.imageExists(base)) {
    return false;
  }
  if (!options.ensure || publisher === undefined) {
    const reuse =
      options.template !== undefined && (await aws.imageExists(options.template))
        ? reuseTemplateHint(options.template)
        : "";
    throw new InvalidArgumentError(
      withBaseImageHint(
        `la imagen base '${base}' no existe en esta cuenta y región y el build ` +
          "la necesita: publícala con " +
          "`rayito image publish --os-capabilities ALL --artifact <zip> " +
          "--base-image-version latest` (ver la guía de instalación), pasa " +
          `basePublisher a deployAgent() o elige otra con [image] base${reuse}`,
        base,
      ),
    );
  }
  await publisher();
  if (!(await aws.imageExists(base))) {
    throw new InvalidArgumentError(`la imagen base '${base}' sigue sin existir tras publicarla`);
  }
  return true;
}
