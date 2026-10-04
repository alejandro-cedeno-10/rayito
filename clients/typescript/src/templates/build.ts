/**
 * Adaptador de AWS de `Template.build()` (m15-templates). Espejo de
 * `rayito._templates._build`: resuelve la versión de la imagen base ->
 * descarga su `codeArtifact` (`s3:GetObject`) -> compone el Dockerfile y el
 * zip (`dockerfile.ts`/`artifact.ts`) -> sube el artefacto por hash de
 * contenido -> `create`/`update-microvm-image` -> sondea el gate de tres
 * estados -> en caso de fallo, relee el grupo de logs y traduce a
 * `BuildError`. Cada nombre de parámetro AWS usado aquí aparece en
 * `AWS_API_NOTES.md` §27.
 */

import { createHash } from "node:crypto";
import type {
  CreateMicrovmImageCommandInput,
  LambdaMicrovmsClientConfig,
  UpdateMicrovmImageCommandInput,
} from "@aws-sdk/client-lambda-microvms";
import {
  CreateMicrovmImageCommand,
  GetMicrovmImageCommand,
  GetMicrovmImageVersionCommand,
  LambdaMicrovmsClient,
  paginateListMicrovmImageVersions,
  UpdateMicrovmImageCommand,
} from "@aws-sdk/client-lambda-microvms";
import type { S3Client } from "@aws-sdk/client-s3";
import { GetCallerIdentityCommand, STSClient } from "@aws-sdk/client-sts";
import { clientConfig } from "../aws/control-plane.js";
import { awsCode, loadOptionalSdkClient, type OptionalSdkClient } from "../aws/optional-client.js";
import { BuildError, InvalidArgumentError, NotFoundError, TemplateError } from "../errors.js";
import {
  accountImageArn,
  BUILD_QUOTA_ERROR_CODE,
  DEFAULT_BUILD_TIMEOUT_MS,
  DEFAULT_MEMORY_MIB,
  findReusableVersion,
  type ImageBuildClients,
  inheritedConfiguration,
  isActiveSuccessful,
  LOG_GROUP_PREFIX,
  MISSING_OBJECT_CODES,
  newestVersion,
  readGate,
  type SubmittedBuild,
  submitImageBuild,
  uploadIfAbsent,
  type VersionGate,
  waitForGate,
} from "../images/gateway.js";
import { SUPPORTED_MEMORY_MIB } from "../limits.js";
import { assembleArtifact } from "./artifact.js";
import { withBuildSlot } from "./concurrency.js";
import { copySteps } from "./context.js";
import { collectContextFiles } from "./context-node.js";
import type { Template } from "./dsl.js";
import { BASE_IMAGE_KIND, type TemplateSpec } from "./instructions.js";
import { classifyReadyFailure, parseBuildFailure } from "./logs.js";

/** Clave S3 del artefacto, direccionada por contenido (`rayito/templates/<sha256>.zip`). */
const S3_KEY_PREFIX = "rayito/templates";
/** Descripción de cada versión que crea `Template.build()`. */
const BUILD_DESCRIPTION = "rayito template build";
/** Líneas de log que se releen para explicar un fallo (`GetLogEvents` `limit`, §27). */
const BUILD_LOG_LINES = 500;
const CLOUDWATCH_LOGS_PEER = "@aws-sdk/client-cloudwatch-logs";

export interface BuildOptions {
  readonly bucket: string;
  readonly memoryMb?: number;
  readonly force?: boolean;
  readonly timeoutMs?: number;
  readonly baseImageVersion?: string;
  readonly onBuildLogs?: (line: string) => void;
  readonly region?: string;
  readonly credentials?: LambdaMicrovmsClientConfig["credentials"];
  readonly contextDir?: string;
}

export interface BuildInfo {
  readonly templateId: string;
  readonly buildId: string;
  readonly alias: string;
}

export interface BuildHandle {
  readonly arn: string;
  readonly version: string;
  readonly name: string;
  readonly region: string | undefined;
  readonly credentials: BuildOptions["credentials"];
}

export type BuildState = "IN_PROGRESS" | "SUCCESSFUL" | "FAILED";

