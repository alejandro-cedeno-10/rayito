import { Code, ConnectError } from "@connectrpc/connect";
import { describe, expect, test } from "vitest";
import {
  AuthenticationError,
  DiskFullError,
  FileNotFoundError,
  InvalidArgumentError,
  LifecycleUnsupportedError,
  NotFoundError,
  RateLimitError,
  SandboxError,
  SandboxStateError,
  TimeoutError,
} from "../../src/errors.js";
import {
  isKernelGate,
  isNotYetReachable,
  isOwnAbort,
  isPhaseGate,
  isProxyForbidden,
  isReconnectable,
  isSandboxTimeout,
  isStreamReset,
  translateRpcError,
  translateSetTimeoutError,
  translateStreamError,
} from "../../src/transport/errors.js";
import {
  assertPlaintextAllowed,
  baseUrl,
  DEFAULT_TRANSPORT_SETTINGS,
  isLoopbackHost,
  openTransport,
  resolveTransportSettings,
} from "../../src/transport/transport.js";

function nodeCause(code: string): Error {
  const error = new Error(code) as Error & { code: string };
  error.code = code;
  return error;
}

function withCause(message: string, code: Code, cause: unknown): ConnectError {
  return new ConnectError(message, code, undefined, undefined, cause);
}

const RECONNECTABLE: Array<[string, ConnectError]> = [
  ["proxy 502", new ConnectError("HTTP 502", Code.Unavailable)],
  ["proxy 503", new ConnectError("HTTP 503", Code.Unavailable)],
  ["proxy 504", new ConnectError("HTTP 504", Code.Unavailable)],
  ["proxy 429", new ConnectError("HTTP 429", Code.Unavailable)],
  [
    "REFUSED_STREAM",
    new ConnectError("http/2 stream closed with error code REFUSED_STREAM (0x7)", Code.Unavailable),
  ],
  ["ECONNREFUSED", withCause("connect ECONNREFUSED", Code.Unavailable, nodeCause("ECONNREFUSED"))],
  ["ETIMEDOUT", withCause("connect ETIMEDOUT", Code.Unavailable, nodeCause("ETIMEDOUT"))],
  ["ECONNRESET as Aborted", withCause("read ECONNRESET", Code.Aborted, nodeCause("ECONNRESET"))],
  [
    "destroyed stream",
    withCause("stream destroyed", Code.Aborted, nodeCause("ERR_STREAM_DESTROYED")),
  ],
  [
    "RST_STREAM CANCEL",
    new ConnectError("http/2 stream closed with error code CANCEL (0x8)", Code.Canceled),
  ],
  [
    "h2 closed Internal",
    new ConnectError("http/2 stream closed with error code PROTOCOL_ERROR (0x1)", Code.Internal),
  ],
  ["missing status", new ConnectError("protocol error: missing status", Code.Internal)],
  [
    "ERR_HTTP2_ in cause",
    withCause("session error", Code.Internal, nodeCause("ERR_HTTP2_SESSION_ERROR")),
  ],
  [
    "ECONNRESET in cause chain",
    withCause("x", Code.Internal, new Error("wrap", { cause: nodeCause("ECONNRESET") })),
  ],
  ["EPIPE in cause", withCause("x", Code.Internal, nodeCause("EPIPE"))],
  ["phase gate suspending", new ConnectError("suspending", Code.Unavailable)],
  ["phase gate terminating", new ConnectError("terminating", Code.Unavailable)],
];

const NOT_RECONNECTABLE: Array<[string, ConnectError]> = [
  ["own abort", new ConnectError("This operation was aborted", Code.Canceled)],
  ["deadline", new ConnectError("the operation timed out", Code.DeadlineExceeded)],
  ["kernel gate", new ConnectError("kernel not ready: x", Code.Unavailable)],
  ["proxy 403", new ConnectError("HTTP 403", Code.PermissionDenied)],
  ["plain Internal", new ConnectError("boom", Code.Internal)],
  ["not found", new ConnectError("pid 1 not found", Code.NotFound)],
  ["unauthenticated", new ConnectError("HTTP 401", Code.Unauthenticated)],
];

