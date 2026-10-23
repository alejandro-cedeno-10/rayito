/**
 * `rayito setup` (dominio puro, ADR-030): el plan de cuatro pasos que prepara
 * una cuenta y una región, las reglas que lo deciden, la lectura de
 * `SHA256SUMS` y el error cerrado `AccountSetupError`. Espejo de
 * `rayito._account_setup._domain`; `testdata/account-setup/` fija los mismos
 * valores y textos en los dos lenguajes. Nada aquí llama a AWS, a la red ni
 * al disco: los puertos de `ports.ts` lo hacen por él.
 */

import {
  ACTIVE_VERSION_STATUS,
  BUILDING_VERSION_STATES,
  DEFAULT_BASE_IMAGE_NAME,
  LAUNCHABLE_IMAGE_STATES,
  SUCCESSFUL_VERSION_STATE,
} from "../images/gateway.js";
import {
  ALL_OS_CAPABILITIES,
  BUILD_ROLE_OUTPUT_KEY,
  DEFAULT_STACK_NAME,
} from "../images/publish-domain.js";
import { SUPPORTED_REGIONS } from "../limits.js";
import {
  classifyStack,
  DELETING_STACK_STATE,
  IN_PROGRESS_STACK_SUFFIX,
  type StackCondition,
  type StackObservation,
  USABLE_STACK_STATES,
} from "../stacks/condition.js";
import {
  formatTemplate,
  type MessageFields,
  messageFields,
  type TemplateFields,
} from "../template-text.js";

/** La pila de IAM de `infra/iam.yaml` con su nombre histórico. */
export const IAM_STACK_NAME = DEFAULT_STACK_NAME;
export const IAM_STACK_COMPONENT_NAME = "iam";
/** `Capability` del modelo `cloudformation` 2010-05-15 (`AWS_API_NOTES.md` §21). */
export const IAM_STACK_CAPABILITIES: readonly string[] = ["CAPABILITY_NAMED_IAM"];
export const ARTIFACT_BUCKET_PARAMETER = "ArtifactBucket";
export { BUILD_ROLE_OUTPUT_KEY };
/** La plantilla de la pila de IAM, para el mensaje de `stack_incomplete`. */
export const IAM_TEMPLATE_PATH = "infra/iam.yaml";
/**
 * La pila que crea el bucket de artefactos cuando no hay ninguno que
 * reutilizar (`infra/artifact-bucket.yaml`, diseño D4 y D10).
 */
export const ARTIFACT_STACK_NAME = "rayito-m0-artifacts";
export const ARTIFACT_COMPONENT_NAME = "artifact-bucket";
export const ARTIFACT_TEMPLATE_PATH = "infra/artifact-bucket.yaml";
/** El parámetro y la salida de la plantilla con el nombre del bucket. */
export const BUCKET_NAME_PARAMETER = "BucketName";
export const BUCKET_NAME_OUTPUT_KEY = "BucketName";
/** El bucket por defecto: determinista, único por cuenta y región (`contract.json`). */
export const DEFAULT_ARTIFACT_BUCKET_TEMPLATE = "rayito-artifacts-{account}-{region}";
/** Días que tarda en caducar un zip de la imagen; el valor por defecto de la plantilla (`ArtifactExpirationDays`). */
export const ARTIFACT_EXPIRATION_DAYS = 7;
/** Los orígenes de un nombre que el usuario pidió expresamente. */
export const EXPLICIT_BUCKET_SOURCES: ReadonlySet<BucketSource> = new Set([
  "argument",
  "environment",
]);
export const BASE_IMAGE_NAME = DEFAULT_BASE_IMAGE_NAME;
export const BASE_IMAGE_VARIANT = "full";
/** La base que el agente en MicroVM usa por defecto (igual a
 * `DEFAULT_AGENT_TEMPLATE_BASE`, que un test compara): la misma imagen `full`
 * publicada con `additionalOsCapabilities` (`AWS_API_NOTES.md` §4, el camino
 * de `rayito image publish --os-capabilities ALL`). */
export const CAPS_IMAGE_NAME = "rayito-base-caps";
/** El único valor de `additionalOsCapabilities` (§4), el de `rayito image
 * publish --os-capabilities ALL`. */
