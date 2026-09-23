import { Code, ConnectError } from "@connectrpc/connect";
import { describe, expect, test } from "vitest";
import {
  AuthenticationError,
  CapacityError,
  CommandExitError,
  DiskFullError,
  FileNotFoundError,
  FileUploadError,
  GitAuthError,
  GitUpstreamError,
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
  TransferError,
  UnimplementedError,
} from "../../src/errors.js";
import * as rayito from "../../src/index.js";
import { translateRpcError } from "../../src/transport/errors.js";
import { createTestSandbox } from "./helpers.js";

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

  test("transfer errors: TransferError and FileUploadError carry code and reason; DiskFullError", () => {
    const download = new TransferError("s3_unavailable: S3 no responde", {
      code: "unavailable",
      reason: "s3_unavailable",
    });
    const upload = new FileUploadError("cancelled: transferencia cancelada", {
      code: "cancelled",
      reason: "cancelled",
    });
    for (const error of [download, upload, new DiskFullError("disk_reserve")]) {
      expect(error).toBeInstanceOf(SandboxError);
      expect(error.name).toBe(error.constructor.name);
      expect(Object.getPrototypeOf(error)).toBe(error.constructor.prototype);
    }
    expect(upload).toBeInstanceOf(TransferError);
    expect(download).not.toBeInstanceOf(FileUploadError);
    expect([download.code, download.reason]).toEqual(["unavailable", "s3_unavailable"]);
    expect([upload.code, upload.reason]).toEqual(["cancelled", "cancelled"]);
    expect(new DiskFullError("disk_full")).not.toBeInstanceOf(RateLimitError);
  });

  test("UnimplementedError stays outside the SandboxError hierarchy", () => {
    const error = new UnimplementedError("uploadUrl", "actualiza la imagen");
    expect(error).toBeInstanceOf(Error);
    expect(error).not.toBeInstanceOf(SandboxError);
    expect([error.feature, error.reason]).toEqual(["uploadUrl", "actualiza la imagen"]);
    expect(error.message).toContain("actualiza la imagen");
    expect(error.cause).toBeUndefined();
    expect(Object.hasOwn(error, "cause")).toBe(false);
  });

  test("UnimplementedError carries an optional cause, like SandboxError", () => {
    const cause = new SandboxError("rpc", { grpcCode: Code.Unimplemented });
    const error = new UnimplementedError("getMetrics", "publica una imagen M9", "docs/x.md", {
      cause,
    });
    expect(error.cause).toBe(cause);
    expect(error.doc).toBe("docs/x.md");
    expect(error.message).toBe(
      "getMetrics no está disponible: publica una imagen M9. Ver docs/x.md",
    );
  });
});

describe("M9 E2B 2.x error classes", () => {
  test("git errors sit under AuthenticationError and SandboxError and are exported", () => {
    expect(new GitAuthError("x")).toBeInstanceOf(AuthenticationError);
    expect(new GitAuthError("x").name).toBe("GitAuthError");
    expect(new GitUpstreamError("x")).toBeInstanceOf(SandboxError);
    expect(new GitUpstreamError("x").name).toBe("GitUpstreamError");
    expect(rayito.GitAuthError).toBe(GitAuthError);
    expect(rayito.GitUpstreamError).toBe(GitUpstreamError);
    expect(rayito.DiskFullError).toBe(DiskFullError);
    expect(rayito.UnimplementedError).toBe(UnimplementedError);
    expect(rayito.TransferError).toBe(TransferError);
    expect(rayito.FileUploadError).toBe(FileUploadError);
  });

  test("ResourceExhausted with disk_reserve/disk_full is DiskFullError; anything else stays RateLimitError", () => {
    for (const detail of ["disk_reserve", "disk_full"]) {
      const error = translateRpcError(new ConnectError(detail, Code.ResourceExhausted));
      expect(error).toBeInstanceOf(DiskFullError);
      expect(error).toBeInstanceOf(SandboxError);
    }
    const other = translateRpcError(new ConnectError("rate", Code.ResourceExhausted));
    expect(other).toBeInstanceOf(RateLimitError);
    expect(other).not.toBeInstanceOf(DiskFullError);
  });

  test("files.write rejected by rayd with disk_reserve is DiskFullError, with rate RateLimitError", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    rayd.filesystem.writeRejections.push(
      new ConnectError("disk_reserve", Code.ResourceExhausted),
      new ConnectError("rate", Code.ResourceExhausted),
    );
    await expect(sandbox.files.write("/home/user/a.txt", "x")).rejects.toBeInstanceOf(
      DiskFullError,
    );
    const second = await sandbox.files
      .write("/home/user/b.txt", "x")
      .catch((error: unknown) => error);
    expect(second).toBeInstanceOf(RateLimitError);
    expect(second).not.toBeInstanceOf(DiskFullError);
  });
});
