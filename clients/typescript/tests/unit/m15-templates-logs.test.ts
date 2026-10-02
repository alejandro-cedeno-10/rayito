/**
 * `src/templates/logs.ts` (m15-templates): espejo de
 * `test_m15_templates_logs.py`.
 */

import { describe, expect, test } from "vitest";
import { classifyReadyFailure, parseBuildFailure } from "../../src/templates/logs.js";

describe("templates/logs", () => {
  test("parseBuildFailure extracts the last step, command and exit code", () => {
    const lines = [
      "#5 [3/6] RUN pip install --no-cache-dir not-a-real-package",
      "#5 1.234 ERROR: could not find a version that satisfies the requirement",
      "#5 ERROR: executor failed running [/bin/sh -c pip install --no-cache-dir " +
        "not-a-real-package]: exit code: 1",
    ];
    const detail = parseBuildFailure(lines);
    expect(detail.step).toBe(3);
    expect(detail.command).toBe("RUN pip install --no-cache-dir not-a-real-package");
    expect(detail.exitCode).toBe(1);
    expect(detail.logTail).toContain(lines.at(-1));
  });

  test("parseBuildFailure with no lines is empty", () => {
    const detail = parseBuildFailure([]);
    expect(detail.step).toBeUndefined();
    expect(detail.logTail).toBeUndefined();
  });

  test("classifyReadyFailure distinguishes client and server errors", () => {
    expect(
      classifyReadyFailure(
        "Ready hook check failed: the application returned a server error (HTTP 5xx) response",
      ),
    ).toBe("ready_server_error");
    expect(
      classifyReadyFailure(
        "Ready hook check failed: the application returned a client error (HTTP 4xx) response",
      ),
    ).toBe("ready_client_error");
    expect(classifyReadyFailure("The container image build failed.")).toBeUndefined();
    expect(classifyReadyFailure(undefined)).toBeUndefined();
  });
});
