import { create } from "@bufbuild/protobuf";
import { describe, expect, test } from "vitest";
import {
  AuthenticationError,
  CommandExitError,
  InvalidArgumentError,
  NotFoundError,
  SandboxError,
  SandboxStateError,
  TimeoutError,
} from "../../src/errors.js";
import { StreamErrorSchema } from "../../src/gen/rayito/v1/common_pb.js";
import { EndEventSchema } from "../../src/gen/rayito/v1/process_pb.js";
import {
  buildStartRequest,
  DecodedStream,
  encodeStdin,
  outcomeFromEnd,
  streamDeadlineMs,
  timeoutToMs,
  validateFromSeq,
  validatePid,
} from "../../src/sandbox/commands.js";
import { Sandbox } from "../../src/sandbox/sandbox.js";
import { Collector, createTestSandbox, readUntil, waitUntil } from "./helpers.js";

describe("pure helpers", () => {
  test("buildStartRequest wraps the command in bash -l -c", () => {
    const request = buildStartRequest("echo hola", {
      envs: { A: "1" },
      user: "user",
      cwd: "/tmp",
      stdin: true,
      timeoutMs: 1500,
      tag: "t",
    });
    expect(request.process?.cmd).toBe("/bin/bash");
    expect(request.process?.args).toEqual(["-l", "-c", "echo hola"]);
    expect(request.process?.envs).toEqual({ A: "1" });
    expect(request.process?.cwd).toBe("/tmp");
    expect(request.user?.username).toBe("user");
    expect(request.stdin).toBe(true);
    expect(request.timeoutMs).toBe(1500n);
    expect(request.tag).toBe("t");
    const bare = buildStartRequest("ls");
    expect(bare.timeoutMs).toBe(60_000n);
    expect(bare.user).toBeUndefined();
    expect(bare.process?.cwd).toBeUndefined();
    expect(() => buildStartRequest("   ")).toThrow(InvalidArgumentError);
  });

  test("timeouts and deadlines", () => {
    expect(timeoutToMs(undefined)).toBe(0);
    expect(timeoutToMs(0)).toBe(0);
    expect(timeoutToMs(0.4)).toBe(1);
    expect(timeoutToMs(1500.4)).toBe(1500);
    expect(() => timeoutToMs(-1)).toThrow(InvalidArgumentError);
    expect(() => timeoutToMs(Number.NaN)).toThrow(InvalidArgumentError);
    expect(streamDeadlineMs(60_000)).toBe(65_000);
    expect(streamDeadlineMs(0)).toBeUndefined();
    expect(streamDeadlineMs(undefined)).toBeUndefined();
  });

  test("pid, fromSeq and stdin validation", () => {
    expect(validatePid(1)).toBe(1);
    expect(() => validatePid(0)).toThrow(InvalidArgumentError);
    expect(() => validatePid(2 ** 32)).toThrow(InvalidArgumentError);
    expect(validateFromSeq(0)).toBe(0);
    expect(() => validateFromSeq(-1)).toThrow(InvalidArgumentError);
    expect(encodeStdin("hí")).toEqual(new TextEncoder().encode("hí"));
    expect(encodeStdin(new Uint8Array([1]))).toEqual(new Uint8Array([1]));
    expect(() => encodeStdin(5 as never)).toThrow(InvalidArgumentError);
  });

  test("a multibyte character split across chunks is reconstructed", () => {
    const seen: string[] = [];
    const stream = new DecodedStream((text) => seen.push(text));
    const bytes = new TextEncoder().encode("é");
    expect(stream.feed(bytes.subarray(0, 1))).toBeUndefined();
    expect(stream.feed(bytes.subarray(1))).toBe("é");
    stream.flush();
    expect(stream.text).toBe("é");
    expect(seen).toEqual(["é"]);
  });

  test("outcomeFromEnd follows the M2 table", () => {
    const exited = create(EndEventSchema, { exitCode: 0, exited: true, status: "exited" });
    expect(outcomeFromEnd(exited, "o", "e")).toEqual({
      stdout: "o",
      stderr: "e",
      exitCode: 0,
      error: undefined,
    });
    const nonZero = outcomeFromEnd(
      create(EndEventSchema, { exitCode: 3, exited: true, status: "exited" }),
      "o",
      "",
    );
    expect(nonZero).toBeInstanceOf(CommandExitError);
    expect((nonZero as CommandExitError).exitCode).toBe(3);
    expect((nonZero as CommandExitError).stdout).toBe("o");
    const signaled = outcomeFromEnd(
      create(EndEventSchema, { exitCode: 137, exited: true, status: "signaled", signal: 9 }),
      "",
      "",
    );
    expect((signaled as CommandExitError).error).toBe("signaled");
    expect(
      outcomeFromEnd(
        create(EndEventSchema, {
          status: "timeout",
          error: create(StreamErrorSchema, { code: "deadline_exceeded", message: "x" }),
        }),
        "",
        "",
      ),
    ).toBeInstanceOf(TimeoutError);
    expect(
      outcomeFromEnd(create(EndEventSchema, { status: "output_truncated" }), "", ""),
    ).toBeInstanceOf(SandboxError);
    expect(outcomeFromEnd(create(EndEventSchema, { status: "suspending" }), "", "")).toBeInstanceOf(
      SandboxStateError,
    );
    const unknown = outcomeFromEnd(
      create(EndEventSchema, {
        status: "weird",
        error: create(StreamErrorSchema, { code: "not_found", message: "gone" }),
      }),
      "",
      "",
    );
    expect(unknown).toBeInstanceOf(NotFoundError);
  });
});

