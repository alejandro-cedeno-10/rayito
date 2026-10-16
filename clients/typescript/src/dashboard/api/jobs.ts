/**
 * La cola de acciones masivas del panel: un único trabajador ejecuta los
 * trabajos de uno en uno con `Fleet.runBulk`, que ya respeta el TPS de la
 * operación; al ser uno solo, dos trabajos nunca suman tasa. Cada trabajo
 * guarda su progreso para que la SPA lo consulte. Sólo guarda ids,
 * resultados y códigos de AWS. Espejo de `rayito._dashboard._api._jobs`.
 */

import { DASHBOARD_JOBS_ACTIVE_MAX, DASHBOARD_JOBS_RETAINED } from "../../limits.js";
import type { BulkAction, BulkItemResult, BulkOutcome, BulkPlan, BulkProgress } from "../domain.js";
import type { RunBulkOptions } from "../fleet.js";
import { isoUtc, type JsonObject } from "./contract.js";
import { planJson } from "./serialize.js";

export type JobStatus = "queued" | "running" | "finished";

const OUTCOMES: readonly BulkOutcome[] = ["done", "unchanged", "failed"];
/**
 * Código del resultado de los sandboxes que un trabajo abortado no llegó a
 * tocar (un fallo inesperado de la cola, no de AWS).
 */
export const ABORTED_CODE = "JobAborted";
const FIRST_JOB_NUMBER = 1;

/**
 * Lo que `onProgress` lanza al cerrar la cola para cortar el trabajo en
 * curso al terminar su sandbox actual; nunca sale de `BulkJobs`.
 */
class JobsClosing extends Error {}

/**
 * Ya hay `DASHBOARD_JOBS_ACTIVE_MAX` trabajos sin terminar: la cola no crece
 * sin tope aunque la SPA (o un script) siga enviando acciones.
 */
export class JobsBusyError extends Error {
  constructor() {
    super("hay demasiados trabajos sin terminar");
    this.name = "JobsBusyError";
  }
}

interface BulkJob {
  readonly jobId: string;
  readonly action: BulkAction;
  readonly sandboxIds: readonly string[];
  readonly plan: BulkPlan;
  readonly appliedRate: number | undefined;
  readonly createdAt: Date;
  status: JobStatus;
  readonly items: BulkItemResult[];
}

export function jobJson(job: BulkJob): JsonObject {
  const counts = Object.fromEntries(
    OUTCOMES.map((outcome) => [
      outcome,
      job.items.filter((item) => item.outcome === outcome).length,
    ]),
  );
  return {
    jobId: job.jobId,
    action: job.action,
    status: job.status,
    createdAt: isoUtc(job.createdAt),
    total: job.sandboxIds.length,
    completed: job.items.length,
    counts,
    plan: planJson(job.plan),
    items: job.items.map((item) => ({
      sandboxId: item.sandboxId,
      outcome: item.outcome,
      code: item.code ?? null,
    })),
  };
}

/** Marca como fallidos, con `ABORTED_CODE`, los sandboxes que un trabajo interrumpido no tocó. */
function abortUnreached(job: BulkJob): void {
  const reached = new Set(job.items.map((item) => item.sandboxId));
  for (const sandboxId of job.sandboxIds) {
    if (!reached.has(sandboxId)) {
      job.items.push({ sandboxId, outcome: "failed", code: ABORTED_CODE });
    }
  }
}

/** Lo único que la cola pide a `Fleet`: aplicar una acción a unos sandboxes al ritmo del TPS. */
export interface BulkRunner {
  runBulk(
    action: BulkAction,
    sandboxIds: Iterable<string>,
    options: RunBulkOptions,
  ): Promise<unknown>;
}

export interface JobSubmission {
  readonly plan: BulkPlan;
  readonly appliedRate: number | undefined;
}

/**
 * Lo que `DashboardApi` pide a la cola de acciones: encolar, consultar por
 * id, listar y cerrar.
 */
