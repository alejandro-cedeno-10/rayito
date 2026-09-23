/**
 * `rayito/e2b` contra el `rayd` falso y el plano de control falso (D17, D22):
 * los miembros sin primitiva rechazan antes de cualquier petición, los
 * wrappers tienen la forma de E2B y las opciones de conexión llegan al cable
 * sin dejar secretos en los logs.
 */

import { Code, ConnectError } from "@connectrpc/connect";
import { afterEach, describe, expect, test, vi } from "vitest";
import {
  AVAILABLE_KERNELS_REASON,
  COMPAT_WARNING_TYPE,
  E2B_DEFAULT_TIMEOUT_MS,
  POLY_KERNELS_REASON,
} from "../../src/e2b/compat.js";
import {
  ConnectionConfig,
  E2B,
  getSignature,
  Sandbox,
  type SandboxOpts,
  Secret,
  Template,
  Volume,
} from "../../src/e2b/index.js";
import { CommandExitError, InvalidArgumentError, UnimplementedError } from "../../src/errors.js";
import { FilesystemEventType as EventTypeProto } from "../../src/gen/rayito/v1/filesystem_pb.js";
import { LifecyclePhase, TimeoutMode } from "../../src/gen/rayito/v1/lifecycle_pb.js";
import { HISTORY_UNIMPLEMENTED_REASON } from "../../src/sandbox/metrics.js";
import { FakeControlPlane, IMAGE_ARN, JWE } from "./fake/control-plane.js";
import { HOME } from "./fake/filesystem.js";
import { managedLifecycle } from "./fake/lifecycle.js";
import type { FakeRayd } from "./fake/server.js";
import { ACCESS_TOKEN, RecordingLogger, startRayd, waitUntil } from "./helpers.js";

const SECRET_HEADER = "valor-secreto";
const PROXY_PASSWORD = "clave";
const GIT_PASSWORD = "gitclave123";

interface ShimTest {
  readonly sandbox: Sandbox;
  readonly rayd: FakeRayd;
  readonly plane: FakeControlPlane;
  readonly logger: RecordingLogger;
}

const rayds: FakeRayd[] = [];
const sandboxes: Sandbox[] = [];

afterEach(async () => {
  vi.restoreAllMocks();
  ConnectionConfig.setIntegration(undefined);
  for (const sandbox of sandboxes.splice(0)) {
    sandbox.close();
  }
  for (const rayd of rayds.splice(0)) {
    await rayd.close();
  }
});

async function fakes(): Promise<{ rayd: FakeRayd; plane: FakeControlPlane }> {
  const rayd = await startRayd(ACCESS_TOKEN);
  rayds.push(rayd);
  rayd.health.lifecycle = managedLifecycle({
    onTimeout: "kill",
    timeoutMs: E2B_DEFAULT_TIMEOUT_MS,
  });
  const plane = new FakeControlPlane({ endpoint: rayd.host, states: ["PENDING"] });
  return { rayd, plane };
}

function bindings(rayd: FakeRayd, plane: FakeControlPlane, logger?: RecordingLogger): SandboxOpts {
  return {
    accessToken: ACCESS_TOKEN,
    controlPlane: plane,
    transport: rayd.transport,
    readyTimeoutMs: 10_000,
    logger,
  };
}

async function shimSandbox(opts: SandboxOpts = {}): Promise<ShimTest> {
  const { rayd, plane } = await fakes();
  const logger = new RecordingLogger();
  const sandbox = await Sandbox.create(IMAGE_ARN, { ...bindings(rayd, plane, logger), ...opts });
  sandboxes.push(sandbox);
  return { sandbox, rayd, plane, logger };
}

function spyWarnings(): string[] {
  const seen: string[] = [];
  vi.spyOn(process, "emitWarning").mockImplementation(((
    warning: string | Error,
    options?: { type?: string },
  ) => {
    if (options?.type === COMPAT_WARNING_TYPE) {
      seen.push(String(warning));
    }
  }) as typeof process.emitWarning);
  return seen;
}

async function outcome(action: () => unknown): Promise<unknown> {
  try {
    await action();
  } catch (error) {
    return error;
  }
  throw new Error("no lanzó ni rechazó");
}

