/**
 * Dominio puro del panel (`src/dashboard/domain.ts`): los vectores
 * compartidos con Python (`testdata/dashboard/`), la caché con caducidad y
 * la frontera hexagonal (el dominio y los puertos no importan
 * infraestructura).
 */

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  type AppliedQuota,
  buildQuotaReport,
  type CostPeriod,
  estimateComputeUsd,
  estimateSandboxCost,
  foldBilledCost,
  microvmComponent,
  planBulk,
  quotaDefault,
  TtlCache,
} from "../../src/dashboard/domain.js";
import { QUOTA_DEFAULTS, QUOTA_NAMES } from "../../src/limits.js";
import { loadVectors } from "./fake/dashboard.js";

interface QuotaCase {
  name: string;
  region: string;
  drop: string[];
  override: Record<string, number>;
  fleet: Array<{ memoryMiB: number | null; count: number }>;
  expected: {
    memory: {
      allocatedGb: number;
      quotaGb: number | null;
      fraction: number | null;
      level: string;
      sandboxes: number;
      unresolved: number;
    };
    warnings: Array<{ kind: string; code: string }>;
    suspendRate: number;
  };
}

interface CostCase {
  name: string;
  periods: Array<{
    start: string;
    end: string;
    estimated: boolean;
    groups: Array<{ usageType: string; costUsd: number; quantity: number; unit: string }>;
  }>;
  expected: {
    lines: Array<{
      usageType: string;
      component: string;
      quantity: number;
      unit: string;
      costUsd: number;
    }>;
    totalUsd: number;
    partial: boolean;
  };
}

const QUOTA_VECTORS = loadVectors<{ cases: QuotaCase[] }>("quota-report-vectors.json").cases;
const COST_VECTORS = loadVectors<{
  billed: CostCase[];
  estimates: Array<{ memoryMiB: number; seconds: number; usd: number }>;
}>("cost-vectors.json");
const BULK_VECTORS = loadVectors<{
  cases: Array<{
    action: "kill" | "suspend" | "resume";
    count: number;
    appliedRate: number | null;
    tps: number;
    estimatedSeconds: number;
  }>;
}>("bulk-plan-vectors.json").cases;

function appliedFor(testCase: QuotaCase): AppliedQuota[] {
  return Object.keys(QUOTA_NAMES)
    .filter((code) => !testCase.drop.includes(code))
    .map((code) => ({
      code,
      name: QUOTA_NAMES[code] as string,
      value: testCase.override[code] ?? quotaDefault(code, testCase.region),
      adjustable: true,
    }));
}

function memorySamples(fleet: QuotaCase["fleet"]): Array<number | undefined> {
  return fleet.flatMap((group) =>
    Array.from({ length: group.count }, () => group.memoryMiB ?? undefined),
  );
}

describe("quota report vectors", () => {
  it.each(QUOTA_VECTORS.map((c) => [c.name, c] as const))("%s", (_name, testCase) => {
    const report = buildQuotaReport(
      testCase.region,
      appliedFor(testCase),
      memorySamples(testCase.fleet),
    );
    const expected = testCase.expected;
    expect(report.memory.allocatedGb).toBeCloseTo(expected.memory.allocatedGb, 9);
    expect(report.memory.quotaGb).toBe(expected.memory.quotaGb ?? undefined);
    if (expected.memory.fraction === null) {
      expect(report.memory.fraction).toBeUndefined();
    } else {
      expect(report.memory.fraction).toBeCloseTo(expected.memory.fraction, 9);
    }
    expect(report.memory.level).toBe(expected.memory.level);
    expect(report.memory.sandboxes).toBe(expected.memory.sandboxes);
    expect(report.memory.unresolved).toBe(expected.memory.unresolved);
    const key = (w: { kind: string; code: string }) => `${w.kind}:${w.code}`;
    expect(report.warnings.map(key).sort()).toEqual(expected.warnings.map(key).sort());
    expect(report.appliedRate("SuspendMicrovm")).toBe(expected.suspendRate);
  });

  it("a warning carries the increase link and no account data", () => {
    const testCase = QUOTA_VECTORS[1] as QuotaCase;
    const report = buildQuotaReport(
      testCase.region,
      appliedFor(testCase),
      memorySamples(testCase.fleet),
    );
    const warning = report.warnings.find((w) => w.kind === "memory_high");
    expect(warning?.increaseUrl).toBe(
      "https://eu-west-1.console.aws.amazon.com/servicequotas/home/services/lambda/quotas/L-CD1C0CC4",
    );
    expect(warning?.message).toContain("80 %");
  });

  it("the catalog is complete and tps pairs rate with burst", () => {
    expect(Object.keys(QUOTA_NAMES).sort()).toEqual(Object.keys(QUOTA_DEFAULTS).sort());
    const testCase = QUOTA_VECTORS[0] as QuotaCase;
    const report = buildQuotaReport("us-east-1", appliedFor(testCase), []);
    const suspend = report.tps.find((limit) => limit.operation === "SuspendMicrovm");
    expect([suspend?.rate, suspend?.burst]).toEqual([2, 2]);
    expect(report.tps.map((limit) => limit.operation).sort()).toEqual([
      "CreateMicrovmAuthToken",
      "CreateMicrovmShellAuthToken",
      "GetMicrovm",
      "ResumeMicrovm",
      "RunMicrovm",
      "SuspendMicrovm",
      "TerminateMicrovm",
    ]);
  });
});

