/**
 * `UsageCost`: coste facturado de Cost Explorer con caché local y
 * estimación por sandbox, siempre rotulados. El adaptador de Cost Explorer
 * se prueba con un cliente falso: parámetros enviados, paginación (cada
 * página es una petición de pago) y errores por código.
 */

import { describe, expect, it } from "vitest";
import {
  type CostPage,
  costExplorerSource,
  costFilter,
  queryCostAndUsage,
} from "../../src/dashboard/aws.js";
import { UsageCost } from "../../src/dashboard/cost.js";
import type { CostQueryResult } from "../../src/dashboard/domain.js";
import { AuthenticationError, DashboardError, InvalidArgumentError } from "../../src/errors.js";
import { COST_CACHE_TTL_SECONDS } from "../../src/limits.js";
import { ConventionCatalog } from "../../src/sizing/catalog.js";
import { IMAGE_ARN } from "./fake/control-plane.js";
import { awsSdkError, FakeCostSource, FakePlane, listItem, NOW } from "./fake/dashboard.js";

const RESULT: CostQueryResult = {
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
      ],
    },
  ],
  requests: 1,
};

function usageCost(
  source: FakeCostSource,
  extra: { now?: { value: number }; plane?: FakePlane; cacheTtlMs?: number } = {},
): UsageCost {
  const clock = extra.now ?? { value: 0 };
  return new UsageCost({
    source,
    now: () => NOW,
    monotonicMs: () => clock.value,
    catalog: new ConventionCatalog(),
    ...(extra.plane === undefined ? {} : { controlPlane: extra.plane }),
    ...(extra.cacheTtlMs === undefined ? {} : { cacheTtlMs: extra.cacheTtlMs }),
  });
}

describe("UsageCost.billed", () => {
  it("defaults to the month so far and labels the result", async () => {
    const source = new FakeCostSource(RESULT);
    const cost = await usageCost(source).billed();

    expect(source.queries).toEqual([
      { start: "2026-10-01", end: "2026-10-14", granularity: "MONTHLY", region: undefined },
    ]);
    expect(cost.label).toBe("billed");
    expect(cost.totalUsd).toBe(0.5);
    expect(cost.partial).toBe(true);
    expect(cost.fetchedAt).toEqual(NOW);
    expect(cost.requests).toBe(1);
  });

  it("accepts ISO strings, dates and a region filter", async () => {
    const source = new FakeCostSource(RESULT);
    await usageCost(source).billed({
      start: "2026-09-01",
      end: new Date(Date.UTC(2026, 9, 1)),
      region: "eu-west-1",
    });
    expect(source.queries).toEqual([
      { start: "2026-09-01", end: "2026-10-01", granularity: "MONTHLY", region: "eu-west-1" },
    ]);
  });

  it.each([
    ["2026-10-05", "2026-10-05"],
    ["2026-10-06", "2026-10-05"],
    ["no", undefined],
    ["2026-13-45", undefined],
  ] as const)("rejects the window %s..%s before any call", async (start, end) => {
    const source = new FakeCostSource(RESULT);
    await expect(usageCost(source).billed({ start, end })).rejects.toBeInstanceOf(
      InvalidArgumentError,
    );
    expect(source.queries).toEqual([]);
  });

  it("serves the second read from the cache until the ttl expires", async () => {
    const source = new FakeCostSource(RESULT);
    const now = { value: 0 };
    const cost = usageCost(source, { now, cacheTtlMs: 100_000 });

    const first = await cost.billed();
    now.value = 99_000;
    expect(await cost.billed()).toBe(first);
    expect(source.queries).toHaveLength(1);
    now.value = 100_000;
    await cost.billed();
    expect(source.queries).toHaveLength(2);
  });

  it("refresh and different windows bypass the cache", async () => {
    const source = new FakeCostSource(RESULT);
    const cost = usageCost(source);
    await cost.billed();
    await cost.billed({ refresh: true });
    await cost.billed({ granularity: "DAILY" });
    await cost.billed({ region: "us-east-1" });
    expect(source.queries).toHaveLength(4);
  });

  it("the default ttl is the named constant", async () => {
    const source = new FakeCostSource(RESULT);
    const now = { value: 0 };
    const cost = new UsageCost({ source, now: () => NOW, monotonicMs: () => now.value });
    await cost.billed();
    now.value = COST_CACHE_TTL_SECONDS * 1000 - 1;
    await cost.billed();
    expect(source.queries).toHaveLength(1);
    now.value = COST_CACHE_TTL_SECONDS * 1000;
    await cost.billed();
    expect(source.queries).toHaveLength(2);
  });
});