describe("create", () => {
  test("a positional template with metadata and one warning for apiKey", async () => {
    const seen = spyWarnings();
    const { rayd, plane } = await fakes();
    const sandbox = await Sandbox.create("tpl", {
      ...bindings(rayd, plane),
      metadata: { a: "1" },
      apiKey: "e2b_x",
    });
    sandboxes.push(sandbox);
    const launch = plane.launches[0];
    expect(launch?.imageArn).toMatch(/:microvm-image:tpl$/);
    expect(launch?.runHookPayload).toContain('"a":"1"');
    expect(launch?.maximumDurationSeconds).toBe(3600);
    expect(launch?.idle).toBeUndefined();
    expect(launch?.ingressConnectors.join(",")).toContain("ALL_INGRESS");
    expect(launch?.egressConnectors.join(",")).toContain("INTERNET_EGRESS");
    expect(seen).toHaveLength(1);
    expect(seen[0]).toMatch(/^apiKey ignorado: /);
    expect(seen[0]).not.toContain("e2b_x");
    expect(sandbox).toBeInstanceOf(Sandbox);
    expect(sandbox.native.sandboxId).toBe(sandbox.sandboxId);
  });

  test("an image without the server timeout is terminated and unimplemented", async () => {
    const { rayd, plane } = await fakes();
    rayd.health.lifecycle = undefined;
    const error = (await outcome(() => Sandbox.create(IMAGE_ARN, bindings(rayd, plane)))) as Error;
    expect(error).toBeInstanceOf(UnimplementedError);
    expect((error as UnimplementedError).feature).toBe("lifecycle");
    expect(plane.callsTo("terminateMicrovm")).toHaveLength(1);
  });

  test("a bound E2B client creates through its control plane; per-call options win", async () => {
    const { rayd, plane } = await fakes();
    const seen = spyWarnings();
    const client = new E2B({ ...bindings(rayd, plane), apiKey: "e2b_bound" });
    expect(seen).toHaveLength(1);
    const sandbox = await client.Sandbox.create({ template: "bound-tpl" });
    sandboxes.push(sandbox);
    expect(seen).toHaveLength(1);
    expect(sandbox).toBeInstanceOf(Sandbox);
    expect(sandbox).toBeInstanceOf(client.Sandbox);
    expect(plane.launches[0]?.imageArn).toMatch(/:microvm-image:bound-tpl$/);
    const other = new FakeControlPlane({ endpoint: rayd.host, states: ["RUNNING"] });
    expect(await client.Sandbox.isRunning(sandbox.sandboxId, { controlPlane: other })).toBe(true);
    expect(other.callsTo("getMicrovm")).toHaveLength(1);
    expect(new E2B().Sandbox).not.toBe(client.Sandbox);
  });
});

