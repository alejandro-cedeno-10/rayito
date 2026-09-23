import { describe, expect, test } from "vitest";
import { InvalidArgumentError } from "../../src/errors.js";
import {
  defaultIdlePolicy,
  Execution,
  FilesystemEventType,
  FileType,
  HostAccess,
  RESULT_FORMAT_ORDER,
  Result,
  sandboxInfo,
  sandboxListItem,
  templateNameFromArn,
  validateIdlePolicy,
  validatePort,
  validatePtySize,
} from "../../src/models.js";
import { IMAGE_ARN, SANDBOX_ID } from "./fake/control-plane.js";

describe("IdlePolicy", () => {
  test("defaults mirror the Python dataclass", () => {
    expect(defaultIdlePolicy()).toEqual({ maxIdleSeconds: 300, autoResume: true });
    expect(validateIdlePolicy({})).toEqual({
      maxIdleSeconds: 300,
      suspendedDurationSeconds: undefined,
      autoResume: true,
    });
  });

  test("validates the API minimums", () => {
    expect(() => validateIdlePolicy({ maxIdleSeconds: 59 })).toThrow(InvalidArgumentError);
    expect(() => validateIdlePolicy({ maxIdleSeconds: 60.5 })).toThrow(InvalidArgumentError);
    expect(() => validateIdlePolicy({ suspendedDurationSeconds: -1 })).toThrow(
      InvalidArgumentError,
    );
    expect(validateIdlePolicy({ maxIdleSeconds: 60, suspendedDurationSeconds: 0 })).toEqual({
      maxIdleSeconds: 60,
      suspendedDurationSeconds: 0,
      autoResume: true,
    });
    expect(validateIdlePolicy({ autoResume: false }).autoResume).toBe(false);
  });
});

describe("SandboxInfo", () => {
  const startedAt = new Date("2026-09-15T14:39:02Z");
  const info = sandboxInfo({
    sandboxId: SANDBOX_ID,
    state: "RUNNING",
    endpoint: "abc.lambda-microvm.us-east-1.on.aws",
    template: IMAGE_ARN,
    templateVersion: "1.0",
    startedAt,
    maximumDurationSeconds: 3600,
  });

  test("derived fields", () => {
    expect(info.endpointUrl).toBe("https://abc.lambda-microvm.us-east-1.on.aws");
    expect(info.expiresAt).toEqual(new Date("2026-09-15T15:39:02Z"));
    expect(info.templateName).toBe("rayito-base-2gb");
    expect(info.remainingSeconds(new Date("2026-09-15T15:39:00Z"))).toBe(2);
    expect(info.remainingSeconds(new Date("2026-09-15T16:00:00Z"))).toBe(0);
    expect(info.terminatedAt).toBeUndefined();
    expect(info.idle).toBeUndefined();
    expect(Object.isFrozen(info)).toBe(true);
  });

  test("expiresAt is the logical deadline when rayd manages it, platformExpiresAt the cap", () => {
    const deadline = new Date("2026-09-15T14:45:00Z");
    const lifecycle = {
      phase: "active" as const,
      deadline,
      cap: new Date("2026-09-15T15:38:02Z"),
      timeoutMs: 300_000,
      onTimeout: "kill" as const,
      autoResume: false,
      extensions: 1,
    };
    const managed = sandboxInfo({ ...info, lifecycle });
    expect(managed.lifecycle).toBe(lifecycle);
    expect(managed.expiresAt).toEqual(deadline);
    expect(managed.platformExpiresAt).toEqual(new Date("2026-09-15T15:39:02Z"));
    expect(managed.remainingSeconds(new Date("2026-09-15T14:44:00Z"))).toBe(60);
    const unmanaged = sandboxInfo({
      ...info,
      lifecycle: { ...lifecycle, phase: "unmanaged", deadline: undefined, cap: undefined },
    });
    expect(unmanaged.expiresAt).toEqual(new Date("2026-09-15T15:39:02Z"));
    expect(info.platformExpiresAt).toEqual(info.expiresAt);
    expect(info.lifecycle).toBeUndefined();
  });

  test("timedOut reads the exit code of a deadline exit", () => {
    expect(info.timedOut).toBe(false);
    const timedOut = sandboxInfo({
      ...info,
      state: "TERMINATED",
      stateReason: "Container Stopped with Exit Code: 124",
    });
    expect(timedOut.timedOut).toBe(true);
    expect(
      sandboxInfo({ ...info, stateReason: "Container Stopped with Exit Code: 0" }).timedOut,
    ).toBe(false);
  });

  test("list items derive the template name", () => {
    const item = sandboxListItem({
      sandboxId: SANDBOX_ID,
      state: "SUSPENDED",
      template: IMAGE_ARN,
      templateVersion: "1.0",
      startedAt,
    });
    expect(item.templateName).toBe("rayito-base-2gb");
    expect(templateNameFromArn("no-colons")).toBe("no-colons");
  });
});

