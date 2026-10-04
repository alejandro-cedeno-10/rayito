/**
 * Adaptador del SDK v3 de `StackProvisioner` (M15 foundations, ADR-016).
 * Espejo de `rayito._stacks._cloudformation`. Parámetros y formas
 * verificados contra `AWS_API_NOTES.md` §21: `CreateStack`, `UpdateStack`
 * (con `UsePreviousValue` para los parámetros que se conservan),
 * `DescribeStacks` (incluidos sus `Parameters`), `DeleteStack`. `putArtifact` nunca se fía de que
 * exista un objeto con la clave esperada: con `ExpectedBucketOwner` (la cuenta de
 * `sts:GetCallerIdentity`) hace `GetObject` y compara el sha256 del contenido; sólo si coincide no
 * lo vuelve a subir, y si falta o no coincide, `PutObject` con `ChecksumSHA256`.
 *
 * `@aws-sdk/client-cloudformation` y `@aws-sdk/client-s3` son peers
 * opcionales (`loadOptionalSdkClient`): construir un `CloudFormationProvisioner`
 * no los carga ni llama a AWS; sólo el primer `deploy`/`status`/`destroy` lo
 * hace.
 */

import { createHash } from "node:crypto";
import type { AwsClientSettings } from "../aws/control-plane.js";
import { awsCode, loadOptionalSdkClient } from "../aws/optional-client.js";
import { sanitizeAwsError } from "../aws/sanitize.js";
import { StackError } from "../errors.js";
import type { Logger } from "../logger.js";
import type { StackComponent, StackStatus } from "./model.js";
import type { DeployTarget, StackProvisioner, UpdateOutcome } from "./port.js";

const CLOUDFORMATION_PEER = "@aws-sdk/client-cloudformation";
const S3_PEER = "@aws-sdk/client-s3";
const STS_MODULE = "@aws-sdk/client-sts";
const VALIDATION_ERROR = "ValidationError";
const NOT_EXISTS_MARKER = "does not exist";
const NO_UPDATES_MARKER = "No updates are to be performed";
/**
 * `GetObject` sobre una clave que no existe (con `s3:ListBucket`; sin él, S3
 * responde `AccessDenied` y el despliegue falla, como antes).
 */
const MISSING_OBJECT_CODES = new Set(["NoSuchKey", "NotFound"]);
const DEFAULT_POLL_INTERVAL_MS = 500;

const TERMINAL_SUCCESS_STATES = new Set(["CREATE_COMPLETE", "UPDATE_COMPLETE"]);
const TERMINAL_DELETED_STATES = new Set(["DELETE_COMPLETE"]);
const TERMINAL_FAILURE_STATES = new Set([
  "CREATE_FAILED",
  "ROLLBACK_COMPLETE",
  "ROLLBACK_FAILED",
  "UPDATE_FAILED",
  "UPDATE_ROLLBACK_COMPLETE",
  "UPDATE_ROLLBACK_FAILED",
  "DELETE_FAILED",
]);

type Credentials = AwsClientSettings["credentials"];

type ProtoParameter =
  | { ParameterKey: string; ParameterValue: string }
  | { ParameterKey: string; UsePreviousValue: true };

interface CloudFormationApi {
  describeStacks(input: { StackName: string }): Promise<{
    Stacks?: Array<{
      StackStatus?: string;
      StackStatusReason?: string;
      Outputs?: Array<{ OutputKey?: string; OutputValue?: string }>;
      Parameters?: Array<{ ParameterKey?: string; ParameterValue?: string }>;
    }>;
  }>;
  createStack(input: {
    StackName: string;
    TemplateBody: string;
    Parameters: Array<{ ParameterKey: string; ParameterValue: string }>;
    Tags: Array<{ Key: string; Value: string }>;
    Capabilities?: string[] | undefined;
  }): Promise<unknown>;
  updateStack(input: {
    StackName: string;
    TemplateBody: string;
    Parameters: ProtoParameter[];
    Tags: Array<{ Key: string; Value: string }>;
    Capabilities?: string[] | undefined;
  }): Promise<unknown>;
  deleteStack(input: { StackName: string }): Promise<unknown>;
}

interface LocatedObject {
  Bucket: string;
  Key: string;
  ExpectedBucketOwner: string;
}