describe("unimplemented members", () => {
  test("every D14 member throws or rejects UnimplementedError with zero requests", async () => {
    const { sandbox, rayd, plane } = await shimSandbox();
    const client = new E2B({ controlPlane: plane });
    const requests = rayd.requests;
    const calls = plane.calls.length;
    const asyncMembers: Array<[string, () => Promise<unknown>]> = [
      ["fork", () => sandbox.fork()],
      ["createSnapshot", () => sandbox.createSnapshot({ name: "s" })],
      ["getMcpToken", () => sandbox.getMcpToken()],
      ["pause({ keepMemory: false })", () => sandbox.pause({ keepMemory: false })],
      ["connect({ onResume: 'reboot' })", () => sandbox.connect({ onResume: "reboot" })],
      ["fork", () => Sandbox.fork(sandbox.sandboxId)],
      ["deleteSnapshot", () => Sandbox.deleteSnapshot("snap")],
      [
        "pause({ keepMemory: false })",
        () => Sandbox.pause(sandbox.sandboxId, { keepMemory: false, controlPlane: plane }),
      ],
      [
        "connect({ onResume: 'reboot' })",
        () => Sandbox.connect(sandbox.sandboxId, { onResume: "reboot", controlPlane: plane }),
      ],
      ["mcp", () => Sandbox.create(IMAGE_ARN, { controlPlane: plane, mcp: { github: {} } })],
      ["iam", () => Sandbox.create(IMAGE_ARN, { controlPlane: plane, iam: { audience: "x" } })],
      [
        "volumeMounts",
        () => Sandbox.create(IMAGE_ARN, { controlPlane: plane, volumeMounts: { v: "/m" } }),
      ],
      [
        "network.rules",
        () => Sandbox.create(IMAGE_ARN, { controlPlane: plane, network: { rules: {} } }),
      ],
      [
        "network.maskRequestHost",
        () => Sandbox.create(IMAGE_ARN, { controlPlane: plane, network: { maskRequestHost: "h" } }),
      ],
      [
        "network.allowPublicTraffic=true",
        () =>
          Sandbox.create(IMAGE_ARN, { controlPlane: plane, network: { allowPublicTraffic: true } }),
      ],
      [
        "lifecycle.onTimeout.keepMemory=false",
        () =>
          Sandbox.create(IMAGE_ARN, {
            controlPlane: plane,
            lifecycle: { onTimeout: { action: "pause", keepMemory: false } },
          }),
      ],
      ["network.rules", () => sandbox.updateNetwork({ rules: {} })],
    ];
    for (const [feature, member] of asyncMembers) {
      const error = await outcome(member);
      expect(error, feature).toBeInstanceOf(UnimplementedError);
      expect((error as UnimplementedError).feature).toBe(feature);
      expect((error as UnimplementedError).reason).toMatch(/AWS_API_NOTES\.md §|SPEC\.md §4/);
    }
    const syncMembers: Array<[string, () => unknown]> = [
      ["listSnapshots", () => sandbox.listSnapshots()],
      ["getMcpUrl", () => sandbox.getMcpUrl()],
      ["listSnapshots", () => Sandbox.listSnapshots()],
      ["getSignature", () => getSignature({ path: "/f", operation: "read" })],
      ["Template", () => Template.build({}, { alias: "a" })],
      ["Template", () => Template.exists("a")],
      ["Volume", () => Volume.create("v")],
      ["Secret", () => Secret.list()],
      ["Template", () => client.Template],
      ["Volume", () => client.Volume],
      ["Secret", () => client.Secret],
    ];
    for (const [feature, member] of syncMembers) {
      let thrown: unknown;
      try {
        member();
      } catch (error) {
        thrown = error;
      }
      expect(thrown, feature).toBeInstanceOf(UnimplementedError);
      expect((thrown as UnimplementedError).feature).toBe(feature);
    }
    expect(rayd.requests).toBe(requests);
    expect(plane.calls).toHaveLength(calls);
  });

  test("neutral values reach the control plane", async () => {
    const { sandbox, plane } = await shimSandbox({ network: { allowPublicTraffic: false } });
    plane.setStates(["RUNNING"]);
    expect(await sandbox.connect({ onResume: "restore" })).toBe(sandbox);
    plane.setStates(["RUNNING", "SUSPENDED"]);
    expect(await sandbox.pause({ keepMemory: true })).toBe(true);
    expect(plane.callsTo("suspendMicrovm")).toHaveLength(1);
  });
});