export interface BuildStatus {
  readonly state: BuildState;
  readonly info?: BuildInfo | undefined;
  readonly errorMessage?: string | undefined;
}

/** Lo que el pipeline de `Template.build()` necesita (puerto, para poder
 * probar con un doble sin AWS real): el de `images/gateway.ts` más la
 * cuenta, la descarga del zip base y la lectura de logs. */
export interface BuildClients extends ImageBuildClients {
  readonly region: string;
  accountId(): Promise<string>;
  getObject(bucket: string, key: string): Promise<Uint8Array | undefined>;
  readBuildLogs(logGroup: string): Promise<string[]>;
}

/** Forma mínima de `@aws-sdk/client-cloudwatch-logs` que necesita
 * `readBuildLogs`; de mano, como `CloudFormationModule` en
 * `stacks/cloudformation.ts`, para no exigir el peer instalado en tiempo
 * de compilación (es opcional: `loadOptionalSdkClient` lo importa en
 * tiempo de ejecución, sólo si un build falla). */
interface CloudWatchLogsModule {
  readonly CloudWatchLogsClient: new (
    config: object,
  ) => { send(command: unknown): Promise<unknown> };
  readonly DescribeLogStreamsCommand: new (input: object) => unknown;
  readonly GetLogEventsCommand: new (input: object) => unknown;
}

// `@aws-sdk/client-s3` carga perezosa (como `sandbox/transfer.ts`
// `loadS3Modules`): importar `rayito` no debe traer el paquete de S3 sólo
// porque `templates/build.ts` existe (`tests/unit/transfer.test.ts`
// comprueba que ningún fichero lo importa estáticamente).
let s3Module: Promise<typeof import("@aws-sdk/client-s3")> | undefined;

function loadS3Module(): Promise<typeof import("@aws-sdk/client-s3")> {
  s3Module ??= import("@aws-sdk/client-s3").catch((error: unknown) => {
    s3Module = undefined;
    throw error;
  });
  return s3Module;
}

class AwsBuildClients implements BuildClients {
  readonly region: string;
  readonly #credentials: BuildOptions["credentials"];
  readonly #config: object;
  readonly #microvms: LambdaMicrovmsClient;
  readonly #sts: STSClient;
  #s3: Promise<S3Client> | undefined;
  #accountId: string | undefined;

  constructor(region: string, credentials: BuildOptions["credentials"]) {
    this.region = region;
    this.#credentials = credentials;
    this.#config = clientConfig(region, credentials === undefined ? {} : { credentials });
    this.#microvms = new LambdaMicrovmsClient(this.#config);
    this.#sts = new STSClient(this.#config);
  }

