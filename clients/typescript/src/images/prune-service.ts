/**
 * `rayito image prune` (aplicación): borra versiones antiguas de una imagen de
 * una en una. Cada `delete-microvm-image-version` mueve la imagen por
 * `UPDATING` y `DELETING`, y un borrado en ese intervalo falla con
 * `ConflictException` (`AWS_API_NOTES.md` §4), así que tras cada borrado se
 * espera a que la versión esté `DELETED` (o ya no exista) **y** a que la imagen
 * salga de `UPDATING`/`DELETING`. Un conflicto espera a que la imagen se asiente
 * y reintenta con esperas de 5/10/20/40/80 s, como mucho cinco veces. Si el
 * servicio se niega a borrar una versión `ACTIVE`, se desactiva primero
 * (`update-microvm-image-version --status INACTIVE`) y se reintenta.
 *
 * Sólo se borran versiones, nunca la imagen. Espejo de `rayito.cli._prune`.
 */

import { MS_PER_SECOND } from "../units.js";
import { ACTIVE_VERSION_STATUS, accountImageArn } from "./gateway.js";
import {
  BUSY_IMAGE_STATES,
  buildPlan,
  CONFLICT_CODE,
  type DeleteReport,
  formatTable,
  GONE_VERSION_STATE,
  newDeleteReport,
  type Plan,
  POLL_INTERVAL_MS,
  type PruneSettings,
  pruneSummary,
  REFUSED_ACTIVE_CODES,
  RETRY_BACKOFF_MS,
  THROTTLED_CODE,
  type Version,
} from "./prune-domain.js";

/** AWS rechazó una operación sobre una versión; `code` es su código de error y `message` ya está redactado. */
export class PruneRefusal extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = "PruneRefusal";
    this.code = code;
  }
}

export interface PrunePorts {
  readonly region: string;
  accountId(): Promise<string>;
  /** Todas las versiones de la imagen, la más nueva primero. */
  listVersions(arn: string): Promise<Version[]>;
  /** Las versiones en las que corre un MicroVM que no está terminado ni terminándose. */
  liveVersions(arn: string): Promise<Set<string>>;
  imageState(arn: string): Promise<string>;
  /** `undefined` cuando la versión ya no existe. */
  versionState(arn: string, imageVersion: string): Promise<string | undefined>;
  /** Un `PruneRefusal` si AWS lo rechaza; una versión que ya no existe no es un error. */
  deleteVersion(arn: string, imageVersion: string): Promise<void>;
  deactivateVersion(arn: string, imageVersion: string): Promise<void>;
}

export interface PruneDeps {
  readonly ports: PrunePorts;
  readonly sleep: (ms: number) => Promise<void>;
  /** Reloj monótono en milisegundos. */
  readonly now: () => number;
  readonly emit: (message: string) => void;
  readonly utcNow: () => Date;
}

function refusalText(error: PruneRefusal): string {
  return `${error.code}: ${error.message}`;
}

class Pruner {
  readonly #deps: PruneDeps;
  readonly #arn: string;
  readonly #waitTimeoutMs: number;

  constructor(deps: PruneDeps, arn: string, waitTimeoutMs: number) {
    this.#deps = deps;
    this.#arn = arn;
    this.#waitTimeoutMs = waitTimeoutMs;
  }

  async #waitUntilImageSettled(report: DeleteReport): Promise<boolean> {
    const { ports, now, sleep } = this.#deps;
    const started = now();
    for (;;) {
      const state = await ports.imageState(this.#arn);
      if (!BUSY_IMAGE_STATES.has(state)) {
        report.waitedMs += now() - started;
        return true;
      }
      if (now() - started >= this.#waitTimeoutMs) {
        report.waitedMs += now() - started;
        report.lastError = `la imagen sigue ${state} tras ${Math.round(this.#waitTimeoutMs / MS_PER_SECOND)}s`;
        return false;
      }
      await sleep(POLL_INTERVAL_MS);
    }
  }

  async #waitUntilVersionGone(report: DeleteReport): Promise<boolean> {
    const { ports, now, sleep } = this.#deps;
    const started = now();
    for (;;) {
      const state = await ports.versionState(this.#arn, report.imageVersion);
      if (state === undefined || state === GONE_VERSION_STATE) {
        report.waitedMs += now() - started;
        return this.#waitUntilImageSettled(report);
      }
      if (now() - started >= this.#waitTimeoutMs) {
        report.waitedMs += now() - started;
        report.lastError = `la versión sigue ${state} tras ${Math.round(this.#waitTimeoutMs / MS_PER_SECOND)}s`;
        return false;
      }
      await sleep(POLL_INTERVAL_MS);
    }
  }

