/**
 * `rayito setup` (dominio puro, ADR-030): el plan de tres pasos que prepara
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
import { BUILD_ROLE_OUTPUT_KEY, DEFAULT_STACK_NAME } from "../images/publish-domain.js";
import { SUPPORTED_REGIONS } from "../limits.js";

/** La pila de IAM de `infra/iam.yaml` con su nombre histórico. */
export const IAM_STACK_NAME = DEFAULT_STACK_NAME;
export const IAM_STACK_COMPONENT_NAME = "iam";
/** `Capability` del modelo `cloudformation` 2010-05-15 (`AWS_API_NOTES.md` §21). */
export const IAM_STACK_CAPABILITIES: readonly string[] = ["CAPABILITY_NAMED_IAM"];
export const ARTIFACT_BUCKET_PARAMETER = "ArtifactBucket";
export { BUILD_ROLE_OUTPUT_KEY };
export const BASE_IMAGE_NAME = DEFAULT_BASE_IMAGE_NAME;
export const BASE_IMAGE_VARIANT = "full";
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
/** Estados de pila que sirven tal cual (`UPDATE_ROLLBACK_COMPLETE` vuelve a la última plantilla buena). */
export const USABLE_STACK_STATES: ReadonlySet<string> = new Set([
  "CREATE_COMPLETE",
  "UPDATE_COMPLETE",
  "UPDATE_ROLLBACK_COMPLETE",
]);
export const IN_PROGRESS_STACK_SUFFIX = "_IN_PROGRESS";
/** Esperar a que una pila que se borra "esté desplegada" no terminaría nunca. */
export const DELETING_STACK_STATE = "DELETE_IN_PROGRESS";
/** Una línea de `sha256sum`: 64 hexadecimales en minúsculas, un espacio y el modo (espacio o `*`). */
const CHECKSUM_LINE_PATTERN = /^([0-9a-f]{64}) [ *](\S.*)$/;

export type ResourceKind = "bucket" | "iamStack" | "baseImage";
export type SetupAction = "create" | "update" | "reuse" | "wait" | "publish";
export type BucketSource = "argument" | "environment" | "iamStack" | "cdkBootstrap";
export type ImageSource = "release" | "artifact";
export type StackCondition = "missing" | "usable" | "in_progress" | "blocked";
export type ImageCondition = "missing" | "launchable" | "building" | "unlaunchable";
export type ErrorCode =
  | "unsupported_region"
  | "no_bucket"
  | "bucket_unavailable"
  | "bucket_region"
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
  no_bucket: EXIT_USAGE,
  bucket_unavailable: EXIT_USAGE,
  bucket_region: EXIT_USAGE,
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
  no_bucket:
    "no hay bucket para el artefacto de la imagen: pasa --bucket, exporta RAYITO_BUCKET o ejecuta `cdk bootstrap` en esta cuenta y región; Rayito no crea buckets",
  bucket_unavailable:
    "el bucket {bucket} ({source}) no existe o no es accesible con estas credenciales",
  bucket_region:
    "el bucket {bucket} está en {bucketRegion}, no en {region}: el build fallaría con S3_CROSS_REGION_ACCESS_DENIED",
  artifact_missing: "{artifact} no existe",
  artifact_variant:
    "{artifact} no es un zip de la variante full sin EFS: publica las otras variantes con `rayito image publish`",
  needs_confirmation:
    "preparar la cuenta crea recursos con coste: confírmalo con --yes (o desde una terminal)",
  stack_blocked:
    "la pila {name} está en {status}: bórrala (aws cloudformation delete-stack --stack-name {name}), espera a que desaparezca y vuelve a ejecutar `rayito setup`",
  stack_incomplete:
    "la pila {name} no tiene la salida BuildRoleArn: no es la plantilla infra/iam.yaml de Rayito",
  release_download: "no se pudo descargar {asset} de la release {release}: {reason}",
  checksum_missing: "SHA256SUMS de la release {release} no lista {asset}: no se publica",
  checksum_mismatch:
    "el sha256 de {asset} no coincide con SHA256SUMS de la release {release}: no se publica",
  image_build_failed:
    "la imagen {name} no quedó lanzable (versión {imageVersion}, {state}): vuelve a ejecutar `rayito setup`",
};

/** Las líneas de progreso de `apply`. */
export const PROGRESS_MESSAGES = {
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
};

/**
 * Las cifras de coste son las ya publicadas: `cost.md` (fila "Mantener
 * publicada una versión de imagen"), `configurar-aws.md` paso 3 (pila de IAM)
 * y paso 2 (zip en el bucket).
 */
export const COST_TEXTS = {
  iamStack: "pila de IAM: gratis",
  image: "imagen: ≈ $0,04 por semana por versión publicada (mínimo una semana)",
  artifact: "zip de la imagen en el bucket: céntimos",
} as const;

export const ACCOUNT_SETUP_HINT =
  "si la cuenta o la región aún no están preparadas, ejecuta `rayito setup`";
export const HINT_TEMPLATE = "{message} ({hint})";

export type TemplateFields = Readonly<Record<string, string | number>>;

/** Rellena los `{campo}` de `template`; un campo sin valor es un error de programación. */
export function formatTemplate(template: string, fields: TemplateFields): string {
  return template.replace(/\{(\w+)\}/g, (_match, key: string) => {
    const value = fields[key];
    if (value === undefined) {
      throw new Error(`falta el campo ${key} del mensaje`);
    }
    return String(value);
  });
}