  async #getS3(): Promise<S3Client> {
    this.#s3 ??= loadS3Module().then((sdk) => new sdk.S3Client(this.#config));
    return this.#s3;
  }

  async accountId(): Promise<string> {
    if (this.#accountId === undefined) {
      const response = await this.#sts.send(new GetCallerIdentityCommand({}));
      this.#accountId = response.Account ?? "";
    }
    return this.#accountId;
  }

  async getMicrovmImage(arn: string): Promise<{ state: string } | undefined> {
    try {
      const response = await this.#microvms.send(
        new GetMicrovmImageCommand({ imageIdentifier: arn }),
      );
      return { state: response.state ?? "" };
    } catch (error) {
      if (awsCode(error) === "ResourceNotFoundException") {
        return undefined;
      }
      throw error;
    }
  }

  async getMicrovmImageVersion(
    arn: string,
    version: string,
  ): Promise<Record<string, unknown> | undefined> {
    try {
      const response = await this.#microvms.send(
        new GetMicrovmImageVersionCommand({ imageIdentifier: arn, imageVersion: version }),
      );
      return response as unknown as Record<string, unknown>;
    } catch (error) {
      if (awsCode(error) === "ResourceNotFoundException") {
        return undefined;
      }
      throw error;
    }
  }

  async listMicrovmImageVersions(arn: string): Promise<Array<Record<string, unknown>>> {
    const items: Array<Record<string, unknown>> = [];
    for await (const page of paginateListMicrovmImageVersions(
      { client: this.#microvms },
      { imageIdentifier: arn },
    )) {
      for (const item of page.items ?? []) {
        items.push(item as unknown as Record<string, unknown>);
      }
    }
    return items;
  }

  async getObject(bucket: string, key: string): Promise<Uint8Array | undefined> {
    const [sdk, s3] = await Promise.all([loadS3Module(), this.#getS3()]);
    try {
      const response = await s3.send(new sdk.GetObjectCommand({ Bucket: bucket, Key: key }));
      const body = await response.Body?.transformToByteArray();
      return body === undefined ? undefined : new Uint8Array(body);
    } catch (error) {
      if (awsCode(error) === "NoSuchKey") {
        return undefined;
      }
      throw error;
    }
  }

  async headObject(bucket: string, key: string): Promise<boolean> {
    const [sdk, s3] = await Promise.all([loadS3Module(), this.#getS3()]);
    try {
      await s3.send(new sdk.HeadObjectCommand({ Bucket: bucket, Key: key }));
      return true;
    } catch (error) {
      const code = awsCode(error);
      if (code !== undefined && MISSING_OBJECT_CODES.has(code)) {
        return false;
      }
      throw error;
    }
  }

  async putObject(bucket: string, key: string, body: Uint8Array): Promise<void> {
    const [sdk, s3] = await Promise.all([loadS3Module(), this.#getS3()]);
    await s3.send(new sdk.PutObjectCommand({ Bucket: bucket, Key: key, Body: body }));
  }

  async createMicrovmImage(
    name: string,
    request: Record<string, unknown>,
  ): Promise<{ imageArn: string; imageVersion: string }> {
    const response = await this.#microvms.send(
      new CreateMicrovmImageCommand({ name, ...request } as CreateMicrovmImageCommandInput),
    );
    return { imageArn: response.imageArn ?? "", imageVersion: response.imageVersion ?? "" };
  }

  async updateMicrovmImage(
    arn: string,
    request: Record<string, unknown>,
  ): Promise<{ imageArn: string; imageVersion: string }> {
    const response = await this.#microvms.send(
      new UpdateMicrovmImageCommand({
        imageIdentifier: arn,
        ...request,
      } as UpdateMicrovmImageCommandInput),
    );
    return { imageArn: response.imageArn ?? arn, imageVersion: response.imageVersion ?? "" };
  }

  async #loadLogsClient(): Promise<OptionalSdkClient<CloudWatchLogsModule> | undefined> {
    try {
      return await loadOptionalSdkClient<CloudWatchLogsModule>(
        CLOUDWATCH_LOGS_PEER,
        "Template.build (lectura de logs de un build fallido)",
        (sdk) => sdk.CloudWatchLogsClient,
        this.region,
        this.#credentials,
      );
    } catch {
      return undefined;
    }
  }

  async readBuildLogs(logGroup: string): Promise<string[]> {
    const client = await this.#loadLogsClient();
    if (client === undefined) {
      return [];
    }
    const sdk = client.sdk;
    let streamName: string | undefined;
    try {
      const streams = await client.send<{ logStreams?: Array<{ logStreamName?: string }> }>(
        new sdk.DescribeLogStreamsCommand({
          logGroupName: logGroup,
          orderBy: "LastEventTime",
          descending: true,
          limit: 1,
        }),
      );
      streamName = streams.logStreams?.[0]?.logStreamName;
    } catch {
      return [];
    }
    if (streamName === undefined) {
      return [];
    }
    const events = await client.send<{ events?: Array<{ message?: string }> }>(
      new sdk.GetLogEventsCommand({
        logGroupName: logGroup,
        logStreamName: streamName,
        limit: BUILD_LOG_LINES,
      }),
    );
    return (events.events ?? []).map((event) => event.message ?? "");
  }
}

/** `name` de `CreateMicrovmImageRequest`: 1-64 de `[a-zA-Z0-9-_]`
 * (`AWS_API_NOTES.md` §4). Un `"nombre:tag"` de E2B no cabe ahí. */
const TEMPLATE_NAME_PATTERN = /^[A-Za-z0-9_-]{1,64}$/;

/** `TemplateError` si `name` no es un nombre de imagen válido; un ARN
 * completo se acepta tal cual. Espejo de `_build.validate_template_name`. */
export function validateTemplateName(name: string): void {
  if (name.startsWith("arn:")) {
    return;
  }
  if (!TEMPLATE_NAME_PATTERN.test(name)) {
    throw new TemplateError(
      `nombre de template inválido ${JSON.stringify(name)}: 1-64 caracteres de [A-Za-z0-9_-] ` +
        "(sin ':tag'; Lambda MicroVMs versiona cada build por su cuenta)",
    );
  }
}

function imageArn(clients: BuildClients, name: string, accountId: string): string {
  return accountImageArn(clients.region, accountId, name);
}

function logGroupOf(name: string): string {
  return `${LOG_GROUP_PREFIX}/${name}`;
}

function validateMemory(memoryMb: number): void {
  if (!(SUPPORTED_MEMORY_MIB as readonly number[]).includes(memoryMb)) {
    throw new InvalidArgumentError(
      `memoryMb debe ser uno de ${SUPPORTED_MEMORY_MIB.join(", ")} (RES-1), se pidió ${memoryMb}`,
    );
  }
}

async function resolveBaseVersion(
  clients: BuildClients,
  baseName: string,
  version: string | undefined,
): Promise<Record<string, unknown>> {
  const baseArn = imageArn(clients, baseName, await clients.accountId());
  const resolved =
    version ??
    newestVersion((await clients.listMicrovmImageVersions(baseArn)).filter(isActiveSuccessful));
  const detail =
    resolved === undefined ? undefined : await clients.getMicrovmImageVersion(baseArn, resolved);
  if (detail === undefined) {
    throw new NotFoundError(
      `la imagen base ${JSON.stringify(baseName)} no tiene ninguna versión activa que componer`,
    );
  }
  return detail;
}

function parseS3Uri(uri: string): { bucket: string; key: string } {
  if (!uri.startsWith("s3://")) {
    throw new BuildError(
      "el codeArtifact de la imagen base no es una URI s3:// (Q84: Lambda MicroVMs sólo acepta " +
        "zips en S3)",
      { reason: "base_image_not_s3" },
    );
  }
  const [bucket = "", ...rest] = uri.slice("s3://".length).split("/");
  return { bucket, key: rest.join("/") };
}

async function fetchBaseArtifact(
  clients: BuildClients,
  baseVersion: Record<string, unknown>,
): Promise<Uint8Array> {
  const codeArtifact = baseVersion["codeArtifact"] as { uri?: string } | undefined;
  const uri = codeArtifact?.uri;
  if (!uri) {
    throw new BuildError("la versión de la imagen base no tiene codeArtifact.uri", {
      reason: "base_image_missing_artifact",
    });
  }
  const { bucket, key } = parseS3Uri(uri);
  const content = await clients.getObject(bucket, key);
  if (content === undefined) {
    throw new BuildError("no se pudo leer el codeArtifact de la imagen base", {
      reason: "base_image_missing_artifact",
    });
  }
  return content;
}

async function uploadArtifact(
  clients: BuildClients,
  bucket: string,
  payload: Uint8Array,
): Promise<string> {
  const key = `${S3_KEY_PREFIX}/${createHash("sha256").update(payload).digest("hex")}.zip`;
  await uploadIfAbsent(clients, bucket, key, payload);
  return `s3://${bucket}/${key}`;
}

/** El cuerpo de `create`/`update-microvm-image`: toda la configuración que
 * la versión base declaró (`inheritedConfiguration`, incluida
 * `additionalOsCapabilities` de la variante `caps`) más el artefacto, la
 * memoria y el grupo de logs propios del template. */
function desiredConfiguration(options: {
  artifactUri: string;
  memoryMb: number;
  baseImageVersion: Record<string, unknown>;
  logGroup: string;
}): Record<string, unknown> {
  return {
    ...inheritedConfiguration(options.baseImageVersion),
    codeArtifact: { uri: options.artifactUri },
    resources: [{ minimumMemoryInMiB: options.memoryMb }],
    logging: { cloudWatch: { logGroup: options.logGroup } },
  };
}

/** La cuota de 10 builds simultáneos (Q83) llega como `BuildError({reason: "build_quota"})`. */
async function submitBuild(
  clients: BuildClients,
  name: string,
  arn: string,
  desired: Record<string, unknown>,
): Promise<SubmittedBuild> {
  try {
    return await submitImageBuild(clients, name, arn, {
      ...desired,
      description: BUILD_DESCRIPTION,
    });
  } catch (error) {
    if (awsCode(error) === BUILD_QUOTA_ERROR_CODE) {
      throw new BuildError(
        `AWS rechazó el build del template ${JSON.stringify(name)}: cuota de builds ` +
          "simultáneos de la cuenta agotada (Q83); reintenta cuando termine alguno",
        { reason: "build_quota" },
      );
    }
    throw error;
  }
}

function composeSpec(template: Template, baseImageVersion: string | undefined): TemplateSpec {
  const spec = template.spec;
  if (spec.base === undefined || baseImageVersion === undefined) {
    return spec;
  }
  return { ...spec, base: { ...spec.base, version: baseImageVersion } };
}

async function compose(
  clients: BuildClients,
  spec: TemplateSpec,
  contextDir: string,
): Promise<{ artifact: Uint8Array; baseVersion: Record<string, unknown> }> {
  if (spec.base === undefined || spec.base.kind !== BASE_IMAGE_KIND) {
    throw new InvalidArgumentError(
      "Template.build: llama a fromBaseImage() antes de construir (0.6 sólo compone sobre una " +
        "imagen rayito-base ya publicada)",
    );
  }
  const baseVersion = await resolveBaseVersion(clients, spec.base.name, spec.base.version);
  const baseZip = await fetchBaseArtifact(clients, baseVersion);
  const contextFiles = await collectContextFiles(contextDir, copySteps(spec.steps));
  return { artifact: assembleArtifact(baseZip, spec, contextFiles), baseVersion };
}

function buildClientsFactory(
  region: string | undefined,
  credentials: BuildOptions["credentials"],
): BuildClients {
  const resolvedRegion = region ?? process.env["AWS_REGION"];
  if (!resolvedRegion) {
    throw new InvalidArgumentError("Template.build: sin región: pasa region o exporta AWS_REGION");
  }
  return new AwsBuildClients(resolvedRegion, credentials);
}

// Costura de pruebas: `tests/unit/m15-templates-build.test.ts` sustituye
// esto por un `BuildClients` falso, igual que el resto del SDK TS sustituye
// sus fábricas de cliente perezosas.
export let createBuildClients = buildClientsFactory;
export function _resetBuildClientsFactory(): void {
  createBuildClients = buildClientsFactory;
}
export function _setBuildClientsFactory(factory: typeof buildClientsFactory): void {
  createBuildClients = factory;
}

async function submitUnslotted(
  template: Template,
  name: string,
  options: BuildOptions,
): Promise<BuildHandle> {
  validateTemplateName(name);
  const memoryMb = options.memoryMb ?? DEFAULT_MEMORY_MIB;
  validateMemory(memoryMb);
  const spec = composeSpec(template, options.baseImageVersion);
  const clients = createBuildClients(options.region, options.credentials);
  const { artifact, baseVersion } = await compose(clients, spec, options.contextDir ?? ".");
  const artifactUri = await uploadArtifact(clients, options.bucket, artifact);
  const arn = imageArn(clients, name, await clients.accountId());
  const desired = desiredConfiguration({
    artifactUri,
    memoryMb,
    baseImageVersion: baseVersion,
    logGroup: logGroupOf(name),
  });
  const handle = { name, region: options.region, credentials: options.credentials };
  // `skipCache()` del DSL es `force: true` (E2B: "reconstruir aunque nada cambie").
  if (!(options.force ?? false) && !spec.skipCache) {
    const reusable = await findReusableVersion(clients, arn, desired);
    if (reusable !== undefined) {
      return { ...handle, arn, version: reusable };
    }
  }
  const submitted = await submitBuild(clients, name, arn, desired);
  return { ...handle, arn: submitted.arn, version: submitted.version };
}

function buildInfo(handle: BuildHandle): BuildInfo {
  return {
    templateId: handle.arn,
    buildId: `${handle.version}/${handle.name}`,
    alias: handle.name,
  };
}

/** `stateReason` sólo clasifica (`classifyReadyFailure`); nunca se copia al mensaje. */
function raiseForFailure(name: string, gate: VersionGate, logLines: string[]): never {
  const readyReason = classifyReadyFailure(gate.stateReason);
  if (readyReason !== undefined) {
    throw new BuildError(
      `el ready cmd del template ${JSON.stringify(name)} respondió con un error HTTP durante el build`,
      { reason: readyReason },
    );
  }
  const detail = parseBuildFailure(logLines);
  throw new BuildError(
    `el build del template ${JSON.stringify(name)} terminó en imagen=${gate.imageState} ` +
      `versión=${gate.versionState}`,
    {
      step: detail.step,
      command: detail.command,
      exitCode: detail.exitCode,
      logTail: detail.logTail,
    },
  );
}

async function awaitBuild(
  clients: BuildClients,
  handle: BuildHandle,
  timeoutMs: number,
  onBuildLogs: ((line: string) => void) | undefined,
): Promise<BuildInfo> {
  const { gate, timedOut } = await waitForGate(clients, handle.arn, handle.version, timeoutMs);
  const needsLogs = onBuildLogs !== undefined || (!timedOut && !gate.launchable);
  const logLines = needsLogs ? await clients.readBuildLogs(logGroupOf(handle.name)) : [];
  if (onBuildLogs !== undefined) {
    for (const line of logLines) {
      onBuildLogs(line);
    }
  }
  if (timedOut) {
    throw new BuildError(
      `el build del template ${JSON.stringify(handle.name)} no terminó en ` +
        `${Math.round(timeoutMs / 1000)} s; sigue en AWS y Template.getBuildStatus() puede consultarlo`,
      { reason: "build_timeout" },
    );
  }
  if (!gate.launchable) {
    raiseForFailure(handle.name, gate, logLines);
  }
  return buildInfo(handle);
}

/** Envía el build y espera el gate; el hueco de `withBuildSlot` se mantiene
 * durante toda la espera (limita builds en vuelo, no sólo envíos). */
export async function build(
  template: Template,
  name: string,
  options: BuildOptions,
): Promise<BuildInfo> {
  return withBuildSlot(async () => {
    const handle = await submitUnslotted(template, name, options);
    const clients = createBuildClients(options.region, options.credentials);
    return awaitBuild(
      clients,
      handle,
      options.timeoutMs ?? DEFAULT_BUILD_TIMEOUT_MS,
      options.onBuildLogs,
    );
  });
}

/** Como `build` pero vuelve tras el envío: el hueco local sólo cubre la
 * composición y el envío; un build en segundo plano sigue en AWS sin contar
 * contra `MAX_CONCURRENT_BUILDS` (la cuota real de AWS sí lo cuenta y llega
 * como `BuildError({reason: "build_quota"})`). */
export async function buildInBackground(
  template: Template,
  name: string,
  options: BuildOptions,
): Promise<BuildHandle> {
  return withBuildSlot(() => submitUnslotted(template, name, options));
}

export async function getBuildStatus(handle: BuildHandle): Promise<BuildStatus> {
  const clients = createBuildClients(handle.region, handle.credentials);
  const gate = await readGate(clients, handle.arn, handle.version);
  if (!gate.settled) {
    return { state: "IN_PROGRESS" };
  }
  if (!gate.launchable) {
    return {
      state: "FAILED",
      errorMessage: `imagen=${gate.imageState} versión=${gate.versionState} estado=${gate.versionStatus}`,
    };
  }
  return { state: "SUCCESSFUL", info: buildInfo(handle) };
}

export async function templateExists(
  name: string,
  options: { region?: string; credentials?: BuildOptions["credentials"] } = {},
): Promise<boolean> {
  validateTemplateName(name);
  const clients = createBuildClients(options.region, options.credentials);
  const arn = imageArn(clients, name, await clients.accountId());
  return (await clients.getMicrovmImage(arn)) !== undefined;
}

export {
  desiredConfiguration as _desiredConfigurationForTests,
  imageArn as _imageArnForTests,
  resolveBaseVersion as _resolveBaseVersionForTests,
};
