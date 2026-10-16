/**
 * Puertos de las órdenes `template`, `agent`, `events` y `domain`: lo que la
 * CLI usa de las clases públicas del SDK (`Template`, `AgentTemplate`,
 * `AgentProject`, `AgentDeployment`, `LifecycleEvents`, `CustomDomain`). Los comandos sólo
 * conocen estas interfaces; `ops-adapters.ts` las cumple con el SDK y los
 * tests con dobles en memoria, nunca con AWS.
 */

import type { AgentEvent, AgentResult } from "../agent/events.js";
import type { AgentTemplate } from "../agent/template.js";
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

/** Lo que `rayito agent deploy` cambia del `serve` del fichero; `undefined` deja el del fichero. */
export interface AgentDeployOverrides {
  readonly port: number | undefined;
  readonly timeoutMs: number | undefined;
  /** El access token del sandbox del despliegue, para que `invoke` y `status` se reconecten. */
  readonly accessToken: string;
  /** Deja vivo el sandbox si el arranque falla, para depurarlo. */
  readonly keepOnFailure: boolean;
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
  invoke(prompt: string, options: { readonly sessionId: string | undefined }): Promise<AgentResult>;
  stream(
    prompt: string,
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

/** Lo que devuelve `deploy`: el agente y el puerto en el que sirve. Nunca lleva las cabeceras de acceso. */
export interface DeployedService {
  readonly agent: DeployedAgent;
  readonly port: number;
  /** Las rutas propias de la app que no se alcanzan desde fuera, para avisar. */
  readonly hiddenRoutes?: HiddenAppRoutes | undefined;
}

/** Un `AgentProject` ya cargado: `rayito agent build|run|deploy|export`. */
export interface AgentProjectTool {
  build(options: AgentBuildRequest): Promise<BuildInfo>;
  run(prompt: string, options: AgentProjectRunRequest): Promise<AgentResult>;
  deploy(overrides: AgentDeployOverrides): Promise<DeployedService>;
  export(options: AgentExportRequest): Promise<AgentExportResult>;
}

/** `rayito agent ...` sobre `AgentProject` y `AgentDeployment`; `init` no los usa. */
export interface AgentProjectTools {
  /** Lee el fichero de proyecto de `file` (un fichero o un directorio); no llama a AWS. */
  load(file: string): Promise<AgentProjectTool>;
  connect(sandboxId: string, accessToken: string): Promise<DeployedAgent>;
  /** `terminate-microvm`: no necesita el access token. `false` si el sandbox ya no existe. */
  stop(sandboxId: string): Promise<boolean>;
}
