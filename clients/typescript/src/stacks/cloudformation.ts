/**
 * Adaptador del SDK v3 de `StackProvisioner` (M15 foundations, ADR-016).
 * Espejo de `rayito._stacks._cloudformation`. Parámetros y formas
 * verificados contra `AWS_API_NOTES.md` §21: `CreateStack`, `UpdateStack`
 * (con `UsePreviousValue` para los parámetros que se conservan),
 * `DescribeStacks` (incluidos sus `Parameters`), `DeleteStack`. El cliente de `putArtifact` sólo hace
 * `HeadObject` + `PutObject`, igual que el resto del SDK.
 *
 * `@aws-sdk/client-cloudformation` y `@aws-sdk/client-s3` son peers
 * opcionales (`loadOptionalSdkClient`): construir un `CloudFormationProvisioner`
 * no los carga ni llama a AWS; sólo el primer `deploy`/`status`/`destroy` lo
 * hace.
 */

import type { AwsClientSettings } from "../aws/control-plane.js";
import { awsCode, loadOptionalSdkClient } from "../aws/optional-client.js";
import { sanitizeAwsError } from "../aws/sanitize.js";
import { StackError } from "../errors.js";
import type { StackComponent, StackStatus } from "./model.js";
import type { DeployTarget, StackProvisioner, UpdateOutcome } from "./port.js";

const CLOUDFORMATION_PEER = "@aws-sdk/client-cloudformation";
const S3_PEER = "@aws-sdk/client-s3";
const VALIDATION_ERROR = "ValidationError";
const NOT_EXISTS_MARKER = "does not exist";
const NO_UPDATES_MARKER = "No updates are to be performed";
const HEAD_NOT_FOUND_CODE = "NotFound";
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

interface S3Api {
  headObject(input: { Bucket: string; Key: string }): Promise<unknown>;
  putObject(input: { Bucket: string; Key: string; Body: Uint8Array }): Promise<unknown>;
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
  readonly HeadObjectCommand: new (input: object) => unknown;
  readonly PutObjectCommand: new (input: object) => unknown;
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
    headObject: (input) => send(new sdk.HeadObjectCommand(input)),
    putObject: (input) => send(new sdk.PutObjectCommand(input)),
  };
}

/**
 * El `StackError` de un error del SDK, con el resumen saneado como mensaje y
 * como `cause`: el error crudo de smithy lleva `$response` (la petición
 * firmada) y, en un error de firma, la cadena canónica en el `message`.
 */
function wrap(error: unknown): StackError {
  const summary = sanitizeAwsError(error, { includeMessage: true });
  return new StackError(`${summary.name}: ${summary.message}`, { code: "failed", cause: summary });
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
}

export class CloudFormationProvisioner implements StackProvisioner {
  readonly #region: string | undefined;
  readonly #credentials: Credentials;
  readonly #pollIntervalMs: number;
  #cloudformation: CloudFormationApi | undefined;
  #s3: S3Api | undefined;

  constructor(options: CloudFormationProvisionerOptions = {}) {
    this.#region = options.region;
    this.#credentials = options.credentials;
    this.#pollIntervalMs = options.pollIntervalMs ?? DEFAULT_POLL_INTERVAL_MS;
    this.#cloudformation = options.cloudformationClient;
    this.#s3 = options.s3Client;
  }

  async #cfn(): Promise<CloudFormationApi> {
    if (this.#cloudformation === undefined) {
      const region = this.#region ?? process.env.AWS_REGION ?? process.env.AWS_DEFAULT_REGION;
      if (!region) {
        throw new StackError(
          "falta la región de OptionalStacks: pasa `region` o define AWS_REGION",
          {
            code: "failed",
          },
        );
      }
      this.#cloudformation = await cloudformationApi(region, this.#credentials);
    }
    return this.#cloudformation;
  }

  async #bucket(): Promise<S3Api> {
    if (this.#s3 === undefined) {
      const region = this.#region ?? process.env.AWS_REGION ?? process.env.AWS_DEFAULT_REGION;
      if (!region) {
        throw new StackError(
          "falta la región de OptionalStacks: pasa `region` o define AWS_REGION",
          {
            code: "failed",
          },
        );
      }
      this.#s3 = await s3Api(region, this.#credentials);
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

  async putArtifact(bucket: string, key: string, data: Uint8Array): Promise<void> {
    const api = await this.#bucket();
    try {
      await api.headObject({ Bucket: bucket, Key: key });
      return;
    } catch (error) {
      if (awsCode(error) !== HEAD_NOT_FOUND_CODE) {
        throw wrap(error);
      }
    }
    try {
      await api.putObject({ Bucket: bucket, Key: key, Body: data });
    } catch (error) {
      throw wrap(error);
    }
  }

  async failureReason(stackName: string): Promise<string | undefined> {
    const status = await this.describe(stackName);
    return status?.reasonCode;
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
