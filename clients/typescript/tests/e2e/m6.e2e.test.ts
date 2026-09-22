/**
 * M6 track B: el flujo de `SPEC.md` §6 a través del SDK TypeScript contra AWS
 * real, con los bloques de paridad M1–M5 en forma compacta y el auto-resume
 * (`openspec/changes/m6-typescript-sdk/design.md` "Acceptance test list").
 * Un MicroVM de ≈ 6 min y otro de ≈ 4 min; cuatro ciclos suspend/resume. Cada
 * tiempo medido se imprime con su nombre de `MILESTONES.md`.
 */

import { describe, expect, test } from "vitest";
import {
  CommandExitError,
  type CommandHandle,
  type ControlPlane,
  type Execution,
  FileNotFoundError,
  InvalidArgumentError,
  type OutputChunk,
  type PtyHandle,
  RateLimitError,
  Sandbox,
  TimeoutError,
  type WatchHandle,
} from "../../src/index.js";
import {
  createTestSandbox,
  e2eEnabled,
  outputLine,
  readUntil,
  report,
  seconds,
  sleep,
  timed,
  useE2E,
  waitUntil,
  withTimeout,
} from "./helpers.js";

const MAIN_TIMEOUT_MS = 1_800_000;
const MAIN_IDLE = { maxIdleSeconds: 600, suspendedDurationSeconds: 1200, autoResume: true };
const IDLE_TIMEOUT_MS = 900_000;
const IDLE_POLICY = { maxIdleSeconds: 60, suspendedDurationSeconds: 600, autoResume: true };
const PTY_READ_BUDGET_MS = 10_000;
const PAUSE_BUDGET_MS = 30_000;
const PAUSED_FOR_MS = 30_000;
const PENDING_CHECK_MS = 5000;
const RESUME_BUDGET_MS = 30_000;
const KERNEL_ALIVE_BUDGET_MS = 10_000;
const RESUBSCRIBE_BUDGET_MS = 10_000;
const REARM_COMMAND_TIMEOUT_MS = 25_000;
const REARM_BUDGET_MS = 35_000;
const REARM_MIN_MS = 10_000;
const WATCH_EVENT_BUDGET_MS = 10_000;
const REATTACH_CELL_SECONDS = 20;
const REATTACH_BUDGET_MS = 90_000;
const IDLE_SUSPEND_BUDGET_MS = 240_000;
const IDLE_POLL_MS = 5000;
const AUTO_RESUME_BUDGET_MS = 30_000;
const TERMINATE_VISIBLE_MS = 30_000;
const TERMINATED_BUDGET_MS = 120_000;
const CLOCK_OFFSET_TOLERANCE_MS = 5000;
const PNG_BASE64_PREFIX = "iVBOR";
const CSV = "a,b\n1,3.5\n2,4.5\n";
const HOME = "/home/user";

interface Pending<T> {
  readonly promise: Promise<T>;
  settled: boolean;
  rejected: boolean;
}

/** Observa una promesa sin consumirla: `settled` y `rejected` se leen durante la pausa. */
function track<T>(promise: Promise<T>): Pending<T> {
  const pending: Pending<T> = { promise, settled: false, rejected: false };
  promise.then(
    () => {
      pending.settled = true;
    },
    () => {
      pending.settled = true;
      pending.rejected = true;
    },
  );
  return pending;
}

