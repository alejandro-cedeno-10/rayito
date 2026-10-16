/**
 * El dashboard de CloudWatch que genera `rayito dashboard cloudwatch` frente a
 * `testdata/dashboard/cloudwatch-board-cases.json`: mismos bytes que la CLI de
 * Python.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import {
  type BoardOptions,
  buildBoard,
  renderBoard,
} from "../../src/dashboard/cloudwatch-board.js";
import { InvalidArgumentError } from "../../src/errors.js";

interface SharedOptions {
  region?: string;
  logGroupPrefix?: string;
  logGroups?: string[];
  includeOtlp?: boolean;
  includeQuota?: boolean;
}

interface SharedBoards {
  cases: Array<{ name: string; options: SharedOptions; rendered: string }>;
  invalid: Array<{ name: string; options: SharedOptions; message: string }>;
}

const shared = JSON.parse(
  readFileSync(
    join(
      import.meta.dirname,
      "..",
      "..",
      "..",
      "..",
      "testdata",
      "dashboard",
      "cloudwatch-board-cases.json",
    ),
    "utf8",
  ),
) as SharedBoards;
const DEFAULT_REGION = "us-east-1";

function optionsOf(options: SharedOptions): BoardOptions {
  return { ...options, region: options.region ?? DEFAULT_REGION };
}

describe("dashboard de CloudWatch", () => {
  it.each(shared.cases)("$name", (testCase) => {
    expect(renderBoard(buildBoard(optionsOf(testCase.options)), true)).toBe(testCase.rendered);
  });

  it.each(shared.invalid)("rechaza: $name", (testCase) => {
    expect(() => buildBoard(optionsOf(testCase.options))).toThrow(InvalidArgumentError);
    expect(() => buildBoard(optionsOf(testCase.options))).toThrow(testCase.message);
  });

  it("sin sangría es una sola línea", () => {
    expect(renderBoard(buildBoard({ region: DEFAULT_REGION }))).not.toContain("\n  ");
  });
});
