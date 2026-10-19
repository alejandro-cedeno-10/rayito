/**
 * El mundo en memoria de un caso de `testdata/cli/command-cases.json`: un
 * plano de control, un catálogo de imágenes, CloudWatch Logs y un
 * provisioner de pilas que salen de la descripción JSON del caso. Es el
 * espejo de `clients/python/tests/unit/cli/command_cases_runner.py`.
 */

import type { AccountSetupGateways } from "../../../src/account-setup/ports.js";
import type {
  ControlPlane,
  ListMicrovmsOptions,
  ListMicrovmsPageOptions,
  PortSpec,
} from "../../../src/aws/control-plane.js";
import { SdkSandboxGateway } from "../../../src/cli/aws-services.js";
import type { DoctorAws } from "../../../src/cli/doctor/ports.js";
import type { TemplateTools } from "../../../src/cli/ops-ports.js";
import type { CliServices, ImageArtifacts } from "../../../src/cli/services.js";
import { CustomDomain } from "../../../src/custom-domain/service.js";
import type { ImageSummary, ImageVersionSummary } from "../../../src/dashboard/domain.js";
import type { ImageCatalog } from "../../../src/dashboard/ports.js";
import { SandboxNotFoundError } from "../../../src/errors.js";
import type { PrunePorts } from "../../../src/images/prune-service.js";
import type { PublishPorts } from "../../../src/images/publish-service.js";
import { LifecycleEvents } from "../../../src/lifecycle-events/service.js";
import { TERMINAL_STATES } from "../../../src/limits.js";
import {
  type MicrovmListPage,
  type SandboxInfo,
  type SandboxListItem,
  sandboxInfo,
  sandboxListItem,
} from "../../../src/models.js";
import type {
  EventsPage,
  GetEventsRequest,
  LogsApi,
  StreamsPage,
} from "../../../src/sandbox-logs/service.js";
import { LogGroupNotFoundError } from "../../../src/sandbox-logs/service.js";
import { OptionalStacks } from "../../../src/stacks/service.js";
import { FakeStackProvisioner } from "../m15-fake-stacks.js";

export const REGION = "us-east-1";
export const ACCOUNT_ID = "123456789012";
const KNOWN_LOG_GROUPS: ReadonlySet<string> = new Set(["/rayito/rayito-base", "/rayito/other"]);
const TOKEN_PAGE_END = "end";

type Json = Record<string, unknown>;
type JsonList = Json[];

export interface CaseWorld {
  readonly sandboxes?: JsonList;
  readonly infos?: JsonList;
  readonly missing?: string[];
  readonly images?: JsonList;
  readonly imageVersions?: Record<string, JsonList>;
  readonly artifacts?: Record<string, string>;
  readonly logs?: { streams?: string[]; events?: Record<string, JsonList> };
  readonly stacks?: Record<
    string,
    { state: string; outputs?: Record<string, string>; parameters?: Record<string, string> }
  >;
}

export function imageArn(name: string): string {
  return `arn:aws:lambda:${REGION}:${ACCOUNT_ID}:microvm-image:${name}`;
}

function listItemOf(raw: Json): SandboxListItem {
  return sandboxListItem({
    sandboxId: String(raw.id),
    state: String(raw.state),
    template: imageArn(String(raw.image)),
    templateVersion: String(raw.version),
    startedAt: new Date(String(raw.startedAt)),
  });
}

function infoOf(raw: Json): SandboxInfo {
  return sandboxInfo({
    sandboxId: String(raw.id),
    state: String(raw.state),
    endpoint: "abc.lambda-microvm.us-east-1.on.aws",
    template: imageArn(String(raw.image)),
    templateVersion: String(raw.version),
    startedAt: new Date(String(raw.startedAt)),
    maximumDurationSeconds: 3600,
    metadata: (raw.metadata ?? undefined) as Record<string, string> | undefined,
  });
}

export class CasePlane implements ControlPlane {
  readonly region = REGION;
  readonly terminated: string[] = [];
  readonly #items: SandboxListItem[];
  readonly #infos: Map<string, SandboxInfo>;
  readonly #missing: ReadonlySet<string>;

  constructor(world: CaseWorld) {
    this.#items = (world.sandboxes ?? []).map(listItemOf);
    this.#infos = new Map((world.infos ?? []).map((raw) => [String(raw.id), infoOf(raw)]));
    this.#missing = new Set(world.missing ?? []);
  }

  async resolveTemplateArn(template: string): Promise<string> {
    return template.startsWith("arn:") ? template : imageArn(template);
  }

  async getMicrovm(sandboxId: string): Promise<SandboxInfo> {
    const info = this.#infos.get(sandboxId);
    if (info === undefined || this.#missing.has(sandboxId)) {
      throw new SandboxNotFoundError(`no existe ${sandboxId}`);
    }
    return info;
  }

  async *listMicrovms(options: ListMicrovmsOptions = {}): AsyncIterable<SandboxListItem> {
    const wanted = options.states === undefined ? undefined : new Set(options.states);
    for (const item of this.#items) {
      const sameImage = options.imageArn === undefined || item.template === options.imageArn;
      const sameVersion =
        options.imageVersion === undefined || item.templateVersion === options.imageVersion;
      const stateWanted =
        wanted === undefined ? !TERMINAL_STATES.has(item.state) : wanted.has(item.state);
      if (sameImage && sameVersion && stateWanted) {
        yield item;
      }
    }
  }