describe.skipIf(!e2eEnabled())("m6 typescript sdk", () => {
  const context = useE2E();

  test("spec §6 flow through the TypeScript SDK", async () => {
    const sbx = await createTestSandbox(context, { timeoutMs: MAIN_TIMEOUT_MS, idle: MAIN_IDLE });

    await checkBoot(sbx, context.templateArn);
    await checkCommandsParity(sbx);
    await checkFilesParity(sbx);
    await checkCode(sbx);
    const pty = await checkPty(sbx);
    const state = await checkStateBeforePause(sbx, pty);
    await checkPause(sbx, state);
    await checkResume(sbx, state);
    await checkKernelAlive(sbx);
    await checkProcessesAliveAndTimeoutRearmed(sbx, state);
    await checkPtyReattached(sbx, pty, state.ptyRead);
    await checkWatchReissued(sbx, state.watch);
    await checkRunCodeAcrossAPause(sbx, state.generationBefore);
    await checkConnectFromASecondSandbox(sbx);
    await checkTeardown(sbx, context.templateArn, context.controlPlane);
  });

  test("auto-resume through the TypeScript SDK", async () => {
    const sbx = await createTestSandbox(context, { timeoutMs: IDLE_TIMEOUT_MS, idle: IDLE_POLICY });
    expect((await sbx.runCode("z = 9")).error).toBeUndefined();
    const idleSuspend = await waitUntil(
      async () => (await sbx.getInfo()).state === "SUSPENDED",
      IDLE_SUSPEND_BUDGET_MS,
      "la suspensión por inactividad",
      IDLE_POLL_MS,
    );
    report("idle -> SUSPENDED (idle_suspend_s)", idleSuspend);
    const [back, autoResume] = await timed("commands.run tras el idle (auto_resume_s)", () =>
      withTimeout(sbx.commands.run("echo back"), AUTO_RESUME_BUDGET_MS, "echo back"),
    );
    expect(back.stdout.trim()).toBe("back");
    expect(autoResume).toBeLessThanOrEqual(AUTO_RESUME_BUDGET_MS / 1000);
    expect((await sbx.runCode("z")).text).toBe("9");
    expect((await sbx.getHealth()).resumeGeneration).toBe(1);
    expect((await sbx.getInfo()).state).toBe("RUNNING");
    expect(await sbx.kill()).toBe(true);
  });
});

// ------------------------------------------------------------------ blocks

async function checkBoot(sbx: Sandbox, templateArn: string): Promise<void> {
  expect(await sbx.isRunning()).toBe(true);
  const health = await sbx.getHealth();
  expect(health.agentReady).toBe(true);
  expect(health.kernelReady).toBe(true);
  expect(health.resumeGeneration).toBe(0);
  expect(["RUNNING", "PENDING"]).toContain(sbx.info.state);
  const listed: string[] = [];
  for await (const item of Sandbox.list({ template: templateArn })) {
    listed.push(item.sandboxId);
  }
  expect(listed).toContain(sbx.sandboxId);
}

async function checkCommandsParity(sbx: Sandbox): Promise<void> {
  const hola = await sbx.commands.run("echo hola");
  expect(hola).toMatchObject({ stdout: "hola\n", exitCode: 0 });
  const exit = await sbx.commands.run("exit 3").catch((error: unknown) => error);
  expect(exit).toBeInstanceOf(CommandExitError);
  expect((exit as CommandExitError).exitCode).toBe(3);
  const cat = await sbx.commands.run("cat", { stdin: true, background: true });
  await cat.sendStdin("ping\n");
  await cat.closeStdin();
  expect((await cat.wait()).stdout).toBe("ping\n");
  const started = performance.now();
  const timeout = await sbx.commands
    .run("sleep 30", { timeoutMs: 1000 })
    .catch((error: unknown) => error);
  const elapsed = seconds(started);
  expect(timeout).toBeInstanceOf(TimeoutError);
  expect(elapsed).toBeGreaterThanOrEqual(1);
  expect(elapsed).toBeLessThanOrEqual(4);
  expect((await sbx.commands.run("id -u")).stdout.trim()).toBe("1000");
  expect((await sbx.getMetrics()).memTotalBytes).toBeGreaterThan(0);
  const burst = performance.now();
  for (let i = 0; i < 30; i += 1) {
    const result = await sbx.commands.run(`echo ${i}`).catch((error: unknown) => error);
    expect(result).not.toBeInstanceOf(RateLimitError);
    expect(result).toMatchObject({ stdout: `${i}\n` });
  }
  report("30 echo secuenciales (burst_s)", seconds(burst));
}

