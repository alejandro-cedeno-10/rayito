import { Code } from "@connectrpc/connect";
import { describe, expect, test } from "vitest";
import {
  AuthenticationError,
  FileNotFoundError,
  InvalidArgumentError,
  SandboxError,
  TimeoutError,
} from "../../src/errors.js";
import { FilesystemEventType as EventTypeProto } from "../../src/gen/rayito/v1/filesystem_pb.js";
import { FilesystemEventType, FileType } from "../../src/models.js";
import {
  buildWriteRequests,
  fileRequestDeadlineMs,
  materialiseWriteData,
  modifiedTimeFromMs,
  validateDepth,
  validateMode,
  validatePath,
  validateReadFormat,
  validateWatchTimeout,
  WatchState,
} from "../../src/sandbox/filesystem.js";
import { Sandbox } from "../../src/sandbox/sandbox.js";
import { entryInfo, FakeDirectory } from "./fake/filesystem.js";
import { createTestSandbox, sleep, waitUntil } from "./helpers.js";

const HOME = "/home/user";

describe("pure helpers", () => {
  test("validators", () => {
    expect(validatePath("a")).toBe("a");
    expect(() => validatePath("")).toThrow(InvalidArgumentError);
    expect(() => validatePath("a\0b")).toThrow(InvalidArgumentError);
    expect(validateMode(undefined)).toBeUndefined();
    expect(validateMode(0o644)).toBe(0o644);
    expect(() => validateMode(0o10000)).toThrow(InvalidArgumentError);
    expect(validateDepth(0)).toBe(0);
    expect(() => validateDepth(-1)).toThrow(InvalidArgumentError);
    expect(validateReadFormat("bytes")).toBe("bytes");
    expect(() => validateReadFormat("json")).toThrow(InvalidArgumentError);
    expect(validateWatchTimeout(undefined)).toBeUndefined();
    expect(validateWatchTimeout(0)).toBeUndefined();
    expect(validateWatchTimeout(500)).toBe(500);
    expect(() => validateWatchTimeout(-1)).toThrow(InvalidArgumentError);
  });

  test("deadline arithmetic: 60 s + 1 s per MB, requestTimeoutMs overrides", () => {
    expect(fileRequestDeadlineMs(0, undefined)).toBe(60_000);
    expect(fileRequestDeadlineMs(8_000_000, undefined)).toBe(68_000);
    expect(fileRequestDeadlineMs(1, undefined)).toBe(61_000);
    expect(fileRequestDeadlineMs(8_000_000, 5000)).toBe(5000);
  });

  test("write data is materialised from every accepted form", async () => {
    const bytes = new Uint8Array([1, 2, 3]);
    expect(await materialiseWriteData("hí")).toEqual(new TextEncoder().encode("hí"));
    expect(await materialiseWriteData(bytes)).toBe(bytes);
    expect(await materialiseWriteData(bytes.buffer.slice(0))).toEqual(bytes);
    expect(await materialiseWriteData(new Blob([bytes]))).toEqual(bytes);
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new Uint8Array([1]));
        controller.enqueue(new Uint8Array([2, 3]));
        controller.close();
      },
    });
    expect(await materialiseWriteData(stream)).toEqual(bytes);
    await expect(materialiseWriteData(42 as never)).rejects.toBeInstanceOf(InvalidArgumentError);
  });

  test("buildWriteRequests: path/user/mode only on each file's first message, 1 MiB chunks", async () => {
    const big = new Uint8Array(2_500_000);
    const requests = [];
    for await (const request of buildWriteRequests(
      [
        { path: "a", data: new Uint8Array(), mode: 0o600 },
        { path: "b", data: big, mode: undefined },
      ],
      "user",
    )) {
      requests.push({
        path: request.path,
        size: request.chunk.byteLength,
        mode: request.mode,
        user: request.user?.username,
      });
    }
    expect(requests).toEqual([
      { path: "a", size: 0, mode: 0o600, user: "user" },
      { path: "b", size: 1_048_576, mode: undefined, user: "user" },
      { path: undefined, size: 1_048_576, mode: undefined, user: undefined },
      { path: undefined, size: 2_500_000 - 2 * 1_048_576, mode: undefined, user: undefined },
    ]);
  });

  test("modified times outside the Date range are clamped", () => {
    expect(modifiedTimeFromMs(1_789_000_000_000).getTime()).toBe(1_789_000_000_000);
    expect(modifiedTimeFromMs(-5).getTime()).toBe(-5);
    expect(modifiedTimeFromMs(9e15).getTime()).toBe(8.64e15);
    expect(modifiedTimeFromMs(-9e15).getTime()).toBe(-8.64e15);
  });

  test("WatchState queues without a callback, dispatches with one, and throws the failure once", () => {
    const seen: string[] = [];
    const warned: string[] = [];
    const withCallback = new WatchState(
      (event) => {
        seen.push(event.name);
        throw new Error("callback boom");
      },
      (message) => warned.push(message),
    );
    withCallback.feed({ event: { case: "keepalive", value: {} } } as never);
    withCallback.feed({
      event: { case: "filesystem", value: { name: "a", type: EventTypeProto.CREATE } },
    } as never);
    expect(seen).toEqual(["a"]);
    expect(warned).toHaveLength(1);
    expect(withCallback.drain()).toEqual([]);
    const queued = new WatchState(undefined);
    queued.feed({
      event: { case: "filesystem", value: { name: "b", type: EventTypeProto.WRITE } },
    } as never);
    expect(queued.drain().map((event) => event.type)).toEqual([FilesystemEventType.WRITE]);
    queued.recordEnd(new TimeoutError("late"));
    expect(queued.isRunning).toBe(false);
    expect(() => queued.drain()).toThrow(TimeoutError);
    expect(queued.drain()).toEqual([]);
    expect(() => queued.feed({ event: { case: "started", value: {} } } as never)).toThrow(
      SandboxError,
    );
  });
});