/** Un fallo de `rayito setup`; `code` es una lista cerrada y `exitCode` el de la CLI. */
export class AccountSetupError extends Error {
  readonly code: ErrorCode;
  readonly exitCode: number;

  constructor(code: ErrorCode, fields: TemplateFields = {}) {
    super(formatTemplate(ERROR_MESSAGES[code], fields));
    this.name = "AccountSetupError";
    this.code = code;
    this.exitCode = ERROR_EXIT_CODES[code];
  }
}

export interface SetupRequest {
  readonly region: string;
  readonly bucket: string | undefined;
  readonly environmentBucket: string | undefined;
  readonly artifact: string | undefined;
  readonly sdkVersion: string;
}

export interface StackObservation {
  readonly name: string;
  /** `undefined` si la pila no existe. */
  readonly status: string | undefined;
  readonly outputs: Readonly<Record<string, string>>;
  readonly parameters: Readonly<Record<string, string>>;
}

export interface BucketCandidate {
  readonly name: string;
  readonly source: BucketSource;
}

export interface BucketProbe {
  readonly exists: boolean;
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
  /** Siempre tres pasos, en el orden `bucket`, `iamStack`, `baseImage`. */
  readonly steps: readonly SetupStep[];
  readonly cost: readonly string[];
  readonly artifact: string | undefined;
  readonly imageArn: string;
  readonly ready: boolean;
  readonly needsConfirmation: boolean;
}

export interface SetupResult {
  readonly bucket: string;
  readonly imageArn: string;
  readonly imageVersion: string;
  /** `undefined` si no hubo publicación. */
  readonly artifactUploaded: boolean | undefined;
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

/**
 * El único bucket que se sondea: el primero presente de `--bucket`,
 * `RAYITO_BUCKET`, el `ArtifactBucket` de la pila de IAM y el del bootstrap
 * de CDK.
 */
export function chooseBucketCandidate(
  request: SetupRequest,
  stack: StackObservation,
  account: string,
): BucketCandidate {
  if (request.bucket) {
    return { name: request.bucket, source: "argument" };
  }
  if (request.environmentBucket) {
    return { name: request.environmentBucket, source: "environment" };
  }
  const fromStack = stack.parameters[ARTIFACT_BUCKET_PARAMETER];
  if (fromStack) {
    return { name: fromStack, source: "iamStack" };
  }
  return { name: cdkBucketName(account, request.region), source: "cdkBootstrap" };
}

/** Un bucket ausente, ajeno o de otra región detiene el plan; nunca se crea uno. */
export function decideBucket(
  candidate: BucketCandidate,
  probe: BucketProbe,
  region: string,
): SetupStep {
  if (!probe.exists) {
    if (candidate.source === "cdkBootstrap") {
      throw new AccountSetupError("no_bucket");
    }
    throw new AccountSetupError("bucket_unavailable", {
      bucket: candidate.name,
      source: BUCKET_SOURCE_LABELS[candidate.source],
    });
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

export function classifyStack(stack: StackObservation): StackCondition {
  const status = stack.status;
  if (status === undefined) {
    return "missing";
  }
  if (USABLE_STACK_STATES.has(status)) {
    return "usable";
  }
  if (status !== DELETING_STACK_STATE && status.endsWith(IN_PROGRESS_STACK_SUFFIX)) {
    return "in_progress";
  }
  return "blocked";
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
  if (!stack.outputs[BUILD_ROLE_OUTPUT_KEY]) {
    throw new AccountSetupError("stack_incomplete", { name: IAM_STACK_NAME });
  }
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

/** Esperar un build en curso, reutilizar una versión lanzable o publicar. */
export function planImage(image: ImageObservation, request: SetupRequest): SetupStep {
  const base = { resource: "baseImage", name: BASE_IMAGE_NAME } as const;
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

/** Las líneas de coste de las acciones que cuestan, en orden: pila, imagen y zip. */
export function costLines(steps: readonly SetupStep[]): readonly string[] {
  const lines: string[] = [];
  const stack = steps.find((step) => step.resource === "iamStack");
  if (stack?.action === "create" || stack?.action === "update") {
    lines.push(COST_TEXTS.iamStack);
  }
  const image = steps.find((step) => step.resource === "baseImage");
  if (image?.action === "publish") {
    lines.push(COST_TEXTS.image, COST_TEXTS.artifact);
  }
  return lines;
}

const WRITE_ACTIONS: ReadonlySet<SetupAction> = new Set(["create", "update", "publish"]);

export function planSetup(input: {
  readonly request: SetupRequest;
  readonly account: string;
  readonly stack: StackObservation;
  readonly candidate: BucketCandidate;
  readonly probe: BucketProbe;
  readonly image: ImageObservation;
}): SetupPlan {
  const { request, stack, image } = input;
  const bucket = decideBucket(input.candidate, input.probe, request.region);
  const steps = [bucket, planStack(stack, bucket.name), planImage(image, request)];
  return {
    region: request.region,
    account: input.account,
    steps,
    cost: costLines(steps),
    artifact: request.artifact,
    imageArn: image.imageArn,
    ready: steps.every((step) => step.action === "reuse"),
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