async function checkFilesParity(sbx: Sandbox): Promise<void> {
  const info = await sbx.files.write(`${HOME}/data.csv`, CSV);
  expect(info.size).toBe(CSV.length);
  const text = await sbx.files.read(`${HOME}/data.csv`);
  const bytes = await sbx.files.read(`${HOME}/data.csv`, { format: "bytes" });
  const stream = await sbx.files.read(`${HOME}/data.csv`, { format: "stream" });
  const chunks: Uint8Array[] = [];
  for await (const chunk of stream) {
    chunks.push(chunk);
  }
  expect(text).toBe(CSV);
  expect(new TextDecoder().decode(bytes)).toBe(CSV);
  expect(new TextDecoder().decode(Buffer.concat(chunks))).toBe(CSV);
  const written = await sbx.files.writeFiles([
    { path: `${HOME}/multi/a.txt`, data: "a" },
    { path: `${HOME}/multi/b.txt`, data: new Uint8Array([98]) },
    { path: `${HOME}/multi/c.txt`, data: new Blob(["c"]) },
  ]);
  expect(written.map((entry) => entry.name)).toEqual(["a.txt", "b.txt", "c.txt"]);
  const listed = await sbx.files.list(HOME, { depth: 2 });
  expect(listed.map((entry) => entry.path)).toEqual(
    expect.arrayContaining([`${HOME}/multi/a.txt`, `${HOME}/multi/b.txt`, `${HOME}/multi/c.txt`]),
  );
  expect(await sbx.files.exists(`${HOME}/multi/a.txt`)).toBe(true);
  expect((await sbx.files.getInfo(`${HOME}/multi/a.txt`)).size).toBe(1);
  expect((await sbx.files.rename(`${HOME}/multi/a.txt`, `${HOME}/multi/z.txt`)).name).toBe("z.txt");
  await sbx.files.remove(`${HOME}/multi/z.txt`);
  expect(await sbx.files.exists(`${HOME}/multi/z.txt`)).toBe(false);
  expect(await sbx.files.makeDir(`${HOME}/newdir`)).toBe(true);
  expect(await sbx.files.makeDir(`${HOME}/newdir`)).toBe(false);
  const megabyte = new Uint8Array(1_000_000).map((_, index) => (index * 31 + 7) & 0xff);
  const roundTrip = performance.now();
  await sbx.files.write(`${HOME}/one.bin`, megabyte);
  const back = await sbx.files.read(`${HOME}/one.bin`, { format: "bytes" });
  report("1 MB escritura + lectura (file_1mb_s)", seconds(roundTrip));
  expect(Buffer.compare(Buffer.from(back), Buffer.from(megabyte))).toBe(0);
  await expect(sbx.files.read(`${HOME}/nope`)).rejects.toBeInstanceOf(FileNotFoundError);
  await sbx.files.makeDir(`${HOME}/w`);
  const seen: string[] = [];
  const watch = await sbx.files.watchDir(`${HOME}/w`, {
    onEvent: (event) => seen.push(`${event.type}:${event.name}`),
  });
  await sbx.commands.run(`touch ${HOME}/w/touched.txt`);
  const watchSeconds = await waitUntil(
    () => seen.some((entry) => entry === "create:touched.txt"),
    WATCH_EVENT_BUDGET_MS,
    "el evento create del watch",
  );
  report("watchDir: evento create (watch_event_s)", watchSeconds);
  await watch.stop();
}

async function checkCode(sbx: Sandbox): Promise<void> {
  const length = await sbx.runCode(
    `import pandas as pd; df = pd.read_csv('${HOME}/data.csv'); len(df)`,
  );
  expect(length.error).toBeUndefined();
  expect(length.text).toBe("2");
  expect((await sbx.runCode("x = 42")).error).toBeUndefined();
  const printed = await sbx.runCode("print(x)");
  expect(printed.logs.stdout.join("")).toContain("42");
  expect(printed.text).toBeUndefined();
  const plot = await sbx.runCode("df.plot(); import matplotlib.pyplot as plt; plt.show()");
  expect(plot.error).toBeUndefined();
  expect(plot.results[0]?.png?.startsWith(PNG_BASE64_PREFIX)).toBe(true);
  expect(plot.results[0]?.chart).toBeDefined();
  expect((await sbx.runCode("1/0")).error?.name).toBe("ZeroDivisionError");
  const ctx = await sbx.createCodeContext({ cwd: "/tmp" });
  expect((await sbx.runCode("import os; os.getcwd()", { context: ctx })).text).toBe("'/tmp'");
  await sbx.removeCodeContext(ctx);
  expect((await sbx.listCodeContexts()).map((item) => item.id)).not.toContain(ctx.id);
}

