import { create } from "@bufbuild/protobuf";
import { describe, expect, test } from "vitest";
import { CommandExitError, InvalidArgumentError, NotFoundError } from "../../src/errors.js";
import { StreamErrorSchema } from "../../src/gen/rayito/v1/common_pb.js";
import { PtyExitedSchema } from "../../src/gen/rayito/v1/pty_pb.js";
import { CommandHandle } from "../../src/sandbox/commands.js";
import {
  buildPtyStartRequest,
  buildResizeRequest,
  endEventFromPtyExited,
  PtyHandle,
  validateShell,
} from "../../src/sandbox/pty.js";
import { Sandbox } from "../../src/sandbox/sandbox.js";
import { Collector, createTestSandbox, readUntil, waitUntil } from "./helpers.js";

describe("pure helpers", () => {
  test("buildPtyStartRequest", () => {
    const request = buildPtyStartRequest({
      size: { cols: 100, rows: 30 },
      user: "user",
      cwd: "/tmp",
      envs: { A: "1" },
      shell: "/bin/zsh",
      timeoutMs: 1000,
    });
    expect(request.size).toMatchObject({ cols: 100, rows: 30 });
    expect(request.user?.username).toBe("user");
    expect(request.cwd).toBe("/tmp");
    expect(request.envs).toEqual({ A: "1" });
    expect(request.shell).toBe("/bin/zsh");
    expect(request.timeoutMs).toBe(1000n);
    const bare = buildPtyStartRequest();
    expect(bare.size).toBeUndefined();
    expect(bare.timeoutMs).toBe(60_000n);
    expect(bare.shell).toBeUndefined();
    expect(() => buildPtyStartRequest({ size: { cols: 0 } })).toThrow(InvalidArgumentError);
  });

  test("validateShell and buildResizeRequest", () => {
    expect(validateShell(undefined)).toBeUndefined();
    expect(validateShell("")).toBeUndefined();
    expect(validateShell("/bin/sh")).toBe("/bin/sh");
    expect(() => validateShell("sh")).toThrow(InvalidArgumentError);
    expect(buildResizeRequest(5, { cols: 1, rows: 2 }).size).toMatchObject({ cols: 1, rows: 2 });
    expect(() => buildResizeRequest(0, { cols: 1, rows: 2 })).toThrow(InvalidArgumentError);
  });

  test("endEventFromPtyExited keeps every field", () => {
    const exited = create(PtyExitedSchema, {
      exitCode: 137,
      exited: true,
      status: "signaled",
      signal: 9,
      error: create(StreamErrorSchema, { code: "x", message: "y" }),
    });
    const end = endEventFromPtyExited(exited);
    expect(end).toMatchObject({ exitCode: 137, exited: true, status: "signaled", signal: 9 });
    expect(end.error?.code).toBe("x");
    const plain = endEventFromPtyExited(
      create(PtyExitedSchema, { exitCode: 0, exited: true, status: "exited" }),
    );
    expect(plain.signal).toBeUndefined();
    expect(plain.error).toBeUndefined();
  });
});