export const CAPS_IMAGE_OS_CAPABILITIES = ALL_OS_CAPABILITIES;
/** Variable con el bucket de artefactos, la misma de `rayito image publish`. */
export const BUCKET_ENV_VAR = "RAYITO_BUCKET";
/** Calificador por defecto del bootstrap de CDK (docs.aws.amazon.com/cdk/v2/guide/bootstrapping-env.html, consultado 2026-10-16). */
export const CDK_BOOTSTRAP_QUALIFIER = "hnb659fds";
export const RELEASE_TAG_PREFIX = "rayd-v";
export const RELEASE_DOWNLOAD_URL_TEMPLATE =
  "https://github.com/alejandro-cedeno-10/rayito/releases/download/{tag}/{asset}";
export const IMAGE_ASSET_NAME = "rayito-image.zip";
export const CHECKSUMS_ASSET_NAME = "SHA256SUMS";
/** El plazo de cada descarga de la release, en milisegundos (600 s). */
export const RELEASE_DOWNLOAD_TIMEOUT_MS = 600_000;
/** Una línea de `sha256sum`: 64 hexadecimales en minúsculas, un espacio y el modo (espacio o `*`). */
const CHECKSUM_LINE_PATTERN = /^([0-9a-f]{64}) [ *](\S.*)$/;

export type ResourceKind = "bucket" | "iamStack" | "baseImage" | "capsImage";
export type SetupAction = "create" | "update" | "reuse" | "wait" | "publish" | "skip";
/** Los cuatro pasos del plan, siempre en este orden. */
export const RESOURCE_ORDER: readonly ResourceKind[] = [
  "bucket",
  "iamStack",
  "baseImage",
  "capsImage",
];
/** Las acciones que admite cada recurso (diseño D2). */
export const ALLOWED_ACTIONS: Readonly<Record<ResourceKind, readonly SetupAction[]>> = {
  bucket: ["create", "reuse", "wait"],
  iamStack: ["create", "update", "reuse", "wait"],
  baseImage: ["publish", "reuse", "wait"],
  capsImage: ["publish", "reuse", "wait", "skip"],
};
export type BucketSource =
  | "argument"
  | "environment"
  | "iamStack"
  | "cdkBootstrap"
  | "artifactStack"
  | "rayitoDefault";
/** El resultado de un `HeadBucket`: 200, 404 (`NoSuchBucket`, `NotFound`) o 403 (existe pero no es accesible). */
export type BucketState = "found" | "missing" | "forbidden";
export type ImageSource = "release" | "artifact";
export type ImageCondition = "missing" | "launchable" | "building" | "unlaunchable";
export type ErrorCode =
  | "unsupported_region"
  | "bucket_unavailable"
  | "bucket_region"
  | "bucket_taken"
  | "artifact_stack_conflict"
  | "artifact_missing"
  | "artifact_variant"
  | "needs_confirmation"
  | "stack_blocked"
  | "stack_incomplete"
  | "release_download"
  | "checksum_missing"
  | "checksum_mismatch"
  | "image_build_failed";

/** Los códigos de salida del contrato (`exitCodes`): 1 la operación falló, 2 uso o entorno. */
const EXIT_FAILURE = 1;
const EXIT_USAGE = 2;

/** Código de salida de cada error (`contract.json`, `errorExitCodes`). */
export const ERROR_EXIT_CODES: Readonly<Record<ErrorCode, number>> = {
  unsupported_region: EXIT_USAGE,
  bucket_unavailable: EXIT_USAGE,
  bucket_region: EXIT_USAGE,
  bucket_taken: EXIT_USAGE,
  artifact_stack_conflict: EXIT_USAGE,
  artifact_missing: EXIT_USAGE,
  artifact_variant: EXIT_USAGE,
  needs_confirmation: EXIT_USAGE,
  stack_blocked: EXIT_FAILURE,
  stack_incomplete: EXIT_FAILURE,
  release_download: EXIT_FAILURE,
  checksum_missing: EXIT_FAILURE,
  checksum_mismatch: EXIT_FAILURE,
  image_build_failed: EXIT_FAILURE,
};