describe("classification truth table", () => {
  test.each(RECONNECTABLE)("%s is reconnectable", (_label, error) => {
    expect(isReconnectable(error)).toBe(true);
  });

  test.each(NOT_RECONNECTABLE)("%s is not reconnectable", (_label, error) => {
    expect(isReconnectable(error)).toBe(false);
  });

  test("the gates are neither resets nor mistaken for each other", () => {
    const suspending = new ConnectError("suspending", Code.Unavailable);
    expect(isPhaseGate(suspending)).toBe(true);
    expect(isStreamReset(suspending)).toBe(false);
    expect(isKernelGate(suspending)).toBe(false);
    const kernel = new ConnectError("kernel not ready: booting", Code.Unavailable);
    expect(isKernelGate(kernel)).toBe(true);
    expect(isStreamReset(kernel)).toBe(false);
    expect(isPhaseGate(kernel)).toBe(false);
    expect(isStreamReset(new ConnectError("HTTP 502", Code.Unavailable))).toBe(true);
  });

  test("own aborts are recognised and never resets", () => {
    const own = new ConnectError("This operation was aborted", Code.Canceled);
    expect(isOwnAbort(own)).toBe(true);
    expect(isStreamReset(own)).toBe(false);
    const proxy = new ConnectError(
      "http/2 stream closed with error code CANCEL (0x8)",
      Code.Canceled,
    );
    expect(isOwnAbort(proxy)).toBe(false);
    expect(isStreamReset(proxy)).toBe(true);
  });

  test("proxy 403 and not-yet-reachable forms", () => {
    expect(isProxyForbidden(new ConnectError("HTTP 403", Code.PermissionDenied))).toBe(true);
    expect(isProxyForbidden(new ConnectError("EACCES", Code.PermissionDenied))).toBe(false);
    expect(isProxyForbidden(new ConnectError("HTTP 403", Code.Unauthenticated))).toBe(false);
    expect(isNotYetReachable(new ConnectError("HTTP 502", Code.Unavailable))).toBe(true);
    expect(isNotYetReachable(new ConnectError("kernel not ready: x", Code.Unavailable))).toBe(true);
    expect(isNotYetReachable(new ConnectError("x", Code.DeadlineExceeded))).toBe(true);
    expect(isNotYetReachable(new ConnectError("x", Code.Internal))).toBe(false);
    expect(isNotYetReachable(new ConnectError("x", Code.Unknown))).toBe(false);
    expect(isNotYetReachable(new ConnectError("HTTP 403", Code.PermissionDenied))).toBe(false);
    expect(isNotYetReachable(new ConnectError("This operation was aborted", Code.Canceled))).toBe(
      false,
    );
    expect(isReconnectable(new Error("plain"))).toBe(false);
  });

  test("every stream reset form is not-yet-reachable for a Health probe", () => {
    for (const [, error] of RECONNECTABLE) {
      expect(isNotYetReachable(error)).toBe(true);
    }
    expect(isNotYetReachable(new ConnectError("ECONNRESET", Code.Aborted))).toBe(true);
    expect(
      isNotYetReachable(
        new ConnectError(
          "http/2 stream closed with error code INTERNAL_ERROR (0x2)",
          Code.Internal,
        ),
      ),
    ).toBe(true);
    expect(
      isNotYetReachable(withCause("socket hang up", Code.Internal, nodeCause("ECONNRESET"))),
    ).toBe(true);
  });
});

describe("translateRpcError", () => {
  const rows: Array<[Code, string, new (...args: never[]) => Error]> = [
    [Code.InvalidArgument, "bad", InvalidArgumentError],
    [Code.FailedPrecondition, "not a pty", InvalidArgumentError],
    [Code.Unimplemented, "nope", InvalidArgumentError],
    [Code.Unauthenticated, "x-access-token", AuthenticationError],
    [Code.PermissionDenied, "EACCES", AuthenticationError],
    [Code.NotFound, "pid", NotFoundError],
    [Code.OutOfRange, "from_seq", NotFoundError],
    [Code.ResourceExhausted, "max", RateLimitError],
    [Code.DeadlineExceeded, "late", TimeoutError],
    [Code.Canceled, "This operation was aborted", SandboxError],
    [Code.Internal, "boom", SandboxError],
    [Code.Unknown, "?", SandboxError],
  ];

  test.each(rows)("code %s yields the listed class with grpcCode", (code, message, expected) => {
    const translated = translateRpcError(new ConnectError(message, code));
    expect(translated).toBeInstanceOf(expected);
    expect((translated as SandboxError).grpcCode).toBe(code);
  });

  test("phase gate and kernel gate", () => {
    const phase = translateRpcError(new ConnectError("suspending", Code.Unavailable));
    expect(phase).toBeInstanceOf(SandboxStateError);
    expect(phase.message).toContain("suspending");
    const kernel = translateRpcError(new ConnectError("kernel not ready: x", Code.Unavailable));
    expect(kernel).toBeInstanceOf(SandboxError);
    expect(kernel).not.toBeInstanceOf(SandboxStateError);
    expect(kernel.message).toContain("kernel");
    const plain = translateRpcError(new ConnectError("HTTP 502", Code.Unavailable));
    expect(plain).toBeInstanceOf(SandboxError);
  });

  test("proxy 403 sets proxyRejected, a genuine PermissionDenied does not", () => {
    const proxy = translateRpcError(
      new ConnectError("HTTP 403", Code.PermissionDenied),
    ) as AuthenticationError;
    expect(proxy).toBeInstanceOf(AuthenticationError);
    expect(proxy.proxyRejected).toBe(true);
    const genuine = translateRpcError(
      new ConnectError("EACCES", Code.PermissionDenied),
    ) as AuthenticationError;
    expect(genuine.proxyRejected).toBe(false);
  });

  test("NotFound is FileNotFoundError for the filesystem", () => {
    expect(
      translateRpcError(new ConnectError("x", Code.NotFound), { filesystem: true }),
    ).toBeInstanceOf(FileNotFoundError);
    expect(translateRpcError(new ConnectError("x", Code.NotFound))).not.toBeInstanceOf(
      FileNotFoundError,
    );
  });

  test("canceled by the client is a SandboxError with the message", () => {
    const translated = translateRpcError(
      new ConnectError("This operation was aborted", Code.Canceled),
    );
    expect(translated.message).toContain("cancelada por el cliente");
    expect(translateRpcError(new Error("plain"))).toBeInstanceOf(Error);
    expect(translateRpcError("text")).toBeInstanceOf(SandboxError);
  });
});

