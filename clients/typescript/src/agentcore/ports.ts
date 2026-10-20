/**
 * Los puertos de `--target agentcore` (ADR-031, D1): lo que el servicio
 * necesita de AgentCore, CodeBuild, ECR, S3, CloudWatch Logs, STS y
 * CloudFormation, sin saber si detrás hay el SDK de AWS o un doble de test.
 * Los cumple `aws.ts` y los tests con dobles en memoria. La pila pasa por el
 * `StackProvisioner` que ya existe. Sin imports de AWS.
 */

import type { StackProvisioner } from "../stacks/port.js";
import type { ImageObservation, ListedRuntime, RuntimeDetails } from "./models.js";

/** Lo que se manda a `CreateAgentRuntime` o `UpdateAgentRuntime` (`runtimeName` y `tags` sólo al crear; `runtimeId` y `requireMMDSV2` sólo al actualizar). */
export interface RuntimeWriteRequest {
  readonly runtimeName?: string;
  readonly runtimeId?: string;
  readonly containerUri: string;
  readonly roleArn: string;
  readonly environmentVariables: Readonly<Record<string, string>>;
  readonly idleRuntimeSessionTimeout: number;
  readonly maxLifetime: number;
  readonly description: string;
  readonly tags?: Readonly<Record<string, string>>;
  readonly requireMMDSV2?: true;
  readonly clientToken: string;
}

export interface RuntimeWriteResult {
  readonly runtimeId: string;
  readonly runtimeArn: string;
  readonly runtimeVersion: string;
  readonly status: string;
}

/** `CreateAgentRuntime` respondió `ConflictException`: otro llamante creó el runtime a la vez. */
export class RuntimeConflictError extends Error {
  constructor() {
    super("el runtime ya existe");
    Object.setPrototypeOf(this, new.target.prototype);
    this.name = new.target.name;
  }
}

/** El proyecto de CodeBuild o el repositorio ECR de la pila no existe: la pila no es la plantilla de Rayito (`stack_failed`). */
export class MissingResourceError extends Error {
  constructor(readonly resource: "repository" | "buildProject") {
    super(`falta ${resource}`);
    Object.setPrototypeOf(this, new.target.prototype);
    this.name = new.target.name;
  }
}

export interface RuntimeControl {
  listRuntimes(): Promise<ListedRuntime[]>;
  /** `undefined` si el runtime ya no existe (`ResourceNotFoundException`). */
  getRuntime(runtimeId: string): Promise<RuntimeDetails | undefined>;
  createRuntime(request: RuntimeWriteRequest): Promise<RuntimeWriteResult>;
  updateRuntime(request: RuntimeWriteRequest): Promise<RuntimeWriteResult>;
  /** Idempotente: borrar uno que no existe no es un error. */
  deleteRuntime(runtimeId: string): Promise<void>;
}

export interface InvokeRequest {
  /** El nombre del agente, para el error si el runtime ya no existe. */
  readonly agentName: string;
  readonly runtimeArn: string;
  readonly sessionId: string;
  readonly accept: string;
  readonly payload: Uint8Array;
  /** Plazo de lectura: 15 min en `invoke` y 60 min en `stream` (cuotas de AgentCore). */
  readonly requestTimeoutMs: number;
  readonly signal?: AbortSignal | undefined;
}

export interface RuntimeInvoker {
  /** El cuerpo de la respuesta en bloques de bytes (el documento JSON o los mensajes SSE del host). */
  invoke(request: InvokeRequest): Promise<AsyncIterable<Uint8Array>>;
  /** `false` si la sesión ya no existe. */
  stopSession(runtimeArn: string, sessionId: string): Promise<boolean>;
}

export interface BuildRequest {
  readonly projectName: string;
  /** `<bucket>/<clave>` del zip del contexto. */
  readonly sourceLocation: string;
  readonly imageTag: string;
  readonly baseImage: string;
}

export interface BuildObservation {
  readonly status: string;
  readonly phase: string | undefined;
  readonly logGroup: string | undefined;
}

export interface ImageBuilder {
  startBuild(request: BuildRequest): Promise<{ readonly buildId: string }>;
  getBuild(buildId: string): Promise<BuildObservation>;
}

export interface ImageRegistry {
  /** Si la etiqueta existe en el repositorio y lo que pesa (`ImageNotFoundException` es `exists: false`). */
  describeImage(repositoryName: string, imageTag: string): Promise<ImageObservation>;
}

export interface SourceStore {
  putSource(bucket: string, key: string, body: Uint8Array, owner: string): Promise<void>;
  /** Idempotente (S3 responde 204 también si no existe). */
  deleteSource(bucket: string, key: string, owner: string): Promise<void>;
  /** La región del bucket, o `undefined` si no existe o no es accesible. */
  bucketRegion(bucket: string): Promise<string | undefined>;
}

export interface BucketDiscovery {
  /** El bucket de artefactos de la cuenta y la región (CDK o `rayito setup`); nunca crea uno. */
  discover(region: string): Promise<string | undefined>;
}

export interface RuntimeLogs {
  /** `false` si el grupo ya no existía. */
  deleteLogGroup(logGroupName: string): Promise<boolean>;
}

export interface AccountIdentity {
  accountId(): Promise<string>;
}

export interface AgentCorePorts {
  readonly control: RuntimeControl;
  readonly invoker: RuntimeInvoker;
  readonly builder: ImageBuilder;
  readonly registry: ImageRegistry;
  readonly source: SourceStore;
  readonly buckets: BucketDiscovery;
  readonly logs: RuntimeLogs;
  readonly identity: AccountIdentity;
  readonly stacks: StackProvisioner;
}
