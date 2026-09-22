import { Code, ConnectError } from "@connectrpc/connect";
import { describe, expect, test } from "vitest";
import { SandboxError, SandboxNotFoundError, SandboxStateError } from "../../src/errors.js";
import { FilesystemEventType as EventTypeProto } from "../../src/gen/rayito/v1/filesystem_pb.js";
import { ReconnectPoll } from "../../src/sandbox/readiness.js";
import { Sandbox } from "../../src/sandbox/sandbox.js";
import { bytes } from "./fake/common.js";
import type { FakeControlPlane } from "./fake/control-plane.js";
import type { FakeRayd } from "./fake/server.js";
import {
  Collector,
  createTestSandbox,
  sleep,
  type TestSandbox,
  useFastStateChecks,
  waitUntil,
  withTimeout,
} from "./helpers.js";

const HOME = "/home/user";
const AUTO_RESUME = { maxIdleSeconds: 60, suspendedDurationSeconds: 0, autoResume: true };
const MANUAL_RESUME = { maxIdleSeconds: 60, suspendedDurationSeconds: 0, autoResume: false };

function healthCalls(rayd: FakeRayd): number {
  return rayd.health.healthCalls.length;
}

async function running(
  options: Parameters<typeof createTestSandbox>[0] = {},
): Promise<TestSandbox> {
  const created = await createTestSandbox(options);
  created.plane.setStates(["RUNNING"]);
  return created;
}

function suspendedPlane(plane: FakeControlPlane, autoResume: boolean): void {
  plane.idle = autoResume ? AUTO_RESUME : MANUAL_RESUME;
  plane.setStates(["SUSPENDED"]);
}

