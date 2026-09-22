import { Code } from "@connectrpc/connect";
import { describe, expect, test } from "vitest";
import {
  AuthenticationError,
  CapacityError,
  CommandExitError,
  FileNotFoundError,
  InvalidArgumentError,
  NotFoundError,
  QuotaExceededError,
  RateLimitError,
  SandboxError,
  SandboxLifetimeError,
  SandboxNotFoundError,
  SandboxNotReadyError,
  SandboxStateError,
  TimeoutError,
} from "../../src/errors.js";

describe("error hierarchy (E2B names)", () => {
  test("every sandbox error is a SandboxError and an Error with its class name", () => {
    const errors = [
      new TimeoutError("t"),
      new InvalidArgumentError("i"),
      new NotFoundError("n"),
      new FileNotFoundError("f"),
      new SandboxNotFoundError("s"),
      new SandboxNotReadyError("r"),
      new SandboxStateError("st"),
      new SandboxLifetimeError("l"),
      new CommandExitError("c", { exitCode: 1 }),
      new RateLimitError("rl"),
    ];
    for (const error of errors) {
      expect(error).toBeInstanceOf(SandboxError);
      expect(error).toBeInstanceOf(Error);
      expect(error.name).toBe(error.constructor.name);
      expect(Object.getPrototypeOf(error)).toBe(error.constructor.prototype);
    }
  });

  test("FileNotFoundError and SandboxNotFoundError extend NotFoundError", () => {
    expect(new FileNotFoundError("x")).toBeInstanceOf(NotFoundError);
    expect(new SandboxNotFoundError("x")).toBeInstanceOf(NotFoundError);
    expect(new FileNotFoundError("x")).not.toBeInstanceOf(SandboxNotFoundError);
  });

  test("account-level errors stay outside the hierarchy", () => {
    for (const error of [
      new AuthenticationError("a"),
      new QuotaExceededError("q"),
      new CapacityError("c"),
    ]) {
      expect(error).toBeInstanceOf(Error);
      expect(error).not.toBeInstanceOf(SandboxError);
      expect(error.name).toBe(error.constructor.name);
    }
  });

  test("fields travel with the error", () => {
    const base = new SandboxError("m", { statusCode: 500, grpcCode: Code.Internal, awsCode: "X" });
    expect([base.statusCode, base.grpcCode, base.awsCode]).toEqual([500, Code.Internal, "X"]);
    const notReady = new SandboxNotReadyError("m", { state: "RUNNING", stateReason: "boot" });
    expect([notReady.state, notReady.stateReason]).toEqual(["RUNNING", "boot"]);
    const exit = new CommandExitError("m", {
      exitCode: 3,
      stdout: "o",
      stderr: "e",
      error: "exited",
    });
    expect([exit.exitCode, exit.stdout, exit.stderr, exit.error]).toEqual([3, "o", "e", "exited"]);
    expect(new CommandExitError("m", { exitCode: 1 }).stdout).toBe("");
    expect(new RateLimitError("m", { retryAfter: 2 }).retryAfter).toBe(2);
    expect(new AuthenticationError("m", { proxyRejected: true }).proxyRejected).toBe(true);
    expect(new AuthenticationError("m").proxyRejected).toBe(false);
    expect(new QuotaExceededError("m", { quotaCode: "L-1" }).quotaCode).toBe("L-1");
  });

  test("cause is kept when given", () => {
    const cause = new Error("root");
    expect(new SandboxError("m", { cause }).cause).toBe(cause);
    expect(new AuthenticationError("m", { cause }).cause).toBe(cause);
    expect(new SandboxError("m").cause).toBeUndefined();
  });
});
