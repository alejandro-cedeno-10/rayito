/**
 * Vectores compartidos con el SDK Python
 * (`testdata/templates/dockerfile-cases.json`): mismas instrucciones, mismas
 * líneas Dockerfile; mismos helpers, mismo `readyCmd`. Espejo de
 * `test_m15_templates_vectors.py`.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, test } from "vitest";
import { InvalidArgumentError } from "../../src/errors.js";
import {
  LAYER_BEGIN_MARKER,
  LAYER_END_MARKER,
  renderAppendedLayer,
} from "../../src/templates/dockerfile.js";
import type { WireStep } from "../../src/templates/instructions.js";
import {
  type ReadyCommand,
  waitForFile,
  waitForPort,
  waitForProcess,
  waitForUrl,
} from "../../src/templates/ready-cmds.js";

interface Vectors {
  readonly layer_cases: ReadonlyArray<{ name: string; steps: WireStep[]; lines: string[] }>;
  readonly rejected_steps: ReadonlyArray<{ name: string; steps: WireStep[] }>;
  readonly ready_cases: ReadonlyArray<{ helper: string; args: unknown[]; cmd: string }>;
}

const VECTORS = JSON.parse(
  readFileSync(
    join(
      import.meta.dirname,
      "..",
      "..",
      "..",
      "..",
      "testdata",
      "templates",
      "dockerfile-cases.json",
    ),
    "utf8",
  ),
) as Vectors;

const HELPERS: Record<string, (...args: never[]) => ReadyCommand> = {
  wait_for_port: waitForPort,
  wait_for_url: waitForUrl,
  wait_for_process: waitForProcess,
  wait_for_file: waitForFile,
};

describe("templates shared vectors", () => {
  test.each(VECTORS.layer_cases)("$name", ({ steps, lines }) => {
    const rendered = renderAppendedLayer({ steps, skipCache: false }).trimEnd().split("\n");
    expect(rendered[0]).toBe(LAYER_BEGIN_MARKER);
    expect(rendered.at(-1)).toBe(LAYER_END_MARKER);
    expect(rendered.slice(1, -1)).toEqual(lines);
  });

  test.each(VECTORS.rejected_steps)("rejects: $name", ({ steps }) => {
    expect(() => renderAppendedLayer({ steps, skipCache: false })).toThrow(InvalidArgumentError);
  });

  test.each(VECTORS.ready_cases)("$helper($args)", ({ helper, args, cmd }) => {
    const fn = HELPERS[helper] as (...values: unknown[]) => ReadyCommand;
    expect(fn(...args).cmd).toBe(cmd);
  });
});