describe("signal on the shim's lifecycle, metrics and network calls", () => {
  class Stop extends Error {}

  const heldCalls: ReadonlyArray<
    readonly [string, string, (sandbox: Sandbox, signal: AbortSignal) => Promise<unknown>]
  > = [
    [
      "setTimeout",
      "/rayito.v1.LifecycleService/SetTimeout",
      (sandbox, signal) => sandbox.setTimeout(60_000, { signal }),
    ],
    [
      "connect({ timeoutMs })",
      "/rayito.v1.LifecycleService/SetTimeout",
      (sandbox, signal) => sandbox.connect({ timeoutMs: 120_000, signal }),
    ],
    [
      "isRunning",
      "/rayito.v1.HealthService/Health",
      (sandbox, signal) => sandbox.isRunning({ signal }),
    ],
    [
      "getMetrics",
      "/rayito.v1.HealthService/MetricsHistory",
      (sandbox, signal) => sandbox.getMetrics({ signal }),
    ],
    [
      "getInfo",
      "/rayito.v1.NetworkService/GetNetwork",
      (sandbox, signal) => sandbox.getInfo({ signal }),
    ],
    [
      "updateNetwork",
      "/rayito.v1.NetworkService/UpdateNetwork",
      (sandbox, signal) => sandbox.updateNetwork({ denyOut: ["10.0.0.0/8"] }, { signal }),
    ],
  ];

  test.each(heldCalls)(
    "%s forwards signal and rejects with its reason",
    async (_name, path, call) => {
      const { sandbox, rayd, plane } = await shimSandbox();
      plane.setStates(["RUNNING"]);
      rayd.held.add(path);
      const controller = new AbortController();
      const reason = new Stop("colgada");
      setTimeout(() => controller.abort(reason), 100);
      await expect(call(sandbox, controller.signal)).rejects.toBe(reason);
    },
  );

  test("kill and pause with an aborted signal reject before touching the control plane", async () => {
    const { sandbox, plane } = await shimSandbox();
    const controller = new AbortController();
    const reason = new Stop("antes");
    controller.abort(reason);
    await expect(sandbox.kill({ signal: controller.signal })).rejects.toBe(reason);
    await expect(sandbox.pause({ signal: controller.signal })).rejects.toBe(reason);
    expect(plane.callsTo("terminateMicrovm")).toHaveLength(0);
    expect(plane.callsTo("suspendMicrovm")).toHaveLength(0);
  });
});