describe("sandbox.pty", () => {
  test("echo through the fake shell with onData and pty chunks", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    const chunks: Uint8Array[] = [];
    const handle = await sandbox.pty.create({ onData: (chunk) => chunks.push(chunk) });
    expect(handle).toBeInstanceOf(PtyHandle);
    expect(handle).toBeInstanceOf(CommandHandle);
    await handle.sendInput("echo hola\n");
    const iterator = handle[Symbol.asyncIterator]();
    let seen = "";
    while (!seen.includes("hola\r\n")) {
      const result = await iterator.next();
      expect(result.done).toBe(false);
      const value = result.value;
      expect(
        Object.keys(value).filter((key) => value[key as keyof typeof value] !== undefined),
      ).toEqual(["pty"]);
      seen += new TextDecoder().decode(value.pty);
    }
    expect(chunks.every((chunk) => chunk instanceof Uint8Array)).toBe(true);
    expect(handle.stdout).toContain("hola");
    expect(handle.stderr).toBe("");
    expect(handle.lastSeq).toBeGreaterThan(0);
    expect(rayd.pty.createRequests[0]?.timeoutMs).toBe(60_000n);
    expect(rayd.pty.deadlines.Create?.[0]).toBe(65_000);
    handle.disconnect();
  });

  test("resize and kill: stty size answers, first kill true, wait 137, second kill false", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    const handle = await sandbox.pty.create({ size: { cols: 80, rows: 24 } });
    const collector = new Collector(handle);
    await sandbox.pty.resize(handle.pid, { cols: 100, rows: 30 });
    expect(rayd.pty.resizeRequests[0]?.size).toMatchObject({ cols: 100, rows: 30 });
    await handle.sendInput("stty size\n");
    await waitUntil(() => collector.text.includes("30 100\r\n"));
    await handle.resize({ cols: 120, rows: 40 });
    await handle.sendStdin("stty size\n");
    await waitUntil(() => collector.text.includes("40 120\r\n"));
    expect(await sandbox.pty.kill(handle.pid)).toBe(true);
    await collector.join();
    const outcome = await handle.wait().catch((error: unknown) => error);
    expect(outcome).toBeInstanceOf(CommandExitError);
    expect((outcome as CommandExitError).exitCode).toBe(137);
    expect(handle.exitCode).toBe(137);
    expect(handle.error).toBe("signaled");
    expect(await sandbox.pty.kill(handle.pid)).toBe(false);
    expect(await handle.kill()).toBe(false);
  });

  test("wrong kind: process RPCs on a PTY pid and PTY RPCs on a process pid", async () => {
    const { sandbox } = await createTestSandbox();
    const pty = await sandbox.pty.create();
    const process = await sandbox.commands.run("sleep 30", { background: true });
    await expect(sandbox.commands.sendStdin(pty.pid, "x")).rejects.toBeInstanceOf(
      InvalidArgumentError,
    );
    await expect(sandbox.pty.sendInput(process.pid, "x")).rejects.toBeInstanceOf(
      InvalidArgumentError,
    );
    await expect(sandbox.commands.connect(pty.pid)).rejects.toBeInstanceOf(InvalidArgumentError);
    await expect(sandbox.pty.connect(process.pid)).rejects.toBeInstanceOf(InvalidArgumentError);
    const listed = await sandbox.commands.list();
    expect(listed.find((info) => info.pid === pty.pid)?.kind).toBe("pty");
    expect(listed.find((info) => info.pid === process.pid)?.kind).toBe("process");
    expect(await sandbox.commands.kill(pty.pid)).toBe(true);
    pty.disconnect();
    process.disconnect();
  });

  test("connect replays from a seq, exit ends the terminal, deadline none for 0", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    const handle = await sandbox.pty.create({ timeoutMs: 0 });
    expect(rayd.pty.deadlines.Create?.[0]).toBeUndefined();
    await handle.sendInput("echo uno\n");
    await readUntil(handle, "uno\r\n");
    const replay = await sandbox.pty.connect(handle.pid, { fromSeq: 1, onData: () => undefined });
    expect(rayd.pty.connectCalls.at(-1)).toEqual([handle.pid, 1]);
    await handle.sendInput("exit 0\n");
    const replayed = await replay.wait();
    expect(replayed.stdout).toContain("uno");
    expect(replayed.exitCode).toBe(0);
    expect(await handle.wait()).toMatchObject({ exitCode: 0 });
    await expect(sandbox.pty.connect(999_999)).rejects.toBeInstanceOf(NotFoundError);
  });

  test("validation happens client-side", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    await expect(sandbox.pty.create({ shell: "bash" })).rejects.toBeInstanceOf(
      InvalidArgumentError,
    );
    await expect(sandbox.pty.create({ size: { cols: 5000 } })).rejects.toBeInstanceOf(
      InvalidArgumentError,
    );
    await expect(sandbox.pty.resize(1, { cols: 0 })).rejects.toBeInstanceOf(InvalidArgumentError);
    expect(rayd.pty.createRequests).toHaveLength(0);
    await expect(
      sandbox.pty.create({ shell: "/bin/false" }).then((h) => h.wait()),
    ).rejects.toBeInstanceOf(CommandExitError);
  });

  test("PTY streams use the stream transport and the unary transport stays for RPCs", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    const handle = await sandbox.pty.create();
    expect(Sandbox.coreOf(sandbox).streamTransportOpened).toBe(true);
    await waitUntil(() => rayd.sessions === 2, 2000);
    await handle.sendInput("echo x\n");
    expect(rayd.sessions).toBe(2);
    handle.disconnect();
  });
});
