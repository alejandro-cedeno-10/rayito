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
import { BuildError, InvalidArgumentError, NotFoundError } from "../errors.js";
import { SUPPORTED_MEMORY_MIB } from "../limits.js";
import { assembleArtifact } from "./artifact.js";
import { withBuildSlot } from "./concurrency.js";
import { copySteps } from "./context.js";
import { collectContextFiles } from "./context-node.js";
import type { Template } from "./dsl.js";
import { BASE_IMAGE_KIND, type TemplateSpec } from "./instructions.js";
import { classifyReadyFailure, parseBuildFailure } from "./logs.js";

const S3_KEY_PREFIX = "rayito/templates";
const LOG_GROUP_PREFIX = "/rayito";
const POLL_INTERVAL_MS = 10_000;
const DEFAULT_BUILD_TIMEOUT_MS = 1_800_000;
const LAUNCHABLE_IMAGE_STATES = new Set(["CREATED", "UPDATED"]);
const SETTLED_VERSION_STATES = new Set(["SUCCESSFUL", "FAILED"]);
const MISSING_OBJECT_CODES = new Set(["404", "NoSuchKey", "NotFound", "403"]);
const CPU_ARCHITECTURE = "ARM_64";
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
 * probar con un doble sin AWS real). */
export interface BuildClients {
  readonly region: string;
  accountId(): Promise<string>;
  getMicrovmImage(arn: string): Promise<{ state: string } | undefined>;
  getMicrovmImageVersion(
    arn: string,
    version: string,
  ): Promise<Record<string, unknown> | undefined>;
  listActiveSuccessfulVersions(arn: string): Promise<Array<Record<string, unknown>>>;
  getObject(bucket: string, key: string): Promise<Uint8Array | undefined>;
  headObject(bucket: string, key: string): Promise<boolean>;
  putObject(bucket: string, key: string, body: Uint8Array): Promise<void>;
  createMicrovmImage(
    name: string,
    request: Record<string, unknown>,
  ): Promise<{ imageArn: string; imageVersion: string }>;
  updateMicrovmImage(
    arn: string,
    request: Record<string, unknown>,
  ): Promise<{ imageArn: string; imageVersion: string }>;
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

  async listActiveSuccessfulVersions(arn: string): Promise<Array<Record<string, unknown>>> {
    const items: Array<Record<string, unknown>> = [];
    for await (const page of paginateListMicrovmImageVersions(
      { client: this.#microvms },
      { imageIdentifier: arn },
    )) {
      for (const item of page.items ?? []) {
        if (item.state === "SUCCESSFUL" && item.status === "ACTIVE") {
          items.push(item as unknown as Record<string, unknown>);
        }
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
        limit: 500,
      }),
    );
    return (events.events ?? []).map((event) => event.message ?? "");
  }
}