/** Los textos de cada error; nunca llevan URL, enlace prefirmado, token ni el mensaje crudo de AWS. */
export const ERROR_MESSAGES: Readonly<Record<ErrorCode, string>> = {
  unsupported_region: "la región {region} no tiene Lambda MicroVMs: usa una de {regions}",
  bucket_unavailable:
    "el bucket {bucket} ({source}) existe pero no es accesible con estas credenciales: puede ser de otra cuenta; pasa otro nombre con --bucket",
  bucket_region:
    "el bucket {bucket} está en {bucketRegion}, no en {region}: el build fallaría con S3_CROSS_REGION_ACCESS_DENIED",
  bucket_taken:
    "el nombre {bucket} ya lo usa un bucket que no creó la pila {stack}: si es tuyo, pásalo con --bucket {bucket}; si no, elige otro nombre con --bucket",
  artifact_stack_conflict:
    "la pila {stack} ya gestiona el bucket {existing}: pasa --bucket {existing} o, para crear {bucket}, borra antes esa pila (el bucket {existing} se conserva)",
  artifact_missing: "{artifact} no existe",
  artifact_variant:
    "{artifact} no es un zip de la variante full sin EFS: publica las otras variantes con `rayito image publish`",
  needs_confirmation:
    "preparar la cuenta crea recursos con coste: confírmalo con --yes (o desde una terminal)",
  stack_blocked:
    "la pila {name} está en {status}: bórrala (aws cloudformation delete-stack --stack-name {name}), espera a que desaparezca y vuelve a ejecutar `rayito setup`",
  stack_incomplete:
    "la pila {name} no tiene la salida {output}: no es la plantilla {template} de Rayito",
  release_download: "no se pudo descargar {asset} de la release {release}: {reason}",
  checksum_missing: "SHA256SUMS de la release {release} no lista {asset}: no se publica",
  checksum_mismatch:
    "el sha256 de {asset} no coincide con SHA256SUMS de la release {release}: no se publica",
  image_build_failed:
    "la imagen {name} no quedó lanzable (versión {imageVersion}, {state}): vuelve a ejecutar `rayito setup`",
};

/** Las líneas de progreso de `apply`. */
export const PROGRESS_MESSAGES = {
  bucketCreate: "creando el bucket {bucket} con la pila {name}",
  bucketWait: "esperando a la pila {name} del bucket ({status})",
  bucketReady: "bucket {bucket} listo (pila {name})",
  stackCreate: "creando la pila de IAM {name}",
  stackUpdate: "actualizando la pila de IAM {name}",
  stackWait: "esperando a la pila de IAM {name} ({status})",
  stackReady: "pila de IAM {name} lista",
  download: "descargando {asset} de {release}",
  verified: "sha256 de {asset} verificado contra SHA256SUMS: {sha256}",
  imageWait: "esperando el build en curso de {name} (versión {imageVersion})",
  imageReady: "imagen {name} {imageVersion} lanzable",
} as const;

export const DOWNLOAD_REASONS = {
  http: "HTTP {status}",
  timeout: "tiempo agotado",
  network: "error de red",
} as const;

/** Cómo se nombra cada origen del bucket en los mensajes. */
export const BUCKET_SOURCE_LABELS: Readonly<Record<BucketSource, string>> = {
  argument: "--bucket",
  environment: "RAYITO_BUCKET",
  iamStack: "el de la pila de IAM",
  cdkBootstrap: "bootstrap de CDK",
  artifactStack: "el de la pila de artefactos",
  rayitoDefault: "nombre por defecto de Rayito",
};

/**
 * Las cifras de coste son las ya publicadas: `cost.md` (fila "Mantener
 * publicada una versión de imagen"), `configurar-aws.md` paso 3 (pila de IAM)
 * y paso 2 (zip en el bucket); la línea del bucket es la del contrato.
 */
export const COST_TEXTS = {
  bucket:
    "bucket de artefactos: los zips de la imagen caducan a los {days} días (regla de ciclo de vida)",
  iamStack: "pila de IAM: gratis",
  image: "imagen: ≈ $0,04 por semana por versión publicada (mínimo una semana)",
  artifact: "zip de la imagen en el bucket: céntimos",
  capsImage:
    "imagen con capacidades: ≈ $0,04 por semana por versión publicada ($0,08 por GB-mes, mínimo una semana); sólo con --caps-image",
} as const;

export const ACCOUNT_SETUP_HINT =
  "si la cuenta o la región aún no están preparadas, ejecuta `rayito setup`";
/** La pista cuando falta `rayito-base-caps`: `rayito setup` sólo la publica
 * con `--caps-image` (otra versión de imagen que cuesta, ADR-034 D9). */
export const CAPS_IMAGE_SETUP_HINT =
  "rayito setup no publica rayito-base-caps por defecto porque cuesta: ejecuta " +
  "`rayito setup --caps-image`";
export const HINT_TEMPLATE = "{message} ({hint})";