  /** Un `delete-microvm-image-version`; el código de error si lo rechazaron. */
  async #deleteOnce(report: DeleteReport): Promise<string | undefined> {
    try {
      await this.#deps.ports.deleteVersion(this.#arn, report.imageVersion);
    } catch (error) {
      if (error instanceof PruneRefusal) {
        report.lastError = refusalText(error);
        return error.code;
      }
      throw error;
    }
    return undefined;
  }

  /** Sólo cuando el servicio se negó a borrar una versión `ACTIVE`. */
  async #deactivate(report: DeleteReport): Promise<void> {
    try {
      await this.#deps.ports.deactivateVersion(this.#arn, report.imageVersion);
    } catch (error) {
      if (error instanceof PruneRefusal) {
        report.lastError = refusalText(error);
        return;
      }
      throw error;
    }
    report.deactivated = true;
    await this.#waitUntilImageSettled(report);
  }

  async prune(version: Version): Promise<DeleteReport> {
    const report = newDeleteReport(version.imageVersion);
    for (const [index, backoffMs] of RETRY_BACKOFF_MS.entries()) {
      const attempt = index + 1;
      report.attempts = attempt;
      const refused = await this.#deleteOnce(report);
      if (refused === undefined) {
        report.outcome = (await this.#waitUntilVersionGone(report)) ? "deleted" : "timeout";
        return report;
      }
      if (refused === CONFLICT_CODE) {
        report.conflicts += 1;
        await this.#waitUntilImageSettled(report);
      } else if (REFUSED_ACTIVE_CODES.has(refused) && version.status === ACTIVE_VERSION_STATUS) {
        await this.#deactivate(report);
      } else if (refused !== THROTTLED_CODE) {
        report.outcome = "refused";
        return report;
      }
      this.#deps.emit(
        `  ${version.imageVersion}: ${refused} en el intento ${attempt}; se reintenta en ` +
          `${Math.round(backoffMs / MS_PER_SECOND)}s`,
      );
      await this.#deps.sleep(backoffMs);
    }
    report.outcome = "failed";
    return report;
  }
}

export interface PruneResult {
  /** El documento final (también en `--dry-run`, con la lista vacía de informes). */
  readonly summary: Record<string, unknown>;
  /** 0 si todo lo planeado se borró; 1 si algún candidato sigue ahí tras sus intentos. */
  readonly exitCode: number;
}

function describePlan(
  deps: PruneDeps,
  arn: string,
  versions: number,
  keep: number,
  live: Set<string>,
): void {
  deps.emit(
    `${arn}: ${versions} versiones, se conservan ${keep}, vivas ${[...live].sort().join(", ") || "ninguna"}`,
  );
}

/** El plan, los borrados serializados y el resumen; `emit` recibe la tabla y el progreso. */
export async function runPrune(deps: PruneDeps, settings: PruneSettings): Promise<PruneResult> {
  const { ports } = deps;
  const arn = accountImageArn(ports.region, await ports.accountId(), settings.imageName);
  const versions = await ports.listVersions(arn);
  const live = await ports.liveVersions(arn);
  const plan: Plan = buildPlan(versions, live, settings.keep);
  describePlan(deps, arn, versions.length, settings.keep, live);
  for (const line of formatTable(plan)) {
    deps.emit(line);
  }
  const reports: DeleteReport[] = [];
  if (settings.dryRun) {
    return {
      summary: pruneSummary({ arn, plan, reports, dryRun: true, finishedAt: deps.utcNow() }),
      exitCode: 0,
    };
  }
  const pruner = new Pruner(deps, arn, settings.waitTimeoutMs);
  for (const version of plan.delete) {
    if ((await ports.liveVersions(arn)).has(version.imageVersion)) {
      deps.emit(`  ${version.imageVersion}: un MicroVM arrancó en ella entretanto; se conserva`);
      continue;
    }
    const started = deps.now();
    const report = await pruner.prune(version);
    reports.push(report);
    deps.emit(
      `  ${version.imageVersion}: ${report.outcome} tras ${report.attempts} intento(s), ` +
        `${report.conflicts} conflicto(s), esperó ${Math.round(report.waitedMs / MS_PER_SECOND)}s ` +
        `(${Math.round((deps.now() - started) / MS_PER_SECOND)}s en total)`,
    );
  }
  return {
    summary: pruneSummary({ arn, plan, reports, dryRun: false, finishedAt: deps.utcNow() }),
    exitCode: reports.every((report) => report.outcome === "deleted") ? 0 : 1,
  };
}