async function checkPty(sbx: Sandbox): Promise<PtyHandle> {
  const chunks: Uint8Array[] = [];
  const [pty, createSeconds] = await timed("pty.create -> started (pty_create_s)", () =>
    sbx.pty.create({
      size: { cols: 100, rows: 30 },
      onData: (chunk) => chunks.push(chunk),
      timeoutMs: 0,
    }),
  );
  expect(createSeconds).toBeLessThanOrEqual(5);
  expect(pty.pid).toBeGreaterThan(0);
  const iterator = pty[Symbol.asyncIterator]();
  const echo = performance.now();
  await pty.sendInput("echo hola\n");
  await readUntil(iterator, outputLine("hola"), PTY_READ_BUDGET_MS);
  report("echo hola por la PTY (pty_echo_s)", seconds(echo));
  expect(chunks.length).toBeGreaterThan(0);
  await pty.sendInput("stty size\n");
  await readUntil(iterator, "30 100", PTY_READ_BUDGET_MS);
  await pty.resize({ cols: 120, rows: 40 });
  await pty.sendInput("stty size\n");
  await readUntil(iterator, "40 120", PTY_READ_BUDGET_MS);
  const listed = await sbx.commands.list();
  expect(listed.find((info) => info.pid === pty.pid)?.kind).toBe("pty");
  await expect(sbx.commands.sendStdin(pty.pid, "x")).rejects.toBeInstanceOf(InvalidArgumentError);
  expect(pty.lastSeq).toBeGreaterThan(0);
  expect(pty.stdout).toContain("hola");
  ptyIterators.set(pty, iterator);
  return pty;
}

const ptyIterators = new WeakMap<PtyHandle, AsyncIterator<OutputChunk>>();

interface StateBeforePause {
  readonly generationBefore: number;
  readonly sleeper: CommandHandle;
  readonly sleeperWait: Pending<unknown>;
  readonly timed: CommandHandle;
  readonly watch: WatchHandle;
  readonly ptyRead: Pending<unknown>;
  readonly resumedAt: { value: number };
}

/**
 * `timed` se desconecta antes de la pausa (como `rearm` en el e2e de Python):
 * un handle vivo con `timeoutMs` lleva un deadline de cliente (`timeoutMs +
 * 5 s`) que corre en el reloj del cliente y una pausa de 30 s lo agota; el
 * timeout del servidor, en cambio, se re-arma en `/resume` y se observa con
 * `commands.connect(pid)` sin deadline.
 */
async function checkStateBeforePause(sbx: Sandbox, pty: PtyHandle): Promise<StateBeforePause> {
  expect((await sbx.runCode("y = 7")).error).toBeUndefined();
  const sleeper = await sbx.commands.run("sleep 4000", { background: true, timeoutMs: 0 });
  const timedHandle = await sbx.commands.run("sleep 60", {
    background: true,
    timeoutMs: REARM_COMMAND_TIMEOUT_MS,
  });
  timedHandle.disconnect();
  const watch = await sbx.files.watchDir(`${HOME}/w`);
  const health = await sbx.getHealth();
  expect(health.agentReady && health.kernelReady).toBe(true);
  expect(health.kernelStateLost).toBe(false);
  const iterator = ptyIterators.get(pty);
  if (iterator === undefined) {
    throw new Error("la PTY no tiene iterador");
  }
  return {
    generationBefore: health.resumeGeneration,
    sleeper,
    sleeperWait: track(sleeper.wait()),
    timed: timedHandle,
    watch,
    ptyRead: track(iterator.next()),
    resumedAt: { value: 0 },
  };
}