export interface S3Api {
  getObject(
    input: LocatedObject,
  ): Promise<{ Body?: { transformToByteArray(): Promise<Uint8Array> } | undefined }>;
  putObject(input: LocatedObject & { Body: Uint8Array; ChecksumSHA256: string }): Promise<unknown>;
}

export interface StsApi {
  getCallerIdentity(): Promise<{ Account?: string | undefined }>;
}

interface CloudFormationModule {
  readonly CloudFormationClient: new (
    config: object,
  ) => { send(command: unknown): Promise<unknown> };
  readonly CreateStackCommand: new (input: object) => unknown;
  readonly UpdateStackCommand: new (input: object) => unknown;
  readonly DescribeStacksCommand: new (input: object) => unknown;
  readonly DeleteStackCommand: new (input: object) => unknown;
}

interface S3Module {
  readonly S3Client: new (config: object) => { send(command: unknown): Promise<unknown> };
  readonly GetObjectCommand: new (input: object) => unknown;
  readonly PutObjectCommand: new (input: object) => unknown;
}

interface StsModule {
  readonly STSClient: new (config: object) => { send(command: unknown): Promise<unknown> };
  readonly GetCallerIdentityCommand: new (input: object) => unknown;
}

async function cloudformationApi(
  region: string,
  credentials: Credentials,
): Promise<CloudFormationApi> {
  const { sdk, send } = await loadOptionalSdkClient<CloudFormationModule>(
    CLOUDFORMATION_PEER,
    "OptionalStacks (rayito stack / OptionalStacks)",
    (module) => module.CloudFormationClient,
    region,
    credentials,
  );
  return {
    describeStacks: (input) => send(new sdk.DescribeStacksCommand(input)),
    createStack: (input) => send(new sdk.CreateStackCommand(input)),
    updateStack: (input) => send(new sdk.UpdateStackCommand(input)),
    deleteStack: (input) => send(new sdk.DeleteStackCommand(input)),
  };
}

async function s3Api(region: string, credentials: Credentials): Promise<S3Api> {
  const { sdk, send } = await loadOptionalSdkClient<S3Module>(
    S3_PEER,
    "OptionalStacks artifact upload (rayito stack / OptionalStacks)",
    (module) => module.S3Client,
    region,
    credentials,
  );
  return {
    getObject: (input) => send(new sdk.GetObjectCommand(input)),
    putObject: (input) => send(new sdk.PutObjectCommand(input)),
  };
}

async function stsApi(region: string, credentials: Credentials): Promise<StsApi> {
  const { sdk, send } = await loadOptionalSdkClient<StsModule>(
    STS_MODULE,
    "OptionalStacks artifact upload (rayito stack / OptionalStacks)",
    (module) => module.STSClient,
    region,
    credentials,
  );
  return { getCallerIdentity: () => send(new sdk.GetCallerIdentityCommand({})) };
}

function sha256(data: Uint8Array): Buffer {
  return createHash("sha256").update(data).digest();
}

function wrap(error: unknown): StackError {
  const summary = sanitizeAwsError(error, { includeMessage: true });
  return new StackError(`${summary.name}: ${summary.message}`, { code: "failed", cause: error });
}

function isMissingStack(error: unknown): boolean {
  return awsCode(error) === VALIDATION_ERROR && String(error).includes(NOT_EXISTS_MARKER);
}

