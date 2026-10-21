/**
 * Puertos de las órdenes `template`, `agent`, `events` y `domain`: lo que la
 * CLI usa de las clases públicas del SDK (`Template`, `AgentTemplate`,
 * `AgentProject`, `AgentDeployment`, `LifecycleEvents`, `CustomDomain`). Los comandos sólo
 * conocen estas interfaces; `ops-adapters.ts` las cumple con el SDK y los
 * tests con dobles en memoria, nunca con AWS.
 */

import type { AgentInvocation } from "../agent/deployment.js";
import type { AgentEvent, AgentResult } from "../agent/events.js";
import type { AgentTemplate } from "../agent/template.js";
import type {
  AgentCoreDeleteResult,
  AgentCoreDeployPlan,
  AgentCoreListItem,
  AgentCoreOptions,
  AgentCoreStatus,
  AgentCoreStep,
} from "../agentcore/models.js";
import type { CustomDomain, DeployCustomDomainOptions } from "../custom-domain/service.js";
import type { LifecycleEvents } from "../lifecycle-events/service.js";
import type { StackStatus } from "../stacks/model.js";
import type { BuildInfo, BuildStatus } from "../templates/build.js";
import type { Template } from "../templates/dsl.js";

/** `rayito template build|status|logs` y `rayito agent template build`. */
export interface TemplateTools {
  /** Carga la variable `template` exportada por el módulo del fichero `specPath`. */
  loadSpec(specPath: string): Promise<Template>;
  build(template: Template, name: string, options: TemplateBuildRequest): Promise<BuildInfo>;
  buildAgent(template: AgentTemplate, options: AgentBuildRequest): Promise<BuildInfo>;
  status(name: string, version: string | undefined): Promise<BuildStatus>;
  logs(name: string, limit: number): Promise<string[]>;
}

export interface AgentBuildRequest {
  readonly bucket: string;
  readonly force: boolean;
  readonly timeoutMs: number;
  /** Cada línea del log del build; ausente en `--json`. */
  readonly onBuildLogs: ((line: string) => void) | undefined;
}

export interface TemplateBuildRequest extends AgentBuildRequest {
  readonly memoryMb: number;
}

/** Lo que `rayito events` usa de `LifecycleEvents`. */
export type LifecycleEventsTool = Pick<
  LifecycleEvents,
  | "deploy"
  | "status"
  | "destroy"
  | "registerWebhook"
  | "listWebhooks"
  | "deleteWebhook"
  | "getEvents"
>;

/** Lo que `rayito domain deploy` usa de `CustomDomain`: valida el dominio y despliega. */
export interface DomainDeployer {
  readonly publicDomain: CustomDomain["publicDomain"];
  deploy(options: DeployCustomDomainOptions): Promise<StackStatus>;
}

export interface CustomDomainRequest {
  readonly publicDomain: string;
  readonly stackName: string | undefined;
}

/** El fichero de spec de `template build` no existe o no exporta `template`. */
export class TemplateSpecError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "TemplateSpecError";
  }
}

/** Lo que `rayito agent run` pasa a `AgentProject.run`. */
export interface AgentProjectRunRequest {
  readonly sessionId: string | undefined;
  readonly model: string | undefined;
  /** Cada evento a medida que llega; ausente sin `--stream`. */
  readonly onEvent: ((event: AgentEvent) => void) | undefined;
}

/** Las rutas de una `HttpApp` que el despliegue no expone (`HttpApp.hidesOwnRoutes`). */
export interface HiddenAppRoutes {
  readonly protocol: string;
  readonly healthPath: string;
  readonly invokePath: string;
}

export interface AgentExportRequest {
  readonly target: string;
  readonly outDir: string;
  readonly overwrite: boolean;
}

export interface AgentExportResult {
  readonly path: string;
  readonly files: readonly string[];
  readonly port: number;
  readonly platform: string;
}

/** Un agente desplegado: lo que la CLI usa de `AgentDeployment`. */
export interface DeployedAgent {
  readonly sandboxId: string;
  readonly url: string;
  invoke(
    request: AgentInvocation,
    options: { readonly sessionId: string | undefined },
  ): Promise<AgentResult>;
  stream(
    request: AgentInvocation,
    options: { readonly sessionId: string | undefined },
  ): AsyncIterable<AgentEvent>;
  status(): Promise<{
    readonly state: string;
    readonly ready: boolean;
    readonly busy: boolean;
    readonly port: number;
    readonly project: string | undefined;
  }>;
}