describe("billed cost vectors", () => {
  it.each(COST_VECTORS.billed.map((c) => [c.name, c] as const))("%s", (_name, testCase) => {
    const periods: CostPeriod[] = testCase.periods;
    const cost = foldBilledCost(
      { periods, requests: 1 },
      {
        start: "2026-10-01",
        end: "2026-11-02",
        region: undefined,
        fetchedAt: new Date(Date.UTC(2026, 9, 13)),
      },
    );
    expect(cost.lines).toEqual(testCase.expected.lines);
    expect(cost.totalUsd).toBe(testCase.expected.totalUsd);
    expect(cost.partial).toBe(testCase.expected.partial);
    expect(cost.label).toBe("billed");
  });

  it.each(COST_VECTORS.estimates.map((e) => [`${e.memoryMiB} MiB ${e.seconds}s`, e] as const))(
    "estimated compute %s",
    (_name, vector) => {
      expect(estimateComputeUsd(vector.memoryMiB, vector.seconds)).toBeCloseTo(vector.usd, 6);
    },
  );

  it("microvmComponent ignores other Lambda usage types", () => {
    expect(microvmComponent("Lambda-MicroVM-vCPU-Second-ARM")).toBe("vcpu");
    expect(microvmComponent("USE2-Lambda-MicroVM-Snapshot-Write-GB")).toBe("snapshot-write");
    expect(microvmComponent("Request")).toBeUndefined();
    expect(microvmComponent("Lambda-GB-Second-ARM")).toBeUndefined();
  });

  it("only running sandboxes with known memory get an estimate", () => {
    const started = new Date(Date.UTC(2026, 9, 13, 11));
    const now = new Date(Date.UTC(2026, 9, 13, 12));
    const running = estimateSandboxCost("a", "RUNNING", started, 2048, now);
    expect(running.activeSeconds).toBe(3600);
    expect(running.costUsd).toBeCloseTo(0.1261, 6);
    expect(running.label).toBe("estimated");
    for (const [state, memory] of [
      ["SUSPENDED", 2048],
      ["RUNNING", undefined],
    ] as const) {
      const skipped = estimateSandboxCost("b", state, started, memory, now);
      expect([skipped.activeSeconds, skipped.costUsd]).toEqual([undefined, undefined]);
      expect(skipped.label).toBe("estimated");
    }
  });

  it("a clock before the start never gives a negative estimate", () => {
    const started = new Date(Date.UTC(2026, 9, 13, 13));
    const now = new Date(Date.UTC(2026, 9, 13, 12));
    expect(estimateSandboxCost("a", "RUNNING", started, 1024, now).costUsd).toBe(0);
  });
});

describe("bulk plan vectors", () => {
  it.each(BULK_VECTORS.map((c) => [`${c.action} x${c.count} @${c.appliedRate}`, c] as const))(
    "%s",
    (_name, testCase) => {
      const plan = planBulk(testCase.action, testCase.count, testCase.appliedRate ?? undefined);
      expect(plan.tps).toBe(testCase.tps);
      expect(plan.estimatedSeconds).toBeCloseTo(testCase.estimatedSeconds, 9);
    },
  );
});

describe("TtlCache", () => {
  it("expires entries after the ttl", () => {
    let now = 0;
    const cache = new TtlCache<string, number>(10_000, () => now);
    expect(cache.get("a")).toBeUndefined();
    cache.put("a", 1);
    now = 9_900;
    expect(cache.get("a")).toBe(1);
    now = 10_000;
    expect(cache.get("a")).toBeUndefined();
  });
});

describe("hexagonal boundary", () => {
  const Here = dirname(fileURLToPath(import.meta.url));
  const Forbidden = /from\s+["'](@aws-sdk\/|@smithy\/|@connectrpc\/|@bufbuild\/|node:http|undici)/;

  it.each(["domain.ts", "ports.ts"])("%s imports no infrastructure", (file) => {
    const source = readFileSync(resolve(Here, "../../src/dashboard", file), "utf8");
    expect(source).not.toMatch(Forbidden);
  });

  it("no module statically imports an optional peer", () => {
    for (const file of ["quotas.ts", "cost.ts", "fleet.ts", "inventory.ts", "plane.ts", "aws.ts"]) {
      const source = readFileSync(resolve(Here, "../../src/dashboard", file), "utf8");
      expect(source, file).not.toMatch(
        /from\s+["']@aws-sdk\/client-(service-quotas|cost-explorer)/,
      );
    }
  });
});
