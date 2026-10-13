/**
 * Coste cero del panel de gestión (ADR-026, decisión D10): sin llamar a un
 * método no hay un cliente de AWS ni una llamada; nada de Cost Explorer
 * (que cobra por petición) se toca salvo con `UsageCost.billed()`; y los
 * peers opcionales no se importan al cargar el SDK.
 */

import { readdirSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it, vi } from "vitest";
import { Fleet, Quotas, UsageCost } from "../../src/index.js";
import { ConventionCatalog } from "../../src/sizing/catalog.js";
import {
  appliedDefaults,
  FakeCostSource,
  FakePlane,
  FakeQuotaSource,
  listItem,
} from "./fake/dashboard.js";

const HERE = dirname(fileURLToPath(import.meta.url));

describe("zero cost", () => {
  it("building every service needs no region, no credentials and calls nothing", () => {
    vi.stubEnv("AWS_REGION", "");
    vi.stubEnv("AWS_DEFAULT_REGION", "");
    expect(() => [new Quotas(), new UsageCost(), new Fleet()]).not.toThrow();
    vi.unstubAllEnvs();
  });

  it("reading quotas, the fleet or estimates never touches Cost Explorer", async () => {
    const plane = new FakePlane([listItem(1)]);
    const costSource = new FakeCostSource({ periods: [], requests: 0 });
    const catalog = new ConventionCatalog();
    await new Quotas({
      controlPlane: plane,
      source: new FakeQuotaSource(appliedDefaults()),
      catalog,
    }).report();
    await new Fleet({ controlPlane: plane, catalog }).sandboxes();
    await new UsageCost({ controlPlane: plane, source: costSource, catalog }).estimate();
    expect(costSource.queries).toEqual([]);
  });

  it("no action runs by default", async () => {
    const plane = new FakePlane([listItem(1)]);
    await expect(new Fleet({ controlPlane: plane }).runBulk("kill", ["microvm-1"])).rejects.toThrow(
      /allowActions/,
    );
    expect(plane.operations().filter((name) => name.endsWith("Microvm"))).toEqual([]);
  });

  it("no dashboard module imports an optional peer statically", () => {
    const dir = resolve(HERE, "../../src/dashboard");
    for (const file of readdirSync(dir)) {
      const source = readFileSync(resolve(dir, file), "utf8");
      expect(source, file).not.toMatch(
        /from\s+["']@aws-sdk\/client-(service-quotas|cost-explorer)/,
      );
    }
  });
});
