/**
 * Los datos compartidos con Python de `--target agentcore`
 * (`testdata/agentcore-deploy/`): el contrato y los vectores, tipados para los
 * tests de la plantilla, del plan, de las decisiones y de los identificadores.
 */

import { loadTestdata } from "./cli/paths.js";

export const FOLDER = "agentcore-deploy";

export interface Contract {
  errorExitCodes: Record<string, number>;
  constants: Record<string, unknown> & {
    stackParameters: Record<string, string>;
    stackOutputs: Record<string, string>;
    runtimeNameTemplate: string;
    agentNamePattern: string;
    agentNameMaxLength: number;
    stackCapabilities: string[];
    runtimeRegionEnvironmentVariables: string[];
    runtimeStates: string[];
    buildStates: string[];
    buildEnvironmentVariables: Record<string, string>;
    runtimeComparedFields: string[];
    export: Record<string, unknown> & {
      awsCredentials: Record<string, unknown>;
    };
  };
  messages: {
    errors: Record<string, string>;
    security: Record<string, string>;
    warnings: Record<string, string>;
    cost: Record<string, string>;
    progress: Record<string, string>;
    deleteProgress: Record<string, string>;
    [key: string]: unknown;
  };
  json: Record<string, string[] | string>;
  commands: Record<string, { appendedOptions?: ContractOption[]; parameters?: ContractOption[] }>;
  optionsTable: { rows: { option: string; reason: string; call: string; file: string }[] };
  sdk: { typescript: Record<string, unknown> };
}

export interface ContractOption {
  name: string;
  flags?: string[];
  type: string;
  default: unknown;
  multiple: boolean;
  required: boolean;
  isFlag?: boolean;
  help: string;
}

export interface PlanCase {
  name: string;
  target: string;
  region: string;
  project: Record<string, unknown>;
  call: {
    scaling?: Record<string, number>;
    secretNames?: Record<string, string>;
    accessToken?: string;
    keepOnFailure?: boolean;
    bucket?: string;
    agentcore?: {
      bedrockModels?: string[];
      readableSecrets?: string[];
      idleSessionTimeoutSeconds?: number;
      maxSessionLifetimeSeconds?: number;
      tags?: Record<string, string>;
    };
  };
  environ: Record<string, string>;
  expected: Record<string, unknown> & { error?: Record<string, unknown> };
}

export interface ApplyCase {
  name: string;
  input: {
    stack: { status: string; outputs: Record<string, string> } | null;
    image: { exists: boolean; sizeBytes?: number } | null;
    runtime: {
      agentRuntimeName: string;
      agentRuntimeId: string;
      agentRuntimeVersion: string;
      status: string;
      description: string;
    } | null;
    runtimeDetails: {
      containerUri: string;
      roleArn: string;
      networkMode: string;
      serverProtocol: string;
      environmentVariables: Record<string, string>;
      idleRuntimeSessionTimeout: number;
      maxLifetime: number;
      requireMMDSV2: boolean;
    } | null;
    buildImage: boolean | null;
    desired: {
      imageTag: string;
      environmentVariables: Record<string, string>;
      idleRuntimeSessionTimeout: number;
      maxLifetime: number;
    };
  };
  expected: { stack?: string; image?: string; runtime?: string; error?: Record<string, unknown> };
}

export interface ModelArnCase {
  name: string;
  models: string[];
  modelRegion: string;
  account: string;
  partition: string;
  expected: string[];
}

export interface SecretArnCase {
  name: string;
  secrets: string[];
  region: string;
  account: string;
  partition: string;
  expected: string[];
}

export interface SessionCase {
  input: string;
  expected: string;
  kind: string;
}

export interface ImageTagCase {
  name: string;
  files: Record<string, string>;
  expected: string;
}

export interface ClientTokenCase {
  name: string;
  request: Record<string, unknown>;
  baseVersion?: string;
  expected: string;
}

export const clientTokenVectors = loadTestdata<{ cases: ClientTokenCase[] }>(
  FOLDER,
  "client-tokens.json",
).cases;

export const contract = loadTestdata<Contract>(FOLDER, "contract.json");
export const planVectors = loadTestdata<{ cases: PlanCase[] }>(FOLDER, "plan-vectors.json").cases;
export const applyVectors = loadTestdata<{ cases: ApplyCase[] }>(
  FOLDER,
  "apply-vectors.json",
).cases;
export interface RejectedArnCase {
  name: string;
  kind: "secret" | "model";
  value: string;
  region: string;
  account: string;
  partition: string;
}

export const modelArnVectors = loadTestdata<{
  bedrock: ModelArnCase[];
  secrets: SecretArnCase[];
  rejected: RejectedArnCase[];
}>(FOLDER, "model-arns.json");
export const sessionVectors = loadTestdata<{
  newPattern: string;
  cases: SessionCase[];
}>(FOLDER, "session-ids.json");
export const imageTagVectors = loadTestdata<{ cases: ImageTagCase[] }>(
  FOLDER,
  "image-tags.json",
).cases;

/** La plantilla de un error del contrato; un código que no existe es un fallo del test. */
export function errorTemplate(code: string): string {
  const template = contract.messages.errors[code];
  if (template === undefined) {
    throw new Error(`el contrato no tiene el error ${code}`);
  }
  return template;
}