function isNoUpdates(error: unknown): boolean {
  return awsCode(error) === VALIDATION_ERROR && String(error).includes(NO_UPDATES_MARKER);
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export interface CloudFormationProvisionerOptions {
  readonly region?: string | undefined;
  readonly credentials?: Credentials;
  readonly pollIntervalMs?: number;
  /** Clientes ya construidos, para tests. */
  readonly cloudformationClient?: CloudFormationApi;
  readonly s3Client?: S3Api;
  readonly stsClient?: StsApi;
  /** Recibe el aviso de un artefacto ya subido con otro contenido (en silencio por defecto). */
  readonly logger?: Logger | undefined;
}

export class CloudFormationProvisioner implements StackProvisioner {
  readonly #region: string | undefined;
  readonly #credentials: Credentials;
  readonly #pollIntervalMs: number;
  #cloudformation: CloudFormationApi | undefined;
  #s3: S3Api | undefined;
  #sts: StsApi | undefined;
  readonly #logger: Logger | undefined;

  constructor(options: CloudFormationProvisionerOptions = {}) {
    this.#region = options.region;
    this.#credentials = options.credentials;
    this.#pollIntervalMs = options.pollIntervalMs ?? DEFAULT_POLL_INTERVAL_MS;
    this.#cloudformation = options.cloudformationClient;
    this.#s3 = options.s3Client;
    this.#sts = options.stsClient;
    this.#logger = options.logger;
  }

  async #cfn(): Promise<CloudFormationApi> {
    if (this.#cloudformation === undefined) {
      this.#cloudformation = await cloudformationApi(this.#requireRegion(), this.#credentials);
    }
    return this.#cloudformation;
  }

  async #identity(): Promise<StsApi> {
    if (this.#sts === undefined) {
      this.#sts = await stsApi(this.#requireRegion(), this.#credentials);
    }
    return this.#sts;
  }

  #requireRegion(): string {
    const region = this.#region ?? process.env.AWS_REGION ?? process.env.AWS_DEFAULT_REGION;
    if (!region) {
      throw new StackError("falta la región de OptionalStacks: pasa `region` o define AWS_REGION", {
        code: "failed",
      });
    }
    return region;
  }

  async #bucket(): Promise<S3Api> {
    if (this.#s3 === undefined) {
      this.#s3 = await s3Api(this.#requireRegion(), this.#credentials);
    }
    return this.#s3;
  }

  async describe(stackName: string): Promise<StackStatus | undefined> {
    const api = await this.#cfn();
    let response: Awaited<ReturnType<CloudFormationApi["describeStacks"]>>;
    try {
      response = await api.describeStacks({ StackName: stackName });
    } catch (error) {
      if (isMissingStack(error)) {
        return undefined;
      }
      throw wrap(error);
    }
    const stack = response.Stacks?.[0];
    if (stack === undefined) {
      return undefined;
    }
    const outputs: Record<string, string> = {};
    for (const output of stack.Outputs ?? []) {
      if (output.OutputKey !== undefined && output.OutputValue !== undefined) {
        outputs[output.OutputKey] = output.OutputValue;
      }
    }
    const parameters: Record<string, string> = {};
    for (const parameter of stack.Parameters ?? []) {
      if (parameter.ParameterKey !== undefined) {
        parameters[parameter.ParameterKey] = parameter.ParameterValue ?? "";
      }
    }
    return {
      name: stackName,
      state: stack.StackStatus,
      outputs,
      reasonCode: stack.StackStatusReason,
      parameters,
    };
  }

  async create(
    component: StackComponent,
    options: {
      readonly stackName: string;
      readonly templateBody: string;
      readonly parameters: Readonly<Record<string, string>>;
      readonly tags: Readonly<Record<string, string>>;
    },
  ): Promise<void> {
    const api = await this.#cfn();
    try {
      await api.createStack({
        StackName: options.stackName,
        TemplateBody: options.templateBody,
        Parameters: protoParameters(options.parameters),
        Tags: protoTags(options.tags),
        Capabilities: component.capabilities ? [...component.capabilities] : undefined,
      });
    } catch (error) {
      throw wrap(error);
    }
  }

  async update(
    component: StackComponent,
    options: {
      readonly stackName: string;
      readonly templateBody: string;
      readonly parameters: Readonly<Record<string, string>>;
      readonly tags: Readonly<Record<string, string>>;
      readonly keepPrevious?: readonly string[];
    },
  ): Promise<UpdateOutcome> {
    const api = await this.#cfn();
    try {
      await api.updateStack({
        StackName: options.stackName,
        TemplateBody: options.templateBody,
        Parameters: protoParameters(options.parameters, options.keepPrevious),
        Tags: protoTags(options.tags),
        Capabilities: component.capabilities ? [...component.capabilities] : undefined,
      });
    } catch (error) {
      if (isNoUpdates(error)) {
        return "no_changes";
      }
      throw wrap(error);
    }
    return "changed";
  }

  async delete(stackName: string): Promise<void> {
    const api = await this.#cfn();
    try {
      await api.deleteStack({ StackName: stackName });
    } catch (error) {
      throw wrap(error);
    }
  }

  async wait(stackName: string, target: DeployTarget, timeoutMs: number): Promise<void> {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
      const status = await this.describe(stackName);
      if (
        target === "deleted" &&
        (status === undefined || TERMINAL_DELETED_STATES.has(status.state ?? ""))
      ) {
        return;
      }
      if (
        target === "deployed" &&
        status !== undefined &&
        TERMINAL_SUCCESS_STATES.has(status.state ?? "")
      ) {
        return;
      }
      if (status !== undefined && TERMINAL_FAILURE_STATES.has(status.state ?? "")) {
        throw new StackError(`la pila ${JSON.stringify(stackName)} terminó en ${status.state}`, {
          code: "failed",
        });
      }
      if (Date.now() >= deadline) {
        throw new StackError(
          `tiempo agotado esperando la pila ${JSON.stringify(stackName)} (${target})`,
          { code: "in_progress" },
        );
      }
      await sleep(this.#pollIntervalMs);
    }
  }

  /**
   * Sube `data` salvo que el objeto ya tenga exactamente ese contenido. Cada
   * llamada a S3 lleva `ExpectedBucketOwner`: un bucket de otra cuenta falla
   * en vez de recibir o servir el código de las Lambdas.
   */
  async putArtifact(bucket: string, key: string, data: Uint8Array): Promise<void> {
    const api = await this.#bucket();
    const located: LocatedObject = {
      Bucket: bucket,
      Key: key,
      ExpectedBucketOwner: await this.#accountId(),
    };
    const digest = sha256(data);
    const stored = await storedDigest(api, located);
    if (stored?.equals(digest)) {
      return;
    }
    if (stored !== undefined) {
      this.#logger?.warn?.(
        "el artefacto del componente ya subido no coincide con el del SDK: se sobrescribe",
      );
    }
    try {
      await api.putObject({ ...located, Body: data, ChecksumSHA256: digest.toString("base64") });
    } catch (error) {
      throw wrap(error);
    }
  }

  async #accountId(): Promise<string> {
    let account: string | undefined;
    try {
      account = (await (await this.#identity()).getCallerIdentity()).Account;
    } catch (error) {
      throw wrap(error);
    }
    if (account === undefined) {
      throw new StackError("GetCallerIdentity no devolvió Account", { code: "failed" });
    }
    return account;
  }

  async failureReason(stackName: string): Promise<string | undefined> {
    const status = await this.describe(stackName);
    return status?.reasonCode;
  }
}

