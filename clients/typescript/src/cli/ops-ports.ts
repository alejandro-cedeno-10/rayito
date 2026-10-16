/**
 * Puertos de las órdenes `template`, `agent template`, `events` y `domain`:
 * lo que la CLI usa de las clases públicas del SDK (`Template`,
 * `AgentTemplate`, `LifecycleEvents`, `CustomDomain`). Los comandos sólo
 * conocen estas interfaces; `ops-adapters.ts` las cumple con el SDK y los
 * tests con dobles en memoria, nunca con AWS.
 */

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
