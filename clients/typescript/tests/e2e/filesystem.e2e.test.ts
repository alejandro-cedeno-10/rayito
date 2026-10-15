/**
 * Espejo de `test_m3_filesystem.py::test_m3_filesystem` para lo que es código
 * del SDK TypeScript (`sandbox/filesystem.ts`): `writeFiles` de 50 ficheros
 * dentro de presupuesto, lectura en streaming de 8 MB en al menos 30 trozos,
 * `list` con tipos y symlink, `remove` de un directorio, y `watchDir`
 * (eventos de shell, callback con entradas, errores y timeout, recursivo). La
 * política de rutas, la identidad y el modo son del guest y se prueban una
 * vez, en Python. Un sandbox compartido.
 */

import { createHash, randomBytes } from "node:crypto";
import { beforeAll, describe, expect, test } from "vitest";
import {
  type EntryInfo,
  FileNotFoundError,
  type FilesystemEvent,
  FilesystemEventType,
  FileType,
  InvalidArgumentError,
  type Sandbox,
  TimeoutError,
  type WatchHandle,
} from "../../src/index.js";
import {
  createTestSandbox,
  e2eEnabled,
  HOME,
  report,
  seconds,
  useE2E,
  waitUntil,
} from "./helpers.js";

const BASE = `${HOME}/fs-e2e`;
const WATCH_DIR = `${BASE}/watch`;
const PAYLOAD_BYTES = 8_000_000;
const READ_CHUNK_BYTES = 262_144;
const MIN_STREAM_CHUNKS = 30;
const BATCH_FILES = 50;
const BATCH_BUDGET_MS = 5000;
const EVENT_BUDGET_MS = 5000;
const EVENT_POLL_MS = 200;
const WATCH_TIMEOUT_MS = 2000;
const WATCH_TIMEOUT_BUDGET_MS = 4000;
const TEMP_PREFIX = ".rayito-tmp-";
const RESTRICTED_MODE = 0o600;
const RESTRICTED_MODE_SHELL = "600";

function sha256(data: Uint8Array): string {
  return createHash("sha256").update(data).digest("hex");
}

function named(events: readonly FilesystemEvent[]): string[] {
  return events.map((event) => `${event.type}:${event.name}`);
}

function expectCleanNames(events: readonly FilesystemEvent[]): void {
  for (const event of events) {
    expect(event.name, named([event]).join()).not.toContain("/");
    expect(event.name.startsWith(TEMP_PREFIX), event.name).toBe(false);
  }
}

/** Acumula `getNewEvents()` cada `EVENT_POLL_MS` hasta que `done` se cumple o se agota el presupuesto. */
async function collectUntil(
  handle: WatchHandle,
  done: (seen: readonly FilesystemEvent[]) => boolean,
): Promise<FilesystemEvent[]> {
  const collected: FilesystemEvent[] = [];
  await waitUntil(
    () => {
      collected.push(...handle.getNewEvents());
      return done(collected);
    },
    EVENT_BUDGET_MS,
    "los eventos del watch",
    EVENT_POLL_MS,
  );
  return collected;
}

function entryNamed(listing: readonly EntryInfo[], name: string): EntryInfo | undefined {
  return listing.find((entry) => entry.name === name);
}