async function checkPause(sbx: Sandbox, state: StateBeforePause): Promise<void> {
  const [paused, pauseSeconds] = await timed("pause() -> SUSPENDED (pause_s)", () => sbx.pause());
  expect(paused).toBe(true);
  expect(pauseSeconds).toBeLessThanOrEqual(PAUSE_BUDGET_MS / 1000);
  expect((await sbx.getInfo()).state).toBe("SUSPENDED");
  expect(await sbx.pause()).toBe(false);
  await sleep(PENDING_CHECK_MS);
  expect(state.ptyRead.rejected).toBe(false);
  expect(state.sleeperWait.settled).toBe(false);
  expect(state.watch.isRunning).toBe(true);
  await sleep(PAUSED_FOR_MS - PENDING_CHECK_MS);
  expect((await sbx.getInfo()).state).toBe("SUSPENDED");
}

async function checkResume(sbx: Sandbox, state: StateBeforePause): Promise<void> {
  const generationBefore = state.generationBefore;
  const [, resumeSeconds] = await timed(
    "resume() -> Health con la generación nueva (resume_s)",
    () => sbx.resume(),
  );
  state.resumedAt.value = performance.now();
  expect(resumeSeconds).toBeLessThanOrEqual(RESUME_BUDGET_MS / 1000);
  const health = await sbx.getHealth();
  expect(health.resumeGeneration).toBe(generationBefore + 1);
  expect(health.kernelStateLost).toBe(false);
  report(`Q41 clockOffsetMs = ${health.clockOffsetMs}`, 0);
  expect(Math.abs(health.clockOffsetMs)).toBeLessThan(CLOCK_OFFSET_TOLERANCE_MS);
  expect((await sbx.getInfo()).state).toBe("RUNNING");
}

async function checkKernelAlive(sbx: Sandbox): Promise<void> {
  const [x, kernelAlive] = await timed("primera celda tras resume (kernel_alive_s)", () =>
    sbx.runCode("x"),
  );
  expect(x.text).toBe("42");
  expect(kernelAlive).toBeLessThanOrEqual(KERNEL_ALIVE_BUDGET_MS / 1000);
  expect((await sbx.runCode("y")).text).toBe("7");
}

async function checkProcessesAliveAndTimeoutRearmed(
  sbx: Sandbox,
  state: StateBeforePause,
): Promise<void> {
  const resumedAt = state.resumedAt.value;
  const pids = (await sbx.commands.list()).map((info) => info.pid);
  expect(pids).toContain(state.sleeper.pid);
  expect(pids).toContain(state.timed.pid);
  const [reconnected, resubscribe] = await timed(
    "commands.connect tras resume (resubscribe_s)",
    () => sbx.commands.connect(state.sleeper.pid),
  );
  expect(resubscribe).toBeLessThanOrEqual(RESUBSCRIBE_BUDGET_MS / 1000);
  expect(reconnected.pid).toBe(state.sleeper.pid);
  expect(await sbx.commands.kill(state.sleeper.pid)).toBe(true);
  const killed = await withTimeout(
    state.sleeperWait.promise.then(
      () => undefined,
      (error: unknown) => error,
    ),
    RESUBSCRIBE_BUDGET_MS,
    "sleeper.wait",
  );
  expect(killed).toBeInstanceOf(CommandExitError);
  expect((killed as CommandExitError).exitCode).toBe(137);
  await expect(reconnected.wait()).rejects.toBeInstanceOf(CommandExitError);
  const rearmed = await sbx.commands.connect(state.timed.pid);
  const timedOut = await withTimeout(
    rearmed.wait().then(
      () => undefined,
      (error: unknown) => error,
    ),
    REARM_BUDGET_MS,
    "timed.wait",
  );
  const rearmSeconds = seconds(resumedAt);
  report("timeout re-armado tras resume (rearm_timeout_s)", rearmSeconds);
  expect(timedOut).toBeInstanceOf(TimeoutError);
  expect(rearmSeconds * 1000).toBeGreaterThanOrEqual(REARM_MIN_MS);
  expect((await sbx.commands.list()).map((info) => info.pid)).not.toContain(state.timed.pid);
  expect(state.sleeper.reconnects).toBe(1);
}