/** Los estados, la clasificación y la observación de una pila viven en `stacks/condition.ts`; `rayito setup` y `--target agentcore` comparten una sola definición. */
export {
  classifyStack,
  DELETING_STACK_STATE,
  formatTemplate,
  IN_PROGRESS_STACK_SUFFIX,
  type StackCondition,
  type StackObservation,
  type TemplateFields,
  USABLE_STACK_STATES,
};

/** Un fallo de `rayito setup`; `code` es una lista cerrada y `exitCode` el de la CLI. */
export class AccountSetupError extends Error {
  readonly code: ErrorCode;
  readonly exitCode: number;
  /** Los valores del mensaje (los reutiliza `MicrovmImageArtifact.downloadRelease`). */
  readonly fields: MessageFields;

  constructor(code: ErrorCode, fields: TemplateFields = {}) {
    super(formatTemplate(ERROR_MESSAGES[code], fields));
    this.name = "AccountSetupError";
    this.code = code;
    this.exitCode = ERROR_EXIT_CODES[code];
    this.fields = messageFields(fields);
  }
}

export interface SetupRequest {
  readonly region: string;
  readonly bucket: string | undefined;
  readonly environmentBucket: string | undefined;
  readonly artifact: string | undefined;
  readonly sdkVersion: string;
  /**
   * El paso `capsImage`: publicar `rayito-base-caps`, la base del agente en
   * MicroVM, desde el mismo zip que `rayito-base`. Por defecto `false` (sin
   * `--caps-image`): el paso es `skip` y no hace ninguna llamada, porque
   * publicar otra imagen cuesta.
   *
   * Coste y activación
   *
   * Activa: `capsImage: true` / `rayito setup --caps-image` (con `--dry-run`
   * sólo lee). Apagada por defecto: sin ella no se lee ni se publica
   * `rayito-base-caps`.
   *
   * Recursos y llamadas AWS: `GetMicrovmImage` y `ListMicrovmImageVersions` de
   * `rayito-base-caps` en el plan y, si falta, `CreateMicrovmImage` o
   * `UpdateMicrovmImage` con `additionalOsCapabilities: ["ALL"]` desde el zip
   * ya subido (§4).
   *
   * Coste aproximado: otra versión de imagen publicada, ≈ $0,04 por semana
   * ($0,08 por GB-mes de snapshot, mínimo una semana; us-east-1, consultado
   * 2026-10-22, https://aws.amazon.com/lambda/pricing/).
   *
   * IAM: las mismas de publicar `rayito-base` (`infra/iam.yaml`).
   *
   * Cómo apagarla: no pases `capsImage` (por defecto `false`) ni `--caps-image`.
   *
   * Ejemplo: `{ region: "us-east-1", ..., capsImage: true }`.
   */
  readonly capsImage?: boolean | undefined;
}

export interface BucketCandidate {
  readonly name: string;
  readonly source: BucketSource;
}

export interface BucketProbe {
  readonly state: BucketState;
  /** La región que declara el bucket; `undefined` si `HeadBucket` no la trajo. */
  readonly region: string | undefined;
}

export interface ImageVersionItem {
  readonly imageVersion: string;
  readonly state: string;
  readonly status: string;
  readonly createdAt: Date;
}

export interface ImageObservation {
  readonly name: string;
  readonly imageArn: string;
  /** `undefined` si la imagen no existe. */
  readonly state: string | undefined;
  readonly versions: readonly ImageVersionItem[];
}

export interface SetupStep {
  readonly resource: ResourceKind;
  readonly name: string;
  readonly action: SetupAction;
  readonly source: BucketSource | ImageSource | undefined;
  readonly status: string | undefined;
  readonly imageVersion: string | undefined;
  readonly release: string | undefined;
  readonly previousBucket: string | undefined;
}

export interface SetupPlan {
  readonly region: string;
  readonly account: string;
  /** Siempre cuatro pasos, en el orden `bucket`, `iamStack`, `baseImage`, `capsImage`. */
  readonly steps: readonly SetupStep[];
  readonly cost: readonly string[];
  readonly artifact: string | undefined;
  readonly imageArn: string;
  /** El ARN de `rayito-base-caps`; `undefined` sin `capsImage`. */
  readonly capsImageArn: string | undefined;
  readonly ready: boolean;
  readonly needsConfirmation: boolean;
}

export interface SetupResult {
  readonly bucket: string;
  readonly imageArn: string;
  readonly imageVersion: string;
  /** `undefined` si no hubo publicación. */
  readonly artifactUploaded: boolean | undefined;
  /** Los de `rayito-base-caps`; `undefined` sin `--caps-image`. */
  readonly capsImageArn: string | undefined;
  readonly capsImageVersion: string | undefined;
}