describe.skipIf(!e2eEnabled())("filesystem through the TypeScript SDK (mirror of M3)", () => {
  const context = useE2E();
  let sbx: Sandbox;

  beforeAll(async () => {
    sbx = await createTestSandbox(context);
    expect(await sbx.files.makeDir(BASE)).toBe(true);
    expect(await sbx.files.makeDir(WATCH_DIR)).toBe(true);
  });

  test("a batch of 50 files in budget and an 8 MB streaming read in at least 30 chunks", async () => {
    const files = Array.from({ length: BATCH_FILES }, (_, index) => ({
      path: `${BASE}/many/f${String(index).padStart(2, "0")}.txt`,
      data: `file ${index}\n`,
    }));
    const started = performance.now();
    const entries = await sbx.files.writeFiles(files);
    const elapsedMs = performance.now() - started;
    report(`writeFiles x${BATCH_FILES} en un stream`, elapsedMs / 1000);
    expect(elapsedMs).toBeLessThanOrEqual(BATCH_BUDGET_MS);
    expect(entries.map((entry) => entry.name)).toEqual(
      files.map((file) => file.path.slice(file.path.lastIndexOf("/") + 1)),
    );
    expect((await sbx.commands.run(`ls ${BASE}/many | wc -l`)).stdout.trim()).toBe(
      String(BATCH_FILES),
    );

    const payload = new Uint8Array(randomBytes(PAYLOAD_BYTES));
    const written = await sbx.files.write(`${BASE}/big.bin`, payload);
    expect(written.size).toBe(PAYLOAD_BYTES);
    const readStarted = performance.now();
    const stream = await sbx.files.read(`${BASE}/big.bin`, { format: "stream" });
    const chunks: Uint8Array[] = [];
    for await (const chunk of stream) {
      chunks.push(chunk);
    }
    report("lectura en streaming de 8 MB", seconds(readStarted));
    expect(chunks.length).toBeGreaterThanOrEqual(MIN_STREAM_CHUNKS);
    expect(chunks.every((chunk) => chunk.length <= READ_CHUNK_BYTES)).toBe(true);
    expect(sha256(Buffer.concat(chunks))).toBe(sha256(payload));
  });

  test("list reports types and a symlink, and remove deletes a directory", async () => {
    await sbx.files.write(`${BASE}/tree/target.txt`, "t");
    await sbx.commands.run(`ln -s target.txt ${BASE}/tree/link`);
    await sbx.files.makeDir(`${BASE}/tree/sub`);
    const listing = await sbx.files.list(BASE, { depth: 2 });
    expect(entryNamed(listing, "tree")?.type).toBe(FileType.DIR);
    expect(entryNamed(listing, "target.txt")?.type).toBe(FileType.FILE);
    expect(entryNamed(listing, "sub")?.type).toBe(FileType.DIR);
    const link = entryNamed(listing, "link");
    expect(link?.type).toBe(FileType.SYMLINK);
    expect(link?.symlinkTarget).toBe("target.txt");
    expect((await sbx.files.getInfo(`${BASE}/tree/link`)).type).toBe(FileType.SYMLINK);
    await expect(sbx.files.read(`${BASE}/tree/link`, { format: "bytes" })).rejects.toBeInstanceOf(
      InvalidArgumentError,
    );
    await expect(sbx.files.list(`${BASE}/tree/target.txt`)).rejects.toBeInstanceOf(
      InvalidArgumentError,
    );
    await expect(sbx.files.list(`${BASE}/nope`)).rejects.toBeInstanceOf(FileNotFoundError);

    await expect(sbx.files.remove(`${BASE}/many`, { recursive: false })).rejects.toBeInstanceOf(
      InvalidArgumentError,
    );
    await sbx.files.remove(`${BASE}/many`);
    expect(await sbx.files.exists(`${BASE}/many`)).toBe(false);
    await expect(sbx.files.remove(`${BASE}/many`)).rejects.toBeInstanceOf(FileNotFoundError);
    expect(await sbx.files.exists(`${BASE}/big.bin`)).toBe(true);
  });

  test("watchDir: shell events in order, then a callback with entries", async () => {
    const handle = await sbx.files.watchDir(WATCH_DIR);
    await sbx.commands.run(
      `echo hola > ${WATCH_DIR}/a.txt; sleep 0.3; echo mas >> ${WATCH_DIR}/a.txt; sleep 0.3; rm ${WATCH_DIR}/a.txt`,
    );
    const events = await collectUntil(handle, (seen) =>
      named(seen).includes(`${FilesystemEventType.REMOVE}:a.txt`),
    );
    const kinds = named(events);
    const createAt = kinds.indexOf(`${FilesystemEventType.CREATE}:a.txt`);
    const writeAt = kinds.indexOf(`${FilesystemEventType.WRITE}:a.txt`);
    const removeAt = kinds.indexOf(`${FilesystemEventType.REMOVE}:a.txt`);
    expect(createAt).toBeGreaterThanOrEqual(0);
    expect(createAt).toBeLessThan(writeAt);
    expect(writeAt).toBeLessThan(removeAt);
    expectCleanNames(events);
    await handle.stop();
    expect(handle.isRunning).toBe(false);
    expect(handle.getNewEvents()).toEqual([]);

    const seen: FilesystemEvent[] = [];
    const withEntries = await sbx.files.watchDir(WATCH_DIR, {
      onEvent: (event) => seen.push(event),
      includeEntry: true,
    });
    try {
      await sbx.files.write(`${WATCH_DIR}/w.txt`, "w");
      await waitUntil(
        () =>
          seen.some(
            (event) =>
              event.name === "w.txt" &&
              event.type === FilesystemEventType.WRITE &&
              event.entry !== undefined,
          ),
        EVENT_BUDGET_MS,
        "el evento write con entrada",
        EVENT_POLL_MS,
      );
      expect(named(seen)).not.toContain(`${FilesystemEventType.RENAME}:w.txt`);
      expectCleanNames(seen);
      await sbx.commands.run(`chmod ${RESTRICTED_MODE_SHELL} ${WATCH_DIR}/w.txt`);
      await waitUntil(
        () =>
          seen.some(
            (event) =>
              event.name === "w.txt" &&
              event.type === FilesystemEventType.CHMOD &&
              event.entry?.mode === RESTRICTED_MODE,
          ),
        EVENT_BUDGET_MS,
        "el evento chmod con entrada",
        EVENT_POLL_MS,
      );
    } finally {
      await withEntries.stop();
    }
  });

  test("watchDir: a missing path, a file and a timeout", async () => {
    await expect(sbx.files.watchDir(`${BASE}/nope`)).rejects.toBeInstanceOf(FileNotFoundError);
    await expect(sbx.files.watchDir(`${BASE}/big.bin`)).rejects.toBeInstanceOf(
      InvalidArgumentError,
    );
    const handle = await sbx.files.watchDir(WATCH_DIR, { timeoutMs: WATCH_TIMEOUT_MS });
    await waitUntil(
      () => !handle.isRunning,
      WATCH_TIMEOUT_BUDGET_MS,
      "el fin del watch por timeout",
      EVENT_POLL_MS,
    );
    expect(() => handle.getNewEvents()).toThrow(TimeoutError);
    expect(handle.isRunning).toBe(false);
  });

  test("watchDir recursive sees a new subdirectory and then its file", async () => {
    const handle = await sbx.files.watchDir(WATCH_DIR, { recursive: true });
    try {
      await sbx.commands.run(`mkdir ${WATCH_DIR}/sub`);
      await collectUntil(handle, (seen) =>
        named(seen).includes(`${FilesystemEventType.CREATE}:sub`),
      );
      await sbx.commands.run(`echo x > ${WATCH_DIR}/sub/n.txt`);
      const events = await collectUntil(handle, (seen) =>
        named(seen).includes(`${FilesystemEventType.CREATE}:sub/n.txt`),
      );
      expect(named(events)).toContain(`${FilesystemEventType.CREATE}:sub/n.txt`);
    } finally {
      await handle.stop();
    }
  });
});