describe("the sandbox deadline (ADR-011)", () => {
  test("FAILED_PRECONDITION sandbox_timeout is TimeoutError, checked before the generic rule", () => {
    const gated = new ConnectError("sandbox_timeout", Code.FailedPrecondition);
    expect(isSandboxTimeout(gated)).toBe(true);
    const translated = translateRpcError(gated);
    expect(translated).toBeInstanceOf(TimeoutError);
    expect((translated as TimeoutError).grpcCode).toBe(Code.FailedPrecondition);
    expect(translateRpcError(gated, { filesystem: true })).toBeInstanceOf(TimeoutError);
    expect(isReconnectable(gated)).toBe(false);
    expect(isSandboxTimeout(new ConnectError("not a pty", Code.FailedPrecondition))).toBe(false);
    expect(isSandboxTimeout(new ConnectError("sandbox_timeout", Code.Unavailable))).toBe(false);
  });

  test("the in-stream code sandbox_timeout is TimeoutError", () => {
    expect(translateStreamError("sandbox_timeout", "sandbox timeout")).toBeInstanceOf(TimeoutError);
  });

  test("the SetTimeout table", () => {
    const cap = Date.UTC(2026, 8, 22, 12, 15, 0);
    const beyond = translateSetTimeoutError(
      new ConnectError(`timeout beyond cap; cap_unix_ms=${cap}`, Code.InvalidArgument),
      2_000_000,
    );
    expect(beyond).toBeInstanceOf(InvalidArgumentError);
    expect(beyond.message).toContain("maxLifetimeMs");
    expect(beyond.message).toContain("28800");
    expect(beyond.message).toContain("reincarnate()");
    expect(beyond.message).toContain(new Date(cap).toISOString());
    const unmanaged = translateSetTimeoutError(
      new ConnectError("lifecycle_unmanaged", Code.FailedPrecondition),
      60_000,
    );
    expect(unmanaged).toBeInstanceOf(InvalidArgumentError);
    expect(unmanaged).not.toBeInstanceOf(LifecycleUnsupportedError);
    expect(unmanaged.message).toContain("maxLifetimeMs");
    expect(
      translateSetTimeoutError(new ConnectError("nope", Code.Unimplemented), 60_000),
    ).toBeInstanceOf(LifecycleUnsupportedError);
    expect(
      translateSetTimeoutError(new ConnectError("sandbox_timeout", Code.FailedPrecondition), 1000),
    ).toBeInstanceOf(TimeoutError);
    expect(
      translateSetTimeoutError(new ConnectError("mode must be EXACT", Code.InvalidArgument), 1000),
    ).toBeInstanceOf(InvalidArgumentError);
    expect(
      translateSetTimeoutError(new ConnectError("x-access-token", Code.Unauthenticated), 1000),
    ).toBeInstanceOf(AuthenticationError);
  });
});

