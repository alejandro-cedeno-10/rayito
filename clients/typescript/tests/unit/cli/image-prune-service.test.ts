/**
 * `rayito image prune` (servicio) contra un `PrunePorts` en memoria: el plan
 * (las más nuevas, la versión con un MicroVM vivo, los builds en vuelo), un
 * conflicto que espera a la imagen y reintenta, una versión activa que se
 * desactiva primero, `--dry-run` sin llamadas que muten y la forma del
 * resumen. Espejo de `test_prune.py`; los planes compartidos con Python están
 * en `image-vectors.test.ts`.
 */

import { describe, expect, it } from "vitest";
import {
  DEFAULT_KEEP,
  DEFAULT_PRUNE_IMAGE_NAME,
  DEFAULT_WAIT_TIMEOUT_MS,
  type PruneSettings,
  validatePruneSettings,
} from "../../../src/images/prune-domain.js";
import { type PruneDeps, runPrune } from "../../../src/images/prune-service.js";
import { ImageSettingsError } from "../../../src/images/publish-domain.js";
import { FakePrunePorts, IMAGE_ARN, versionOf } from "./image-fakes.js";

const NOW = new Date("2026-09-15T16:02:07Z");
const MS_PER_SECOND = 1000;

function settings(overrides: Partial<PruneSettings> = {}): PruneSettings {
  return {
    imageName: DEFAULT_PRUNE_IMAGE_NAME,
    keep: DEFAULT_KEEP,
    dryRun: false,
    waitTimeoutMs: DEFAULT_WAIT_TIMEOUT_MS,
    ...overrides,
  };
}

function harness(ports: FakePrunePorts): {
  deps: PruneDeps;
  lines: string[];
  sleeps: number[];
} {
  const lines: string[] = [];
  const sleeps: number[] = [];
  let tick = 0;
  return {
    lines,
    sleeps,
    deps: {
      ports,
      sleep: async (ms) => {
        sleeps.push(ms);
      },
      now: () => {
        tick += MS_PER_SECOND;
        return tick;
      },
      emit: (message) => lines.push(message),
      utcNow: () => NOW,
    },
  };
}

function numbered(count: number): ReturnType<typeof versionOf>[] {
  return Array.from({ length: count }, (_, index) => versionOf(index + 1));
}

const MUTATING = /^(deleteVersion|deactivateVersion)/;

describe("el plan", () => {
  it("--dry-run informa el plan y no hace ninguna llamada que mute", async () => {
    const ports = new FakePrunePorts(numbered(8));
    ports.live = new Set(["2.0"]);
    const { deps, lines } = harness(ports);
    const result = await runPrune(deps, settings({ keep: 3, dryRun: true, waitTimeoutMs: 1000 }));
    expect(result.exitCode).toBe(0);
    expect(ports.calls.some((call) => MUTATING.test(call))).toBe(false);
    expect(result.summary).toMatchObject({
      imageArn: IMAGE_ARN,
      dryRun: true,
      kept: ["8.0", "7.0", "6.0", "2.0"],
      planned: ["1.0", "3.0", "4.0", "5.0"],
      deleted: [],
      reports: [],
      finishedAt: "2026-09-15T16:02:07Z",
    });
    expect(lines.some((line) => line.includes("keep (live microvm)"))).toBe(true);
    expect(lines[0]).toContain("8 versiones, se conservan 3, vivas 2.0");
  });

  it("borra de la más antigua a la más nueva y informa cada borrado", async () => {
    const ports = new FakePrunePorts(numbered(5));
    const { deps, lines } = harness(ports);
    const result = await runPrune(deps, settings({ keep: 2 }));
    expect(result.exitCode).toBe(0);
    expect(ports.calls.filter((call) => call.startsWith("deleteVersion"))).toEqual([
      "deleteVersion:1.0",
      "deleteVersion:2.0",
      "deleteVersion:3.0",
    ]);
    expect(result.summary).toMatchObject({ deleted: ["1.0", "2.0", "3.0"] });
    expect(lines.some((line) => line.startsWith("  1.0: deleted tras 1 intento(s)"))).toBe(true);
  });

  it("una versión que se vuelve viva entretanto se conserva", async () => {
    const ports = new FakePrunePorts(numbered(4));
    let reads = 0;
    ports.liveVersions = async () => {
      reads += 1;
      return reads === 1 ? new Set<string>() : new Set(["1.0"]);
    };
    const { deps, lines } = harness(ports);
    const result = await runPrune(deps, settings({ keep: 1 }));
    expect(ports.calls).not.toContain("deleteVersion:1.0");
    expect(result.summary).toMatchObject({ deleted: ["2.0", "3.0"] });
    expect(lines).toContain("  1.0: un MicroVM arrancó en ella entretanto; se conserva");
  });
});