describe("HostAccess", () => {
  test("stringifies to the host and reads headers on each access", () => {
    let jwe = "first";
    const host = new HostAccess("abc.example", 3000, () => jwe);
    expect(`https://${host}`).toBe("https://abc.example");
    expect(host.url).toBe("https://abc.example");
    expect(host.port).toBe(3000);
    expect(host.headers).toEqual({ "x-aws-proxy-auth": "first", "x-aws-proxy-port": "3000" });
    jwe = "second";
    expect(host.headers["x-aws-proxy-auth"]).toBe("second");
    expect(host.headers).not.toHaveProperty("x-aws-proxy-force-h2");
    expect(JSON.stringify(host)).toBe('{"host":"abc.example","port":3000}');
  });
});

describe("validators", () => {
  test("validatePort", () => {
    expect(validatePort(8080)).toBe(8080);
    for (const bad of [0, 65536, 1.5, "80", true, undefined]) {
      expect(() => validatePort(bad)).toThrow(InvalidArgumentError);
    }
  });

  test("validatePtySize", () => {
    expect(validatePtySize(undefined)).toBeUndefined();
    expect(validatePtySize({})).toEqual({ cols: 80, rows: 24 });
    expect(validatePtySize({ cols: 120 })).toEqual({ cols: 120, rows: 24 });
    expect(() => validatePtySize({ cols: 0 })).toThrow(InvalidArgumentError);
    expect(() => validatePtySize({ rows: 4097 })).toThrow(InvalidArgumentError);
    expect(() => validatePtySize({ cols: 1.5 })).toThrow(InvalidArgumentError);
  });

  test("string enums", () => {
    expect(FileType.DIR).toBe("dir");
    expect(FilesystemEventType.CREATE).toBe("create");
  });
});

describe("Result and Execution", () => {
  test("formats follow RESULT_FORMAT_ORDER then extra keys", () => {
    const result = new Result({
      chart: undefined,
      png: "iVBOR",
      text: "42",
      extra: { "rayito/omitted": "x" },
      raw: { "text/plain": "42", "image/png": "iVBOR", "rayito/omitted": "x" },
      isMainResult: true,
    });
    expect(result.formats()).toEqual(["text", "png", "rayito/omitted"]);
    expect(String(result)).toBe("42");
    expect(result.toJSON()).toEqual({
      is_main_result: true,
      "text/plain": "42",
      "image/png": "iVBOR",
      "rayito/omitted": "x",
    });
    expect(RESULT_FORMAT_ORDER[0]).toBe("text");
    expect(Object.isFrozen(result)).toBe(true);
  });

  test("Execution.text is the main result and toJSON has the Python shape", () => {
    const execution = new Execution({
      results: [
        new Result({ text: "one" }),
        new Result({ text: "two", isMainResult: true, raw: { "text/plain": "two" } }),
      ],
      logs: { stdout: ["a"], stderr: [] },
      error: { name: "E", value: "v", traceback: "t" },
      executionCount: 3,
    });
    expect(execution.text).toBe("two");
    expect(execution.toJSON()).toEqual({
      results: [{ is_main_result: false }, { is_main_result: true, "text/plain": "two" }],
      logs: { stdout: ["a"], stderr: [] },
      error: { name: "E", value: "v", traceback: "t" },
      execution_count: 3,
    });
    expect(new Execution().text).toBeUndefined();
    expect(new Execution().toJSON()).toEqual({
      results: [],
      logs: { stdout: [], stderr: [] },
      error: null,
      execution_count: null,
    });
  });
});