function imageArn(clients: BuildClients, name: string, accountId: string): string {
  if (name.startsWith("arn:")) {
    return name;
  }
  return `arn:aws:lambda:${clients.region}:${accountId}:microvm-image:${name}`;
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
  baseArn: string,
  version: string | undefined,
): Promise<Record<string, unknown>> {
  if (version !== undefined) {
    const detail = await clients.getMicrovmImageVersion(baseArn, version);
    if (detail === undefined) {
      throw new NotFoundError(`la imagen base ${baseArn} no tiene la versión ${version}`);
    }
    return detail;
  }
  const candidates = await clients.listActiveSuccessfulVersions(baseArn);
  if (candidates.length === 0) {
    throw new NotFoundError(
      `la imagen base ${baseArn} no tiene ninguna versión activa que componer`,
    );
  }
  const latest = candidates.reduce((best, item) =>
    new Date(best["createdAt"] as string | number | Date).getTime() >
    new Date(item["createdAt"] as string | number | Date).getTime()
      ? best
      : item,
  );
  const detail = await clients.getMicrovmImageVersion(baseArn, String(latest["imageVersion"]));
  if (detail === undefined) {
    throw new NotFoundError(
      `la imagen base ${baseArn} no tiene ninguna versión activa que componer`,
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
  if (!(await clients.headObject(bucket, key))) {
    await clients.putObject(bucket, key, payload);
  }
  return `s3://${bucket}/${key}`;
}

function desiredConfiguration(options: {
  artifactUri: string;
  memoryMb: number;
  baseImageVersion: Record<string, unknown>;
  logGroup: string;
}): Record<string, unknown> {
  const base = options.baseImageVersion;
  return {
    baseImageArn: base["baseImageArn"],
    baseImageVersion: String(base["baseImageVersion"]),
    buildRoleArn: base["buildRoleArn"],
    codeArtifact: { uri: options.artifactUri },
    resources: [{ minimumMemoryInMiB: options.memoryMb }],
    cpuConfigurations: [{ architecture: CPU_ARCHITECTURE }],
    hooks: base["hooks"],
    logging: { cloudWatch: { logGroup: options.logGroup } },
  };
}

function configurationMatches(
  version: Record<string, unknown>,
  desired: Record<string, unknown>,
): boolean {
  return Object.entries(desired).every(([key, value]) => deepEqual(version[key], value));
}

function deepEqual(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

async function reusableVersion(
  clients: BuildClients,
  arn: string,
  desired: Record<string, unknown>,
): Promise<string | undefined> {
  const candidates = (await clients.listActiveSuccessfulVersions(arn)).filter((item) =>
    configurationMatches(item, desired),
  );
  if (candidates.length === 0) {
    return undefined;
  }
  const latest = candidates.reduce((best, item) =>
    new Date(best["createdAt"] as string | number | Date).getTime() >
    new Date(item["createdAt"] as string | number | Date).getTime()
      ? best
      : item,
  );
  return String(latest["imageVersion"]);
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
  const accountId = await clients.accountId();
  const baseArn = imageArn(clients, spec.base.name, accountId);
  const baseVersion = await resolveBaseVersion(clients, baseArn, spec.base.version);
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

async function submit(
  template: Template,
  name: string,
  options: BuildOptions,
): Promise<BuildHandle> {
  const memoryMb = options.memoryMb ?? 2048;
  validateMemory(memoryMb);
  const spec = composeSpec(template, options.baseImageVersion);
  return withBuildSlot(async () => {
    const clients = createBuildClients(options.region, options.credentials);
    const { artifact, baseVersion } = await compose(clients, spec, options.contextDir ?? ".");
    const artifactUri = await uploadArtifact(clients, options.bucket, artifact);
    const accountId = await clients.accountId();
    const arn = imageArn(clients, name, accountId);
    const logGroup = `${LOG_GROUP_PREFIX}/${name}`;
    const desired = desiredConfiguration({
      artifactUri,
      memoryMb,
      baseImageVersion: baseVersion,
      logGroup,
    });
    if (!(options.force ?? false)) {
      const reusable = await reusableVersion(clients, arn, desired);
      if (reusable !== undefined) {
        return {
          arn,
          version: reusable,
          name,
          region: options.region,
          credentials: options.credentials,
        };
      }
    }
    const existing = await clients.getMicrovmImage(arn);
    const response =
      existing === undefined
        ? await clients.createMicrovmImage(name, desired)
        : await clients.updateMicrovmImage(arn, desired);
    return {
      arn: response.imageArn,
      version: response.imageVersion,
      name,
      region: options.region,
      credentials: options.credentials,
    };
  });
}

function settled(imageState: string, versionState: string): boolean {
  return (
    SETTLED_VERSION_STATES.has(versionState) &&
    (LAUNCHABLE_IMAGE_STATES.has(imageState) || versionState === "FAILED")
  );
}

async function readGate(
  clients: BuildClients,
  arn: string,
  version: string,
): Promise<{ imageState: string; versionState: string; stateReason: string | undefined }> {
  const image = await clients.getMicrovmImage(arn);
  const detail = await clients.getMicrovmImageVersion(arn, version);
  return {
    imageState: image?.state ?? "",
    versionState: String(detail?.["state"] ?? ""),
    stateReason: detail?.["stateReason"] as string | undefined,
  };
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitForGate(
  clients: BuildClients,
  arn: string,
  version: string,
  timeoutMs: number,
): Promise<{ imageState: string; versionState: string; stateReason: string | undefined }> {
  const started = Date.now();
  for (;;) {
    const gate = await readGate(clients, arn, version);
    if (settled(gate.imageState, gate.versionState) || Date.now() - started >= timeoutMs) {
      return gate;
    }
    await sleep(POLL_INTERVAL_MS);
  }
}

function raiseForFailure(
  versionState: string,
  stateReason: string | undefined,
  logLines: string[],
): never {
  const readyReason = classifyReadyFailure(stateReason);
  if (readyReason !== undefined) {
    throw new BuildError(stateReason ?? "el ready cmd falló", { reason: readyReason });
  }
  const detail = parseBuildFailure(logLines);
  throw new BuildError(stateReason ?? `el build terminó en estado ${versionState}`, {
    step: detail.step,
    command: detail.command,
    exitCode: detail.exitCode,
    logTail: detail.logTail,
  });
}

async function awaitBuild(
  clients: BuildClients,
  handle: BuildHandle,
  timeoutMs: number,
  onBuildLogs: ((line: string) => void) | undefined,
): Promise<BuildInfo> {
  const gate = await waitForGate(clients, handle.arn, handle.version, timeoutMs);
  const logGroup = `${LOG_GROUP_PREFIX}/${handle.name}`;
  const logLines =
    onBuildLogs !== undefined || gate.versionState === "FAILED"
      ? await clients.readBuildLogs(logGroup)
      : [];
  if (onBuildLogs !== undefined) {
    for (const line of logLines) {
      onBuildLogs(line);
    }
  }
  if (gate.versionState !== "SUCCESSFUL" || !LAUNCHABLE_IMAGE_STATES.has(gate.imageState)) {
    raiseForFailure(gate.versionState, gate.stateReason, logLines);
  }
  return {
    templateId: handle.arn,
    buildId: `${handle.version}/${handle.name}`,
    alias: handle.name,
  };
}

export async function build(
  template: Template,
  name: string,
  options: BuildOptions,
): Promise<BuildInfo> {
  const handle = await submit(template, name, options);
  const clients = createBuildClients(options.region, options.credentials);
  return awaitBuild(
    clients,
    handle,
    options.timeoutMs ?? DEFAULT_BUILD_TIMEOUT_MS,
    options.onBuildLogs,
  );
}

export async function buildInBackground(
  template: Template,
  name: string,
  options: BuildOptions,
): Promise<BuildHandle> {
  return submit(template, name, options);
}

export async function getBuildStatus(handle: BuildHandle): Promise<BuildStatus> {
  const clients = createBuildClients(handle.region, handle.credentials);
  const gate = await readGate(clients, handle.arn, handle.version);
  if (!settled(gate.imageState, gate.versionState)) {
    return { state: "IN_PROGRESS" };
  }
  if (gate.versionState === "FAILED") {
    return { state: "FAILED", errorMessage: gate.stateReason };
  }
  return {
    state: "SUCCESSFUL",
    info: {
      templateId: handle.arn,
      buildId: `${handle.version}/${handle.name}`,
      alias: handle.name,
    },
  };
}

export async function templateExists(
  name: string,
  options: { region?: string; credentials?: BuildOptions["credentials"] } = {},
): Promise<boolean> {
  const clients = createBuildClients(options.region, options.credentials);
  const accountId = await clients.accountId();
  const arn = imageArn(clients, name, accountId);
  return (await clients.getMicrovmImage(arn)) !== undefined;
}

export { desiredConfiguration as _desiredConfigurationForTests, imageArn as _imageArnForTests };
