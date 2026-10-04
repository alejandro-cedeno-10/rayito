/**
 * El ciclo de vida y los subclientes del SDK contra el `rayd` real del guest
 * local (`make local-e2e`), espejo de `clients/python/tests/local/
 * test_local_sandbox.py`. Lo que depende del proxy de AWS (el JWE, el 403 de
 * un token falso, los keepalives a través del proxy) solo lo cubren los e2e
 * contra AWS real (`tests/e2e`).
 */

import { randomBytes } from "node:crypto";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { CommandExitError, type CommandHandle, Sandbox } from "../../src/index.js";
import { readUntil } from "../e2e/helpers.js";
import {
  createLocalSandbox,
  killQuietly,
  type LocalHarness,
  localEnabled,
  localHarness,
  releaseGuest,
} from "./helpers.js";

const SANDBOX_USER = "user";
const PTY_MARKER = "rayito-local-pty";
const PTY_READ_BUDGET_MS = 20_000;
const TERMINAL_STATES = new Set(["TERMINATING", "TERMINATED"]);

describe.skipIf(!localEnabled())("sandbox local", () => {
  let harness: LocalHarness;
  let shared: Sandbox;

  beforeAll(async () => {
    harness = await localHarness();
    shared = await createLocalSandbox(harness, { metadata: { suite: "local" } });
  });

  afterAll(async () => {
    if (shared !== undefined) {
      await killQuietly(shared);
    }
    if (harness !== undefined) {
      await releaseGuest(harness);
    }
  });

  it("Health, getInfo y metadatos", async () => {
    const health = await shared.getHealth();
    expect(health.agentReady).toBe(true);
    expect(health.kernelReady).toBe(true);
    const info = await shared.getInfo();
    expect(info.sandboxId).toBe(shared.sandboxId);
    expect(info.state).toBe("RUNNING");
    expect(info.metadata).toEqual({ suite: "local" });
  });

  it("comandos como el usuario del sandbox", async () => {
    const result = await shared.commands.run("echo hola && whoami");
    expect(result.stdout.split(/\s+/).filter(Boolean)).toEqual(["hola", SANDBOX_USER]);
    const withEnv = await shared.commands.run("echo $RAYITO_LOCAL", {
      envs: { RAYITO_LOCAL: "sí" },
      cwd: "/tmp",
    });
    expect(withEnv.stdout.trim()).toBe("sí");
    await expect(shared.commands.run("exit 3")).rejects.toBeInstanceOf(CommandExitError);
    const handle = (await shared.commands.run("sleep 60", { background: true })) as CommandHandle;
    expect((await shared.commands.list()).map((process) => process.pid)).toContain(handle.pid);
    expect(await handle.kill()).toBe(true);
  });

  it("ficheros de ida y vuelta", async () => {
    const base = `/home/${SANDBOX_USER}/local-${randomBytes(4).toString("hex")}`;
    expect(await shared.files.makeDir(base)).toBe(true);
    const payload = new Uint8Array(randomBytes(256 * 1024));
    await shared.files.write(`${base}/blob.bin`, payload);
    await shared.files.write(`${base}/note.txt`, "hola desde local");
    expect(await shared.files.read(`${base}/blob.bin`, { format: "bytes" })).toEqual(payload);
    expect(await shared.files.read(`${base}/note.txt`)).toBe("hola desde local");
    const names = (await shared.files.list(base)).map((entry) => entry.name).sort();
    expect(names).toEqual(["blob.bin", "note.txt"]);
    expect((await shared.files.rename(`${base}/note.txt`, `${base}/renamed.txt`)).name).toBe(
      "renamed.txt",
    );
    const owner = await shared.commands.run(`stat -c %U ${base}/renamed.txt`);
    expect(owner.stdout.trim()).toBe(SANDBOX_USER);
    await shared.files.remove(base);
    expect(await shared.files.exists(base)).toBe(false);
  });

  it("PTY con eco y redimensionado", async () => {
    const pty = await shared.pty.create({ size: { cols: 100, rows: 30 }, timeoutMs: 0 });
    try {
      const iterator = pty[Symbol.asyncIterator]();
      await pty.sendInput(`echo ${PTY_MARKER}\n`);
      await readUntil(iterator, `${PTY_MARKER}\r\n`, PTY_READ_BUDGET_MS);
      await pty.resize({ cols: 80, rows: 24 });
    } finally {
      expect(await pty.kill()).toBe(true);
    }
  });

  it("runCode conserva el estado y devuelve los errores", async () => {
    expect((await shared.runCode("x = 41")).error).toBeUndefined();
    expect((await shared.runCode("x + 1")).text).toBe("42");
    expect((await shared.runCode("1 / 0")).error?.name).toBe("ZeroDivisionError");
    const context = await shared.createCodeContext({ cwd: "/tmp" });
    expect((await shared.runCode("import os; os.getcwd()", { context })).text).toBe("'/tmp'");
  });

  it("connect reutiliza el sandbox vivo", async () => {
    const reconnected = await Sandbox.connect(shared.sandboxId, {
      accessToken: shared.accessToken,
      controlPlane: harness.controlPlane,
      transport: harness.settings.address.transport(),
    });
    try {
      expect((await reconnected.commands.run("echo de-nuevo")).stdout.trim()).toBe("de-nuevo");
    } finally {
      await reconnected.close();
    }
  });

  it("pausa, reanuda y termina (va al final: el guest aloja un sandbox)", async () => {
    const sandbox = await createLocalSandbox(harness, { idle: null });
    await sandbox.files.write("/tmp/antes-de-pausar", "sigue aquí");
    expect(await sandbox.pause()).toBe(true);
    const paused = await Sandbox.getInfo(sandbox.sandboxId, { controlPlane: harness.controlPlane });
    expect(paused.state).toBe("SUSPENDED");
    await sandbox.resume();
    expect(await sandbox.files.read("/tmp/antes-de-pausar")).toBe("sigue aquí");
    expect(await sandbox.kill()).toBe(true);
    const final = await Sandbox.getInfo(sandbox.sandboxId, { controlPlane: harness.controlPlane });
    expect(TERMINAL_STATES.has(final.state)).toBe(true);
  });
});
