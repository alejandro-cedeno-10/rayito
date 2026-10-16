/**
 * El mundo en memoria que describen los vectores de `testdata/dashboard/`
 * (`api-v1-cases.json`, `guard-vectors.json`): un plano de control, un
 * historial de eventos, logs, una fuente de cuotas y otra de coste, más una
 * `DashboardApi` armada con ellos. Espejo de
 * `clients/python/tests/unit/dashboard_vectors.py`.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { Ajv2020 } from "ajv/dist/2020.js";
import type {
  ApiRequest,
  LogLine,
  LogPage,
  LogsSource,
} from "../../../src/dashboard/api/contract.js";
import type { JobQueue } from "../../../src/dashboard/api/jobs.js";
import { DashboardApi } from "../../../src/dashboard/api/router.js";
import { UsageCost } from "../../../src/dashboard/cost.js";
import type { CostQueryResult } from "../../../src/dashboard/domain.js";
import { Fleet } from "../../../src/dashboard/fleet.js";
import type { QuotaSource } from "../../../src/dashboard/ports.js";
import { Quotas } from "../../../src/dashboard/quotas.js";
import {
  AuthenticationError,
  InvalidArgumentError,
  NotFoundError,
  RateLimitError,
  SandboxError,
} from "../../../src/errors.js";
import type { EventRecord } from "../../../src/lifecycle-events/domain.js";
import { ConventionCatalog } from "../../../src/sizing/catalog.js";
import { IMAGE_ARN } from "./control-plane.js";
import {
  appliedDefaults,
  FakeClock,
  FakeCostSource,
  FakeEvents,
  FakeImageCatalog,
  FakePlane,
  FakeQuotaSource,
  listItem,
  NOW,
  RaisingError,
  sandboxId,
} from "./dashboard.js";

const TESTDATA_DASHBOARD = join(
  import.meta.dirname,
  "..",
  "..",
  "..",
  "..",
  "..",
  "testdata",
  "dashboard",
);
const EVENT_OCCURRED_AT_MS = 1_760_000_000_000;
const DEFAULT_QUOTA_MEMORY_GB = 100;
const MS_PER_SECOND = 1000;
const JOB_TIMEOUT_MS = 5000;
const JOB_POLL_MS = 5;
const AWS_MESSAGE_THAT_MUST_NOT_LEAK = "mensaje que no debe salir";
const API_PREFIX = "/api/v1";
const ENCODER = new TextEncoder();

export function loadDashboardVectors<T>(name: string): T {
  return JSON.parse(readFileSync(join(TESTDATA_DASHBOARD, name), "utf8")) as T;
}

const SCHEMA_DEFINITIONS = "$defs";
const SCHEMA_REFERENCE = "$ref";
const schema = loadDashboardVectors<Record<string, unknown>>("api-v1.schema.json");
const ajv = new Ajv2020({ strict: false, allErrors: true });

/** Valida `document` contra la definición `definition` de `api-v1.schema.json`. */
export function validateAgainstSchema(document: unknown, definition: string): void {
  const validate = ajv.compile({
    [SCHEMA_REFERENCE]: `#/${SCHEMA_DEFINITIONS}/${definition}`,
    [SCHEMA_DEFINITIONS]: schema[SCHEMA_DEFINITIONS],
  });
  if (!validate(document)) {
    throw new Error(`${definition}: ${ajv.errorsText(validate.errors)}`);
  }
}

export interface FailureSpec {
  readonly kind: string;
  readonly code?: string;
  readonly message?: string;
}

export interface World {
  readonly sandboxes?: ReadonlyArray<{ n: number; state?: string }>;
  readonly quotaMemoryGb?: number;
  readonly allowActions?: boolean;
  readonly costExplorer?: boolean;
  readonly events?: ReadonlyArray<{ n: number }> | null;
  readonly logs?: { lines: LogLine[]; truncated?: boolean; fallback?: boolean } | null;
  readonly failures?: Readonly<Record<string, FailureSpec>>;
  readonly unchanged?: readonly string[];
}

export function failureFor(spec: FailureSpec): Error {
  const message = spec.message ?? "";
  const awsCode = spec.code;
  switch (spec.kind) {
    case "aws":
      return message === AWS_MESSAGE_THAT_MUST_NOT_LEAK
        ? new RaisingError(awsCode ?? "")
        : new SandboxError(message, { awsCode });
    case "authentication":
      return new AuthenticationError(message, { awsCode });
    case "invalid_argument":
      return new InvalidArgumentError(message, { awsCode });
    case "rate_limit":
      return new RateLimitError(message, { awsCode });
    case "not_found":
      return new NotFoundError(message);
    default:
      return new Error(message);
  }
}

function eventOf(number: number): EventRecord {
  return {
    eventId: `evt-${number}`,
    sandboxId: sandboxId(number),
    kind: "killed",
    killReason: "timeout",
    generation: 1,
    occurredAtMs: EVENT_OCCURRED_AT_MS,
    imageArn: IMAGE_ARN,
    imageVersion: "1.0",
  };
}

const BILLED_RESULT: CostQueryResult = {
  periods: [
    {
      start: "2026-10-01",
      end: "2026-10-14",
      estimated: true,
      groups: [
        {
          usageType: "Lambda-MicroVM-vCPU-Second-ARM",
          costUsd: 0.5,
          quantity: 100,
          unit: "vCPU-Seconds",
        },
        { usageType: "Requests", costUsd: 9, quantity: 5, unit: "Requests" },
      ],
    },
  ],
  requests: 1,
};

