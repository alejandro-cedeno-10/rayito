import { Code, ConnectError } from "@connectrpc/connect";
import { describe, expect, test, vi } from "vitest";
import { PortSpec } from "../../src/aws/control-plane.js";
import {
  AuthenticationError,
  InvalidArgumentError,
  SandboxNotFoundError,
  SandboxNotReadyError,
} from "../../src/errors.js";
import { ReadinessPoll } from "../../src/sandbox/readiness.js";
import { Sandbox } from "../../src/sandbox/sandbox.js";
import {
  PROXY_AUTH_HEADER,
  PROXY_FORCE_H2_HEADER,
  PROXY_PORT_HEADER,
} from "../../src/transport/headers.js";
import { DEFAULT_TRANSPORT_SETTINGS } from "../../src/transport/transport.js";
import { FakeControlPlane, IMAGE_ARN, SANDBOX_ID } from "./fake/control-plane.js";
import type { FakeRayd } from "./fake/server.js";
import {
  ACCESS_TOKEN,
  Collector,
  createTestSandbox,
  RecordingLogger,
  sleep,
  startRayd,
  waitUntil,
} from "./helpers.js";

function serverConnections(rayd: FakeRayd): Promise<number> {
  return new Promise((resolve, reject) => {
    rayd.server.getConnections((error, count) => (error ? reject(error) : resolve(count)));
  });
}

async function waitForConnections(rayd: FakeRayd, expected: number): Promise<void> {
  const deadline = performance.now() + 5000;
  while ((await serverConnections(rayd)) !== expected) {
    if (performance.now() > deadline) {
      throw new Error(`el servidor no llegó a ${expected} conexiones`);
    }
    await sleep(20);
  }
}