export interface DownloadedRelease {
  readonly checksumsText: string;
  readonly artifact: string;
  readonly artifactSha256: string;
}

export interface PublishedImage {
  readonly imageArn: string;
  readonly imageVersion: string;
  readonly artifactUploaded: boolean;
}

export interface ImageGateResult {
  readonly launchable: boolean;
  readonly versionState: string;
}

const NULLABLE_STEP_KEYS = [
  "source",
  "status",
  "imageVersion",
  "release",
  "previousBucket",
] as const;

/** Un paso con los campos opcionales ausentes (`undefined`). */
export function newStep(
  base: Pick<SetupStep, "resource" | "name" | "action">,
  extra: Partial<Omit<SetupStep, "resource" | "name" | "action">> = {},
): SetupStep {
  return {
    ...base,
    source: extra.source,
    status: extra.status,
    imageVersion: extra.imageVersion,
    release: extra.release,
    previousBucket: extra.previousBucket,
  };
}

/** El paso como lo escribe el documento JSON, con las claves en el orden del contrato. */
export function stepJson(step: SetupStep): Record<string, unknown> {
  const document: Record<string, unknown> = {
    resource: step.resource,
    name: step.name,
    action: step.action,
  };
  for (const key of NULLABLE_STEP_KEYS) {
    document[key] = step[key] ?? null;
  }
  return document;
}

/** El resultado como lo escribe el documento JSON. */
export function resultJson(result: SetupResult): Record<string, unknown> {
  return {
    bucket: result.bucket,
    imageArn: result.imageArn,
    imageVersion: result.imageVersion,
    artifactUploaded: result.artifactUploaded ?? null,
    capsImageArn: result.capsImageArn ?? null,
    capsImageVersion: result.capsImageVersion ?? null,
  };
}

/** El paso de `resource` del plan; siempre existe. */
export function stepOf(plan: SetupPlan, resource: ResourceKind): SetupStep {
  const found = plan.steps.find((step) => step.resource === resource);
  if (found === undefined) {
    throw new Error(`el plan no tiene el paso ${resource}`);
  }
  return found;
}

export function requireSupportedRegion(region: string): void {
  if (!(SUPPORTED_REGIONS as readonly string[]).includes(region)) {
    throw new AccountSetupError("unsupported_region", {
      region,
      regions: SUPPORTED_REGIONS.join(", "),
    });
  }
}

export function releaseTag(sdkVersion: string): string {
  return `${RELEASE_TAG_PREFIX}${sdkVersion}`;
}

export function releaseAssetUrl(tag: string, asset: string): string {
  return formatTemplate(RELEASE_DOWNLOAD_URL_TEMPLATE, { tag, asset });
}

/** El bucket de activos del bootstrap de CDK: `cdk-<calificador>-assets-<cuenta>-<región>`. */
export function cdkBucketName(account: string, region: string): string {
  return `cdk-${CDK_BOOTSTRAP_QUALIFIER}-assets-${account}-${region}`;
}

/** El bucket por defecto de Rayito: `rayito-artifacts-<cuenta>-<región>`. */
export function defaultArtifactBucketName(account: string, region: string): string {
  return formatTemplate(DEFAULT_ARTIFACT_BUCKET_TEMPLATE, { account, region });
}

function explicitCandidate(request: SetupRequest): BucketCandidate | undefined {
  if (request.bucket) {
    return { name: request.bucket, source: "argument" };
  }
  if (request.environmentBucket) {
    return { name: request.environmentBucket, source: "environment" };
  }
  return undefined;
}

/**
 * Los candidatos a reutilizar, en el orden en que se sondean: un nombre
 * explícito (`--bucket`, `RAYITO_BUCKET`) es el único; si no, el
 * `ArtifactBucket` de la pila de IAM, si la hay, y el del bootstrap de CDK.
 */
export function bucketCandidates(
  request: SetupRequest,
  stack: StackObservation,
  account: string,
): readonly BucketCandidate[] {
  const explicit = explicitCandidate(request);
  if (explicit !== undefined) {
    return [explicit];
  }
  const candidates: BucketCandidate[] = [];
  const fromStack = stack.parameters[ARTIFACT_BUCKET_PARAMETER];
  if (fromStack) {
    candidates.push({ name: fromStack, source: "iamStack" });
  }
  candidates.push({ name: cdkBucketName(account, request.region), source: "cdkBootstrap" });
  return candidates;
}

