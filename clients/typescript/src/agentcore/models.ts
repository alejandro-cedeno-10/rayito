/**
 * Los valores del despliegue en Amazon Bedrock AgentCore Runtime (ADR-031):
 * las opciones que el llamante pasa, el plan que se calcula sin AWS, lo que el
 * despliegue devuelve y las observaciones de la cuenta que las decisiones de
 * `decisions.ts` leen. Son tipos puros, sin AWS ni E/S; los campos y sus
 * nombres son los de `testdata/agentcore-deploy/contract.json`.
 */

import type { BucketSource } from "../agent/aws-resolve.js";

export const AGENT_TARGETS = ["microvm", "agentcore"] as const;
export type AgentTarget = (typeof AGENT_TARGETS)[number];
export const TARGET_MICROVM: AgentTarget = "microvm";
export const TARGET_AGENTCORE: AgentTarget = "agentcore";

/**
 * Lo que sólo existe con `target: "agentcore"`: los modelos de Bedrock y los
 * secretos que el rol del runtime puede usar (ninguno por defecto), la vida de
 * una sesión y las etiquetas de la pila y del runtime. Los plazos van en
 * milisegundos y se convierten a segundos enteros: un valor que no es múltiplo
 * de 1000 es `lifecycle_invalid`. `externalModel` permite un modelo fuera de
 * Bedrock: el rol no recibe permisos de Bedrock que no pidas y la clave la
 * obtiene tu código con `readableSecrets`; nunca va en la configuración del runtime.
 *
 * Coste y activación
 * -------------------
 * Activa: pasarlas a `deployAgent()` o `planDeploy()` con `target: "agentcore"`;
 *     por sí solas no crean nada, y con otro `target` dan `option_requires_agentcore`.
 * Recursos y llamadas AWS: ninguno aquí; `bedrockModels` y `readableSecrets`
 *     son los únicos permisos de Bedrock y Secrets Manager del rol del runtime,
 *     y `tags` se aplica a la pila y al runtime.
 * Coste aproximado: ninguno aquí; ver `deployAgent()`.
 * IAM: ninguna para este valor.
 * Cómo apagarla: los valores por defecto no dan al rol ningún modelo propio ni
 *     ningún secreto, la vida de la sesión es la de AgentCore (15 min sin uso,
 *     8 h como máximo) y sin `externalModel: true` un proveedor que no es
 *     Bedrock es `provider_unsupported`.
 * Ejemplo:
 *   const agentcore: AgentCoreOptions = { bedrockModels: ["amazon.nova-lite-v1:0"] };
 */
export interface AgentCoreOptions {
  readonly bedrockModels?: readonly string[] | undefined;
  readonly readableSecrets?: readonly string[] | undefined;
  readonly idleSessionTimeoutMs?: number | undefined;
  readonly maxSessionLifetimeMs?: number | undefined;
  readonly tags?: Readonly<Record<string, string>> | undefined;
  readonly externalModel?: boolean | undefined;
}

/** `AgentCoreOptions` ya validadas, con los plazos en segundos enteros. */
export interface ResolvedAgentCoreOptions {
  readonly bedrockModels: readonly string[];
  readonly readableSecrets: readonly string[];
  readonly idleSessionTimeoutSeconds: number | undefined;
  readonly maxSessionLifetimeSeconds: number | undefined;
  readonly tags: Readonly<Record<string, string>>;
  readonly externalModel: boolean;
}

export type ResourceKind = "stack" | "image" | "runtime";

/** Un paso del plan o del despliegue: `action` es `planned` en el plan y, al
 * aplicar, `create`, `update`, `reuse`, `wait` o `build`; `status` es el
 * estado que se vio (`null` en el plan). */
export interface AgentCoreStep {
  readonly resource: ResourceKind;
  readonly name: string;
  readonly action: string;
  readonly status: string | null;
}

export interface AgentCoreLifecycle {
  readonly idleSessionTimeoutSeconds: number;
  readonly maxSessionLifetimeSeconds: number;
}

/**
 * Lo que `deployAgent()` haría con `target: "agentcore"`, calculado sin llamar
 * a AWS: los nombres derivados, el bucket y de dónde saldrá, los modelos de
 * Bedrock, los secretos legibles, el entorno y la vida del runtime, las
 * etiquetas, los tres pasos, las claves de seguridad y de avisos (los textos
 * están en `messages.ts`), las líneas de coste de lista y si el modelo está
 * fuera de Bedrock (`externalModel`).
 */
