/**
 * Fakes en memoria de los puertos del panel de gestión: un plano de control
 * (inventario, acciones, memoria por versión de imagen), una fuente de
 * cuotas, una de coste, un catálogo de imágenes y un historial de eventos.
 * Cada fake anota sus llamadas, así los tests comprueban qué se llamó y,
 * sobre todo, qué no.
 */

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type {
  AppliedQuota,
  CostQueryResult,
  ImageSummary,
  ImageVersionSummary,
} from "../../../src/dashboard/domain.js";
import { quotaDefault } from "../../../src/dashboard/domain.js";
import type {
  CostQuery,
  CostSource,
  EventsHistory,
  EventsQuery,
  ImageCatalog,
  ManagementPlane,
  QuotaSource,
} from "../../../src/dashboard/ports.js";
import { SandboxError, SandboxNotFoundError } from "../../../src/errors.js";
import type { EventRecord } from "../../../src/lifecycle-events/domain.js";
import { QUOTA_NAMES } from "../../../src/limits.js";
import { type SandboxListItem, sandboxListItem } from "../../../src/models.js";
import { IMAGE_ARN, REGION } from "./control-plane.js";

export const NOW = new Date(Date.UTC(2026, 9, 13, 12, 0, 0));
export const OTHER_IMAGE_ARN = IMAGE_ARN.replace("rayito-base-2gb", "rayito-base-8gb");
const HERE = dirname(fileURLToPath(import.meta.url));
const VECTORS_DIR = resolve(HERE, "../../../../../testdata/dashboard");

export function loadVectors<T>(name: string): T {
  return JSON.parse(readFileSync(resolve(VECTORS_DIR, name), "utf8")) as T;
}

export function sandboxId(number: number): string {
  return `microvm-00000000-0000-0000-0000-${String(number).padStart(12, "0")}`;
}

export function listItem(
  number: number,
  options: {
    state?: string;
    imageArn?: string;
    version?: string;
    startedAt?: Date;
  } = {},
): SandboxListItem {
  return sandboxListItem({
    sandboxId: sandboxId(number),
    state: options.state ?? "RUNNING",
    template: options.imageArn ?? IMAGE_ARN,
    templateVersion: options.version ?? "1.0",
    startedAt: options.startedAt ?? NOW,
  });
}

/** Un error con la forma de los del SDK de AWS v3: `name` es el código y lleva `$metadata`. */
export function awsSdkError(message: string, code: string): Error {
  const error = Object.assign(new Error(message), { name: code });
  Object.defineProperty(error, "$metadata", { value: {} });
  return error;
}

/** Una excepción de AWS con código, para las acciones que fallan. */
export class RaisingError extends SandboxError {
  constructor(code: string) {
    super("mensaje que no debe salir", { awsCode: code });
  }
}

export class FakePlane implements ManagementPlane {
  readonly region: string;
  readonly calls: Array<readonly [string, unknown]> = [];
  readonly fail = new Map<string, Error>();
  readonly unchanged = new Set<string>();
  readonly memory: Map<string, number>;
  items: SandboxListItem[];

  constructor(
    items: readonly SandboxListItem[] = [],
    memory: Record<string, number> = { [`${IMAGE_ARN}@1.0`]: 2048 },
    region: string = REGION,
  ) {
    this.items = [...items];
    this.memory = new Map(Object.entries(memory));
    this.region = region;
  }

  async resolveTemplateArn(template: string): Promise<string> {
    this.calls.push(["resolveTemplateArn", template]);
    return template.startsWith("arn:") ? template : IMAGE_ARN;
  }

  async *listMicrovms(
    options: { states?: readonly string[] | undefined; imageArn?: string | undefined } = {},
  ): AsyncIterable<SandboxListItem> {
    const wanted = options.states === undefined ? undefined : new Set(options.states);
    this.calls.push(["listMicrovms", wanted === undefined ? undefined : [...wanted].sort()]);
    for (const item of this.items) {
      if (options.imageArn !== undefined && item.template !== options.imageArn) {
        continue;
      }
      if (wanted === undefined && ["TERMINATING", "TERMINATED"].includes(item.state)) {
        continue;
      }
      if (wanted !== undefined && !wanted.has(item.state)) {
        continue;
      }
      yield item;
    }
  }