/**
 * La decisión de un candidato sondeado: reutilizarlo, detener el plan, o
 * `undefined` si hay que seguir con el siguiente candidato o con la fase de
 * creación (reglas de D2).
 */
export function decideBucket(
  candidate: BucketCandidate,
  probe: BucketProbe,
  region: string,
): SetupStep | undefined {
  if (probe.state === "forbidden") {
    if (candidate.source === "cdkBootstrap") {
      return undefined;
    }
    throw new AccountSetupError("bucket_unavailable", {
      bucket: candidate.name,
      source: BUCKET_SOURCE_LABELS[candidate.source],
    });
  }
  if (probe.state === "missing") {
    return undefined;
  }
  if (probe.region && probe.region !== region) {
    throw new AccountSetupError("bucket_region", {
      bucket: candidate.name,
      bucketRegion: probe.region,
      region,
    });
  }
  return newStep(
    { resource: "bucket", name: candidate.name, action: "reuse" },
    { source: candidate.source },
  );
}

/** El bucket que se creará si nada se reutiliza: el nombre explícito o el de por defecto. */
export function creationTarget(request: SetupRequest, account: string): BucketCandidate {
  return (
    explicitCandidate(request) ?? {
      name: defaultArtifactBucketName(account, request.region),
      source: "rayitoDefault",
    }
  );
}

/** La salida `key` de una pila; sin ella no es la plantilla `template` de Rayito (`stack_incomplete`). */
export function stackOutput(stack: StackObservation, key: string, template: string): string {
  const value = stack.outputs[key];
  if (!value) {
    throw new AccountSetupError("stack_incomplete", { name: stack.name, output: key, template });
  }
  return value;
}

/**
 * El bucket de `rayito-m0-artifacts` si la pila está desplegada y lo declara;
 * `undefined` si no hay pila, no está lista o no es la plantilla de Rayito. Lo
 * lee `deployAgent()` (sólo lectura) para encontrar el bucket que creó `setup`.
 */
export function artifactStackBucket(
  stack: Pick<StackObservation, "status" | "outputs">,
): string | undefined {
  const usable =
    classifyStack({ name: ARTIFACT_STACK_NAME, parameters: {}, ...stack }) === "usable";
  return usable ? stack.outputs[BUCKET_NAME_OUTPUT_KEY] || undefined : undefined;
}

/** Sólo la creación por defecto, sin la pila del bucket, sondea además el nombre por defecto. */
export function needsDefaultProbe(
  target: BucketCandidate,
  artifactStack: StackObservation,
): boolean {
  return target.source === "rayitoDefault" && classifyStack(artifactStack) === "missing";
}

/** Una pila que gestiona otro bucket que el nombre explícito pedido nunca se renombra. */
function requireSameBucket(target: BucketCandidate, existing: string): void {
  if (EXPLICIT_BUCKET_SOURCES.has(target.source) && target.name !== existing) {
    throw new AccountSetupError("artifact_stack_conflict", {
      stack: ARTIFACT_STACK_NAME,
      existing,
      bucket: target.name,
    });
  }
}

/**
 * La fase de creación (D2): reutilizar o esperar el bucket de la pila
 * `rayito-m0-artifacts`, o crearlo. `probe` es el `HeadBucket` del nombre por
 * defecto, sólo presente cuando `needsDefaultProbe`: un bucket con ese nombre
 * que no creó la pila nunca se adopta.
 */
export function planBucketCreation(
  target: BucketCandidate,
  artifactStack: StackObservation,
  probe: BucketProbe | undefined,
): SetupStep {
  const base = { resource: "bucket" } as const;
  const condition = classifyStack(artifactStack);
  if (condition === "blocked") {
    throw new AccountSetupError("stack_blocked", {
      name: ARTIFACT_STACK_NAME,
      status: artifactStack.status ?? "",
    });
  }
  if (condition === "usable") {
    const existing = stackOutput(artifactStack, BUCKET_NAME_OUTPUT_KEY, ARTIFACT_TEMPLATE_PATH);
    requireSameBucket(target, existing);
    return newStep(
      { ...base, name: existing, action: "reuse" },
      { source: "artifactStack", status: artifactStack.status },
    );
  }
  if (condition === "in_progress") {
    const existing = artifactStack.parameters[BUCKET_NAME_PARAMETER] ?? "";
    requireSameBucket(target, existing);
    return newStep(
      { ...base, name: existing, action: "wait" },
      { source: "artifactStack", status: artifactStack.status },
    );
  }
  if (probe !== undefined && probe.state !== "missing") {
    throw new AccountSetupError("bucket_taken", {
      bucket: target.name,
      stack: ARTIFACT_STACK_NAME,
    });
  }
  return newStep({ ...base, name: target.name, action: "create" }, { source: target.source });
}