describe("Sandbox.create", () => {
  test("run-microvm, token, four Health probes, refresher scheduled", async () => {
    const { sandbox, plane, rayd } = await createTestSandbox({
      beforeCreate: (fake) => {
        fake.health.notReadyCalls = 2;
        fake.health.kernelNotReadyCalls = 1;
      },
    });
    const operations = plane.calls.map((call) => call.operation);
    expect(operations.filter((name) => name !== "resolveTemplateArn").slice(0, 2)).toEqual([
      "runMicrovm",
      "createAuthToken",
    ]);
    expect(rayd.health.healthCalls).toHaveLength(4);
    expect(Sandbox.coreOf(sandbox).refresher.scheduled).toBe(true);
    expect(sandbox.info.sandboxId).toBe(SANDBOX_ID);
    expect(sandbox.sandboxId).toBe(SANDBOX_ID);
    expect(sandbox.accessToken).toBe(ACCESS_TOKEN);
    expect(sandbox.endpoint).toBe(rayd.host);
    expect(sandbox.endpointUrl).toBe(`https://${rayd.host}`);
    expect(sandbox.region).toBe("us-east-1");
    expect(sandbox.resumeGeneration).toBe(0);
    expect(plane.callsTo("createAuthToken")[0]?.ports).toEqual([PortSpec.single(8080)]);
  });

  test("the launch request carries the resolved options", async () => {
    const { plane } = await createTestSandbox({
      create: { idle: { maxIdleSeconds: 60 }, timeoutMs: 120_000, allowedPorts: [3000] },
    });
    const request = plane.launches[0];
    expect(request?.maximumDurationSeconds).toBe(120);
    expect(request?.idle).toEqual({
      maxIdleSeconds: 60,
      suspendedDurationSeconds: 60,
      autoResume: true,
    });
    expect(plane.callsTo("createAuthToken")[0]?.ports).toEqual([
      PortSpec.single(8080),
      PortSpec.single(3000),
    ]);
  });

  test("not ready within readyTimeoutMs terminates the VM unless keepOnFailure", async () => {
    const rayd = await startRayd();
    rayd.health.notReadyCalls = 1000;
    const previous = ReadinessPoll.maxDelayMs;
    ReadinessPoll.maxDelayMs = 200;
    try {
      const plane = new FakeControlPlane({ endpoint: rayd.host, states: ["RUNNING"] });
      const error = await Sandbox.create({
        template: IMAGE_ARN,
        idle: null,
        accessToken: ACCESS_TOKEN,
        controlPlane: plane,
        transport: rayd.transport,
        readyTimeoutMs: 1000,
      }).catch((caught: unknown) => caught);
      expect(error).toBeInstanceOf(SandboxNotReadyError);
      expect((error as SandboxNotReadyError).state).toBe("RUNNING");
      expect((error as SandboxNotReadyError).message).toContain("terminado");
      expect(plane.callsTo("terminateMicrovm")).toHaveLength(1);

      const kept = new FakeControlPlane({ endpoint: rayd.host, states: ["RUNNING"] });
      await expect(
        Sandbox.create({
          template: IMAGE_ARN,
          idle: null,
          accessToken: ACCESS_TOKEN,
          controlPlane: kept,
          transport: rayd.transport,
          readyTimeoutMs: 1000,
          keepOnFailure: true,
        }),
      ).rejects.toBeInstanceOf(SandboxNotReadyError);
      expect(kept.callsTo("terminateMicrovm")).toHaveLength(0);
    } finally {
      ReadinessPoll.maxDelayMs = previous;
      await rayd.close();
    }
  });

  test("a MicroVM that dies during boot is SandboxNotReadyError with its stateReason", async () => {
    const rayd = await startRayd();
    rayd.health.unavailableCalls = 1000;
    const previous = ReadinessPoll.stateCheckIntervalMs;
    ReadinessPoll.stateCheckIntervalMs = 100;
    try {
      const plane = new FakeControlPlane({ endpoint: rayd.host, states: ["TERMINATED"] });
      plane.stateReason = "run hook failed";
      const error = await Sandbox.create({
        template: IMAGE_ARN,
        idle: null,
        accessToken: ACCESS_TOKEN,
        controlPlane: plane,
        transport: rayd.transport,
        readyTimeoutMs: 5000,
      }).catch((caught: unknown) => caught);
      expect(error).toBeInstanceOf(SandboxNotReadyError);
      expect((error as SandboxNotReadyError).stateReason).toBe("run hook failed");
      expect(plane.callsTo("terminateMicrovm")).toHaveLength(0);
    } finally {
      ReadinessPoll.stateCheckIntervalMs = previous;
      await rayd.close();
    }
  });

  test("a transport reset during the boot poll counts as not yet", async () => {
    const { sandbox, rayd, plane } = await createTestSandbox({
      beforeCreate: (fake) => {
        fake.health.failNext.push(
          new ConnectError("read ECONNRESET", Code.Aborted),
          new ConnectError(
            "http/2 stream closed with error code INTERNAL_ERROR (0x2)",
            Code.Internal,
          ),
        );
      },
    });
    expect(rayd.health.healthCalls).toHaveLength(3);
    expect(sandbox.resumeGeneration).toBe(0);
    expect(plane.callsTo("terminateMicrovm")).toHaveLength(0);
  });

  test("plaintext http towards a non-loopback endpoint is refused and the VM terminated", async () => {
    const rayd = await startRayd();
    try {
      const plane = new FakeControlPlane({ endpoint: "abc.example", states: ["PENDING"] });
      await expect(
        Sandbox.create({
          template: IMAGE_ARN,
          accessToken: ACCESS_TOKEN,
          controlPlane: plane,
          transport: { scheme: "http", port: rayd.port },
        }),
      ).rejects.toBeInstanceOf(InvalidArgumentError);
      expect(plane.callsTo("terminateMicrovm")).toHaveLength(1);
      expect(rayd.sessions).toBe(0);
    } finally {
      await rayd.close();
    }
  });

  test("a failure before readiness that is not NotReady terminates the VM", async () => {
    const rayd = await startRayd();
    try {
      const plane = new FakeControlPlane({ endpoint: rayd.host });
      plane.createAuthToken = async () => {
        throw new Error("mint failed");
      };
      await expect(
        Sandbox.create({
          template: IMAGE_ARN,
          idle: null,
          accessToken: ACCESS_TOKEN,
          controlPlane: plane,
          transport: rayd.transport,
        }),
      ).rejects.toThrow("mint failed");
      expect(plane.callsTo("terminateMicrovm")).toHaveLength(1);
    } finally {
      await rayd.close();
    }
  });

  test("template and payload validation happen before any AWS call", async () => {
    const plane = new FakeControlPlane({ endpoint: "127.0.0.1" });
    await expect(
      Sandbox.create({ template: IMAGE_ARN, controlPlane: plane, envs: { BIG: "x".repeat(5000) } }),
    ).rejects.toBeInstanceOf(InvalidArgumentError);
    expect(plane.callsTo("runMicrovm")).toHaveLength(0);
  });
});