describe("files", () => {
  test("write and read a text file, exists and getInfo", async () => {
    const { sandbox } = await createTestSandbox();
    const info = await sandbox.files.write(`${HOME}/data.csv`, "a,b\n1,2\n");
    expect(info).toMatchObject({
      name: "data.csv",
      path: `${HOME}/data.csv`,
      size: 8,
      type: FileType.FILE,
    });
    expect(info.permissions).toBe("-rw-r--r--");
    expect(info.modifiedTime.getTime()).toBe(1_789_000_000_000);
    expect(await sandbox.files.read("data.csv")).toBe("a,b\n1,2\n");
    expect(await sandbox.files.exists("data.csv")).toBe(true);
    expect(await sandbox.files.exists("nope")).toBe(false);
    expect((await sandbox.files.getInfo(HOME)).type).toBe(FileType.DIR);
    await expect(sandbox.files.read(`${HOME}/nope`)).rejects.toBeInstanceOf(FileNotFoundError);
    await expect(sandbox.files.getInfo("/etc/passwd")).rejects.toBeInstanceOf(AuthenticationError);
  });

  test("formats agree on a 3 MB file and text rejects invalid UTF-8", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    const data = new Uint8Array(3_000_000).map((_, index) => (index * 7 + 255) & 0xff);
    await sandbox.files.write("big.bin", data);
    expect(await sandbox.files.read("big.bin", { format: "bytes" })).toEqual(data);
    const stream = await sandbox.files.read("big.bin", { format: "stream" });
    const chunks: Uint8Array[] = [];
    for await (const chunk of stream) {
      chunks.push(chunk);
    }
    expect(chunks.every((chunk) => chunk.byteLength <= 262_144)).toBe(true);
    expect(Buffer.concat(chunks)).toEqual(Buffer.from(data));
    await expect(sandbox.files.read("big.bin")).rejects.toBeInstanceOf(InvalidArgumentError);
    expect(rayd.filesystem.deadlines.Read?.[0]).toBe(63_000);
    expect(rayd.filesystem.deadlines.Write?.[0]).toBe(63_000);
  });

  test("an empty file reads as empty in every format", async () => {
    const { sandbox } = await createTestSandbox();
    await sandbox.files.write("empty", "");
    expect(await sandbox.files.read("empty")).toBe("");
    expect(await sandbox.files.read("empty", { format: "bytes" })).toEqual(new Uint8Array());
    const stream = await sandbox.files.read("empty", { format: "stream" });
    const reader = stream.getReader();
    expect((await reader.read()).done).toBe(true);
  });

  test("read refuses directories and symlinks client-side", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    rayd.filesystem.addSymlink(`${HOME}/link`, "/etc/passwd");
    await expect(sandbox.files.read(HOME)).rejects.toThrow(/directorio/);
    await expect(sandbox.files.read("link")).rejects.toThrow(/simbólico/);
    expect(rayd.filesystem.readCalls).toBe(0);
    expect((await sandbox.files.getInfo("link")).symlinkTarget).toBe("/etc/passwd");
  });

  test("deadline arithmetic on the wire", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    await sandbox.files.write("eight", new Uint8Array(8_000_000));
    expect(rayd.filesystem.deadlines.Write?.[0]).toBe(68_000);
    await sandbox.files.write("eight", new Uint8Array(8_000_000), { requestTimeoutMs: 5000 });
    expect(rayd.filesystem.deadlines.Write?.[1]).toBe(5000);
  });

  test("writeFiles sends one stream with the header on each file's first message", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    const bytes = new Uint8Array([104, 105]);
    const entries = await sandbox.files.writeFiles(
      [
        { path: "a", data: "hola" },
        { path: "b", data: new Blob([bytes]) },
        { path: "c", data: new Uint8Array(2_500_000), mode: 0o600 },
      ],
      { user: "user" },
    );
    expect(entries.map((entry) => [entry.name, entry.size])).toEqual([
      ["a", 4],
      ["b", 2],
      ["c", 2_500_000],
    ]);
    expect(entries[2]?.mode).toBe(0o600);
    expect(rayd.filesystem.writeStreams).toHaveLength(1);
    expect(rayd.filesystem.writeStreams[0]).toEqual([
      { path: "a", chunk: 4, mode: undefined, user: "user" },
      { path: "b", chunk: 2, mode: undefined, user: "user" },
      { path: "c", chunk: 1_048_576, mode: 0o600, user: "user" },
      { path: undefined, chunk: 1_048_576, mode: undefined, user: undefined },
      { path: undefined, chunk: 2_500_000 - 2 * 1_048_576, mode: undefined, user: undefined },
    ]);
    expect(rayd.filesystem.fileBytes("b")).toEqual(bytes);
    await expect(sandbox.files.writeFiles([])).rejects.toBeInstanceOf(InvalidArgumentError);
    await expect(sandbox.files.write("/etc/x", "no")).rejects.toBeInstanceOf(AuthenticationError);
  });

  test("a 403 on Write re-creates the request iterable for the retry", async () => {
    const { sandbox, rayd, plane } = await createTestSandbox();
    const mints = plane.callsTo("createAuthToken").length;
    rayd.forbidNext(1);
    await sandbox.files.write("retry", "twice");
    expect(plane.callsTo("createAuthToken")).toHaveLength(mints + 1);
    expect(rayd.filesystem.writeStreams).toHaveLength(1);
    expect(await sandbox.files.read("retry")).toBe("twice");
  });

  test("list with depth, makeDir, rename and remove", async () => {
    const { sandbox } = await createTestSandbox();
    expect(await sandbox.files.makeDir("dir/sub")).toBe(true);
    expect(await sandbox.files.makeDir("dir/sub")).toBe(false);
    await sandbox.files.write("dir/sub/f.txt", "x");
    await sandbox.files.write("dir/g.txt", "y");
    const shallow = await sandbox.files.list("dir");
    expect(shallow.map((entry) => entry.name)).toEqual(["g.txt", "sub"]);
    const deep = await sandbox.files.list("dir", { depth: 2 });
    expect(deep.map((entry) => entry.path)).toEqual([
      `${HOME}/dir/g.txt`,
      `${HOME}/dir/sub`,
      `${HOME}/dir/sub/f.txt`,
    ]);
    const renamed = await sandbox.files.rename("dir/g.txt", "dir/h.txt");
    expect(renamed.path).toBe(`${HOME}/dir/h.txt`);
    expect(await sandbox.files.exists("dir/g.txt")).toBe(false);
    await expect(sandbox.files.remove("dir", { recursive: false })).rejects.toBeInstanceOf(
      InvalidArgumentError,
    );
    await sandbox.files.remove("dir");
    expect(await sandbox.files.exists("dir")).toBe(false);
    await expect(sandbox.files.remove("dir")).rejects.toBeInstanceOf(FileNotFoundError);
    await expect(sandbox.files.rename("nope", "x")).rejects.toBeInstanceOf(FileNotFoundError);
    await expect(sandbox.files.list("nope")).rejects.toBeInstanceOf(FileNotFoundError);
    await expect(sandbox.files.makeDir("/etc/new")).rejects.toBeInstanceOf(AuthenticationError);
  });

  test("unary file RPCs travel on the unary transport, WatchDir on the stream transport", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    await sandbox.files.write("a", "1");
    await sandbox.files.read("a");
    await sandbox.files.list(HOME);
    expect(Sandbox.coreOf(sandbox).streamTransportOpened).toBe(false);
    const watch = await sandbox.files.watchDir(HOME);
    expect(Sandbox.coreOf(sandbox).streamTransportOpened).toBe(true);
    await waitUntil(() => rayd.sessions === 2, 2000);
    await watch.stop();
  });
});