async function checkPtyReattached(
  sbx: Sandbox,
  pty: PtyHandle,
  firstRead: Pending<unknown>,
): Promise<void> {
  const iterator = ptyIterators.get(pty);
  if (iterator === undefined) {
    throw new Error("la PTY no tiene iterador");
  }
  const started = performance.now();
  await pty.sendInput("echo resumed-$((6*7))\n");
  const first = (await withTimeout(
    firstRead.promise,
    PTY_READ_BUDGET_MS,
    "primer chunk tras resume",
  )) as {
    value?: { pty?: Uint8Array };
  };
  let buffer = first.value?.pty === undefined ? "" : new TextDecoder().decode(first.value.pty);
  if (!buffer.includes("resumed-42")) {
    buffer += await readUntil(iterator, "resumed-42", PTY_READ_BUDGET_MS);
  }
  report("PTY reenganchada: echo tras resume (pty_reattach_s)", seconds(started));
  expect(buffer).toContain("resumed-42");
  expect(pty.reconnects).toBe(1);
  expect(await sbx.pty.kill(pty.pid)).toBe(true);
  expect(await sbx.pty.kill(pty.pid)).toBe(false);
}

async function checkWatchReissued(sbx: Sandbox, watch: WatchHandle): Promise<void> {
  await sbx.commands.run(`touch ${HOME}/w/after-resume.txt`);
  const seen: string[] = [];
  const watchSeconds = await waitUntil(
    () => {
      seen.push(...watch.getNewEvents().map((event) => event.name));
      return seen.includes("after-resume.txt");
    },
    WATCH_EVENT_BUDGET_MS,
    "el evento del watch tras resume",
  );
  report("watch re-emitido: evento tras resume (watch_reissue_s)", watchSeconds);
  expect(watch.isRunning).toBe(true);
  expect(watch.reconnects).toBe(1);
  await watch.stop();
}

async function checkRunCodeAcrossAPause(sbx: Sandbox, generationBefore: number): Promise<void> {
  const pending: Promise<Execution> = sbx.runCode(
    `import time; time.sleep(${REATTACH_CELL_SECONDS}); 'slept'`,
    { timeoutMs: 120_000 },
  );
  await sleep(3000);
  expect(await sbx.pause()).toBe(true);
  await sleep(5000);
  expect((await sbx.getInfo()).state).toBe("SUSPENDED");
  const started = performance.now();
  await sbx.resume();
  const execution = await withTimeout(pending, REATTACH_BUDGET_MS, "runCode con Reattach");
  report("runCode continuado con Reattach (reattach_s)", seconds(started));
  expect(execution.error).toBeUndefined();
  expect(execution.text).toBe("'slept'");
  expect((await sbx.runCode("1+1")).text).toBe("2");
  expect((await sbx.getHealth()).resumeGeneration).toBe(generationBefore + 2);
}

async function checkConnectFromASecondSandbox(sbx: Sandbox): Promise<void> {
  const other = await Sandbox.connect(sbx.sandboxId, { accessToken: sbx.accessToken });
  try {
    expect((await other.runCode("x")).text).toBe("42");
    expect((await other.commands.run("echo other")).stdout).toBe("other\n");
  } finally {
    other.close();
  }
  expect((await sbx.commands.run("echo still")).stdout).toBe("still\n");
}

async function checkTeardown(
  sbx: Sandbox,
  templateArn: string,
  controlPlane: ControlPlane,
): Promise<void> {
  expect(await sbx.kill()).toBe(true);
  const visible = await waitUntil(
    async () =>
      ["TERMINATING", "TERMINATED"].includes(
        (await Sandbox.getInfo(sbx.sandboxId, { controlPlane })).state,
      ),
    TERMINATE_VISIBLE_MS,
    "TERMINATING|TERMINATED",
    1000,
  );
  report("terminate-microvm -> TERMINATING|TERMINATED visible", visible);
  const terminated = await waitUntil(
    async () => (await Sandbox.getInfo(sbx.sandboxId, { controlPlane })).state === "TERMINATED",
    TERMINATED_BUDGET_MS,
    "TERMINATED",
    1000,
  );
  report("terminate-microvm -> TERMINATED", terminated);
  const listed: string[] = [];
  for await (const item of Sandbox.list({ template: templateArn, controlPlane })) {
    listed.push(item.sandboxId);
  }
  expect(listed).not.toContain(sbx.sandboxId);
  expect(await Sandbox.kill(sbx.sandboxId, { controlPlane })).toBe(true);
}