export class FakeLogs implements LogsSource {
  readonly reads: Array<{ sandboxId: string; limit: number }> = [];

  constructor(
    readonly lines: readonly LogLine[] = [],
    readonly truncated = false,
    readonly fallback = false,
  ) {}

  async read(id: string, options: { readonly limit: number }): Promise<LogPage> {
    this.reads.push({ sandboxId: id, limit: options.limit });
    return { lines: this.lines, truncated: this.truncated, fallback: this.fallback };
  }
}

function planeOf(world: World): FakePlane {
  const plane = new FakePlane(
    (world.sandboxes ?? []).map((entry) => listItem(entry.n, { state: entry.state ?? "RUNNING" })),
  );
  for (const text of world.unchanged ?? []) {
    const [operation, number] = text.split(":") as [string, string];
    plane.unchanged.add(`${operation}:${sandboxId(Number(number))}`);
  }
  for (const [key, spec] of Object.entries(world.failures ?? {})) {
    const [operation, number] = key.split(":") as [string, string | undefined];
    const failure = failureFor(spec);
    if (number === undefined) {
      plane.listMicrovms = () => {
        throw failure;
      };
    } else {
      plane.fail.set(`${operation}:${sandboxId(Number(number))}`, failure);
    }
  }
  return plane;
}

/** Todo lo que un test quiere inspeccionar de una `DashboardApi`. */
export class Rig {
  readonly clock = new FakeClock();
  readonly plane: FakePlane;
  readonly costSource = new FakeCostSource(BILLED_RESULT);
  readonly logs: FakeLogs | undefined;
  readonly events: FakeEvents | undefined;
  readonly api: DashboardApi;

  constructor(
    world: World = {},
    options: { readonly quotaSource?: QuotaSource; readonly jobs?: JobQueue } = {},
  ) {
    this.plane = planeOf(world);
    this.logs =
      world.logs === undefined || world.logs === null
        ? undefined
        : new FakeLogs(
            world.logs.lines,
            world.logs.truncated ?? false,
            world.logs.fallback ?? false,
          );
    this.events =
      world.events === undefined || world.events === null
        ? undefined
        : new FakeEvents(world.events.map((entry) => eventOf(entry.n)));
    const catalog = new ConventionCatalog();
    const fleet = new Fleet({
      controlPlane: this.plane,
      images: new FakeImageCatalog(),
      events: this.events,
      allowActions: world.allowActions ?? false,
      catalog,
      monotonic: this.clock.monotonic,
      sleep: this.clock.sleep,
    });
    this.api = new DashboardApi({
      fleet,
      quotas: new Quotas({
        controlPlane: this.plane,
        source:
          options.quotaSource ??
          new FakeQuotaSource(
            appliedDefaults(undefined, {
              memoryGb: world.quotaMemoryGb ?? DEFAULT_QUOTA_MEMORY_GB,
            }),
          ),
        catalog,
      }),
      cost: new UsageCost({
        controlPlane: this.plane,
        source: this.costSource,
        catalog,
        now: () => NOW,
        monotonicMs: () => this.clock.now * MS_PER_SECOND,
      }),
      region: this.plane.region,
      jobs: options.jobs,
      logs: this.logs,
      eventsConfigured: this.events !== undefined,
      costExplorer: world.costExplorer ?? false,
      monotonicMs: () => this.clock.now * MS_PER_SECOND,
    });
  }

  /** Las llamadas de escritura que vio el plano, en orden. */
  writes(): string[] {
    return this.plane
      .operations()
      .filter((name) => ["terminateMicrovm", "suspendMicrovm", "resumeMicrovm"].includes(name));
  }

  /** Sondea el trabajo hasta que termina (la cola real corre en segundo plano). */
  async waitForJob(jobId: string): Promise<void> {
    const deadline = Date.now() + JOB_TIMEOUT_MS;
    while (Date.now() < deadline) {
      const response = await this.api.handle(apiRequest("GET", `/actions/${jobId}`));
      const job = (response.body as { job?: { status?: string } }).job;
      if (job?.status === "finished") {
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, JOB_POLL_MS));
    }
    throw new Error("el trabajo no terminó");
  }
}

export function apiRequest(
  method: string,
  path: string,
  options: {
    readonly query?: Readonly<Record<string, readonly string[]>>;
    readonly body?: unknown;
    readonly rawBody?: string;
  } = {},
): ApiRequest {
  const text =
    options.rawBody ??
    (options.body === undefined || options.body === null ? "" : JSON.stringify(options.body));
  return {
    method,
    path: `${API_PREFIX}${path}`,
    query: options.query ?? {},
    body: ENCODER.encode(text),
  };
}

/** Resuelve `{"$repeat": {...}}` y `{"$sandboxIds": N}` de los vectores. */
export function expand(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(expand);
  }
  if (typeof value === "object" && value !== null) {
    const record = value as Record<string, unknown>;
    if ("$repeat" in record) {
      const repeat = record.$repeat as { text: string; times: number };
      return repeat.text.repeat(repeat.times);
    }
    if ("$sandboxIds" in record) {
      return Array.from({ length: record.$sandboxIds as number }, (_, number) => sandboxId(number));
    }
    return Object.fromEntries(Object.entries(record).map(([key, item]) => [key, expand(item)]));
  }
  return value;
}