describe("instance surface", () => {
  test("getInfo keeps an unenforced guest policy, falls back to the connectors and only swallows UnimplementedError", async () => {
    const { sandbox, rayd, plane } = await shimSandbox();
    plane.setStates(["RUNNING"]);
    const unenforced = await sandbox.getInfo();
    expect(unenforced.network).toEqual({ allowOut: [], denyOut: [] });
    expect(unenforced.allowInternetAccess).toBe(true);
    rayd.network.failNext.push(new ConnectError("sin GetNetwork", Code.Unimplemented));
    const older = await sandbox.getInfo();
    expect(older.network).toBeUndefined();
    expect(older.allowInternetAccess).toBe(true);
    rayd.network.failNext.push(new ConnectError("boom", Code.Internal));
    const failure = await outcome(() => sandbox.getInfo());
    expect(failure).toBeInstanceOf(Error);
    expect(failure).not.toBeInstanceOf(UnimplementedError);
  });

  test("getHost is synchronous and validates the port; headers and the proxy JWE", async () => {
    const { sandbox, rayd, plane } = await shimSandbox();
    const host = sandbox.getHost(3000);
    expect(typeof host).toBe("string");
    expect(host).toBe(rayd.host);
    expect(sandbox.sandboxDomain).toBe(rayd.host);
    expect(() => sandbox.getHost(9000)).toThrow(InvalidArgumentError);
    expect(sandbox.trafficAccessToken?.startsWith(JWE)).toBe(true);
    expect(sandbox.trafficAccessToken).toBe(sandbox.native.currentProxyToken(8080));
    const headers = await sandbox.getHostHeaders(3000);
    expect(headers["x-aws-proxy-port"]).toBe("3000");
    expect(headers["x-aws-proxy-auth"]?.startsWith(JWE)).toBe(true);
    expect(plane.callsTo("createAuthToken").length).toBeGreaterThanOrEqual(2);
  });

  test("watchDir(path, onEvent) delivers an event; pty.create records 80x24", async () => {
    const { sandbox, rayd } = await shimSandbox();
    const seen: string[] = [];
    const watch = await sandbox.files.watchDir(HOME, (event) => {
      seen.push(`${event.type}:${event.name}`);
    });
    rayd.filesystem.emit(HOME, "new.txt", EventTypeProto.CREATE);
    await waitUntil(() => seen.length === 1);
    expect(seen).toEqual(["create:new.txt"]);
    await watch.stop();
    const chunks: Uint8Array[] = [];
    const handle = await sandbox.pty.create({
      cols: 80,
      rows: 24,
      onData: (data) => {
        chunks.push(data);
      },
    });
    expect(rayd.pty.createRequests.at(-1)?.size).toMatchObject({ cols: 80, rows: 24 });
    await sandbox.pty.resize(handle.pid, { cols: 100, rows: 30 });
    expect(rayd.pty.resizeRequests.at(-1)?.size).toMatchObject({ cols: 100, rows: 30 });
    expect(await sandbox.pty.kill(handle.pid)).toBe(true);
  });

  test("watchDir forwards includeEntry, the 60 s default deadline and signal", async () => {
    const { sandbox, rayd } = await shimSandbox();
    const watch = await sandbox.files.watchDir(HOME, () => undefined, { includeEntry: true });
    expect(rayd.filesystem.watchRequests.at(-1)?.includeEntry).toBe(true);
    const deadline = rayd.filesystem.deadlines.WatchDir?.at(-1) ?? 0;
    expect(deadline).toBeGreaterThan(55_000);
    expect(deadline).toBeLessThanOrEqual(60_000);
    await watch.stop();
    const short = await sandbox.files.watchDir(HOME, () => undefined, { timeoutMs: 5000 });
    expect(rayd.filesystem.watchRequests.at(-1)?.includeEntry).toBe(false);
    expect(rayd.filesystem.deadlines.WatchDir?.at(-1)).toBeLessThanOrEqual(5000);
    await short.stop();
    const controller = new AbortController();
    const reason = new Error("parado");
    controller.abort(reason);
    const watches = rayd.filesystem.watchRequests.length;
    await expect(
      sandbox.files.watchDir(HOME, () => undefined, { signal: controller.signal }),
    ).rejects.toBe(reason);
    expect(rayd.filesystem.watchRequests).toHaveLength(watches);
  });

  test("pty.create forwards signal; pty.connect attaches with fromSeq 0 and onData", async () => {
    const { sandbox, rayd } = await shimSandbox();
    const controller = new AbortController();
    const reason = new Error("parado");
    controller.abort(reason);
    await expect(
      sandbox.pty.create({
        cols: 80,
        rows: 24,
        onData: () => undefined,
        signal: controller.signal,
      }),
    ).rejects.toBe(reason);
    expect(rayd.pty.createRequests).toHaveLength(0);
    const handle = await sandbox.pty.create({ cols: 80, rows: 24, onData: () => undefined });
    const chunks: Uint8Array[] = [];
    const attached = await sandbox.pty.connect(handle.pid, {
      onData: (data) => {
        chunks.push(data);
      },
    });
    expect(rayd.pty.connectRequests.at(-1)?.pid).toBe(handle.pid);
    expect(rayd.pty.connectRequests.at(-1)?.fromSeq).toBe(0n);
    await sandbox.pty.sendInput(handle.pid, "echo hola\n");
    await sandbox.pty.sendInput(handle.pid, "exit 0\n");
    expect(await attached.wait()).toMatchObject({ exitCode: 0 });
    expect(Buffer.concat(chunks).toString()).toContain("hola");
  });

  test("a rejecting async onEvent or onData is logged and never left unhandled", async () => {
    const { sandbox, rayd, logger } = await shimSandbox();
    const unhandled: unknown[] = [];
    const onUnhandled = (reason: unknown) => unhandled.push(reason);
    process.on("unhandledRejection", onUnhandled);
    try {
      const seen: string[] = [];
      const watch = await sandbox.files.watchDir(HOME, async (event) => {
        seen.push(event.name);
        throw new Error("fallo del watch");
      });
      rayd.filesystem.emit(HOME, "a.txt", EventTypeProto.CREATE);
      rayd.filesystem.emit(HOME, "b.txt", EventTypeProto.CREATE);
      await waitUntil(() => seen.length === 2);
      await waitUntil(() =>
        logger.at("warn").some((line) => line.message.startsWith("onEvent rechazó")),
      );
      await watch.stop();
      const handle = await sandbox.pty.create({
        cols: 80,
        rows: 24,
        onData: async () => {
          throw new Error("fallo de la pty");
        },
      });
      await sandbox.pty.sendInput(handle.pid, "echo hola\n");
      await sandbox.pty.sendInput(handle.pid, "exit 0\n");
      expect(await handle.wait()).toMatchObject({ exitCode: 0 });
      await waitUntil(() =>
        logger.at("warn").some((line) => line.message.startsWith("onData rechazó")),
      );
      await new Promise((resolve) => setImmediate(resolve));
      expect(unhandled).toEqual([]);
      expect(logger.dump()).toContain("fallo del watch");
    } finally {
      process.off("unhandledRejection", onUnhandled);
    }
  });

  test("pause gives true, then the static pause gives false on SUSPENDED", async () => {
    const { sandbox, plane } = await shimSandbox();
    plane.setStates(["RUNNING", "SUSPENDING", "SUSPENDED"]);
    expect(await sandbox.pause()).toBe(true);
    plane.setStates(["SUSPENDED"]);
    expect(await Sandbox.pause(sandbox.sandboxId, { controlPlane: plane })).toBe(false);
    expect(await Sandbox.betaPause(sandbox.sandboxId, { controlPlane: plane })).toBe(false);
    expect(plane.callsTo("suspendMicrovm")).toHaveLength(1);
  });

  test("static kill, getInfo and isRunning go through the control plane", async () => {
    const { sandbox, rayd, plane } = await shimSandbox();
    plane.setStates(["RUNNING"]);
    expect(await Sandbox.isRunning(sandbox.sandboxId, { controlPlane: plane })).toBe(true);
    const info = await Sandbox.getInfo(sandbox.sandboxId, {
      controlPlane: plane,
      transport: rayd.transport,
    });
    expect(info).toMatchObject({ sandboxId: sandbox.sandboxId, state: "running" });
    expect(
      await Sandbox.getFullInfo(sandbox.sandboxId, {
        controlPlane: plane,
        transport: rayd.transport,
      }),
    ).toEqual(info);
    expect(await Sandbox.kill(sandbox.sandboxId, { controlPlane: plane })).toBe(true);
    expect(plane.callsTo("terminateMicrovm")).toHaveLength(1);
    plane.setStates(["SUSPENDED"]);
    expect(await Sandbox.isRunning(sandbox.sandboxId, { controlPlane: plane })).toBe(false);
  });

  test("a connected handle's getInfo carries the Health metadata, like the static getInfo", async () => {
    const { rayd, plane } = await fakes();
    rayd.health.metadata = { a: "1" };
    const created = await Sandbox.create(IMAGE_ARN, bindings(rayd, plane));
    sandboxes.push(created);
    plane.setStates(["RUNNING"]);
    const connected = await Sandbox.connect(created.sandboxId, bindings(rayd, plane));
    sandboxes.push(connected);
    const instance = await connected.getInfo();
    const byId = await Sandbox.getInfo(created.sandboxId, bindings(rayd, plane));
    expect(instance.metadata).toEqual({ a: "1" });
    expect(byId.metadata).toEqual(instance.metadata);
    expect((await connected.native.getInfo()).metadata).toEqual({ a: "1" });
  });

  test("the static getInfo reads the logical deadline and metadata with one Health, never waking a paused sandbox", async () => {
    const { sandbox, rayd, plane } = await shimSandbox();
    rayd.health.metadata = { a: "1" };
    rayd.health.cpuCount = 2;
    plane.setStates(["RUNNING"]);
    const probes = rayd.health.healthCalls.length;
    const mints = plane.callsTo("createAuthToken").length;
    const opts = { controlPlane: plane, transport: rayd.transport };
    const info = await Sandbox.getInfo(sandbox.sandboxId, opts);
    expect(info.endAt).toEqual(new Date(Number(rayd.health.lifecycle?.deadlineUnixMs ?? 0n)));
    expect(info).toMatchObject({
      metadata: { a: "1" },
      lifecycle: { onTimeout: "kill", autoResume: false },
      cpuCount: 2,
      envdVersion: "test",
    });
    expect(rayd.health.healthCalls).toHaveLength(probes + 1);
    expect(rayd.health.healthCalls.at(-1)?.["x-access-token"]).toBeUndefined();
    plane.setStates(["SUSPENDED"]);
    const paused = await Sandbox.getInfo(sandbox.sandboxId, opts);
    expect(paused.state).toBe("paused");
    expect(paused.lifecycle).toBeUndefined();
    expect(rayd.health.healthCalls).toHaveLength(probes + 1);
    expect(plane.callsTo("createAuthToken")).toHaveLength(mints + 1);
  });

  test("getInfo is E2B-shaped; isRunning, setTimeout, connect and getMetrics delegate", async () => {
    const { rayd, plane } = await fakes();
    rayd.health.metadata = { a: "1" };
    const sandbox = await Sandbox.create(IMAGE_ARN, {
      ...bindings(rayd, plane),
      metadata: { a: "1" },
    });
    sandboxes.push(sandbox);
    rayd.health.cpuCount = 2;
    plane.setStates(["RUNNING"]);
    const info = await sandbox.getInfo();
    expect(info).toMatchObject({
      state: "running",
      metadata: { a: "1" },
      envdVersion: "test",
      cpuCount: 2,
      lifecycle: { onTimeout: "kill", autoResume: false },
      volumeMounts: [],
      sandboxDomain: rayd.host,
    });
    expect(info.endAt).toEqual(new Date(Number(rayd.health.lifecycle?.deadlineUnixMs ?? 0n)));
    expect(await sandbox.isRunning({ requestTimeoutMs: 500 })).toBe(true);
    await sandbox.setTimeout(60_000);
    expect(rayd.lifecycle.calls.at(-1)?.timeoutMs).toBe(60_000);
    expect(await sandbox.connect({ timeoutMs: 120_000 })).toBe(sandbox);
    expect(rayd.lifecycle.calls.at(-1)).toMatchObject({
      timeoutMs: 120_000,
      mode: TimeoutMode.AT_LEAST,
    });
    const metrics = await sandbox.getMetrics();
    expect(metrics).toHaveLength(1);
    expect(metrics[0]).toHaveProperty("memCache");
    rayd.health.historyUnimplemented = true;
    const bounded = await outcome(() => sandbox.getMetrics({ start: new Date(0) }));
    expect(bounded).toBeInstanceOf(UnimplementedError);
    expect(bounded).toMatchObject({
      feature: "getMetrics({ start, end })",
      reason: HISTORY_UNIMPLEMENTED_REASON,
    });
    expect(await sandbox.getMetrics()).toHaveLength(1);
    await expect(sandbox.uploadUrl()).rejects.toBeInstanceOf(InvalidArgumentError);
    rayd.health.lifecycle = managedLifecycle({ phase: LifecyclePhase.UNMANAGED });
    expect((await sandbox.getInfo()).lifecycle).toBeUndefined();
  });

  test("the static getMetrics on a pre-M9 image is the same UnimplementedError as Python", async () => {
    const { sandbox, rayd, plane } = await shimSandbox();
    plane.setStates(["RUNNING"]);
    rayd.health.historyUnimplemented = true;
    const error = await outcome(() => Sandbox.getMetrics(sandbox.sandboxId, bindings(rayd, plane)));
    expect(error).toBeInstanceOf(UnimplementedError);
    expect(error).toMatchObject({
      feature: "Sandbox.getMetrics(sandboxId)",
      reason: HISTORY_UNIMPLEMENTED_REASON,
    });
    expect((error as Error).cause).toBeInstanceOf(UnimplementedError);
  });

  test("code contexts delegate to the native client", async () => {
    const { sandbox } = await shimSandbox();
    const context = await sandbox.createCodeContext();
    expect((await sandbox.listCodeContexts()).map((item) => item.id)).toContain(context.id);
    await sandbox.restartCodeContext(context);
    await sandbox.removeCodeContext(context.id);
  });
});