describe("Sandbox.connect", () => {
  test("connects to a running sandbox and never terminates on failure", async () => {
    const rayd = await startRayd();
    try {
      const plane = new FakeControlPlane({ endpoint: rayd.host, states: ["RUNNING"] });
      const sandbox = await Sandbox.connect(SANDBOX_ID, {
        accessToken: ACCESS_TOKEN,
        controlPlane: plane,
        transport: rayd.transport,
      });
      expect(await sandbox.isRunning()).toBe(true);
      expect(plane.callsTo("resumeMicrovm")).toHaveLength(0);
      expect(plane.callsTo("createAuthToken")[0]?.ports).toEqual([PortSpec.single(8080)]);
      sandbox.close();
      const gone = new FakeControlPlane({ endpoint: rayd.host, states: ["TERMINATED"] });
      await expect(
        Sandbox.connect(SANDBOX_ID, {
          accessToken: ACCESS_TOKEN,
          controlPlane: gone,
          transport: rayd.transport,
        }),
      ).rejects.toBeInstanceOf(SandboxNotFoundError);
      expect(gone.callsTo("terminateMicrovm")).toHaveLength(0);
      await expect(
        Sandbox.connect(SANDBOX_ID, { controlPlane: gone, accessToken: "" }),
      ).rejects.toBeInstanceOf(AuthenticationError);
    } finally {
      await rayd.close();
    }
  });

  test("resumes a SUSPENDED sandbox only without auto-resume", async () => {
    const rayd = await startRayd();
    try {
      const manual = new FakeControlPlane({
        endpoint: rayd.host,
        states: ["SUSPENDED"],
        idle: { maxIdleSeconds: 60, suspendedDurationSeconds: 60, autoResume: false },
      });
      const first = await Sandbox.connect(SANDBOX_ID, {
        accessToken: ACCESS_TOKEN,
        controlPlane: manual,
        transport: rayd.transport,
      });
      first.close();
      const operations = manual.calls.map((call) => call.operation);
      expect(operations.indexOf("resumeMicrovm")).toBeGreaterThan(-1);
      expect(operations.indexOf("resumeMicrovm")).toBeLessThan(
        operations.indexOf("createAuthToken"),
      );

      const automatic = new FakeControlPlane({
        endpoint: rayd.host,
        states: ["SUSPENDED"],
        idle: { maxIdleSeconds: 60, suspendedDurationSeconds: 60, autoResume: true },
      });
      const second = await Sandbox.connect(SANDBOX_ID, {
        accessToken: ACCESS_TOKEN,
        controlPlane: automatic,
        transport: rayd.transport,
      });
      second.close();
      expect(automatic.callsTo("resumeMicrovm")).toHaveLength(0);
    } finally {
      await rayd.close();
    }
  });
});