describe("commands.run", () => {
  test("foreground and background parity", async () => {
    const { sandbox } = await createTestSandbox();
    const foreground = await sandbox.commands.run("echo hola");
    const background = await (await sandbox.commands.run("echo hola", { background: true })).wait();
    const expected = { stdout: "hola\n", stderr: "", exitCode: 0, error: undefined };
    expect(foreground).toEqual(expected);
    expect(background).toEqual(expected);
  });

  test("stderr, unknown commands and the wrapper request", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    const result = await sandbox.commands.run("err warning", {
      user: "user",
      cwd: "/tmp",
      envs: { A: "1" },
      tag: "t",
    });
    expect(result.stderr).toBe("warning\n");
    const request = rayd.process.startRequests[0];
    expect(request?.process?.args).toEqual(["-l", "-c", "err warning"]);
    expect(request?.process?.cwd).toBe("/tmp");
    expect(request?.user?.username).toBe("user");
    expect(request?.tag).toBe("t");
    const missing = await sandbox.commands.run("nope").catch((error: unknown) => error);
    expect(missing).toBeInstanceOf(CommandExitError);
    expect((missing as CommandExitError).exitCode).toBe(127);
    expect((missing as CommandExitError).stderr).toContain("command not found");
    expect(await sandbox.commands.run("pwd", { cwd: "/tmp" })).toMatchObject({ stdout: "/tmp\n" });
    expect(await sandbox.commands.run("env", { envs: { B: "2", A: "1" } })).toMatchObject({
      stdout: "A=1\nB=2\n",
    });
  });

  test("exit code and timeout", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    const exit = await sandbox.commands.run("exit 3").catch((error: unknown) => error);
    expect(exit).toBeInstanceOf(CommandExitError);
    expect((exit as CommandExitError).exitCode).toBe(3);
    await expect(sandbox.commands.run("sleep 30", { timeoutMs: 1000 })).rejects.toBeInstanceOf(
      TimeoutError,
    );
    expect(rayd.process.startRequests[1]?.timeoutMs).toBe(1000n);
    expect(rayd.process.startDeadlines[1]).toBe(6000);
    await sandbox.commands.run("echo x", { timeoutMs: 0 });
    expect(rayd.process.startRequests[2]?.timeoutMs).toBe(0n);
    expect(rayd.process.startDeadlines[2]).toBeUndefined();
  });

  test("stdin and callbacks", async () => {
    const { sandbox } = await createTestSandbox();
    const seen: string[] = [];
    const handle = await sandbox.commands.run("cat", {
      background: true,
      stdin: true,
      onStdout: (text) => seen.push(text),
    });
    await handle.sendStdin("ping\n");
    await handle.closeStdin();
    const result = await handle.wait();
    expect(result.stdout).toBe("ping\n");
    expect(seen.join("")).toBe("ping\n");
    expect(handle.exitCode).toBe(0);
    expect(handle.error).toBeUndefined();
    await expect(sandbox.commands.sendStdin(handle.pid, "x")).rejects.toBeInstanceOf(NotFoundError);
  });

  test("stdin without the flag is refused by the agent", async () => {
    const { sandbox } = await createTestSandbox();
    const handle = await sandbox.commands.run("sleep 5", { background: true });
    await expect(handle.sendStdin("x")).rejects.toBeInstanceOf(InvalidArgumentError);
    await expect(handle.closeStdin()).rejects.toBeInstanceOf(InvalidArgumentError);
    expect(await handle.kill()).toBe(true);
    const outcome = await handle.wait().catch((error: unknown) => error);
    expect((outcome as CommandExitError).exitCode).toBe(137);
  });

  test("iteration yields typed chunks lazily and wait resolves once", async () => {
    const { sandbox } = await createTestSandbox();
    const handle = await sandbox.commands.run("seq 3", { background: true });
    const chunks = [];
    for await (const chunk of handle) {
      chunks.push(chunk);
    }
    expect(chunks.every((chunk) => chunk.stdout !== undefined && chunk.stderr === undefined)).toBe(
      true,
    );
    expect(chunks.map((chunk) => chunk.stdout).join("")).toBe("1\n2\n3\n");
    expect(handle.stdout).toBe("1\n2\n3\n");
    expect(handle.lastSeq).toBe(3);
    expect(await handle.wait()).toMatchObject({ exitCode: 0 });
    expect(await handle.wait()).toMatchObject({ exitCode: 0 });
    const failing = await sandbox.commands.run("exit 2", { background: true });
    await expect(failing.wait()).rejects.toBeInstanceOf(CommandExitError);
    await expect(failing.wait()).rejects.toBeInstanceOf(CommandExitError);
  });

  test("large output and split multibyte characters arrive intact", async () => {
    const { sandbox } = await createTestSandbox();
    const big = await sandbox.commands.run("big 100000");
    expect(big.stdout).toHaveLength(100_000);
    const split = await sandbox.commands.run("split");
    expect(split.stdout.endsWith("é\n")).toBe(true);
    expect(split.stdout).toHaveLength(32 * 1024 - 1 + 2);
  });

  test("list, kill true/false and connect(fromSeq) replay", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    const handle = await sandbox.commands.run("sleep 30", { background: true, tag: "sleeper" });
    const listed = await sandbox.commands.list();
    expect(listed).toHaveLength(1);
    expect(listed[0]).toMatchObject({
      pid: handle.pid,
      cmd: "/bin/bash",
      tag: "sleeper",
      kind: "process",
    });
    expect(listed[0]?.args).toEqual(["-l", "-c", "sleep 30"]);
    expect(await sandbox.commands.kill(handle.pid)).toBe(true);
    await expect(handle.wait()).rejects.toBeInstanceOf(CommandExitError);
    expect(await sandbox.commands.kill(handle.pid)).toBe(false);

    const seq = await sandbox.commands.run("seq 3", { background: true });
    await seq.wait();
    const replay = await sandbox.commands.connect(seq.pid, { fromSeq: 2 });
    expect(rayd.process.connectCalls.at(-1)).toEqual([seq.pid, 2]);
    const text = (await replay.wait()).stdout;
    expect(text).toBe("2\n3\n");
    await expect(sandbox.commands.connect(seq.pid, { fromSeq: 99 })).rejects.toBeInstanceOf(
      NotFoundError,
    );
    await expect(sandbox.commands.connect(999_999)).rejects.toBeInstanceOf(NotFoundError);
    await expect(sandbox.commands.connect(0)).rejects.toBeInstanceOf(InvalidArgumentError);
  });

  test("disconnect keeps the process alive and never reconnects", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    const handle = await sandbox.commands.run("sleep 30", { background: true });
    const collector = new Collector(handle);
    handle.disconnect();
    handle.disconnect();
    await collector.join();
    expect(collector.error).toBeUndefined();
    await waitUntil(() => (rayd.process.processes.get(handle.pid)?.subscribers.length ?? 1) === 0);
    expect((await sandbox.commands.list()).map((info) => info.pid)).toContain(handle.pid);
    await expect(handle.wait()).rejects.toThrow(/desconectado/);
    expect(await handle.kill()).toBe(true);
  });

  test("connect streams live output with callbacks", async () => {
    const { sandbox } = await createTestSandbox();
    const seen: string[] = [];
    const handle = await sandbox.commands.run("seq 40", { background: true });
    const attached = await sandbox.commands.connect(handle.pid, {
      onStdout: (text) => seen.push(text),
    });
    await attached.wait();
    expect(seen.join("")).toContain("40\n");
    expect(attached.reconnects).toBe(0);
    handle.disconnect();
  });

  test("a wrong token is AuthenticationError on Start and on List", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    rayd.process.tokenSha256 = "0".repeat(64);
    await expect(sandbox.commands.run("echo hola")).rejects.toBeInstanceOf(AuthenticationError);
    await expect(sandbox.commands.list()).rejects.toBeInstanceOf(AuthenticationError);
  });

  test("foreground Start uses the unary transport, background the stream transport", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    await sandbox.commands.run("echo hola");
    expect(rayd.sessions).toBe(1);
    expect(Sandbox.coreOf(sandbox).streamTransportOpened).toBe(false);
    const handle = await sandbox.commands.run("echo hola", { background: true });
    await handle.wait();
    expect(Sandbox.coreOf(sandbox).streamTransportOpened).toBe(true);
    await waitUntil(() => rayd.sessions === 2, 2000);
  });

  test("expired JWE at stream open: one re-mint, one extra Start, the command runs once", async () => {
    const { sandbox, rayd, plane } = await createTestSandbox();
    const mints = plane.callsTo("createAuthToken").length;
    rayd.forbidNext(1);
    const result = await sandbox.commands.run("echo hola");
    expect(result.stdout).toBe("hola\n");
    expect(plane.callsTo("createAuthToken")).toHaveLength(mints + 1);
    expect(rayd.process.startRequests).toHaveLength(1);
    expect(rayd.process.startHeaders[0]?.["x-aws-proxy-auth"]).toMatch(/\.2$/);
  });

  test("a 403 on a unary is retried once after re-minting", async () => {
    const { sandbox, rayd, plane } = await createTestSandbox();
    const mints = plane.callsTo("createAuthToken").length;
    rayd.forbidNext(1);
    expect(await sandbox.commands.list()).toEqual([]);
    expect(plane.callsTo("createAuthToken")).toHaveLength(mints + 1);
    rayd.forbidNext(2);
    await expect(sandbox.commands.list()).rejects.toBeInstanceOf(AuthenticationError);
  });

  test("output_truncated closes only the subscriber", async () => {
    const { sandbox } = await createTestSandbox();
    const handle = await sandbox.commands.run("truncate", { background: true });
    const outcome = await handle.wait().catch((error: unknown) => error);
    expect(outcome).toBeInstanceOf(SandboxError);
    expect((outcome as Error).message).toContain("output_truncated");
    expect(handle.error).toBe("output_truncated");
    expect(handle.stdout).toBe("partial\n");
    expect((await sandbox.commands.list()).map((info) => info.pid)).toContain(handle.pid);
    expect(await sandbox.commands.kill(handle.pid)).toBe(true);
  });

  test("readUntil helper sees streamed output", async () => {
    const { sandbox } = await createTestSandbox();
    const handle = await sandbox.commands.run("seq 5", { background: true });
    const seen = await readUntil(handle, "3\n");
    expect(seen).toContain("1\n");
    handle.disconnect();
  });
});
