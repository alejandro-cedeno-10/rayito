/**
 * Espejo de `test_m2_processes.py::test_m2_processes` para lo que es código
 * del SDK TypeScript (`sandbox/commands.ts`, `HostAccess`, el token de
 * acceso): `envs` y `cwd`, callbacks de salida, procesos en segundo plano con
 * `tag`, `list` y `kill` (137, "signaled"), `sendStdin` sin stdin, `connect`
 * con `fromSeq`, salida grande, un `sleep` largo a través del proxy,
 * `getHost` con 200 y 403, métricas completas y un token de acceso impostor.
 * La identidad, el entorno desde cero, los rlimits y la política de root son
 * del guest y se prueban una vez, en Python. Un sandbox compartido.
 */

import { beforeAll, describe, expect, test } from "vitest";
import {
  AuthenticationError,
  CommandExitError,
  InvalidArgumentError,
  NotFoundError,
  Sandbox,
} from "../../src/index.js";
import { generateAccessToken } from "../../src/payload.js";
import {
  createTestSandbox,
  e2eEnabled,
  HOME,
  report,
  seconds,
  useE2E,
  waitForTerminalState,
  waitUntil,
} from "./helpers.js";

const LARGE_OUTPUT_BYTES = 3_000_000;
const KEEPALIVE_SLEEP_SECONDS = 35;
const HOST_PORT = 3000;
const HOST_READY_BUDGET_MS = 10_000;
const HOST_POLL_MS = 500;
const HTTP_OK = 200;
const HTTP_FORBIDDEN = 403;
const KILLED_EXIT_CODE = 137;
const KILLED_ERROR = "signaled";
const UNKNOWN_PID = 999_999;
const METRICS_CLOCK_SKEW_MS = 60_000;
const PERCENT_MAX = 100;
const TAG = "ts-e2e";
const REPLAY_COMMAND = "for i in 1 2 3; do echo $i; sleep 1; done";
const REPLAY_OUTPUT = "1\n2\n3\n";
const HTTP_SERVER_COMMAND = `python3 -m http.server ${HOST_PORT} --bind 0.0.0.0`;

async function statusOf(url: string, headers: Record<string, string>): Promise<number> {
  try {
    const response = await fetch(url, { headers });
    await response.arrayBuffer();
    return response.status;
  } catch {
    return 0;
  }
}

describe.skipIf(!e2eEnabled())("processes through the TypeScript SDK (mirror of M2)", () => {
  const context = useE2E();
  let sbx: Sandbox;

  beforeAll(async () => {
    sbx = await createTestSandbox(context);
  });

  test("envs, cwd and output callbacks", async () => {
    expect((await sbx.commands.run("echo $FOO", { envs: { FOO: "bar" } })).stdout.trim()).toBe(
      "bar",
    );
    expect((await sbx.commands.run("pwd", { cwd: "/tmp" })).stdout.trim()).toBe("/tmp");
    await expect(sbx.commands.run("true", { cwd: "/does/not/exist" })).rejects.toBeInstanceOf(
      InvalidArgumentError,
    );
    const out: string[] = [];
    const err: string[] = [];
    await sbx.commands.run("echo a; echo b >&2", {
      onStdout: (text) => out.push(text),
      onStderr: (text) => err.push(text),
    });
    expect(out.join("")).toBe("a\n");
    expect(err.join("")).toBe("b\n");
  });

  test("background with tag, list and kill, and sendStdin without stdin", async () => {
    const handle = await sbx.commands.run("sleep 30", { background: true, tag: TAG });
    const listed = await sbx.commands.list();
    expect(
      listed.some((info) => info.pid === handle.pid && info.kind === "process" && info.tag === TAG),
    ).toBe(true);
    await expect(handle.sendStdin("x")).rejects.toBeInstanceOf(InvalidArgumentError);
    expect(await sbx.commands.kill(handle.pid)).toBe(true);
    const failure = await handle.wait().catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(CommandExitError);
    expect((failure as CommandExitError).exitCode).toBe(KILLED_EXIT_CODE);
    expect((failure as CommandExitError).error).toBe(KILLED_ERROR);
    expect(await sbx.commands.kill(handle.pid)).toBe(false);
  });

  test("connect replays from a sequence and rejects an unknown pid", async () => {
    const handle = await sbx.commands.run(REPLAY_COMMAND, { background: true });
    const full = await sbx.commands.connect(handle.pid, { fromSeq: 1 });
    expect((await full.wait()).stdout).toBe(REPLAY_OUTPUT);
    expect((await handle.wait()).stdout).toBe(REPLAY_OUTPUT);
    const retained = await (await sbx.commands.connect(handle.pid)).wait();
    expect(retained).toMatchObject({ exitCode: 0, stdout: "" });
    await expect(sbx.commands.connect(UNKNOWN_PID)).rejects.toBeInstanceOf(NotFoundError);
  });

  test("large output and a long sleep through the proxy", async () => {
    const started = performance.now();
    const large = await sbx.commands.run(`head -c ${LARGE_OUTPUT_BYTES} /dev/zero | tr '\\0' a`);
    report(`${LARGE_OUTPUT_BYTES} bytes de stdout`, seconds(started));
    expect(large.stdout).toHaveLength(LARGE_OUTPUT_BYTES);
    const keepalive = await sbx.commands.run(`sleep ${KEEPALIVE_SLEEP_SECONDS}`, { timeoutMs: 0 });
    expect(keepalive.exitCode).toBe(0);
  });

  test("getHost serves 200 with the proxy headers and 403 without them", async () => {
    const server = await sbx.commands.run(HTTP_SERVER_COMMAND, {
      background: true,
      timeoutMs: 0,
      cwd: HOME,
    });
    try {
      const host = await sbx.getHost(HOST_PORT);
      await waitUntil(
        async () => (await statusOf(host.url, host.headers)) === HTTP_OK,
        HOST_READY_BUDGET_MS,
        "el servidor HTTP de getHost",
        HOST_POLL_MS,
      );
      expect(await statusOf(host.url, {})).toBe(HTTP_FORBIDDEN);
    } finally {
      expect(await server.kill()).toBe(true);
    }
  });

  test("an impostor access token is rejected and the metrics are complete", async () => {
    const other = await Sandbox.connect(sbx.sandboxId, {
      accessToken: sbx.accessToken,
      controlPlane: context.controlPlane,
    });
    try {
      expect((await other.commands.run("echo hola")).stdout.trim()).toBe("hola");
    } finally {
      other.close();
    }
    const impostor = await Sandbox.connect(sbx.sandboxId, {
      accessToken: generateAccessToken(),
      controlPlane: context.controlPlane,
    });
    try {
      await expect(impostor.commands.run("true")).rejects.toBeInstanceOf(AuthenticationError);
    } finally {
      impostor.close();
    }
    const metrics = await sbx.getMetrics();
    expect(metrics.cpuCount).toBeGreaterThanOrEqual(1);
    expect(metrics.memTotalBytes).toBeGreaterThan(0);
    expect(metrics.memUsedBytes).toBeLessThanOrEqual(metrics.memTotalBytes);
    expect(metrics.diskTotalBytes).toBeGreaterThan(0);
    expect(metrics.cpuUsedPct).toBeGreaterThanOrEqual(0);
    expect(metrics.cpuUsedPct).toBeLessThanOrEqual(PERCENT_MAX);
    expect(Math.abs(Date.now() - metrics.timestamp.getTime())).toBeLessThan(METRICS_CLOCK_SKEW_MS);
    expect(await sbx.kill()).toBe(true);
    await waitForTerminalState(sbx.sandboxId, context.controlPlane);
  });
});