describe("reconnection contract", () => {
  useFastStateChecks();

  test("background handle survives a suspend/resume", async () => {
    const { sandbox, rayd } = await running();
    const handle = await sandbox.commands.run("seq 40", { background: true, timeoutMs: 0 });
    const collector = new Collector(handle);
    await waitUntil(() => collector.chunks.length >= 2);
    const seenBefore = handle.lastSeq;
    const healthBefore = healthCalls(rayd);
    rayd.suspendResume({ unavailableCalls: 3 });
    await collector.join();
    expect(collector.error).toBeUndefined();
    expect(collector.text).toBe(Array.from({ length: 40 }, (_, i) => `${i + 1}\n`).join(""));
    expect((await handle.wait()).exitCode).toBe(0);
    expect(handle.reconnects).toBe(1);
    const [pid, fromSeq] = rayd.process.connectCalls.at(-1) as [number, number];
    expect(pid).toBe(handle.pid);
    expect(fromSeq).toBeGreaterThan(seenBefore);
    expect(fromSeq).toBeLessThanOrEqual(handle.lastSeq + 1);
    expect(healthCalls(rayd) - healthBefore).toBe(4);
    expect(sandbox.resumeGeneration).toBe(1);
  });

  test("a dropped HTTP/2 session during the Health poll is not yet, not a failure", async () => {
    const { sandbox, rayd } = await running();
    const handle = await sandbox.commands.run("seq 40", { background: true, timeoutMs: 0 });
    const collector = new Collector(handle);
    await waitUntil(() => collector.chunks.length >= 2);
    const healthBefore = healthCalls(rayd);
    rayd.suspendResume({ unavailableCalls: 1 });
    rayd.health.failNext.push(
      new ConnectError("read ECONNRESET", Code.Aborted),
      new ConnectError("http/2 stream closed with error code INTERNAL_ERROR (0x2)", Code.Internal),
      new ConnectError("http/2 stream closed with error code CANCEL (0x8)", Code.Canceled),
    );
    await collector.join();
    expect(collector.error).toBeUndefined();
    expect(collector.text).toBe(Array.from({ length: 40 }, (_, i) => `${i + 1}\n`).join(""));
    expect((await handle.wait()).exitCode).toBe(0);
    expect(handle.reconnects).toBe(1);
    expect(healthCalls(rayd) - healthBefore).toBe(5);
    expect(sandbox.resumeGeneration).toBe(1);
  });

  test("a foreground run survives a reset form on the first probe after a cut", async () => {
    const { sandbox, rayd } = await running();
    const pending = sandbox.commands.run("seq 40", { timeoutMs: 0 });
    await waitUntil(() => (rayd.process.processes.values().next().value?.ring.length ?? 0) >= 2);
    rayd.suspendResume();
    rayd.health.failNext.push(new ConnectError("read ECONNRESET", Code.Aborted));
    const result = await withTimeout(pending, 10_000);
    expect(result.exitCode).toBe(0);
    expect(result.stdout).toBe(Array.from({ length: 40 }, (_, i) => `${i + 1}\n`).join(""));
    expect(sandbox.resumeGeneration).toBe(1);
  });

  test("an unread handle reconnects on its first wait", async () => {
    const { sandbox, rayd } = await running();
    const handle = await sandbox.commands.run("sleep 1", { background: true, timeoutMs: 0 });
    rayd.suspendResume({ unavailableCalls: 1 });
    expect((await handle.wait()).exitCode).toBe(0);
    expect(handle.reconnects).toBe(1);
    expect(rayd.process.connectCalls.at(-1)).toEqual([handle.pid, 1]);
  });

  test("OutOfRange falls back to fromSeq 0 with a warning", async () => {
    const { sandbox, rayd, logger } = await running();
    const handle = await sandbox.commands.run("cat", {
      background: true,
      stdin: true,
      timeoutMs: 0,
    });
    const process = rayd.process.processes.get(handle.pid);
    if (process === undefined) {
      throw new Error("el fake no registró el proceso");
    }
    const collector = new Collector(handle);
    process.stdin.push(bytes("a\n"));
    await waitUntil(() => collector.text === "a\n");
    rayd.suspend();
    process.stdin.push(bytes("b\n"));
    process.stdin.push(bytes("c\n"));
    await waitUntil(() => process.nextSeq === 4);
    process.ring.splice(0, 2);
    rayd.resume();
    await waitUntil(() => handle.reconnects === 1);
    process.stdin.push(bytes("d\n"));
    process.stdin.push(null);
    await collector.join();
    expect(collector.error).toBeUndefined();
    expect(collector.text).toBe("a\nd\n");
    expect(rayd.process.connectCalls.slice(-2)).toEqual([
      [handle.pid, 2],
      [handle.pid, 0],
    ]);
    expect(logger.at("warn").some((line) => line.message.includes("se perdió salida"))).toBe(true);
  });

  test("PTY re-attaches through Pty.Connect and a watch re-issues WatchDir", async () => {
    const { sandbox, rayd } = await running();
    const pty = await sandbox.pty.create({ timeoutMs: 0 });
    await pty.sendInput("echo uno\n");
    const iterator = pty[Symbol.asyncIterator]();
    const outputLine = (word: string) => new RegExp(`(^|\\n)${word}\\r\\n`);
    let seen = "";
    while (!outputLine("uno").test(seen)) {
      seen += new TextDecoder().decode((await iterator.next()).value?.pty);
    }
    const seenSeq = pty.lastSeq;
    const exits: Error[] = [];
    const watch = await sandbox.files.watchDir(HOME, {
      recursive: true,
      onExit: (error) => exits.push(error),
    });
    rayd.suspendResume({ unavailableCalls: 2 });
    await waitUntil(() => watch.reconnects === 1);
    await sandbox.pty.sendInput(pty.pid, "echo dos\n");
    seen = "";
    while (!outputLine("dos").test(seen)) {
      seen += new TextDecoder().decode((await withTimeout(iterator.next(), 10_000)).value?.pty);
    }
    expect(seen).not.toContain("uno");
    expect(pty.reconnects).toBe(1);
    expect(rayd.pty.connectCalls.at(-1)).toEqual([pty.pid, seenSeq + 1]);
    expect(rayd.filesystem.watchCalls).toHaveLength(2);
    expect(rayd.filesystem.watchCalls[0]).toEqual(rayd.filesystem.watchCalls[1]);
    await waitUntil(() => rayd.filesystem.liveWatches === 1);
    rayd.filesystem.emit(HOME, "after.txt", EventTypeProto.CREATE);
    await waitUntil(() => (rayd.filesystem.watches.get(HOME)?.[0]?.events.size ?? 1) === 0);
    await sleep(50);
    expect(watch.isRunning).toBe(true);
    expect(exits).toEqual([]);
    expect(watch.getNewEvents().map((event) => event.name)).toEqual(["after.txt"]);
    expect(await pty.kill()).toBe(true);
    await watch.stop();
  });

  test("runCode reattaches after started", async () => {
    const { sandbox, rayd } = await running();
    const pending = sandbox.runCode("slow 2");
    await waitUntil(() => (rayd.code.runs.values().next().value?.ring.length ?? 0) >= 2);
    await sleep(50);
    rayd.suspendResume({ unavailableCalls: 1 });
    const execution = await pending;
    expect(execution.error).toBeUndefined();
    expect(execution.text).toBe("'slow'");
    const [contextId, executionId, fromSeq] = rayd.code.reattachCalls[0] as [
      string,
      string,
      number,
    ];
    expect(contextId).toBe("default");
    expect(executionId).toBe(rayd.code.executions[0]?.executionId);
    expect(fromSeq).toBeGreaterThanOrEqual(2);
    expect(rayd.code.executeRequests).toHaveLength(1);
  });

  test("a cut before started is not retried", async () => {
    const { sandbox, rayd } = await running();
    rayd.code.phase = "suspending";
    await expect(sandbox.runCode("x = 1")).rejects.toBeInstanceOf(SandboxStateError);
    expect(rayd.code.executeRequests).toHaveLength(1);
  });

  test("a unary is retried once after a reconnect", async () => {
    const { sandbox, rayd } = await running();
    const handle = await sandbox.commands.run("sleep 30", { background: true, timeoutMs: 0 });
    rayd.process.signalUnavailableCalls = 1;
    rayd.health.resumeGeneration = 1;
    expect(await sandbox.commands.kill(handle.pid)).toBe(true);
    expect(rayd.process.signalRequests).toHaveLength(2);
    expect(sandbox.resumeGeneration).toBe(1);
    handle.disconnect();
  });

  test("terminated during the poll is SandboxNotFoundError", async () => {
    const { sandbox, rayd, plane } = await running();
    const handle = await sandbox.commands.run("sleep 30", { background: true, timeoutMs: 0 });
    rayd.health.unavailableCalls = 1000;
    rayd.suspend();
    plane.setStates(["TERMINATED"]);
    const started = performance.now();
    await expect(handle.wait()).rejects.toBeInstanceOf(SandboxNotFoundError);
    expect(performance.now() - started).toBeLessThan(4000);
  });

  test("suspended without auto-resume stops a foreground run", async () => {
    const { sandbox, rayd, plane } = await running();
    const pending = sandbox.commands.run("slow 5", { timeoutMs: 0 });
    await waitUntil(() => rayd.process.liveProcesses().length === 1);
    await sleep(50);
    rayd.health.unavailableCalls = 1000;
    rayd.suspend();
    suspendedPlane(plane, false);
    const error = await pending.catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(SandboxStateError);
    expect((error as Error).message).toContain("resume()");
  });

  test("a background handle sleeps through a suspension without probing Health", async () => {
    const { sandbox, rayd, plane } = await running({ reconnectTimeoutMs: 500 });
    suspendedPlane(plane, true);
    const handle = await sandbox.commands.run("sleep 1", { background: true, timeoutMs: 0 });
    rayd.suspend();
    const healthBefore = healthCalls(rayd);
    const collector = new Collector(handle);
    await sleep(1500);
    expect(collector.done).toBe(false);
    expect(healthCalls(rayd)).toBe(healthBefore);
    expect(plane.callsTo("getMicrovm").length).toBeGreaterThanOrEqual(2);
    rayd.resume();
    plane.setStates(["RUNNING"]);
    await collector.join();
    expect(collector.error).toBeUndefined();
    expect((await handle.wait()).exitCode).toBe(0);
    expect(handle.reconnects).toBe(1);
    expect(healthCalls(rayd) - healthBefore).toBe(1);
  });

  test("a dormant handle waits for resume() even without auto-resume", async () => {
    const { sandbox, rayd, plane } = await running({ reconnectTimeoutMs: 500 });
    suspendedPlane(plane, false);
    const handle = await sandbox.commands.run("sleep 1", { background: true, timeoutMs: 0 });
    rayd.suspend();
    const collector = new Collector(handle);
    await sleep(1200);
    expect(collector.done).toBe(false);
    rayd.resume();
    plane.setStates(["RUNNING"]);
    await collector.join();
    expect(collector.error).toBeUndefined();
    expect(handle.reconnects).toBe(1);
  });

  test("explicit pause keeps in-flight foreground streams dormant until resume", async () => {
    const { sandbox, rayd, plane } = await running();
    const command = sandbox.commands.run("slow 3", { timeoutMs: 0 });
    const cell = sandbox.runCode("slow 3");
    await waitUntil(
      () => rayd.process.liveProcesses().length === 1 && rayd.code.executions.length === 1,
    );
    await sleep(100);
    expect(await sandbox.pause({ wait: false })).toBe(true);
    expect(Sandbox.coreOf(sandbox).paused).toBe(true);
    rayd.suspend();
    suspendedPlane(plane, true);
    const healthBefore = healthCalls(rayd);
    await sleep(600);
    expect(healthCalls(rayd)).toBe(healthBefore);
    rayd.resume();
    plane.setStates(["RUNNING"]);
    await sandbox.resume();
    expect(Sandbox.coreOf(sandbox).paused).toBe(false);
    const result = await withTimeout(command, 10_000);
    expect(result.stdout).toContain("slow end");
    const execution = await withTimeout(cell, 10_000);
    expect(execution.text).toBe("'slow'");
    expect(rayd.process.connectCalls.length).toBeGreaterThanOrEqual(1);
    expect(rayd.code.reattachCalls).toHaveLength(1);
  });

  test("two handles cut together share one Health poll", async () => {
    const { sandbox, rayd } = await running();
    const first = await sandbox.commands.run("seq 40", { background: true, timeoutMs: 0 });
    const second = await sandbox.commands.connect(first.pid, { fromSeq: 1 });
    const collectors = [new Collector(first), new Collector(second)];
    await waitUntil(() => collectors.every((collector) => collector.chunks.length >= 2));
    const healthBefore = healthCalls(rayd);
    rayd.suspendResume({ unavailableCalls: 3 });
    for (const collector of collectors) {
      await collector.join();
      expect(collector.error).toBeUndefined();
      expect(collector.text).toBe(Array.from({ length: 40 }, (_, i) => `${i + 1}\n`).join(""));
    }
    expect(first.reconnects).toBe(1);
    expect(second.reconnects).toBe(1);
    expect(healthCalls(rayd) - healthBefore).toBe(4);
  });

  test("disconnected handles never reconnect and close() ends dormant waits", async () => {
    const { sandbox, rayd, plane } = await running();
    const handle = await sandbox.commands.run("sleep 30", { background: true, timeoutMs: 0 });
    handle.disconnect();
    rayd.suspendResume({ unavailableCalls: 1 });
    await expect(handle.wait()).rejects.toThrow(/desconectado/);
    expect(rayd.process.connectCalls).toEqual([]);
    expect(await sandbox.commands.kill(handle.pid)).toBe(true);

    const dormant = await sandbox.commands.run("sleep 30", { background: true, timeoutMs: 0 });
    rayd.suspend();
    suspendedPlane(plane, true);
    const collector = new Collector(dormant);
    await sleep(200);
    sandbox.close();
    await collector.join();
    expect(collector.error).toBeInstanceOf(SandboxError);
    await expect(dormant.wait()).rejects.toBeInstanceOf(SandboxError);
  });

  test("the reconnect deadline surfaces as a plain SandboxError", async () => {
    const { sandbox, rayd } = await running({ reconnectTimeoutMs: 1000 });
    const handle = await sandbox.commands.run("sleep 30", { background: true, timeoutMs: 0 });
    const collector = new Collector(handle);
    await sleep(50);
    rayd.health.unavailableCalls = 10_000;
    const healthBefore = healthCalls(rayd);
    rayd.process.processes.get(handle.pid)?.cut(Code.Unavailable, "HTTP 502");
    await collector.join(10_000);
    expect(collector.error).toBeInstanceOf(SandboxError);
    expect(collector.error).not.toBeInstanceOf(SandboxStateError);
    expect(collector.error?.message).toContain("no volvió a responder");
    expect(healthCalls(rayd) - healthBefore).toBeGreaterThanOrEqual(2);
  });

  test("three futile reconnects end the handle with the M2 classification", async () => {
    const { sandbox, rayd } = await running();
    const handle = await sandbox.commands.run("sleep 30", { background: true, timeoutMs: 0 });
    const collector = new Collector(handle);
    await sleep(50);
    const process = rayd.process.processes.get(handle.pid);
    if (process === undefined) {
      throw new Error("el fake no registró el proceso");
    }
    for (let cut = 0; cut < 4; cut += 1) {
      await waitUntil(() => process.subscribers.length === 1);
      process.cut(Code.Unavailable, "HTTP 502");
    }
    await collector.join(15_000);
    expect(collector.error).toBeInstanceOf(SandboxError);
    expect(collector.error?.message).toContain("commands.connect(pid)");
    expect(handle.reconnects).toBe(3);
    expect(rayd.process.connectCalls).toHaveLength(3);
  });
});

