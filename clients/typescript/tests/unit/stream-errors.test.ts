/**
 * `streamFailureError` en aislamiento (M10 ts-core-55): vive en
 * `stream-errors.ts`, sin ciclo con `core.ts` ni `commands.ts`, y
 * `commands.ts` la reexporta para que los imports existentes no cambien.
 */

import { Code, ConnectError } from "@connectrpc/connect";
import { describe, expect, test } from "vitest";
import {
  NotFoundError,
  SandboxError,
  SandboxNotFoundError,
  SandboxStateError,
} from "../../src/errors.js";
import {
  STREAM_PROBE_TIMEOUT_MS as STREAM_PROBE_TIMEOUT_MS_FROM_COMMANDS,
  streamFailureError as streamFailureErrorFromCommands,
} from "../../src/sandbox/commands.js";
import { STREAM_PROBE_TIMEOUT_MS, streamFailureError } from "../../src/sandbox/stream-errors.js";

const RESET = new ConnectError("stream cut", Code.Aborted);

describe("streamFailureError", () => {
  test("a non-reset error goes through the unary translation table", () => {
    const notFound = new ConnectError("nope", Code.NotFound);
    const error = streamFailureError(notFound, { healthOk: false, state: "RUNNING" });
    expect(error).toBeInstanceOf(NotFoundError);
  });

  test("Health still answers: the sandbox lives, the caller can reconnect", () => {
    const error = streamFailureError(RESET, { healthOk: true, state: undefined });
    expect(error).toBeInstanceOf(SandboxError);
    expect(error.message).toContain("reconecta con commands.connect(pid)");
  });

  test("a terminal state: the sandbox is gone", () => {
    const error = streamFailureError(RESET, { healthOk: false, state: "TERMINATED" });
    expect(error).toBeInstanceOf(SandboxNotFoundError);
    expect(error.message).toContain("TERMINATED");
  });

  test("a suspended state: the sandbox is paused, not gone", () => {
    const error = streamFailureError(RESET, { healthOk: false, state: "SUSPENDED" });
    expect(error).toBeInstanceOf(SandboxStateError);
    expect(error.message).toContain("SUSPENDED");
  });

  test("no Health answer and no known state: reported as unresponsive", () => {
    const error = streamFailureError(RESET, { healthOk: false, state: undefined });
    expect(error).toBeInstanceOf(SandboxError);
    expect(error.message).toContain("el agente no responde (estado desconocido)");
  });
});

test("commands.ts re-exports both names unchanged, for every existing import path", () => {
  expect(STREAM_PROBE_TIMEOUT_MS_FROM_COMMANDS).toBe(STREAM_PROBE_TIMEOUT_MS);
  expect(streamFailureErrorFromCommands).toBe(streamFailureError);
});