/** Crear, actualizar sólo `ArtifactBucket`, reutilizar o esperar la pila de IAM. */
export function planStack(stack: StackObservation, bucket: string): SetupStep {
  const base = { resource: "iamStack", name: IAM_STACK_NAME } as const;
  const condition = classifyStack(stack);
  if (condition === "missing") {
    return newStep({ ...base, action: "create" });
  }
  if (condition === "blocked") {
    throw new AccountSetupError("stack_blocked", {
      name: IAM_STACK_NAME,
      status: stack.status ?? "",
    });
  }
  if (condition === "in_progress") {
    return newStep({ ...base, action: "wait" }, { status: stack.status });
  }
  stackOutput(stack, BUILD_ROLE_OUTPUT_KEY, IAM_TEMPLATE_PATH);
  const previous = stack.parameters[ARTIFACT_BUCKET_PARAMETER];
  if (previous === bucket) {
    return newStep({ ...base, action: "reuse" }, { status: stack.status });
  }
  return newStep({ ...base, action: "update" }, { status: stack.status, previousBucket: previous });
}

function createdAtMs(item: ImageVersionItem): number {
  return item.createdAt.getTime();
}

function newestOf(items: readonly ImageVersionItem[]): ImageVersionItem | undefined {
  return items.reduce<ImageVersionItem | undefined>(
    (best, item) => (best === undefined || createdAtMs(item) > createdAtMs(best) ? item : best),
    undefined,
  );
}

function buildingVersions(image: ImageObservation): ImageVersionItem[] {
  return image.versions.filter((item) => BUILDING_VERSION_STATES.has(item.state));
}

function launchableVersions(image: ImageObservation): ImageVersionItem[] {
  if (image.state === undefined || !LAUNCHABLE_IMAGE_STATES.has(image.state)) {
    return [];
  }
  return image.versions.filter(
    (item) => item.state === SUCCESSFUL_VERSION_STATE && item.status === ACTIVE_VERSION_STATUS,
  );
}

export function classifyImage(image: ImageObservation): ImageCondition {
  if (image.state === undefined) {
    return "missing";
  }
  if (buildingVersions(image).length > 0) {
    return "building";
  }
  return launchableVersions(image).length > 0 ? "launchable" : "unlaunchable";
}

/** Esperar un build en curso, reutilizar una versión lanzable o publicar; vale
 * para `rayito-base` y para `rayito-base-caps` (`target`). */
export function planImage(
  image: ImageObservation,
  request: SetupRequest,
  target: { readonly resource: ResourceKind; readonly name: string } = {
    resource: "baseImage",
    name: BASE_IMAGE_NAME,
  },
): SetupStep {
  const base = { resource: target.resource, name: target.name };
  const condition = classifyImage(image);
  if (condition === "building") {
    return newStep(
      { ...base, action: "wait" },
      { imageVersion: newestOf(buildingVersions(image))?.imageVersion },
    );
  }
  if (condition === "launchable") {
    return newStep(
      { ...base, action: "reuse" },
      { imageVersion: newestOf(launchableVersions(image))?.imageVersion },
    );
  }
  if (request.artifact) {
    return newStep({ ...base, action: "publish" }, { source: "artifact" });
  }
  return newStep(
    { ...base, action: "publish" },
    { source: "release", release: releaseTag(request.sdkVersion) },
  );
}

/** El paso `capsImage`: `skip` sin `capsImage` (y entonces no se observó nada);
 * si no, la misma decisión que la imagen base. */
export function planCapsImage(
  caps: ImageObservation | undefined,
  request: SetupRequest,
): SetupStep {
  if (request.capsImage !== true || caps === undefined) {
    return newStep({ resource: "capsImage", name: CAPS_IMAGE_NAME, action: "skip" });
  }
  return planImage(caps, request, { resource: "capsImage", name: CAPS_IMAGE_NAME });
}

/** Las líneas de coste de las acciones que cuestan, en orden: bucket, pila,
 * imagen y zip, e imagen con capacidades (y el zip si sólo se publica ésa). */