describe("connection options and log hygiene", () => {
  test("headers reach rayd; proxy and git secrets never reach the logger", async () => {
    const seen = spyWarnings();
    const { sandbox, rayd, logger } = await shimSandbox({ headers: { "x-trace": SECRET_HEADER } });
    expect(rayd.health.healthCalls.at(-1)?.["x-trace"]).toBe(SECRET_HEADER);
    expect(sandbox.connectionConfig.headers).toEqual({ "x-trace": SECRET_HEADER });
    const { rayd: other, plane } = await fakes();
    const proxyError = (await outcome(() =>
      Sandbox.create({
        ...bindings(other, plane, logger),
        proxy: `http://u:${PROXY_PASSWORD}@127.0.0.1:3128`,
      }),
    )) as Error;
    expect(proxyError).toBeInstanceOf(InvalidArgumentError);
    expect(proxyError.message).not.toContain(PROXY_PASSWORD);
    expect(plane.callsTo("runMicrovm")).toHaveLength(0);
    const reserved = await outcome(() =>
      Sandbox.create({ ...bindings(other, plane), headers: { "x-access-token": "x" } }),
    );
    expect(reserved).toBeInstanceOf(InvalidArgumentError);
    expect(plane.callsTo("runMicrovm")).toHaveLength(0);
    await expect(
      sandbox.git.clone("https://github.com/acme/app.git", {
        path: "/w/app",
        username: "bot",
        password: GIT_PASSWORD,
      }),
    ).rejects.toBeInstanceOf(CommandExitError);
    const dump = `${logger.dump()}\n${seen.join("\n")}`;
    for (const secret of [SECRET_HEADER, PROXY_PASSWORD, `u:${PROXY_PASSWORD}`, GIT_PASSWORD]) {
      expect(dump).not.toContain(secret);
    }
  });

  test("setIntegration with an explicit control plane is refused before any call", async () => {
    const { rayd, plane } = await fakes();
    ConnectionConfig.setIntegration("acme/1.0");
    expect(new ConnectionConfig().integration).toBe("acme/1.0");
    await expect(Sandbox.create(bindings(rayd, plane))).rejects.toBeInstanceOf(
      InvalidArgumentError,
    );
    expect(plane.calls).toHaveLength(0);
  });
});