describe("lifecycle", () => {
  test("kill terminates and closes, also as static and via await using", async () => {
    const { sandbox, plane } = await createTestSandbox();
    expect(await sandbox.kill()).toBe(true);
    expect(plane.callsTo("terminateMicrovm")).toHaveLength(1);
    expect(Sandbox.coreOf(sandbox).closed).toBe(true);
    expect(Sandbox.coreOf(sandbox).refresher.scheduled).toBe(false);
    plane.terminateMissing = true;
    expect(await Sandbox.kill(SANDBOX_ID, { controlPlane: plane })).toBe(false);
    await expect(Sandbox.kill("", { controlPlane: plane })).rejects.toBeInstanceOf(
      InvalidArgumentError,
    );
  });

  test("await using kills on scope exit", async () => {
    const { rayd, plane } = await createTestSandbox();
    const disposedCores = [];
    {
      await using sbx = await Sandbox.create({
        template: IMAGE_ARN,
        idle: null,
        accessToken: ACCESS_TOKEN,
        controlPlane: plane,
        transport: rayd.transport,
      });
      disposedCores.push(Sandbox.coreOf(sbx));
      expect(await sbx.isRunning()).toBe(true);
    }
    expect(plane.callsTo("terminateMicrovm")).toHaveLength(1);
    expect(disposedCores[0]?.closed).toBe(true);
  });

  test("getInfo refreshes info; static getInfo validates the id", async () => {
    const { sandbox, plane } = await createTestSandbox();
    plane.setStates(["RUNNING"]);
    expect((await sandbox.getInfo()).state).toBe("RUNNING");
    expect(sandbox.info.state).toBe("RUNNING");
    expect((await Sandbox.getInfo(SANDBOX_ID, { controlPlane: plane })).sandboxId).toBe(SANDBOX_ID);
    await expect(Sandbox.getInfo("x".repeat(300), { controlPlane: plane })).rejects.toBeInstanceOf(
      InvalidArgumentError,
    );
  });

  test("pause is idempotent, marks the instance and waits for SUSPENDED", async () => {
    const { sandbox, plane } = await createTestSandbox();
    plane.setStates(["RUNNING", "SUSPENDING", "SUSPENDED"]);
    expect(await sandbox.pause()).toBe(true);
    expect(plane.callsTo("suspendMicrovm")).toHaveLength(1);
    expect(sandbox.info.state).toBe("SUSPENDED");
    expect(Sandbox.coreOf(sandbox).paused).toBe(true);
    expect(Sandbox.coreOf(sandbox).refresher.scheduled).toBe(true);
    expect(await sandbox.pause()).toBe(false);
    expect(plane.callsTo("suspendMicrovm")).toHaveLength(1);
    plane.setStates(["RUNNING"]);
    plane.suspendConflicts = true;
    expect(await sandbox.pause({ wait: false })).toBe(false);
    expect(Sandbox.coreOf(sandbox).paused).toBe(true);
  });

  test("pause with wait false returns right after suspend-microvm", async () => {
    const { sandbox, plane } = await createTestSandbox();
    plane.setStates(["RUNNING"]);
    expect(await sandbox.pause({ wait: false })).toBe(true);
    expect(plane.callsTo("getMicrovm").length).toBeGreaterThanOrEqual(1);
  });

  test("resume clears the pause, re-mints and records the generation", async () => {
    const { sandbox, plane, rayd } = await createTestSandbox();
    Sandbox.coreOf(sandbox).paused = true;
    rayd.health.resumeGeneration = 1;
    const mintsBefore = plane.callsTo("createAuthToken").length;
    await sandbox.resume();
    expect(plane.callsTo("resumeMicrovm")).toHaveLength(1);
    expect(plane.callsTo("createAuthToken")).toHaveLength(mintsBefore + 1);
    expect(sandbox.resumeGeneration).toBe(1);
    expect(Sandbox.coreOf(sandbox).paused).toBe(false);
    expect((await sandbox.getHealth()).resumeGeneration).toBe(1);
    plane.resumeConflicts = true;
    await expect(sandbox.resume({ wait: false })).resolves.toBeUndefined();
  });

  test("static pause and resume poll get-microvm", async () => {
    const plane = new FakeControlPlane({ endpoint: "127.0.0.1", states: ["RUNNING", "SUSPENDED"] });
    expect(await Sandbox.pause(SANDBOX_ID, { controlPlane: plane })).toBe(true);
    expect(await Sandbox.pause(SANDBOX_ID, { controlPlane: plane })).toBe(false);
    plane.setStates(["SUSPENDED", "RUNNING"]);
    await Sandbox.resume(SANDBOX_ID, { controlPlane: plane });
    expect(plane.callsTo("resumeMicrovm")).toHaveLength(1);
    plane.setStates(["TERMINATED"]);
    await expect(Sandbox.resume(SANDBOX_ID, { controlPlane: plane })).rejects.toBeInstanceOf(
      SandboxNotFoundError,
    );
  });

  test("isRunning, getHealth and getMetrics", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    expect(await sandbox.isRunning()).toBe(true);
    const health = await sandbox.getHealth();
    expect(health).toMatchObject({
      agentReady: true,
      kernelReady: true,
      agentVersion: "test",
      uptimeMs: 12_345,
      sandboxId: SANDBOX_ID,
      resumeGeneration: 0,
      clockOffsetMs: 0,
      kernelStateLost: false,
    });
    const metrics = await sandbox.getMetrics();
    expect(metrics.memTotalBytes).toBe(2 * 1024 * 1024 * 1024);
    expect(metrics.cpuCount).toBe(1);
    expect(metrics.timestamp.getTime()).toBe(1_789_000_000_123);
    expect(rayd.health.metricsCalls[0]?.["x-access-token"]).toBe(ACCESS_TOKEN);
    rayd.health.unavailableCalls = 1;
    expect(await sandbox.isRunning()).toBe(false);
  });

  test("getHost mints a token for the port and exposes headers without force-h2", async () => {
    const { sandbox, plane } = await createTestSandbox();
    const host = await sandbox.getHost(3000);
    expect(plane.callsTo("createAuthToken").at(-1)?.ports).toEqual([PortSpec.single(3000)]);
    expect(`https://${host}`).toBe(sandbox.endpointUrl);
    expect(host.port).toBe(3000);
    expect(host.headers[PROXY_AUTH_HEADER]).toMatch(/fake\.jwe/);
    expect(host.headers[PROXY_PORT_HEADER]).toBe("3000");
    expect(host.headers).not.toHaveProperty(PROXY_FORCE_H2_HEADER);
    await expect(sandbox.getHost(9000)).rejects.toBeInstanceOf(InvalidArgumentError);
    const mints = plane.callsTo("createAuthToken").length;
    await sandbox.getHost(3000);
    expect(plane.callsTo("createAuthToken")).toHaveLength(mints);
  });

  test("missing token is refused locally", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    const host = await sandbox.getHost(3000);
    const requests = rayd.requests;
    Sandbox.coreOf(sandbox).refresher.store.clear();
    expect(() => host.headers).toThrow(AuthenticationError);
    expect(() => host.headers).toThrow(/3000/);
    await expect(sandbox.commands.list()).rejects.toBeInstanceOf(AuthenticationError);
    expect(rayd.requests).toBe(requests);
  });

  test("close is idempotent and never touches the VM", async () => {
    const { sandbox, plane } = await createTestSandbox();
    sandbox.close();
    sandbox.close();
    expect(Sandbox.coreOf(sandbox).closed).toBe(true);
    expect(plane.callsTo("terminateMicrovm")).toHaveLength(0);
    expect(String(sandbox)).toContain(SANDBOX_ID);
  });

  test("close ends both HTTP/2 sessions", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    const background = await sandbox.commands.run("sleep 30", { background: true, timeoutMs: 0 });
    await waitUntil(() => rayd.sessions >= 2, 2000);
    expect(await serverConnections(rayd)).toBe(2);
    sandbox.close();
    await waitForConnections(rayd, 0);
    await expect(background.wait()).rejects.toThrow();
    await sleep(300);
    expect(await serverConnections(rayd)).toBe(0);
    expect(rayd.sessions).toBe(2);
  });

  test("close with a live stream leaves no PING timer on the destroyed session", async () => {
    const rayd = await startRayd();
    const plane = new FakeControlPlane({ endpoint: rayd.host, states: ["PENDING"] });
    const sandbox = await Sandbox.create({
      template: IMAGE_ARN,
      idle: null,
      accessToken: ACCESS_TOKEN,
      controlPlane: plane,
      transport: { scheme: "http", port: rayd.port },
      readyTimeoutMs: 10_000,
    });
    try {
      await sandbox.commands.run("sleep 30", { background: true, timeoutMs: 0 });
      await waitUntil(() => rayd.sessions >= 2, 2000);
      sandbox.close();
      vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
      try {
        await new Promise((resolve) => setImmediate(resolve));
        await new Promise((resolve) => setImmediate(resolve));
        expect(() =>
          vi.advanceTimersByTime(DEFAULT_TRANSPORT_SETTINGS.pingIntervalMs * 2),
        ).not.toThrow();
      } finally {
        vi.useRealTimers();
      }
    } finally {
      sandbox.close();
      await rayd.close();
    }
  });

  test("finished streams leave the live registry before close", async () => {
    const { sandbox } = await createTestSandbox();
    const core = Sandbox.coreOf(sandbox);
    for (let i = 0; i < 5; i += 1) {
      await sandbox.commands.run("echo x");
    }
    expect(core.liveStreamCount).toBe(0);
    const background = await sandbox.commands.run("echo bg", { background: true });
    expect(core.liveStreamCount).toBe(1);
    expect((await background.wait()).stdout).toBe("bg\n");
    expect(core.liveStreamCount).toBe(0);
    const pty = await sandbox.pty.create();
    const collector = new Collector(pty);
    expect(core.liveStreamCount).toBe(1);
    await pty.sendInput("exit\n");
    await collector.join();
    await pty.wait().catch(() => undefined);
    expect(core.liveStreamCount).toBe(0);
    expect((await sandbox.runCode("x = 1")).error).toBeUndefined();
    expect(core.liveStreamCount).toBe(0);
    await sandbox.files.write("/home/user/a.txt", "hola");
    expect(await sandbox.files.read("/home/user/a.txt")).toBe("hola");
    expect(core.liveStreamCount).toBe(0);
  });

  test("Sandbox.list resolves the template and filters states", async () => {
    const plane = new FakeControlPlane({ endpoint: "127.0.0.1" });
    plane.addListed("a", "RUNNING");
    plane.addListed("b", "TERMINATED");
    plane.addListed("c", "SUSPENDED");
    const ids = [];
    for await (const item of Sandbox.list({ controlPlane: plane, template: "rayito-base-2gb" })) {
      ids.push(item.sandboxId);
    }
    expect(ids).toEqual(["a", "c"]);
    expect(plane.callsTo("resolveTemplateArn")).toHaveLength(1);
    const terminated = [];
    for await (const item of Sandbox.list({ controlPlane: plane, states: ["TERMINATED"] })) {
      terminated.push(item.sandboxId);
    }
    expect(terminated).toEqual(["b"]);
  });
});