/** Un `AgentProject` ya cargado: `rayito agent build|run|export`. */
export interface AgentProjectTool {
  build(options: AgentBuildRequest): Promise<BuildInfo>;
  run(prompt: string, options: AgentProjectRunRequest): Promise<AgentResult>;
  export(options: AgentExportRequest): Promise<AgentExportResult>;
}

/** Lo que la detección fija a mano (`--framework`, `--language`, `--entry`,
 * `--protocol`); `undefined` deja decidir a la detección. */
export interface AgentDetectOverrides {
  readonly framework: string | undefined;
  readonly language: string | undefined;
  readonly entry: string | undefined;
  readonly protocol: string | undefined;
  readonly modelId?: string | undefined;
  readonly modelProvider?: string | undefined;
  readonly modelBaseUrl?: string | undefined;
  readonly graph?: string | undefined;
}

/** Lo que las banderas de `rayito agent deploy` cambian del proyecto
 * detectado; `undefined` (o vacío) deja el valor del fichero o de la
 * detección. */
export interface AgentDeployFlags {
  readonly port: number | undefined;
  readonly timeoutMs: number | undefined;
  readonly memory: number | undefined;
  readonly maxSessions: number | undefined;
  readonly idleSeconds: number | undefined;
  readonly queue: number | undefined;
  readonly queueTimeoutMs: number | undefined;
  readonly env: Readonly<Record<string, string>>;
  readonly allowOut: readonly string[];
  readonly minVms: number | undefined;
  readonly maxVms: number | undefined;
  readonly warm: number | undefined;
  readonly sessions: string | undefined;
  /** `--secret-env NOMBRE=<id>[#clave]`, tal cual (se leen al aplicar). */
  readonly secretEnv?: readonly string[] | undefined;
  /** `--secret-env-from <id>[,prefix=P][,keys=A|B]`, tal cual. */
  readonly secretEnvFrom?: readonly string[] | undefined;
}

/** Dónde y cómo despliega `rayito agent deploy`: lo de `DeployOptions` que
 * sale de las banderas, más los avisos de progreso. */
export interface AgentDeploySettings {
  readonly bucket: string | undefined;
  readonly secretNames: Readonly<Record<string, string>>;
  readonly createSecretFromEnv: boolean;
  /** Quien publica la imagen base si falta (`--base-artifact`); `undefined` sin la bandera. */
  readonly basePublisher: (() => Promise<void>) | undefined;
  readonly accessToken: string | undefined;
  /** El fichero donde quedó el access token: si cae dentro del código del
   * agente, se excluye de la imagen. */
  readonly tokenFile?: string | undefined;
  readonly keepOnFailure: boolean;
  readonly buildImage: boolean | undefined;
  readonly onBuildLogs: ((line: string) => void) | undefined;
  readonly onStep: ((step: { readonly kind: string; readonly detail: string }) => void) | undefined;
  /** `--create-secrets-from`: el `.env` cuyos valores van a un secreto JSON. */
  readonly createSecretsFrom?: string | undefined;
  /** `--check-env`: avisa de las variables de `.env.example` sin valor. */
  readonly checkEnv?: boolean | undefined;
}

export interface AgentDeployRequest {
  readonly path: string;
  readonly overrides: AgentDetectOverrides;
  readonly flags: AgentDeployFlags;
  readonly settings: AgentDeploySettings;
}

/** Lo que `agent detect` devuelve: el informe de la detección (o `undefined`
 * si el proyecto salió de su fichero) y el proyecto resuelto como documento. */
export interface DetectedProject {
  readonly report: unknown;
  readonly document: Record<string, unknown>;
}

/** El plan de un despliegue (`--dry-run`): sin AWS. `document` es el plan
 * serializable, `lines` el texto. */
export interface DeployPlanView {
  readonly report: unknown;
  readonly document: Record<string, unknown>;
  readonly lines: readonly string[];
}

/** Una flota desplegada: lo que `agent deploy` imprime. Nunca lleva las
 * cabeceras de acceso. */
export interface DeployedFleetView {
  readonly name: string;
  readonly sandboxIds: readonly string[];
  readonly url: string;
  readonly port: number;
  readonly hiddenRoutes: HiddenAppRoutes | undefined;
}

/** El estado de una flota, para `agent status <nombre>`. */
export interface FleetStatusView {
  readonly name: string;
  readonly minVms: number;
  readonly maxVms: number;
  readonly members: readonly { readonly sandboxId: string; readonly state: string }[];
}