describe("code language (the E2B kernel contract)", () => {
  test("python, bash and the aliases reach the agent; python travels without language", async () => {
    const { sandbox, rayd } = await shimSandbox();
    expect((await sandbox.runCode("echo 1", { language: "Bash" })).logs.stdout.join("")).toBe(
      "1\n",
    );
    expect(rayd.code.executeRequests.at(-1)?.language).toBe("bash");
    expect((await sandbox.runCode("1 + 1", { language: "js" })).text).toBe("2");
    expect(rayd.code.executeRequests.at(-1)?.language).toBe("javascript");
    await sandbox.runCode("1 + 1", { language: "Python" });
    expect(rayd.code.executeRequests.at(-1)?.language).toBeUndefined();
    const context = await sandbox.createCodeContext({ language: "bash" });
    expect(context.language).toBe("bash");
    expect(rayd.code.createRequests.at(-1)?.language).toBe("bash");
  });

  test("a kernel Rayito does not offer is UnimplementedError before any call", async () => {
    const { sandbox, rayd } = await shimSandbox();
    const executes = rayd.code.executeRequests.length;
    const creates = rayd.code.createRequests.length;
    const r = (await outcome(() => sandbox.runCode("1", { language: "r" }))) as UnimplementedError;
    expect(r).toBeInstanceOf(UnimplementedError);
    expect(r.feature).toBe('runCode({ language: "r" })');
    expect(r.reason).toBe(AVAILABLE_KERNELS_REASON);
    expect(r.cause).toBeInstanceOf(InvalidArgumentError);
    const java = (await outcome(() =>
      sandbox.createCodeContext({ language: "java" }),
    )) as UnimplementedError;
    expect(java).toBeInstanceOf(UnimplementedError);
    expect(java.feature).toBe('createCodeContext({ language: "java" })');
    expect(rayd.code.executeRequests).toHaveLength(executes);
    expect(rayd.code.createRequests).toHaveLength(creates);
  });

  test("an image without the kernel turns the agent's Unimplemented into UnimplementedError", async () => {
    const { sandbox, rayd } = await shimSandbox();
    rayd.code.languages = new Set(["python"]);
    const run = (await outcome(() =>
      sandbox.runCode("1", { language: "ts" }),
    )) as UnimplementedError;
    expect(run).toBeInstanceOf(UnimplementedError);
    expect(run.feature).toBe('runCode({ language: "ts" })');
    expect(run.reason).toBe(POLY_KERNELS_REASON);
    expect(run.cause).toBeInstanceOf(InvalidArgumentError);
    const context = (await outcome(() =>
      sandbox.createCodeContext({ language: "bash" }),
    )) as UnimplementedError;
    expect(context.reason).toBe(POLY_KERNELS_REASON);
    const conflict = await outcome(() =>
      sandbox.runCode("1", { language: "bash", context: "default" }),
    );
    expect(conflict).toBeInstanceOf(InvalidArgumentError);
  });
});
