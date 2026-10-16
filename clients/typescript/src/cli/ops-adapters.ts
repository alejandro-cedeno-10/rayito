/**
 * Los adaptadores de `ops-ports.ts` sobre el SDK: cada orden construye lo que
 * usa en su primera llamada y nunca antes, así que `rayito events` o `rayito
 * domain` sin llamar a AWS no cargan ningún peer.
 */

import { existsSync, statSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import type { AgentTemplate } from "../agent/template.js";
import type { AwsClientSettings } from "../aws/control-plane.js";
import { CustomDomain } from "../custom-domain/service.js";
import { LifecycleEvents } from "../lifecycle-events/service.js";
import type { OptionalStacks } from "../stacks/service.js";
import {
  type BuildInfo,
  type BuildStatus,
  readRecentLogs,
  statusByName,
} from "../templates/build.js";
import { Template } from "../templates/dsl.js";
import {
  type AgentBuildRequest,
  type CustomDomainRequest,
  type DomainDeployer,
  type LifecycleEventsTool,
  type TemplateBuildRequest,
  TemplateSpecError,
  type TemplateTools,
} from "./ops-ports.js";

type Credentials = AwsClientSettings["credentials"];

/** El nombre de la exportación del módulo de spec que `template build` busca. */
export const SPEC_EXPORT_NAME = "template";

export class SdkTemplateTools implements TemplateTools {
  readonly #region: string;
  readonly #credentials: Credentials;

  constructor(region: string, credentials: Credentials) {
    this.#region = region;
    this.#credentials = credentials;
  }

  #awsOptions(): { region: string; credentials?: NonNullable<Credentials> } {
    return {
      region: this.#region,
      ...(this.#credentials === undefined ? {} : { credentials: this.#credentials }),
    };
  }

  async loadSpec(specPath: string): Promise<Template> {
    const absolute = resolve(specPath);
    if (!existsSync(absolute) || !statSync(absolute).isFile()) {
      throw new TemplateSpecError(`no existe: ${specPath}`);
    }
    const loaded = (await import(pathToFileURL(absolute).href)) as Record<string, unknown>;
    const candidate = loaded[SPEC_EXPORT_NAME];
    if (!(candidate instanceof Template)) {
      throw new TemplateSpecError(
        `${specPath}: exporta una variable \`${SPEC_EXPORT_NAME}\` (rayito.Template)`,
      );
    }
    return candidate;
  }

  build(template: Template, name: string, options: TemplateBuildRequest): Promise<BuildInfo> {
    return Template.build(template, name, {
      ...this.#awsOptions(),
      bucket: options.bucket,
      memoryMb: options.memoryMb,
      force: options.force,
      timeoutMs: options.timeoutMs,
      ...(options.onBuildLogs === undefined ? {} : { onBuildLogs: options.onBuildLogs }),
    });
  }

  buildAgent(template: AgentTemplate, options: AgentBuildRequest): Promise<BuildInfo> {
    return template.build({
      ...this.#awsOptions(),
      bucket: options.bucket,
      force: options.force,
      timeoutMs: options.timeoutMs,
      ...(options.onBuildLogs === undefined ? {} : { onBuildLogs: options.onBuildLogs }),
    });
  }

  status(name: string, version: string | undefined): Promise<BuildStatus> {
    return statusByName(name, version, this.#awsOptions());
  }

  logs(name: string, limit: number): Promise<string[]> {
    return readRecentLogs(name, { ...this.#awsOptions(), limit });
  }
}

/** `LifecycleEvents` sobre las pilas de la sesión: `deploy`/`status`/`destroy` van por ellas. */
export function sdkLifecycleEvents(
  region: string,
  credentials: Credentials,
  stacks: OptionalStacks,
): (stackName: string) => LifecycleEventsTool {
  return (stackName) => new LifecycleEvents({ stackName, region, credentials, stacks });
}

export function sdkCustomDomain(
  region: string,
  credentials: Credentials,
): (request: CustomDomainRequest) => DomainDeployer {
  return (request) =>
    new CustomDomain({
      publicDomain: request.publicDomain,
      stackName: request.stackName,
      region,
      credentials,
    });
}