  async getMicrovmImageVersion(imageArn: string, imageVersion: string): Promise<number> {
    this.calls.push(["getMicrovmImageVersion", [imageArn, imageVersion]]);
    const mib = this.memory.get(`${imageArn}@${imageVersion}`);
    if (mib === undefined) {
      throw new SandboxNotFoundError("versión de imagen desconocida");
    }
    return mib;
  }

  terminateMicrovm(sandbox: string): Promise<boolean> {
    return this.#act("terminateMicrovm", sandbox);
  }

  suspendMicrovm(sandbox: string): Promise<boolean> {
    return this.#act("suspendMicrovm", sandbox);
  }

  resumeMicrovm(sandbox: string): Promise<boolean> {
    return this.#act("resumeMicrovm", sandbox);
  }

  async #act(operation: string, sandbox: string): Promise<boolean> {
    this.calls.push([operation, sandbox]);
    const failure = this.fail.get(`${operation}:${sandbox}`);
    if (failure !== undefined) {
      throw failure;
    }
    return !this.unchanged.has(`${operation}:${sandbox}`);
  }

  operations(): string[] {
    return this.calls.map(([name]) => name);
  }
}

export class FakeQuotaSource implements QuotaSource {
  calls = 0;

  constructor(readonly applied: readonly AppliedQuota[]) {}

  async listAppliedQuotas(): Promise<AppliedQuota[]> {
    this.calls += 1;
    return [...this.applied];
  }
}

export class FakeCostSource implements CostSource {
  readonly queries: CostQuery[] = [];

  constructor(readonly result: CostQueryResult) {}

  async getCostAndUsage(query: CostQuery): Promise<CostQueryResult> {
    this.queries.push(query);
    return this.result;
  }
}

export class FakeImageCatalog implements ImageCatalog {
  readonly calls: Array<readonly [string, unknown]> = [];

  async listImages(options: { nameFilter?: string | undefined } = {}): Promise<ImageSummary[]> {
    this.calls.push(["listImages", options.nameFilter]);
    return [
      {
        name: "rayito-base",
        imageArn: IMAGE_ARN,
        state: "CREATED",
        latestActiveVersion: "2.0",
        latestFailedVersion: undefined,
        createdAt: NOW,
      },
    ];
  }

  async listVersions(imageArn: string): Promise<ImageVersionSummary[]> {
    this.calls.push(["listVersions", imageArn]);
    return [
      {
        imageVersion: "2.0",
        state: "SUCCESSFUL",
        status: "ACTIVE",
        baseImageVersion: "1",
        artifact: "artifact.zip",
        memoryMib: 2048,
        createdAt: NOW,
      },
    ];
  }
}

export class FakeEvents implements EventsHistory {
  readonly calls: EventsQuery[] = [];

  constructor(readonly records: readonly EventRecord[] = []) {}

  async getEvents(options: EventsQuery = {}): Promise<EventRecord[]> {
    this.calls.push(options);
    return [...this.records];
  }
}

/** Todas las cuotas del catálogo en su valor por defecto de `region`. */
export function appliedDefaults(
  region: string = REGION,
  options: { memoryGb?: number } = {},
): AppliedQuota[] {
  return Object.entries(QUOTA_NAMES).map(([code, name]) => ({
    code,
    name,
    value:
      options.memoryGb !== undefined && code === "L-CD1C0CC4"
        ? options.memoryGb
        : quotaDefault(code, region),
    adjustable: true,
  }));
}

/** Un reloj monótono manual en segundos con su `sleep`. */
export class FakeClock {
  now = 1_000_000;
  readonly sleeps: number[] = [];

  readonly monotonic = (): number => this.now;

  readonly sleep = async (seconds: number): Promise<void> => {
    this.sleeps.push(seconds);
    this.now += seconds;
  };

  advance(seconds: number): void {
    this.now += seconds;
  }
}