/** Una flota reconectada: lo que la CLI usa de `AgentFleet`. */
export interface FleetClient {
  invoke(
    request: AgentInvocation,
    options: { readonly sessionId: string | undefined },
  ): Promise<AgentResult>;
  stream(
    request: AgentInvocation,
    options: { readonly sessionId: string | undefined },
  ): Promise<AsyncIterable<AgentEvent>>;
  status(): Promise<FleetStatusView>;
  /** Termina todos los VMs de la flota; devuelve cuántos eran. */
  stop(): Promise<number>;
}

/** `rayito agent ...` sobre `AgentProject` y `AgentDeployment`; `init` no los usa. */
export interface AgentProjectTools {
  /** Lee el fichero de proyecto de `file` (un fichero o un directorio); no llama a AWS. */
  load(file: string): Promise<AgentProjectTool>;
  connect(sandboxId: string, accessToken: string): Promise<DeployedAgent>;
  /** Detecta el proyecto de `path` sin ejecutar su código; no llama a AWS. */
  detect(path: string, overrides: AgentDetectOverrides): Promise<DetectedProject>;
  /** Guarda el proyecto detectado como `rayito-agent.json` en `path`; devuelve el nombre del fichero. */
  writeDetected(path: string, overrides: AgentDetectOverrides): Promise<string>;
  /** El plan y el coste de `deploy`, sin AWS. */
  planDeploy(request: AgentDeployRequest): Promise<DeployPlanView>;
  /** Detecta, resuelve y despliega la flota (`deployAgent`); la deja viva al volver. */
  deployFleet(request: AgentDeployRequest): Promise<DeployedFleetView>;
  /** Reconecta la flota de la imagen `name`; sin token sólo puede listarse o terminarse. */
  connectFleet(name: string, accessToken: string | undefined): Promise<FleetClient>;
  /** `terminate-microvm`: no necesita el access token. `false` si el sandbox ya no existe. */
  stop(sandboxId: string): Promise<boolean>;
}

/** Lo que `rayito agent deploy --target agentcore` pide al SDK: el proyecto, las banderas, `--tag` y las opciones de AgentCore. `region` es el `--region` global (el plan no resuelve la región con ninguna llamada a AWS). */
export interface AgentCoreDeployRequest {
  readonly path: string;
  readonly overrides: AgentDetectOverrides;
  readonly flags: AgentDeployFlags;
  readonly settings: AgentDeploySettings;
  readonly agentcore: AgentCoreOptions;
  readonly tokenFile: string | undefined;
  readonly region: string | undefined;
  /** El entorno del proceso: de él salen el bucket (`RAYITO_BUCKET`) y la región. */
  readonly environ: Readonly<Record<string, string | undefined>>;
}

/** El resultado de desplegar en AgentCore: lo que `agent deploy` imprime. */
export interface AgentCoreDeployedView {
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

/** El plan calculado sin AWS y, para quien lo confirme, cómo aplicarlo. */
export interface AgentCorePlanView {
  readonly report: unknown;
  readonly plan: AgentCoreDeployPlan;
  apply(): Promise<AgentCoreDeployedView>;
  /** Lo que el plan avisa de `secretEnv` (`env_missing` y el secreto que
   * `--create-secrets-from` creará): sólo nombres, nunca valores. */
  readonly notices?: readonly string[] | undefined;
}

/** Un agente de AgentCore reconectado: lo que la CLI usa de `AgentCoreFleet`. */
export interface AgentCoreClient {
  invoke(
    request: AgentInvocation,
    options: { readonly sessionId: string | undefined },
  ): Promise<AgentResult>;
  stream(
    request: AgentInvocation,
    options: { readonly sessionId: string | undefined },
  ): Promise<AsyncIterable<AgentEvent>>;
  status(): Promise<AgentCoreStatus>;
  stopSession(sessionId: string): Promise<boolean>;
}

/** Qué borraría `rayito agent delete`: lo que existe de ese agente en la región. */
export interface AgentCoreDeletePreview {
  readonly region: string;
  readonly runtimeName: string;
  readonly runtimeId: string | undefined;
  readonly stackName: string;
  readonly stackExists: boolean;
  readonly logGroup: string | undefined;
}

/** `rayito agent ... --target agentcore` sobre el SDK; `plan` no llama a AWS. */
export interface AgentCoreTools {
  plan(request: AgentCoreDeployRequest): Promise<AgentCorePlanView>;
  connect(name: string): Promise<AgentCoreClient>;
  list(): Promise<{ readonly region: string; readonly agents: readonly AgentCoreListItem[] }>;
  previewDelete(name: string): Promise<AgentCoreDeletePreview>;
  delete(
    name: string,
    onStep: (step: { readonly kind: string; readonly detail: string }) => void,
  ): Promise<AgentCoreDeleteResult>;
}