export interface JobQueue {
  submit(action: BulkAction, sandboxIds: readonly string[], submission: JobSubmission): JsonObject;
  get(jobId: string): JsonObject | undefined;
  snapshots(): JsonObject[];
  close(): Promise<void>;
}

export interface BulkJobsOptions {
  readonly retained?: number;
  readonly activeMax?: number;
  readonly clock?: () => Date;
  /** Se llama al acabar cada trabajo (el servidor invalida la caché de lecturas). */
  readonly onFinished?: () => void;
}

/** Registro y cola de trabajos sobre `Fleet.runBulk`. */
export class BulkJobs implements JobQueue {
  readonly #fleet: BulkRunner;
  readonly #retained: number;
  readonly #activeMax: number;
  readonly #clock: () => Date;
  readonly #onFinished: () => void;
  readonly #jobs = new Map<string, BulkJob>();
  readonly #queue: BulkJob[] = [];
  #sequence = FIRST_JOB_NUMBER;
  #draining: Promise<void> | undefined;
  #closing = false;

  constructor(fleet: BulkRunner, options: BulkJobsOptions = {}) {
    this.#fleet = fleet;
    this.#retained = options.retained ?? DASHBOARD_JOBS_RETAINED;
    this.#activeMax = options.activeMax ?? DASHBOARD_JOBS_ACTIVE_MAX;
    this.#clock = options.clock ?? (() => new Date());
    this.#onFinished = options.onFinished ?? (() => undefined);
  }

  submit(action: BulkAction, sandboxIds: readonly string[], submission: JobSubmission): JsonObject {
    if (this.#active() >= this.#activeMax) {
      throw new JobsBusyError();
    }
    const job: BulkJob = {
      jobId: `job-${this.#sequence++}`,
      action,
      sandboxIds: [...sandboxIds],
      plan: submission.plan,
      appliedRate: submission.appliedRate,
      createdAt: this.#clock(),
      status: "queued",
      items: [],
    };
    this.#jobs.set(job.jobId, job);
    this.#evictFinished();
    const snapshot = jobJson(job);
    this.#queue.push(job);
    this.#draining ??= this.#drain();
    return snapshot;
  }

  get(jobId: string): JsonObject | undefined {
    const job = this.#jobs.get(jobId);
    return job === undefined ? undefined : jobJson(job);
  }

  snapshots(): JsonObject[] {
    return [...this.#jobs.values()].reverse().map(jobJson);
  }

  /**
   * Para el trabajador: el sandbox en curso termina, el resto del trabajo en
   * curso y todos los encolados quedan `JobAborted` sin tocarse, para que
   * cerrar el panel no siga matando ni suspendiendo.
   */
  async close(): Promise<void> {
    this.#closing = true;
    await this.#draining;
  }

  #active(): number {
    return [...this.#jobs.values()].filter((job) => job.status !== "finished").length;
  }

  #evictFinished(): void {
    while (this.#jobs.size > this.#retained) {
      const oldest = [...this.#jobs.values()].find((job) => job.status === "finished");
      if (oldest === undefined) {
        return;
      }
      this.#jobs.delete(oldest.jobId);
    }
  }

  async #drain(): Promise<void> {
    try {
      for (let job = this.#queue.shift(); job !== undefined; job = this.#queue.shift()) {
        await this.#execute(job);
      }
    } finally {
      this.#draining = undefined;
    }
  }

  async #execute(job: BulkJob): Promise<void> {
    job.status = "running";
    try {
      if (this.#closing) {
        throw new JobsClosing();
      }
      await this.#fleet.runBulk(job.action, job.sandboxIds, {
        appliedRate: job.appliedRate,
        onProgress: (progress) => this.#record(job, progress),
      });
    } catch {
      abortUnreached(job);
    }
    job.status = "finished";
    this.#onFinished();
  }

  #record(job: BulkJob, progress: BulkProgress): void {
    job.items.push(progress.last);
    if (this.#closing) {
      throw new JobsClosing();
    }
  }
}