export interface AgentCoreDeployPlan {
  readonly target: "agentcore";
  /** Opcional en el tipo para que un plan escrito a mano siga compilando; el
   * plan que calcula el SDK siempre lo trae. */
  readonly externalModel?: boolean | undefined;
  readonly name: string;
  readonly region: string;
  readonly runtimeName: string;
  readonly stackName: string;
  readonly repositoryName: string;
  readonly buildProjectName: string;
  readonly imageTag: string;
  readonly bucket: string | null;
  readonly bucketSource: BucketSource;
  readonly network: string;
  readonly bedrockModels: readonly string[];
  readonly readableSecrets: readonly string[];
  readonly environment: Readonly<Record<string, string>>;
  readonly lifecycle: AgentCoreLifecycle;
  readonly tags: Readonly<Record<string, string>>;
  readonly steps: readonly AgentCoreStep[];
  readonly security: readonly string[];
  readonly warnings: readonly string[];
  readonly cost: readonly string[];
  /** Las variables que el host pondrá desde Secrets Manager (ADR-033 D4): sólo
   * nombres, nunca valores; ausente si el proyecto no usa `secretEnv`. */
  readonly secretEnvNames?: readonly string[] | undefined;
}

/** El estado de un agente desplegado (`AgentCoreFleet.status()`). */
export interface AgentCoreStatus {
  readonly name: string;
  readonly region: string;
  readonly runtimeName: string;
  readonly runtimeId: string;
  readonly runtimeArn: string;
  readonly runtimeVersion: string;
  readonly status: string;
  readonly imageUri: string;
  readonly logGroup: string;
}

/** Qué se borró en `AgentCoreFleet.delete()`: cada campo es `false` si no había nada que borrar. */
export interface AgentCoreDeleteResult {
  readonly name: string;
  readonly region: string;
  readonly runtimeDeleted: boolean;
  readonly stackDeleted: boolean;
  readonly logGroupDeleted: boolean;
}

/** Un agente de la lista de `rayito agent list --target agentcore`. */
export interface AgentCoreListItem {
  readonly name: string;
  readonly runtimeName: string;
  readonly runtimeId: string;
  readonly runtimeVersion: string;
  readonly status: string;
}

/** Lo que devuelve el servicio al terminar de desplegar. */
export interface AgentCoreDeployResult {
  readonly name: string;
  readonly region: string;
  readonly runtimeName: string;
  readonly runtimeId: string;
  readonly runtimeArn: string;
  readonly runtimeVersion: string;
  readonly status: string;
  readonly imageUri: string;
  readonly stackName: string;
  readonly logGroup: string;
  readonly steps: readonly AgentCoreStep[];
}

/** El elemento de `ListAgentRuntimes`. */
export interface ListedRuntime {
  readonly runtimeId: string;
  readonly runtimeArn: string;
  readonly runtimeName: string;
  readonly runtimeVersion: string;
  readonly status: string;
  readonly description: string;
}

/** Lo que se lee de `GetAgentRuntime` (`failureReason` no se lee: es texto crudo de AWS). */
export interface RuntimeDetails extends ListedRuntime {
  readonly roleArn: string;
  readonly networkMode: string;
  readonly serverProtocol: string;
  readonly environmentVariables: Readonly<Record<string, string>>;
  readonly idleRuntimeSessionTimeout: number;
  readonly maxLifetime: number;
  readonly containerUri: string;
  readonly requireMMDSV2: boolean;
}

/** La configuración que se quiere en el runtime; `runtimeMatches` la compara campo a campo. */
export interface DesiredRuntime {
  readonly containerUri: string;
  readonly roleArn: string;
  readonly environmentVariables: Readonly<Record<string, string>>;
  readonly idleRuntimeSessionTimeout: number;
  readonly maxLifetime: number;
}

export interface ImageObservation {
  readonly exists: boolean;
  readonly sizeBytes: number | undefined;
}

export type StackAction = "create" | "update" | "wait";
export type ImageAction = "build" | "reuse";
export type RuntimeAction = "create" | "update" | "reuse" | "wait";