describe("UsageCost.estimate", () => {
  it("is labelled and only prices running sandboxes", async () => {
    const plane = new FakePlane(
      [
        listItem(1, { startedAt: new Date(NOW.getTime() - 3_600_000) }),
        listItem(2, { state: "SUSPENDED", startedAt: new Date(NOW.getTime() - 18_000_000) }),
      ],
      { [`${IMAGE_ARN}@1.0`]: 2048 },
    );
    const source = new FakeCostSource(RESULT);

    const estimate = await usageCost(source, { plane }).estimate();

    expect(estimate.label).toBe("estimated");
    expect(estimate.totalUsd).toBeCloseTo(0.1261, 6);
    const [running, suspended] = estimate.items;
    expect([running?.label, suspended?.label]).toEqual(["estimated", "estimated"]);
    expect(suspended?.costUsd).toBeUndefined();
    expect(source.queries).toEqual([]);
  });
});

function ceResponse(token?: string): CostPage {
  return {
    ResultsByTime: [
      {
        TimePeriod: { Start: "2026-10-01", End: "2026-10-14" },
        Estimated: true,
        Groups: [
          {
            Keys: ["Lambda-MicroVM-vCPU-Second-ARM"],
            Metrics: {
              UnblendedCost: { Amount: "0.25", Unit: "USD" },
              UsageQuantity: { Amount: "50", Unit: "vCPU-Seconds" },
            },
          },
        ],
      },
    ],
    ...(token === undefined ? {} : { NextPageToken: token }),
  };
}

function expectedParams(region?: string, token?: string): Record<string, unknown> {
  const service = { Dimensions: { Key: "SERVICE", Values: ["AWS Lambda"] } };
  return {
    TimePeriod: { Start: "2026-10-01", End: "2026-10-14" },
    Granularity: "MONTHLY",
    Metrics: ["UnblendedCost", "UsageQuantity"],
    Filter:
      region === undefined
        ? service
        : { And: [service, { Dimensions: { Key: "REGION", Values: [region] } }] },
    GroupBy: [{ Type: "DIMENSION", Key: "USAGE_TYPE" }],
    ...(token === undefined ? {} : { NextPageToken: token }),
  };
}

const QUERY = {
  start: "2026-10-01",
  end: "2026-10-14",
  granularity: "MONTHLY",
  region: undefined,
} as const;

describe("Cost Explorer adapter", () => {
  it("sends the verified query and counts each page", async () => {
    const sent: Array<Record<string, unknown>> = [];
    const pages = [ceResponse("next"), ceResponse()];
    const result = await queryCostAndUsage(async (input) => {
      sent.push(structuredClone(input));
      return pages[sent.length - 1] as CostPage;
    }, QUERY);

    expect(sent).toEqual([expectedParams(), expectedParams(undefined, "next")]);
    expect(result.requests).toBe(2);
    expect(result.periods).toHaveLength(2);
    expect(result.periods[0]?.groups[0]).toEqual({
      usageType: "Lambda-MicroVM-vCPU-Second-ARM",
      costUsd: 0.25,
      quantity: 50,
      unit: "vCPU-Seconds",
    });
    expect(result.periods[0]?.estimated).toBe(true);
  });

  it("filters by region", () => {
    expect(costFilter("eu-west-1")).toEqual(expectedParams("eu-west-1").Filter);
  });

  it("builds no client until the first call", async () => {
    let built = 0;
    const source = costExplorerSource(async () => {
      built += 1;
      return async () => ceResponse();
    });
    expect(built).toBe(0);
    await source.getCostAndUsage(QUERY);
    expect(built).toBe(1);
  });

  it.each([
    ["AccessDeniedException", AuthenticationError],
    ["DataUnavailableException", DashboardError],
  ] as const)("maps %s by code", async (code, expected) => {
    const failure = awsSdkError("cuenta secreta", code);
    const source = costExplorerSource(async () => async () => {
      throw failure;
    });
    const error = await source.getCostAndUsage(QUERY).catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(expected);
    expect((error as Error).message).not.toContain("secreta");
    expect((error as { awsCode?: string }).awsCode).toBe(code);
  });

  it("a missing peer keeps its install hint and a non-AWS bug is not mislabelled", async () => {
    const missing = new InvalidArgumentError("instala @aws-sdk/client-cost-explorer");
    const noPeer = costExplorerSource(async () => {
      throw missing;
    });
    await expect(noPeer.getCostAndUsage(QUERY)).rejects.toBe(missing);
    const bug = new TypeError("bug");
    const buggy = costExplorerSource(async () => async () => {
      throw bug;
    });
    await expect(buggy.getCostAndUsage(QUERY)).rejects.toBe(bug);
  });
});