  async listMicrovmsPage(options: ListMicrovmsPageOptions): Promise<MicrovmListPage> {
    const items = this.#items.filter(
      (item) =>
        (options.imageArn === undefined || item.template === options.imageArn) &&
        (options.imageVersion === undefined || item.templateVersion === options.imageVersion),
    );
    return { items: items.slice(0, options.maxResults), nextToken: undefined };
  }

  async terminateMicrovm(sandboxId: string): Promise<boolean> {
    if (this.#missing.has(sandboxId)) {
      return false;
    }
    this.terminated.push(sandboxId);
    return true;
  }

  async runMicrovm(): Promise<SandboxInfo> {
    throw new Error("la CLI nunca lanza MicroVMs en estos casos");
  }

  async suspendMicrovm(): Promise<boolean> {
    return true;
  }

  async resumeMicrovm(): Promise<boolean> {
    return true;
  }

  async createAuthToken(_sandboxId: string, _ports: readonly PortSpec[]): Promise<string> {
    throw new Error("estos casos no acuñan tokens");
  }

  async getMicrovmImageVersion(): Promise<number> {
    throw new Error("estos casos no leen la memoria de una imagen");
  }
}

export class CaseImages implements ImageCatalog, ImageArtifacts {
  readonly #images: JsonList;
  readonly #versions: Record<string, JsonList>;
  readonly #artifacts: Record<string, string>;

  constructor(world: CaseWorld) {
    this.#images = world.images ?? [];
    this.#versions = world.imageVersions ?? {};
    this.#artifacts = world.artifacts ?? {};
  }

  async listImages(
    options: { readonly nameFilter?: string | undefined } = {},
  ): Promise<ImageSummary[]> {
    const prefix = options.nameFilter ?? "";
    return this.#images
      .filter((image) => String(image.name).startsWith(prefix))
      .map((image) => ({
        name: String(image.name),
        imageArn: imageArn(String(image.name)),
        state: String(image.state),
        latestActiveVersion: (image.active ?? undefined) as string | undefined,
        latestFailedVersion: (image.failed ?? undefined) as string | undefined,
        createdAt: new Date(String(image.createdAt)),
      }));
  }

  async listVersions(arn: string): Promise<ImageVersionSummary[]> {
    const name = arn.slice(arn.lastIndexOf(":") + 1);
    return (this.#versions[name] ?? [])
      .map((version) => ({
        imageVersion: String(version.version),
        state: String(version.state),
        status: String(version.status),
        baseImageVersion: (version.baseVersion ?? undefined) as string | undefined,
        artifact: (version.artifact ?? undefined) as string | undefined,
        memoryMib: undefined,
        createdAt: new Date(String(version.createdAt)),
      }))
      .sort((left, right) => right.createdAt.getTime() - left.createdAt.getTime());
  }

  async activeArtifact(arn: string, imageVersion: string): Promise<string | undefined> {
    return this.#artifacts[`${arn.slice(arn.lastIndexOf(":") + 1)}@${imageVersion}`];
  }
}

export class CaseLogs implements LogsApi {
  readonly #streams: string[];
  readonly #events: Record<string, JsonList>;

  constructor(world: CaseWorld) {
    this.#streams = world.logs?.streams ?? [];
    this.#events = world.logs?.events ?? {};
  }

  async describeStreams(request: { group: string; prefix?: string }): Promise<StreamsPage> {
    if (!KNOWN_LOG_GROUPS.has(request.group)) {
      throw new LogGroupNotFoundError();
    }
    const prefix = request.prefix ?? "";
    return {
      names: this.#streams.filter((name) => name.startsWith(prefix)),
      nextToken: undefined,
    };
  }

  async getEvents(request: GetEventsRequest): Promise<EventsPage> {
    if (request.nextToken === TOKEN_PAGE_END) {
      return { events: [], nextForwardToken: TOKEN_PAGE_END };
    }
    const start = request.startTimeMs ?? 0;
    const events = (this.#events[request.stream] ?? [])
      .filter((event) => Number(event.timestamp) >= start)
      .map((event) => ({ timestamp: Number(event.timestamp), message: String(event.message) }));
    return { events, nextForwardToken: TOKEN_PAGE_END };
  }
}

/** Lo que estos casos no ejecutan: tocarlo es un error del test. */
function unused<T extends object>(what: string): T {
  return new Proxy({} as T, {
    get(): never {
      throw new Error(`estos casos no ejecutan ${what}`);
    },
  });
}

export interface BuiltWorld {
  readonly services: CliServices;
  readonly plane: CasePlane;
  readonly provisioner: FakeStackProvisioner;
}

export function buildWorld(world: CaseWorld): BuiltWorld {
  const plane = new CasePlane(world);
  const provisioner = new FakeStackProvisioner();
  for (const [name, stack] of Object.entries(world.stacks ?? {})) {
    provisioner.stacks.set(name, {
      name,
      state: stack.state,
      outputs: stack.outputs ?? {},
      parameters: stack.parameters ?? {},
    });
  }
  const images = new CaseImages(world);
  const services: CliServices = {
    region: REGION,
    controlPlane: plane,
    sandboxes: new SdkSandboxGateway(plane, REGION, undefined),
    stacks: new OptionalStacks({ provisioner }),
    images,
    imageArtifacts: images,
    logs: new CaseLogs(world),
    doctor: unused<DoctorAws>("doctor"),
    publishPorts: unused<PublishPorts>("image publish"),
    accountSetup: unused<AccountSetupGateways>("setup"),
    prunePorts: unused<PrunePorts>("image prune"),
    templates: unused<TemplateTools>("template"),
    lifecycleEvents: (stackName) =>
      new LifecycleEvents({ stackName, stacks: new OptionalStacks({ provisioner }) }),
    customDomain: (request) =>
      new CustomDomain({
        publicDomain: request.publicDomain,
        stackName: request.stackName,
        provisioner,
      }),
  };
  return { services, plane, provisioner };
}