describe("un borrado", () => {
  it("un conflicto espera a que la imagen se asiente y reintenta", async () => {
    const ports = new FakePrunePorts([versionOf(2), versionOf(1)]);
    ports.refusals = [
      { code: "ConflictException", message: "MicroVM Image is already in state: UPDATING" },
    ];
    ports.imageStates = ["UPDATING", "UPDATING", "UPDATED", "DELETING", "UPDATED"];
    ports.versionStatesAfterDelete = ["DELETING", undefined];
    const { deps, lines, sleeps } = harness(ports);
    const result = await runPrune(deps, settings({ keep: 1 }));
    expect(result.exitCode).toBe(0);
    expect(result.summary).toMatchObject({
      deleted: ["1.0"],
      reports: [
        { imageVersion: "1.0", outcome: "deleted", attempts: 2, conflicts: 1, deactivated: false },
      ],
    });
    expect(sleeps).toEqual([5000, 5000, 5000, 5000, 5000]);
    expect(lines).toContain("  1.0: ConflictException en el intento 1; se reintenta en 5s");
  });

  it("una versión activa rechazada se desactiva primero", async () => {
    const ports = new FakePrunePorts([versionOf(2), versionOf(1)]);
    ports.refusals = [{ code: "ValidationException", message: "version is ACTIVE" }];
    const { deps } = harness(ports);
    const result = await runPrune(deps, settings({ keep: 1 }));
    expect(ports.calls).toContain("deactivateVersion:1.0");
    expect(ports.calls.filter((call) => call === "deleteVersion:1.0")).toHaveLength(2);
    expect(result.summary).toMatchObject({
      reports: [{ outcome: "deleted", deactivated: true, attempts: 2 }],
    });
  });

  it("un rechazo que no es de conflicto ni de validación termina como rechazado y sale con 1", async () => {
    const ports = new FakePrunePorts([versionOf(2), versionOf(1)]);
    ports.refusals = [{ code: "AccessDeniedException", message: "nope" }];
    const { deps } = harness(ports);
    const result = await runPrune(deps, settings({ keep: 1, waitTimeoutMs: 1000 }));
    expect(result.exitCode).toBe(1);
    expect(result.summary).toMatchObject({
      deleted: [],
      kept: ["2.0"],
      reports: [{ outcome: "refused", lastError: "AccessDeniedException: nope" }],
    });
  });

  it("el throttling se reintenta con la espera siguiente, y tras cinco intentos falla", async () => {
    const ports = new FakePrunePorts([versionOf(2), versionOf(1)]);
    ports.refusals = Array.from({ length: 5 }, () => ({
      code: "ThrottlingException",
      message: "slow",
    }));
    const { deps, sleeps } = harness(ports);
    const result = await runPrune(deps, settings({ keep: 1 }));
    expect(result.exitCode).toBe(1);
    expect(sleeps).toEqual([5000, 10_000, 20_000, 40_000, 80_000]);
    expect(result.summary).toMatchObject({ reports: [{ outcome: "failed", attempts: 5 }] });
  });

  it("si la versión no llega a DELETED en el plazo, termina como timeout", async () => {
    const ports = new FakePrunePorts([versionOf(2), versionOf(1)]);
    ports.versionStatesAfterDelete = ["DELETING"];
    const { deps } = harness(ports);
    const result = await runPrune(deps, settings({ keep: 1, waitTimeoutMs: 20_000 }));
    expect(result.exitCode).toBe(1);
    expect(result.summary).toMatchObject({
      reports: [{ outcome: "timeout", lastError: "la versión sigue DELETING tras 20s" }],
    });
  });

  it("si la imagen no se asienta en el plazo, lo dice", async () => {
    const ports = new FakePrunePorts([versionOf(2), versionOf(1)]);
    ports.imageStates = ["UPDATING"];
    const { deps } = harness(ports);
    const result = await runPrune(deps, settings({ keep: 1, waitTimeoutMs: 20_000 }));
    expect(result.exitCode).toBe(1);
    expect(result.summary).toMatchObject({
      reports: [{ outcome: "timeout", lastError: "la imagen sigue UPDATING tras 20s" }],
    });
  });
});

describe("la configuración de la poda", () => {
  it("los valores por defecto son los de Python y --keep tiene un mínimo", () => {
    expect(settings()).toEqual({
      imageName: "rayito-base",
      keep: 5,
      dryRun: false,
      waitTimeoutMs: 600_000,
    });
    expect(() => validatePruneSettings(settings({ keep: 0 }))).toThrow(ImageSettingsError);
    expect(() => validatePruneSettings(settings({ keep: 0 }))).toThrow(/--keep/);
    expect(validatePruneSettings(settings({ keep: 1 })).keep).toBe(1);
  });
});