describe("watchDir", () => {
  test("lifecycle with a callback: event, stop, idempotent stop", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    const seen: string[] = [];
    const watch = await sandbox.files.watchDir(HOME, {
      onEvent: (event) => seen.push(`${event.type}:${event.name}`),
    });
    expect(watch.isRunning).toBe(true);
    expect(watch.path).toBe(HOME);
    rayd.filesystem.emit(HOME, "new.txt", EventTypeProto.CREATE);
    await waitUntil(() => seen.length === 1);
    expect(seen).toEqual(["create:new.txt"]);
    await watch.stop();
    expect(watch.isRunning).toBe(false);
    expect(watch.getNewEvents()).toEqual([]);
    const started = performance.now();
    await watch.stop();
    expect(performance.now() - started).toBeLessThan(100);
    await waitUntil(() => rayd.filesystem.liveWatches === 0);
  });

  test("without a callback events queue up for getNewEvents; keepalives are ignored", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    const watch = await sandbox.files.watchDir(HOME, { recursive: true, includeEntry: true });
    expect(rayd.filesystem.watchCalls[0]?.recursive).toBe(true);
    expect(rayd.filesystem.watchCalls[0]?.includeEntry).toBe(true);
    rayd.filesystem.emitKeepalive(HOME);
    rayd.filesystem.emit(
      HOME,
      "sub/a.txt",
      EventTypeProto.WRITE,
      entryInfo(HOME, new FakeDirectory()),
    );
    rayd.filesystem.emit(HOME, "sub/b.txt", EventTypeProto.REMOVE);
    await waitUntil(() => (rayd.filesystem.watches.get(HOME)?.[0]?.events.size ?? 1) === 0);
    await sleep(50);
    const events = watch.getNewEvents();
    expect(events.map((event) => [event.type, event.name])).toEqual([
      [FilesystemEventType.WRITE, "sub/a.txt"],
      [FilesystemEventType.REMOVE, "sub/b.txt"],
    ]);
    expect(events[0]?.entry?.type).toBe(FileType.DIR);
    expect(events[1]?.entry).toBeUndefined();
    expect(watch.getNewEvents()).toEqual([]);
    await watch.stop();
  });

  test("the server ending the watch surfaces through getNewEvents and onExit once", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    const exits: Error[] = [];
    const watch = await sandbox.files.watchDir(HOME, { onExit: (error) => exits.push(error) });
    rayd.filesystem.endWatch(HOME, Code.NotFound, "directory removed");
    await waitUntil(() => !watch.isRunning);
    expect(exits).toHaveLength(1);
    expect(exits[0]).toBeInstanceOf(FileNotFoundError);
    expect(() => watch.getNewEvents()).toThrow(FileNotFoundError);
    expect(watch.getNewEvents()).toEqual([]);
  });

  test("timeoutMs > 0 is the stream deadline and ends the watch with TimeoutError", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    const exits: Error[] = [];
    const watch = await sandbox.files.watchDir(HOME, {
      timeoutMs: 300,
      onExit: (error) => exits.push(error),
    });
    expect(rayd.filesystem.deadlines.WatchDir?.[0]).toBe(300);
    await waitUntil(() => !watch.isRunning, 5000);
    expect(exits[0]).toBeInstanceOf(TimeoutError);
    const unlimited = await sandbox.files.watchDir(HOME);
    expect(rayd.filesystem.deadlines.WatchDir?.[1]).toBeUndefined();
    await unlimited.stop();
  });

  test("watchDir fails before resolving on a missing directory or a bad first message", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    await expect(sandbox.files.watchDir("/nope")).rejects.toBeInstanceOf(FileNotFoundError);
    await expect(sandbox.files.watchDir("/etc")).rejects.toBeInstanceOf(AuthenticationError);
    rayd.filesystem.watchFirstMessage = "keepalive";
    await expect(sandbox.files.watchDir(HOME)).rejects.toThrow(/WatchStarted/);
    await waitUntil(() => rayd.filesystem.liveWatches === 0);
  });

  test("await using stops the watch and Sandbox.close stops every live handle", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    {
      await using watch = await sandbox.files.watchDir(HOME);
      expect(watch.isRunning).toBe(true);
    }
    await waitUntil(() => rayd.filesystem.liveWatches === 0);
    const first = await sandbox.files.watchDir(HOME);
    const second = await sandbox.files.watchDir("/tmp");
    sandbox.close();
    expect(first.isRunning).toBe(false);
    expect(second.isRunning).toBe(false);
    await waitUntil(() => rayd.filesystem.liveWatches === 0);
  });
});