describe("transport", () => {
  test("headers on a unary and on a stream", async () => {
    const { sandbox, rayd, plane } = await createTestSandbox();
    await sandbox.commands.list();
    const handle = await sandbox.commands.run("sleep 1", { background: true });
    const expected = {
      [PROXY_AUTH_HEADER]: `${plane.jwe}.1`,
      [PROXY_PORT_HEADER]: "8080",
      [PROXY_FORCE_H2_HEADER]: "true",
      "x-access-token": ACCESS_TOKEN,
    };
    expect(rayd.process.listHeaders[0]).toMatchObject(expected);
    expect(rayd.process.startHeaders[0]).toMatchObject(expected);
    handle.disconnect();
  });

  test("token rotation reaches the next request without a new transport", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    await sandbox.files.exists("/");
    await Sandbox.coreOf(sandbox).refresher.refreshAll();
    await sandbox.files.exists("/");
    const seen = rayd.filesystem.headers.Stat?.map((headers) => headers[PROXY_AUTH_HEADER]);
    expect(seen).toHaveLength(2);
    expect(seen?.[0]).not.toBe(seen?.[1]);
    expect(seen?.[1]).toMatch(/\.2$/);
    expect(rayd.sessions).toBe(1);
  });

  test("two transports at most", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    for (let i = 0; i < 30; i += 1) {
      await sandbox.commands.run(`echo ${i}`);
    }
    const background = await sandbox.commands.run("sleep 5", { background: true });
    const watch = await sandbox.files.watchDir("/home/user");
    const pty = await sandbox.pty.create();
    await waitUntil(() => rayd.sessions >= 2, 2000);
    expect(rayd.sessions).toBe(2);
    expect(Sandbox.coreOf(sandbox).streamTransportOpened).toBe(true);
    background.disconnect();
    await watch.stop();
    pty.disconnect();
  });

  test("a logger receives lifecycle lines without secrets", async () => {
    const logger = new RecordingLogger();
    const { sandbox, plane } = await createTestSandbox({ create: { logger } });
    await sandbox.commands.run("echo hola");
    expect(logger.at("info").some((line) => line.message.includes("agente listo"))).toBe(true);
    const dump = logger.dump();
    expect(dump).not.toContain(ACCESS_TOKEN);
    expect(dump).not.toContain(plane.jwe);
    expect(dump).toContain(SANDBOX_ID);
  });
});
