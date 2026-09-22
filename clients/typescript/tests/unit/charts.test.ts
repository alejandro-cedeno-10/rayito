import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, test } from "vitest";
import {
  type BarChart,
  type BoxAndWhiskerChart,
  ChartType,
  type LineChart,
  type PieChart,
  parseChart,
  ScaleType,
  type ScatterChart,
  type SuperChart,
} from "../../src/charts.js";

const FIXTURES = new URL("./fixtures/charts/", import.meta.url);

function fixtureText(name: string): string {
  return readFileSync(fileURLToPath(new URL(`${name}.json`, FIXTURES)), "utf8");
}

describe("parseChart over the e2b/chart fixtures", () => {
  test("line chart carries axes, scales and points", () => {
    const chart = parseChart(fixtureText("line")) as LineChart;
    expect(chart.type).toBe(ChartType.LINE);
    expect(chart.title).toBe("Line chart");
    expect([chart.xLabel, chart.yLabel, chart.xUnit, chart.yUnit]).toEqual([
      "x",
      "y",
      undefined,
      "cm",
    ]);
    expect(chart.xTicks).toEqual([0, 1, 2]);
    expect(chart.xTickLabels).toEqual(["0", "1", "2"]);
    expect(chart.xScale).toBe(ScaleType.LINEAR);
    expect(chart.yScale).toBe(ScaleType.LOG);
    expect(chart.elements).toEqual([
      {
        label: "_child0",
        points: [
          [0, 1],
          [1, 2],
          [2, 3],
        ],
      },
      {
        label: "series b",
        points: [
          ["2024-01-01", 5],
          ["2024-01-02", 6.5],
        ],
      },
    ]);
  });

  test("scatter chart keeps categorical coordinates", () => {
    const chart = parseChart(JSON.parse(fixtureText("scatter"))) as ScatterChart;
    expect(chart.type).toBe(ChartType.SCATTER);
    expect(chart.xScale).toBe(ScaleType.DATETIME);
    expect(chart.yScale).toBe(ScaleType.CATEGORICAL);
    expect(chart.xTicks).toEqual([]);
    expect(chart.elements).toEqual([
      {
        label: "dots",
        points: [
          [1, "a"],
          [2, "b"],
        ],
      },
    ]);
  });

  test("bar chart elements", () => {
    const chart = parseChart(fixtureText("bar")) as BarChart;
    expect(chart.type).toBe(ChartType.BAR);
    expect([chart.xLabel, chart.yLabel]).toEqual(["category", "count"]);
    expect(chart.elements).toEqual([
      { label: "a", group: "_container0", value: "1.0" },
      { label: "b", group: "_container0", value: 2.5 },
    ]);
  });

  test("pie chart elements", () => {
    const chart = parseChart(fixtureText("pie")) as PieChart;
    expect(chart.type).toBe(ChartType.PIE);
    expect(chart.elements).toEqual([
      { label: "a", angle: 120, radius: 1, autopct: "33.3%" },
      { label: "b", angle: 240, radius: 1, autopct: 66.7 },
    ]);
  });

  test("box and whisker chart elements", () => {
    const chart = parseChart(fixtureText("box_and_whisker")) as BoxAndWhiskerChart;
    expect(chart.type).toBe(ChartType.BOX_AND_WHISKER);
    expect(chart.elements).toEqual([
      {
        label: "a",
        min: 1,
        firstQuartile: 2,
        median: 3,
        thirdQuartile: 4,
        max: 5,
        outliers: [9, 10.5],
      },
    ]);
  });

  test("superchart parses children recursively", () => {
    const chart = parseChart(fixtureText("superchart")) as SuperChart;
    expect(chart.type).toBe(ChartType.SUPERCHART);
    expect(chart.title).toBe("Super");
    expect(chart.elements.map((child) => child.type)).toEqual([ChartType.LINE, ChartType.BAR]);
    expect(chart.elements[0]).toEqual(parseChart(fixtureText("line")));
  });

  test("unknown type is a bare chart", () => {
    const chart = parseChart(fixtureText("unknown"));
    expect(chart.type).toBe(ChartType.UNKNOWN);
    expect(chart.title).toBe("Heat");
    expect(chart.elements).toEqual([]);
  });

  test.each(["{not json", "[1, 2]", "42", '"line"', ""])(
    "documents that are not objects are unknown: %s",
    (document) => {
      expect(parseChart(document)).toEqual({
        type: ChartType.UNKNOWN,
        title: undefined,
        elements: [],
      });
    },
  );

  test("missing and malformed keys default instead of throwing", () => {
    const chart = parseChart({ type: "line" }) as LineChart;
    expect(chart.type).toBe(ChartType.LINE);
    expect(chart.title).toBeUndefined();
    expect(chart.xScale).toBeUndefined();
    expect(chart.elements).toEqual([]);
    const malformed = parseChart({
      type: "line",
      title: 7,
      x_scale: "polar",
      x_ticks: "nope",
      elements: [{ label: "a", points: [[1], [1, 2, 3], "x", [1, 2]] }, "junk"],
    }) as LineChart;
    expect(malformed.title).toBe("7");
    expect(malformed.xScale).toBeUndefined();
    expect(malformed.xTicks).toEqual([]);
    expect(malformed.elements).toEqual([{ label: "a", points: [[1, 2]] }]);
    expect(
      (parseChart({ type: "pie", elements: [{ angle: "wide" }] }) as PieChart).elements,
    ).toEqual([{ label: undefined, angle: undefined, radius: undefined, autopct: undefined }]);
    expect(
      (parseChart({ type: "box_and_whisker", elements: [{ outliers: 3 }] }) as BoxAndWhiskerChart)
        .elements,
    ).toEqual([
      {
        label: undefined,
        min: undefined,
        firstQuartile: undefined,
        median: undefined,
        thirdQuartile: undefined,
        max: undefined,
        outliers: [],
      },
    ]);
    expect(parseChart({}).type).toBe(ChartType.UNKNOWN);
  });

  test("chart types and scales are string constants", () => {
    expect(ChartType.BOX_AND_WHISKER).toBe("box_and_whisker");
    expect(ScaleType.FUNCTIONLOG).toBe("functionlog");
    expect(new Set(Object.values(ScaleType))).toEqual(
      new Set([
        "linear",
        "datetime",
        "categorical",
        "log",
        "symlog",
        "logit",
        "function",
        "functionlog",
        "asinh",
      ]),
    );
  });

  test("parsed charts are frozen", () => {
    const chart = parseChart(fixtureText("line"));
    expect(Object.isFrozen(chart)).toBe(true);
  });
});