/**
 * sha256 del objeto ya subido, o `undefined` si no existe. La clave es el
 * sha256 del contenido: uno distinto sólo puede ser un objeto manipulado o
 * corrupto, y `putArtifact` lo sobrescribe.
 */
async function storedDigest(api: S3Api, located: LocatedObject): Promise<Buffer | undefined> {
  try {
    const response = await api.getObject(located);
    return sha256((await response.Body?.transformToByteArray()) ?? new Uint8Array());
  } catch (error) {
    if (MISSING_OBJECT_CODES.has(awsCode(error) ?? "")) {
      return undefined;
    }
    throw wrap(error);
  }
}

function protoParameters(
  parameters: Readonly<Record<string, string>>,
): Array<{ ParameterKey: string; ParameterValue: string }>;
function protoParameters(
  parameters: Readonly<Record<string, string>>,
  keepPrevious: readonly string[] | undefined,
): ProtoParameter[];
function protoParameters(
  parameters: Readonly<Record<string, string>>,
  keepPrevious: readonly string[] = [],
): ProtoParameter[] {
  const entries = new Map<string, ProtoParameter>(
    Object.entries(parameters).map(([ParameterKey, ParameterValue]) => [
      ParameterKey,
      { ParameterKey, ParameterValue },
    ]),
  );
  for (const ParameterKey of keepPrevious) {
    entries.set(ParameterKey, { ParameterKey, UsePreviousValue: true });
  }
  return [...entries.values()].sort((a, b) => a.ParameterKey.localeCompare(b.ParameterKey));
}

function protoTags(tags: Readonly<Record<string, string>>): Array<{ Key: string; Value: string }> {
  return Object.entries(tags)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([Key, Value]) => ({ Key, Value }));
}