describe("translateStreamError", () => {
  test("closed set of StreamError codes", () => {
    expect(translateStreamError("not_found", "m")).toBeInstanceOf(NotFoundError);
    expect(translateStreamError("not_found", "m", { filesystem: true })).toBeInstanceOf(
      FileNotFoundError,
    );
    expect(translateStreamError("permission_denied", "m")).toBeInstanceOf(AuthenticationError);
    expect(translateStreamError("deadline_exceeded", "m")).toBeInstanceOf(TimeoutError);
    expect(translateStreamError("unimplemented", "m")).toBeInstanceOf(InvalidArgumentError);
    expect(translateStreamError("invalid_argument", "m")).toBeInstanceOf(InvalidArgumentError);
    expect(translateStreamError("suspending", "m")).toBeInstanceOf(SandboxStateError);
    expect(translateStreamError("output_truncated", "m").message).toContain("output_truncated");
    expect(translateStreamError("kernel_died", "m").message).toBe("kernel_died: m");
  });

  test("the M9 codes: failed_precondition, resource_exhausted, unavailable, cancelled", () => {
    expect(translateStreamError("failed_precondition", "m")).toBeInstanceOf(InvalidArgumentError);
    expect(translateStreamError("resource_exhausted", "disk_reserve")).toBeInstanceOf(
      DiskFullError,
    );
    expect(translateStreamError("resource_exhausted", "disk_full")).toBeInstanceOf(DiskFullError);
    const limited = translateStreamError("resource_exhausted", "demasiadas transferencias");
    expect(limited).toBeInstanceOf(RateLimitError);
    expect(limited).not.toBeInstanceOf(DiskFullError);
    expect(translateStreamError("unavailable", "m").message).toBe("unavailable: m");
    expect(translateStreamError("cancelled", "m").message).toBe("cancelled: m");
  });
});

describe("transport settings", () => {
  test("defaults and overrides", () => {
    expect(DEFAULT_TRANSPORT_SETTINGS).toMatchObject({
      scheme: "https",
      port: 443,
      pingIntervalMs: 30_000,
      pingTimeoutMs: 10_000,
      pingIdleConnection: false,
      readMaxBytes: 64 * 1024 * 1024,
    });
    expect(baseUrl("abc.example", DEFAULT_TRANSPORT_SETTINGS)).toBe("https://abc.example:443");
    const local = resolveTransportSettings({ scheme: "http", port: 5000 });
    expect(baseUrl("127.0.0.1", local)).toBe("http://127.0.0.1:5000");
    expect(local.pingIntervalMs).toBe(30_000);
  });

  test("plaintext http is allowed towards loopback only", () => {
    const plaintext = resolveTransportSettings({ scheme: "http", port: 5000 });
    for (const host of ["127.0.0.1", "::1", "localhost", "LOCALHOST"]) {
      expect(isLoopbackHost(host)).toBe(true);
      expect(() => assertPlaintextAllowed(host, plaintext)).not.toThrow();
    }
    const opened = openTransport("127.0.0.1", plaintext, (next) => next);
    expect(opened.sessionManager.state()).toBe("closed");
    opened.sessionManager.abort();
    expect(() => assertPlaintextAllowed("abc.example", DEFAULT_TRANSPORT_SETTINGS)).not.toThrow();
  });

  test("plaintext http towards a non-loopback host is refused before any connection", () => {
    const plaintext = resolveTransportSettings({ scheme: "http", port: 5000 });
    for (const host of ["abc.example", "10.0.0.5", "127.0.0.1.evil.example", "2001:db8::1"]) {
      expect(isLoopbackHost(host)).toBe(false);
      expect(() => openTransport(host, plaintext, (next) => next)).toThrow(InvalidArgumentError);
    }
    expect(() => openTransport("abc.example", plaintext, (next) => next)).toThrow(
      /sólo se admite hacia loopback/,
    );
  });
});

describe("explicit endpoint ports", () => {
  test("host:port overrides the transport port and keeps loopback detection", () => {
    const plaintext = resolveTransportSettings({ scheme: "http", port: 5000 });
    expect(baseUrl("127.0.0.1:4242", plaintext)).toBe("http://127.0.0.1:4242");
    expect(baseUrl("[::1]:4242", plaintext)).toBe("http://[::1]:4242");
    expect(isLoopbackHost("127.0.0.1:4242")).toBe(true);
    expect(isLoopbackHost("[::1]:4242")).toBe(true);
    expect(isLoopbackHost("::1")).toBe(true);
    expect(isLoopbackHost("2001:db8::1")).toBe(false);
    expect(isLoopbackHost("abc.example:443")).toBe(false);
    expect(baseUrl("abc.example", plaintext)).toBe("http://abc.example:5000");
  });
});
