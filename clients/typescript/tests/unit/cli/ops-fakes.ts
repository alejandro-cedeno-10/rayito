/**
 * Dobles de los puertos de `ops-ports.ts` para probar `template`, `agent
 * template`, `events` y `domain` sin AWS: registran cada llamada con sus
 * argumentos, como los fakes de los tests de Python.
 */

import type { AgentTemplate } from "../../../src/agent/template.js";
import type {
  AgentBuildRequest,
  DomainDeployer,
  LifecycleEventsTool,
  TemplateBuildRequest,
  TemplateTools,
} from "../../../src/cli/ops-ports.js";
import { TemplateSpecError } from "../../../src/cli/ops-ports.js";
import type { DeployCustomDomainOptions } from "../../../src/custom-domain/service.js";
import type { EventRecord, WebhookInfo } from "../../../src/lifecycle-events/domain.js";
import type { StackStatus } from "../../../src/stacks/model.js";
import type { BuildInfo, BuildStatus } from "../../../src/templates/build.js";
import { Template } from "../../../src/templates/dsl.js";

export type Call = readonly [string, ...unknown[]];

export const BUILD_INFO: BuildInfo = {
  templateId: "arn:aws:lambda:us-east-1:123456789012:microvm-image:mi-template",
  buildId: "3.0/mi-template",
  alias: "mi-template",
};

export class FakeTemplateTools implements TemplateTools {
  readonly calls: Call[] = [];
  readonly logLines: string[] = ["Step 1/2 : FROM base", "Step 2/2 : RUN pip install pandas"];
  buildFailure: Error | undefined;
  specFailure: Error | undefined;
  statusResult: BuildStatus = { state: "SUCCESSFUL", info: BUILD_INFO };
  recentLogs: string[] = ["linea 1", "linea 2"];

  async loadSpec(specPath: string): Promise<Template> {
    this.calls.push(["loadSpec", specPath]);
    if (this.specFailure !== undefined) {
      throw this.specFailure;
    }
    return new Template();
  }

  async build(
    _template: Template,
    name: string,
    options: TemplateBuildRequest,
  ): Promise<BuildInfo> {
    this.calls.push([
      "build",
      name,
      options.bucket,
      options.memoryMb,
      options.force,
      options.timeoutMs,
    ]);
    return this.#finish(options);
  }

  async buildAgent(template: AgentTemplate, options: AgentBuildRequest): Promise<BuildInfo> {
    this.calls.push([
      "buildAgent",
      {
        name: template.name,
        base: template.base,
        runtimes: [...template.runtimes],
        prefetch: template.prefetch,
        kernelWarmup: template.kernelWarmup,
        memoryMib: template.memoryMib,
      },
      options.bucket,
      options.force,
      options.timeoutMs,
    ]);
    return this.#finish(options);
  }

  #finish(options: AgentBuildRequest): BuildInfo {
    for (const line of this.logLines) {
      options.onBuildLogs?.(line);
    }
    if (this.buildFailure !== undefined) {
      throw this.buildFailure;
    }
    return BUILD_INFO;
  }

  async status(name: string, version: string | undefined): Promise<BuildStatus> {
    this.calls.push(["status", name, version]);
    return this.statusResult;
  }

  async logs(name: string, limit: number): Promise<string[]> {
    this.calls.push(["logs", name, limit]);
    return this.recentLogs;
  }
}

export { TemplateSpecError };

export class FakeEvents implements LifecycleEventsTool {
  readonly calls: Call[] = [];
  deployed: StackStatus = { name: "rayito-events-webhooks", state: "CREATE_COMPLETE", outputs: {} };
  current: StackStatus | undefined;
  webhooks: WebhookInfo[] = [];
  records: EventRecord[] = [];

  async deploy(options: Parameters<LifecycleEventsTool["deploy"]>[0]): Promise<StackStatus> {
    this.calls.push(["deploy", options]);
    return this.deployed;
  }

  async status(): Promise<StackStatus | undefined> {
    this.calls.push(["status"]);
    return this.current;
  }

  async destroy(options?: { wait?: boolean }): Promise<void> {
    this.calls.push(["destroy", options]);
  }

  async registerWebhook(
    url: string,
    options: { secretName: string; types: readonly string[] },
  ): Promise<WebhookInfo> {
    this.calls.push(["registerWebhook", url, options]);
    return { webhookId: "abc123", url, types: [...options.types] };
  }

  async listWebhooks(): Promise<WebhookInfo[]> {
    this.calls.push(["listWebhooks"]);
    return this.webhooks;
  }

  async deleteWebhook(webhookId: string): Promise<void> {
    this.calls.push(["deleteWebhook", webhookId]);
  }

  async getEvents(
    options?: Parameters<LifecycleEventsTool["getEvents"]>[0],
  ): Promise<EventRecord[]> {
    this.calls.push(["getEvents", options]);
    return this.records;
  }
}

export class FakeDomain implements DomainDeployer {
  readonly calls: Call[] = [];
  deployed: StackStatus = { name: "rayito-custom-domain", state: "CREATE_COMPLETE", outputs: {} };

  constructor(readonly publicDomain: string) {}

  async deploy(options: DeployCustomDomainOptions): Promise<StackStatus> {
    this.calls.push(["deploy", options]);
    return this.deployed;
  }
}
