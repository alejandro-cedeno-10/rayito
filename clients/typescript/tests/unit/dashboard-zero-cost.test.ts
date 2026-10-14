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
import * as optionalClient from "../../src/aws/optional-client.js";
import { Fleet, Quotas, UsageCost } from "../../src/index.js";
import { ConventionCatalog } from "../../src/sizing/catalog.js";
import {
  appliedDefaults,
  FakeCostSource,
  FakePlane,
  FakeQuotaSource,
  listItem,
} from "./fake/dashboard.js";

vi.mock("../../src/aws/optional-client.js", async (importOriginal) => {
  const original = await importOriginal<typeof import("../../src/aws/optional-client.js")>();
  return { ...original, loadOptionalSdkClient: vi.fn(original.loadOptionalSdkClient) };
});

const HERE = dirname(fileURLToPath(import.meta.url));
const COST_EXPLORER_PEER = "@aws-sdk/client-cost-explorer";
const FREE_WORD = "Gratuita";
const PAID_WORD = "De pago";

function peersLoaded(): string[] {
  return vi.mocked(optionalClient.loadOptionalSdkClient).mock.calls.map(([peer]) => String(peer));
}

function readSource(file: string): string {
  return readFileSync(resolve(HERE, "../../src/dashboard", file), "utf8");
}

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

  it("never loads the Cost Explorer peer unless billed() is called", async () => {
    vi.mocked(optionalClient.loadOptionalSdkClient).mockClear();
    const plane = new FakePlane([listItem(1)]);
    const catalog = new ConventionCatalog();
    new UsageCost({ controlPlane: plane, catalog });
    await new UsageCost({ controlPlane: plane, catalog }).estimate();
    await new Quotas({
      controlPlane: plane,
      source: new FakeQuotaSource(appliedDefaults()),
      catalog,
    }).report();
    await new Fleet({ controlPlane: plane, catalog }).sandboxes();
    expect(peersLoaded()).not.toContain(COST_EXPLORER_PEER);
  });

  it("loads the Cost Explorer peer only when billed() runs without an injected source", async () => {
    vi.mocked(optionalClient.loadOptionalSdkClient)
      .mockClear()
      .mockRejectedValueOnce(new Error("peer ausente"));
    const usage = new UsageCost({ controlPlane: new FakePlane([listItem(1)]) });
    await expect(usage.billed()).rejects.toThrow();
    expect(peersLoaded()).toEqual([COST_EXPLORER_PEER]);
  });
});

describe("cost clarity in the TSDoc", () => {
  it("marks every free method as free and every billable one as billable", () => {
    const cost = readSource("cost.ts");
    const fleet = readSource("fleet.ts");
    const quotas = readSource("quotas.ts");
    expect(cost).toMatch(/De pago: cada fallo de caché/);
    expect(cost).toMatch(/Gratuita: sólo lee Lambda MicroVMs/);
    expect(quotas).toContain(FREE_WORD);
    expect(fleet.split(FREE_WORD).length - 1).toBeGreaterThanOrEqual(4);
    expect(fleet).toContain(`${PAID_WORD}, aunque mínimo`);
  });

  it("documents the price of refresh and the per-instance cache", () => {
    const cost = readSource("cost.ts");
    expect(cost).toContain("`refresh: true`");
    expect(cost).toContain("$0,01");
    expect(cost).toContain("cada instancia");
    expect(cost).toContain("COST_CACHE_MAX_ENTRIES");
  });

  it("says in the fleet TSDoc that resume bills compute again", () => {
    const fleet = readSource("fleet.ts");
    for (const fragment of ["`resume`", "$0,00155/GB", "$0,0038/GB", "$0,08/GB-mes"]) {
      expect(fleet).toContain(fragment);
    }
    expect(fleet).toContain("vuelve a facturar cómputo");
  });
});