describe("resume() wakes dormant handles at once", () => {
  test("re-subscribes within 2 s, before the next 5 s state check", async () => {
    expect(ReconnectPoll.stateCheckIntervalMs).toBe(5000);
    const { sandbox, rayd, plane } = await createTestSandbox();
    suspendedPlane(plane, true);
    const handle = await sandbox.commands.run("sleep 1", { background: true, timeoutMs: 0 });
    rayd.suspend();
    const collector = new Collector(handle);
    await waitUntil(() => plane.callsTo("getMicrovm").length >= 1);
    rayd.resume();
    plane.setStates(["RUNNING"]);
    const started = performance.now();
    await sandbox.resume();
    await collector.join(2000);
    expect(performance.now() - started).toBeLessThan(2000);
    expect(collector.error).toBeUndefined();
    expect(handle.reconnects).toBe(1);
  });

  test("a dormant handle does not block a foreground call", async () => {
    expect(ReconnectPoll.stateCheckIntervalMs).toBe(5000);
    const { sandbox, rayd, plane } = await createTestSandbox();
    suspendedPlane(plane, true);
    const handle = await sandbox.commands.run("sleep 1", { background: true, timeoutMs: 0 });
    rayd.suspend();
    const collector = new Collector(handle);
    await waitUntil(() => plane.callsTo("getMicrovm").length >= 1);
    const healthBefore = healthCalls(rayd);
    setTimeout(() => rayd.resume(), 300);
    const started = performance.now();
    const result = await sandbox.commands.run("echo hola");
    expect(result.stdout.trim()).toBe("hola");
    expect(performance.now() - started).toBeLessThan(ReconnectPoll.stateCheckIntervalMs);
    expect(healthCalls(rayd)).toBeGreaterThan(healthBefore);
    await collector.join(2000);
    expect(collector.error).toBeUndefined();
    expect(handle.reconnects).toBe(1);
  });
});