export function costLines(steps: readonly SetupStep[]): readonly string[] {
  const lines: string[] = [];
  if (steps.find((step) => step.resource === "bucket")?.action === "create") {
    lines.push(formatTemplate(COST_TEXTS.bucket, { days: ARTIFACT_EXPIRATION_DAYS }));
  }
  const stack = steps.find((step) => step.resource === "iamStack");
  if (stack?.action === "create" || stack?.action === "update") {
    lines.push(COST_TEXTS.iamStack);
  }
  const basePublishes = steps.find((step) => step.resource === "baseImage")?.action === "publish";
  if (basePublishes) {
    lines.push(COST_TEXTS.image, COST_TEXTS.artifact);
  }
  if (steps.find((step) => step.resource === "capsImage")?.action === "publish") {
    lines.push(COST_TEXTS.capsImage);
    if (!basePublishes) {
      lines.push(COST_TEXTS.artifact);
    }
  }
  return lines;
}

const WRITE_ACTIONS: ReadonlySet<SetupAction> = new Set(["create", "update", "publish"]);
/** Las acciones de una cuenta ya preparada: nada que hacer. */
const READY_ACTIONS: ReadonlySet<SetupAction> = new Set(["reuse", "skip"]);

export function planSetup(input: {
  readonly request: SetupRequest;
  readonly account: string;
  readonly stack: StackObservation;
  /** El paso `bucket` ya decidido (`reuse`, `wait` o `create`). */
  readonly bucketStep: SetupStep;
  readonly image: ImageObservation;
  /** `rayito-base-caps`, o `undefined` con `capsImage: false`. */
  readonly caps?: ImageObservation | undefined;
}): SetupPlan {
  const { request, stack, bucketStep, image, caps } = input;
  const steps = [
    bucketStep,
    planStack(stack, bucketStep.name),
    planImage(image, request),
    planCapsImage(caps, request),
  ];
  return {
    region: request.region,
    account: input.account,
    steps,
    cost: costLines(steps),
    artifact: request.artifact,
    imageArn: image.imageArn,
    capsImageArn: request.capsImage === true ? caps?.imageArn : undefined,
    ready: steps.every((step) => READY_ACTIONS.has(step.action)),
    needsConfirmation: steps.some((step) => WRITE_ACTIONS.has(step.action)),
  };
}

/** Las entradas de un `SHA256SUMS` (formato de `sha256sum`); las líneas mal formadas se ignoran. */
export function parseChecksums(text: string): Map<string, string> {
  const checksums = new Map<string, string>();
  for (const line of text.split("\n")) {
    const match = CHECKSUM_LINE_PATTERN.exec(line.replace(/\r$/, ""));
    if (match?.[1] !== undefined && match[2] !== undefined) {
      checksums.set(match[2], match[1]);
    }
  }
  return checksums;
}

export function checksumFor(text: string, asset: string): string | undefined {
  return parseChecksums(text).get(asset);
}

/**
 * Compara el sha256 del zip descargado con su entrada de `SHA256SUMS`. Prueba
 * que el fichero no está truncado ni corrupto, no su autenticidad (la firma se
 * comprueba con `cosign verify-blob`, y `--artifact` publica el zip ya
 * verificado).
 */
export function verifyChecksum(release: DownloadedRelease, tag: string): void {
  const expected = checksumFor(release.checksumsText, IMAGE_ASSET_NAME);
  if (expected === undefined) {
    throw new AccountSetupError("checksum_missing", { release: tag, asset: IMAGE_ASSET_NAME });
  }
  if (expected !== release.artifactSha256) {
    throw new AccountSetupError("checksum_mismatch", { release: tag, asset: IMAGE_ASSET_NAME });
  }
}

/** Añade la pista de `rayito setup` al mensaje de una cuenta sin preparar. */
export function withSetupHint(message: string): string {
  return formatTemplate(HINT_TEMPLATE, { message, hint: ACCOUNT_SETUP_HINT });
}

/** `message` con la pista de la imagen base que falta: para `rayito-base-caps`,
 * `rayito setup --caps-image`; para cualquier otra, la de `rayito setup`. */
export function withBaseImageHint(message: string, base: string): string {
  const hint = base === CAPS_IMAGE_NAME ? CAPS_IMAGE_SETUP_HINT : ACCOUNT_SETUP_HINT;
  return formatTemplate(HINT_TEMPLATE, { message, hint });
}
