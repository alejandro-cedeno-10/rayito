/**
 * Espejo de `test_m1_hello.py::test_hello_rayd`: el transporte propio del SDK
 * TypeScript (h2c + trailers a través del proxy de AWS) y su auth de dos
 * niveles, con clientes gRPC ajenos al `Sandbox` (`raw-rpc.ts`). Sin
 * `x-access-token` sólo `Health` responde y `ProcessService.List` da
 * `UNAUTHENTICATED`; con él, `List` responde vacío y `Resize` sobre un pid
 * desconocido da `NOT_FOUND`; un JWE falso lo rechaza el proxy con un 403 que
 * el SDK reconoce como tal. Un solo sandbox.
 */

import { create } from "@bufbuild/protobuf";
import { Code, ConnectError } from "@connectrpc/connect";
import { describe, expect, test } from "vitest";
import { HealthRequestSchema } from "../../src/gen/rayito/v1/health_pb.js";
import { ListRequestSchema } from "../../src/gen/rayito/v1/process_pb.js";
import { ResizeRequestSchema } from "../../src/gen/rayito/v1/pty_pb.js";
import { Sandbox } from "../../src/index.js";
import { isProxyForbidden } from "../../src/transport/errors.js";
import { createTestSandbox, e2eEnabled, useE2E, waitForTerminalState } from "./helpers.js";
import { openRawClients, type RawClients } from "./raw-rpc.js";

const UNKNOWN_PID = 1;
const FAKE_JWE = "not-a-jwe";
const RESIZE_COLS = 80;
const RESIZE_ROWS = 24;
const RPC_TIMEOUT_MS = 30_000;

async function codeOf(call: Promise<unknown>): Promise<Code | undefined> {
  try {
    await call;
    return undefined;
  } catch (error) {
    return error instanceof ConnectError ? error.code : undefined;
  }
}

function listProcesses(clients: RawClients): Promise<unknown> {
  return clients.process.list(create(ListRequestSchema), { timeoutMs: RPC_TIMEOUT_MS });
}

function resizeUnknownPty(clients: RawClients): Promise<unknown> {
  const request = create(ResizeRequestSchema, {
    pid: UNKNOWN_PID,
    size: { cols: RESIZE_COLS, rows: RESIZE_ROWS },
  });
  return clients.pty.resize(request, { timeoutMs: RPC_TIMEOUT_MS });
}

describe.skipIf(!e2eEnabled())("transport two-level auth (mirror of M1)", () => {
  const context = useE2E();

  test("anonymous Health ok and List UNAUTHENTICATED, fake JWE is a proxy 403, authenticated Resize NOT_FOUND, connect and kill", async () => {
    const sbx = await createTestSandbox(context);
    expect(await sbx.isRunning()).toBe(true);
    expect(["PENDING", "RUNNING"]).toContain((await sbx.getInfo()).state);

    const anonymous = await openRawClients(context.controlPlane, sbx.sandboxId, {});
    try {
      const health = await anonymous.health.health(create(HealthRequestSchema), {
        timeoutMs: RPC_TIMEOUT_MS,
      });
      expect(health.agentReady).toBe(true);
      expect(health.sandboxId).toBe(sbx.sandboxId);
      expect(await codeOf(listProcesses(anonymous))).toBe(Code.Unauthenticated);
    } finally {
      anonymous.close();
    }

    const authenticated = await openRawClients(context.controlPlane, sbx.sandboxId, {
      accessToken: sbx.accessToken,
    });
    try {
      expect(await listProcesses(authenticated)).toMatchObject({ processes: [] });
      expect(await codeOf(resizeUnknownPty(authenticated))).toBe(Code.NotFound);
    } finally {
      authenticated.close();
    }

    const forged = await openRawClients(context.controlPlane, sbx.sandboxId, { jwe: FAKE_JWE });
    try {
      const failure = await forged.health
        .health(create(HealthRequestSchema), { timeoutMs: RPC_TIMEOUT_MS })
        .catch((error: unknown) => error);
      expect(failure).toBeInstanceOf(ConnectError);
      expect((failure as ConnectError).code).toBe(Code.PermissionDenied);
      expect(isProxyForbidden(failure)).toBe(true);
    } finally {
      forged.close();
    }

    const reconnected = await Sandbox.connect(sbx.sandboxId, {
      accessToken: sbx.accessToken,
      controlPlane: context.controlPlane,
    });
    try {
      expect(await reconnected.isRunning()).toBe(true);
    } finally {
      reconnected.close();
    }

    expect(await sbx.kill()).toBe(true);
    await waitForTerminalState(sbx.sandboxId, context.controlPlane);
  });
});
