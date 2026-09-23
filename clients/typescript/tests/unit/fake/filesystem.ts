/**
 * `FilesystemService` falso en proceso, con el contrato de `rayd` en M3/M5.
 * Árbol en memoria sembrado con `/home/user`, `/tmp` y `/etc/passwd`; lista
 * de denegación `/etc` y `/usr` sobre la ruta canónica; rutas relativas al
 * home; `..` rechazado; `Read` en chunks de 256 KiB sin seguir el symlink
 * final; `Write` con la máquina de estados de un fichero por mensaje con
 * `path` y el tope de 1 MiB por chunk; `ListDir` con `depth`; `MakeDir`/
 * `Move`/`Remove` con sus status; `WatchDir` que emite `WatchStarted`, un
 * `keepalive` y luego lo que el test empuje con `emit()`/`endWatch()`. Cada
 * RPC exige `x-access-token` y registra las cabeceras y el deadline recibido.
 */

import { create } from "@bufbuild/protobuf";
import { Code, ConnectError, type HandlerContext } from "@connectrpc/connect";
import {
  type EntryInfo,
  EntryInfoSchema,
  FileType,
  KeepAliveSchema,
  UserSchema,
} from "../../../src/gen/rayito/v1/common_pb.js";
import {
  type CancelTransferRequest,
  type CheckpointEvent,
  type CheckpointRequest,
  FilesystemEventSchema,
  type FilesystemEventType,
  type GetTransferRequest,
  type ListDirRequest,
  ListDirResponseSchema,
  type MakeDirRequest,
  MakeDirResponseSchema,
  type MoveRequest,
  MoveResponseSchema,
  type ReadRequest,
  ReadRequestSchema,
  type ReadResponse,
  ReadResponseSchema,
  type RemoveRequest,
  RemoveResponseSchema,
  type RestoreEvent,
  type RestoreRequest,
  type StartExportRequest,
  type StartImportRequest,
  type StatRequest,
  StatResponseSchema,
  type TransferEvent,
  type WatchDirRequest,
  type WatchDirResponse,
  WatchDirResponseSchema,
  WatchStartedSchema,
  type WatchTransferRequest,
  type WriteRequest,
  WriteRequestSchema,
  WriteResponseSchema,
} from "../../../src/gen/rayito/v1/filesystem_pb.js";
import {
  AsyncQueue,
  abortWith,
  assertProxyHeaders,
  bytes,
  chunks,
  deadlineFromHeaders,
  type HeaderMap,
  headerMap,
  record,
  requireAccessToken,
  StreamEnd,
} from "./common.js";
import { SANDBOX_ID } from "./control-plane.js";
import { FakePersistence } from "./persistence.js";
import { FakeTransfers } from "./transfer.js";

export const HOME = "/home/user";
const DENIED_PREFIXES = ["/etc", "/usr"];
export const READ_CHUNK_BYTES = 262_144;
const MAX_WRITE_CHUNK_BYTES = 1_048_576;
const MODE_MAX = 0o7777;
const DEFAULT_FILE_MODE = 0o644;
const DEFAULT_DIR_MODE = 0o755;
const MAX_LIST_ENTRIES = 10_000;
const KNOWN_USERS: ReadonlySet<string> = new Set(["user", "root"]);
export const MODIFIED_UNIX_MS = 1_789_000_000_000;
const DIRECTORY_SIZE = 4096;
const PERMISSION_BITS = "rwxrwxrwx";

export class FakeFile {
  data: Uint8Array;
  mode: number;
  readonly metadata: Readonly<Record<string, string>>;
  readonly kind = "file";

  constructor(
    data: Uint8Array = new Uint8Array(),
    mode = DEFAULT_FILE_MODE,
    metadata: Readonly<Record<string, string>> = {},
  ) {
    this.data = data;
    this.mode = mode;
    this.metadata = metadata;
  }
}

/** Lo que hace `rayd` con `WriteRequest.metadata`: claves en minúsculas. */
export function lowercasedMetadata(
  metadata: Readonly<Record<string, string>>,
): Readonly<Record<string, string>> {
  return Object.fromEntries(
    Object.entries(metadata).map(([key, value]) => [key.toLowerCase(), value]),
  );
}

export class FakeDirectory {
  mode: number;
  readonly kind = "dir";

  constructor(mode = DEFAULT_DIR_MODE) {
    this.mode = mode;
  }
}

export class FakeSymlink {
  readonly target: string;
  readonly kind = "symlink";

  constructor(target: string) {
    this.target = target;
  }
}

export type Node = FakeFile | FakeDirectory | FakeSymlink;

function invalid(message: string): ConnectError {
  return new ConnectError(message, Code.InvalidArgument);
}

function notFound(): ConnectError {
  return new ConnectError("path not found", Code.NotFound);
}

function denied(): ConnectError {
  return new ConnectError("path is denied by policy", Code.PermissionDenied);
}

function failedPrecondition(message: string): ConnectError {
  return new ConnectError(message, Code.FailedPrecondition);
}

export function normalize(raw: string): string {
  if (!raw) {
    throw invalid("path is empty");
  }
  if (raw.includes("\0")) {
    throw invalid("path contains NUL");
  }
  const absolute = raw.startsWith("/") ? raw : `${HOME}/${raw}`;
  const parts: string[] = [];
  for (const component of absolute.split("/")) {
    if (component === "" || component === ".") {
      continue;
    }
    if (component === "..") {
      throw invalid("parent references are not allowed");
    }
    parts.push(component);
  }
  return `/${parts.join("/")}`;
}

function parentOf(path: string): string {
  const index = path.lastIndexOf("/");
  return index <= 0 ? "/" : path.slice(0, index);
}

function nameOf(path: string): string {
  const index = path.lastIndexOf("/");
  return path.slice(index + 1) || "/";
}

function join(parent: string, name: string): string {
  return parent === "/" ? `/${name}` : `${parent}/${name}`;
}

function isDenied(canonical: string): boolean {
  return DENIED_PREFIXES.some(
    (prefix) => canonical === prefix || canonical.startsWith(`${prefix}/`),
  );
}

function permissionsString(node: Node): string {
  if (node instanceof FakeSymlink) {
    return "lrwxrwxrwx";
  }
  const kind = node instanceof FakeDirectory ? "d" : "-";
  let bits = "";
  for (let index = 0; index < 9; index += 1) {
    bits += node.mode & (1 << (8 - index)) ? PERMISSION_BITS[index] : "-";
  }
  return kind + bits;
}

export function entryInfo(path: string, node: Node): EntryInfo {
  let type: FileType;
  let size: number;
  let mode: number;
  if (node instanceof FakeFile) {
    type = FileType.FILE;
    size = node.data.byteLength;
    mode = node.mode;
  } else if (node instanceof FakeDirectory) {
    type = FileType.DIRECTORY;
    size = DIRECTORY_SIZE;
    mode = node.mode;
  } else {
    type = FileType.SYMLINK;
    size = node.target.length;
    mode = 0o777;
  }
  const info = create(EntryInfoSchema, {
    name: nameOf(path),
    type,
    path,
    size: BigInt(size),
    mode,
    permissions: permissionsString(node),
    owner: "user",
    group: "user",
    modifiedTimeUnixMs: BigInt(MODIFIED_UNIX_MS),
  });
  if (node instanceof FakeSymlink) {
    info.symlinkTarget = node.target;
  }
  if (node instanceof FakeFile) {
    info.metadata = { ...node.metadata };
  }
  return info;
}

/** Árbol de nodos indexado por ruta canónica. */
export class FakeTree {
  readonly nodes = new Map<string, Node>([
    ["/", new FakeDirectory()],
    ["/home", new FakeDirectory()],
    [HOME, new FakeDirectory()],
    ["/tmp", new FakeDirectory()],
    ["/etc", new FakeDirectory()],
    ["/etc/passwd", new FakeFile(bytes("root:x:0:0:root:/root:/bin/bash\n"))],
    ["/usr", new FakeDirectory()],
  ]);

  /** `realpath` léxico: sigue symlinks en cada componente y anexa tal cual los componentes que no existen. */
  resolve(path: string): string {
    let current = "/";
    for (const component of path.split("/")) {
      if (!component) {
        continue;
      }
      current = join(current, component);
      const node = this.nodes.get(current);
      if (node instanceof FakeSymlink) {
        const target = node.target;
        const absolute = target.startsWith("/") ? target : join(parentOf(current), target);
        current = this.resolve(normalize(absolute));
      }
    }
    return current;
  }

  /** Padre resuelto + nombre final sin resolver (el símil de `O_NOFOLLOW`). */
  canonical(path: string): string {
    if (path === "/") {
      return "/";
    }
    return join(this.resolve(parentOf(path)), nameOf(path));
  }

  children(canonical: string): string[] {
    const names: string[] = [];
    for (const path of this.nodes.keys()) {
      if (path !== "/" && parentOf(path) === canonical) {
        names.push(nameOf(path));
      }
    }
    return names.sort((a, b) => Buffer.compare(Buffer.from(a), Buffer.from(b)));
  }

  subtree(canonical: string): string[] {
    const prefix = `${canonical.replace(/\/+$/, "")}/`;
    return [...this.nodes.keys()].filter((path) => path === canonical || path.startsWith(prefix));
  }

  ensureParents(canonical: string): void {
    let parent = parentOf(canonical);
    const missing: string[] = [];
    while (!this.nodes.has(parent)) {
      missing.push(parent);
      parent = parentOf(parent);
    }
    if (!(this.nodes.get(parent) instanceof FakeDirectory)) {
      throw invalid("a parent component is not a directory");
    }
    for (const path of missing.reverse()) {
      this.nodes.set(path, new FakeDirectory());
    }
  }
}

interface OpenWrite {
  path: string;
  canonical: string;
  mode: number;
  metadata: Readonly<Record<string, string>>;
  parts: Uint8Array[];
}

export interface WriteMessageSummary {
  readonly path: string | undefined;
  readonly chunk: number;
  readonly mode: number | undefined;
  readonly user: string | undefined;
}

interface FakeWatch {
  readonly path: string;
  readonly request: WatchDirRequest;
  readonly events: AsyncQueue<WatchDirResponse | StreamEnd>;
}

interface WithUser {
  readonly user?: { readonly username: string } | undefined;
}

/** `FilesystemService` como lo implementa `rayd` en M3/M5 (ver módulo). */
export class FakeFilesystemService {
  readonly tokenSha256: string;
  allowRoot = false;
  phase: string | undefined;
  maxListEntries = MAX_LIST_ENTRIES;
  watchFirstMessage: "started" | "keepalive" = "started";
  readonly tree = new FakeTree();
  readCalls = 0;
  readonly writeStreams: WriteMessageSummary[][] = [];
  readonly deadlines: Record<string, Array<number | undefined>> = {};
  readonly headers: Record<string, HeaderMap[]> = {};
  readonly watches = new Map<string, FakeWatch[]>();
  readonly watchRequests: WatchDirRequest[] = [];
  liveWatches = 0;
  readonly persistence = new FakePersistence();
  sandboxId = SANDBOX_ID;
  readStallAfterFirstChunk = false;
  readonly writeMetadata: Array<Readonly<Record<string, string>>> = [];
  readonly transfers: FakeTransfers;
  /** Los siguientes `Write` terminan con este status tras leer el stream (p. ej. `disk_reserve`). */
  readonly writeRejections: ConnectError[] = [];

  constructor(tokenSha256: string) {
    this.tokenSha256 = tokenSha256;
    this.transfers = new FakeTransfers({
      sandboxId: () => this.sandboxId,
      snapshot: (path, username) => this.#snapshot(path, username),
      commit: (path, username, data, mode, metadata) =>
        this.#importFile(path, username, data, mode, metadata),
    });
  }

  get watchCalls(): WatchDirRequest[] {
    return this.watchRequests;
  }

  // ------------------------------------------------------------------ RPCs

  read(request: ReadRequest, context: HandlerContext): AsyncIterable<ReadResponse> {
    this.#enter("Read", context);
    this.#gatePhase();
    const data = this.#readable(request);
    this.readCalls += 1;
    return this.readStallAfterFirstChunk
      ? this.#stallAfterFirstChunk(data, context.signal)
      : this.#readChunks(data);
  }

  async write(requests: AsyncIterable<WriteRequest>, context: HandlerContext) {
    this.#enter("Write", context);
    this.#gatePhase();
    const rejection = this.writeRejections.shift();
    if (rejection !== undefined) {
      for await (const _request of requests) {
      }
      throw rejection;
    }
    const messages: WriteMessageSummary[] = [];
    const entries: EntryInfo[] = [];
    let current: OpenWrite | undefined;
    try {
      for await (const request of requests) {
        messages.push(summarizeWrite(request));
        const chunk = request.chunk;
        if (chunk.byteLength > MAX_WRITE_CHUNK_BYTES) {
          throw invalid("chunk exceeds 1 MiB");
        }
        if (request.path !== undefined) {
          if (current !== undefined) {
            entries.push(this.#commit(current));
          }
          current = this.#begin(request);
        } else if (current === undefined) {
          throw invalid("first message must carry path");
        } else if (
          request.user !== undefined ||
          request.mode !== undefined ||
          Object.keys(request.metadata).length > 0
        ) {
          throw invalid("user, mode and metadata only travel with path");
        }
        current.parts.push(chunk);
      }
      if (current === undefined) {
        throw invalid("stream carried no files");
      }
      entries.push(this.#commit(current));
    } finally {
      this.writeStreams.push(messages);
    }
    return create(WriteResponseSchema, { entries });
  }

  stat(request: StatRequest, context: HandlerContext) {
    this.#enter("Stat", context);
    this.#identity(request);
    const path = normalize(request.path);
    const node = this.#existing(this.#target(request.path));
    return create(StatResponseSchema, { entry: entryInfo(path, node) });
  }

  listDir(request: ListDirRequest, context: HandlerContext) {
    this.#enter("ListDir", context);
    this.#identity(request);
    const path = normalize(request.path);
    const canonical = this.#directoryRoot(path);
    const entries = this.#walk(path, canonical, Math.max(1, request.depth));
    return create(ListDirResponseSchema, { entries });
  }

  makeDir(request: MakeDirRequest, context: HandlerContext) {
    this.#enter("MakeDir", context);
    this.#identity(request);
    const path = normalize(request.path);
    const canonical = this.#target(request.path);
    const existing = this.tree.nodes.get(canonical);
    if (existing instanceof FakeDirectory) {
      throw new ConnectError("directory already exists", Code.AlreadyExists);
    }
    if (existing !== undefined) {
      throw invalid("exists and is not a directory");
    }
    this.tree.ensureParents(canonical);
    const directory = new FakeDirectory();
    this.tree.nodes.set(canonical, directory);
    return create(MakeDirResponseSchema, { entry: entryInfo(path, directory) });
  }

  move(request: MoveRequest, context: HandlerContext) {
    this.#enter("Move", context);
    this.#identity(request);
    const destination = normalize(request.destination);
    const sourceCanonical = this.#target(request.source);
    const source = this.#existing(sourceCanonical);
    const destinationCanonical = this.#target(request.destination);
    this.#checkMoveDestination(source, destinationCanonical);
    this.#relocate(sourceCanonical, destinationCanonical);
    return create(MoveResponseSchema, {
      entry: entryInfo(destination, this.tree.nodes.get(destinationCanonical) as Node),
    });
  }

  remove(request: RemoveRequest, context: HandlerContext) {
    this.#enter("Remove", context);
    this.#identity(request);
    const canonical = this.#target(request.path);
    const node = this.#existing(canonical);
    if (node instanceof FakeDirectory) {
      if (this.tree.children(canonical).length > 0 && !request.recursive) {
        throw failedPrecondition("directory not empty; use recursive");
      }
      for (const path of this.tree.subtree(canonical)) {
        this.tree.nodes.delete(path);
      }
    } else {
      this.tree.nodes.delete(canonical);
    }
    return create(RemoveResponseSchema, {});
  }

  watchDir(request: WatchDirRequest, context: HandlerContext): AsyncIterable<WatchDirResponse> {
    this.#enter("WatchDir", context);
    this.#gatePhase();
    this.#identity(request);
    const path = normalize(request.path);
    this.#directoryRoot(path);
    const watch: FakeWatch = { path, request, events: new AsyncQueue() };
    this.watchRequests.push(request);
    (this.watches.get(path) ?? this.watches.set(path, []).get(path))?.push(watch);
    this.liveWatches += 1;
    return this.#watchStream(watch, context);
  }

  checkpoint(request: CheckpointRequest, context: HandlerContext): AsyncIterable<CheckpointEvent> {
    this.#enter("Checkpoint", context);
    this.#gatePhase();
    return this.persistence.checkpoint(request, context);
  }

  restore(request: RestoreRequest, context: HandlerContext): AsyncIterable<RestoreEvent> {
    this.#enter("Restore", context);
    this.#gatePhase();
    return this.persistence.restore(request, context);
  }

  startImport(request: StartImportRequest, context: HandlerContext) {
    this.#enter("StartImport", context);
    return this.transfers.startImport(request);
  }

  startExport(request: StartExportRequest, context: HandlerContext) {
    this.#enter("StartExport", context);
    return this.transfers.startExport(request);
  }

  getTransfer(request: GetTransferRequest, context: HandlerContext) {
    this.#enter("GetTransfer", context);
    return this.transfers.getTransfer(request);
  }

  watchTransfer(
    request: WatchTransferRequest,
    context: HandlerContext,
  ): AsyncIterable<TransferEvent> {
    this.#enter("WatchTransfer", context);
    return this.transfers.watchTransfer(request, context.signal);
  }

  cancelTransfer(request: CancelTransferRequest, context: HandlerContext) {
    this.#enter("CancelTransfer", context);
    return this.transfers.cancelTransfer(request);
  }

  // --------------------------------------------------------- test controls

  /** Entrega un `FilesystemEvent` a todos los watches vivos de `path`. */
  emit(path: string, name: string, type: FilesystemEventType, entry?: EntryInfo): void {
    const event = create(FilesystemEventSchema, { name, type });
    if (entry !== undefined) {
      event.entry = entry;
    }
    this.#broadcast(
      path,
      create(WatchDirResponseSchema, { event: { case: "filesystem", value: event } }),
    );
  }

  emitKeepalive(path: string): void {
    this.#broadcast(
      path,
      create(WatchDirResponseSchema, {
        event: { case: "keepalive", value: create(KeepAliveSchema, {}) },
      }),
    );
  }

  /** Termina los watches de `path` con un status gRPC (p. ej. `NotFound` cuando el directorio desaparece). */
  endWatch(path: string, code: Code, message = "watch ended"): void {
    this.#broadcast(path, new StreamEnd(code, message));
  }

  /** Lo que hace `/suspend`: todo `WatchDir` vivo termina con `Unavailable suspending` y los nuevos se rechazan. */
  suspend(): void {
    for (const watches of this.watches.values()) {
      for (const watch of watches) {
        watch.events.push(new StreamEnd(Code.Unavailable, "suspending"));
      }
    }
    this.phase = "suspending";
    this.transfers.suspend();
  }

  resume(): void {
    this.phase = undefined;
    this.transfers.resume();
  }

  nodeAt(path: string): Node | undefined {
    return this.tree.nodes.get(this.tree.canonical(normalize(path)));
  }

  fileBytes(path: string): Uint8Array {
    const node = this.nodeAt(path);
    if (!(node instanceof FakeFile)) {
      throw new Error(`${path} no es un fichero del fake`);
    }
    return node.data;
  }

  addFile(
    path: string,
    data: Uint8Array | string,
    mode = DEFAULT_FILE_MODE,
    metadata: Readonly<Record<string, string>> = {},
  ): void {
    const canonical = this.tree.canonical(normalize(path));
    this.tree.ensureParents(canonical);
    this.tree.nodes.set(
      canonical,
      new FakeFile(typeof data === "string" ? bytes(data) : data, mode, metadata),
    );
  }

  addDir(path: string): void {
    const canonical = this.tree.canonical(normalize(path));
    this.tree.ensureParents(canonical);
    this.tree.nodes.set(canonical, new FakeDirectory());
  }

  addSymlink(path: string, target: string): void {
    const canonical = this.tree.canonical(normalize(path));
    this.tree.ensureParents(canonical);
    this.tree.nodes.set(canonical, new FakeSymlink(target));
  }

  /** Siembra un fichero bajo un prefijo denegado sin pasar por la política. */
  addDenied(path: string, data: Uint8Array): void {
    const canonical = normalize(path);
    this.tree.ensureParents(canonical);
    this.tree.nodes.set(canonical, new FakeFile(data));
  }

  // -------------------------------------------------------------- internals

  #enter(rpc: string, context: HandlerContext): void {
    const headers = headerMap(context);
    assertProxyHeaders(headers);
    record(this.headers, rpc, headers);
    record(this.deadlines, rpc, deadlineFromHeaders(headers));
    requireAccessToken(headers, this.tokenSha256);
  }

  #gatePhase(): void {
    if (this.phase !== undefined) {
      throw new ConnectError(this.phase, Code.Unavailable);
    }
  }

  #identity(request: WithUser): void {
    const username = request.user?.username;
    if (username === undefined) {
      return;
    }
    if (username === "root" && !this.allowRoot) {
      throw new ConnectError("root is not allowed", Code.PermissionDenied);
    }
    if (!KNOWN_USERS.has(username)) {
      throw invalid("unknown user");
    }
  }

  /** Ruta canónica de un componente final sin resolver, ya pasada por la lista de denegación. */
  #target(raw: string): string {
    const canonical = this.tree.canonical(normalize(raw));
    if (isDenied(canonical)) {
      throw denied();
    }
    return canonical;
  }

  /** Raíz de `ListDir`/`WatchDir`: se sigue el symlink final a propósito. */
  #directoryRoot(path: string): string {
    const canonical = this.tree.resolve(path);
    if (isDenied(canonical)) {
      throw denied();
    }
    const node = this.tree.nodes.get(canonical);
    if (node === undefined) {
      throw notFound();
    }
    if (!(node instanceof FakeDirectory)) {
      throw invalid("path is not a directory");
    }
    return canonical;
  }

  #existing(canonical: string): Node {
    const node = this.tree.nodes.get(canonical);
    if (node === undefined) {
      throw notFound();
    }
    return node;
  }

  #readable(request: ReadRequest): Uint8Array {
    this.#identity(request);
    const node = this.#existing(this.#target(request.path));
    if (node instanceof FakeDirectory) {
      throw invalid("path is a directory");
    }
    if (node instanceof FakeSymlink) {
      throw invalid("path is a symlink");
    }
    return node.data;
  }

  async *#readChunks(data: Uint8Array): AsyncGenerator<ReadResponse, void, undefined> {
    for (const chunk of chunks(data, READ_CHUNK_BYTES)) {
      yield create(ReadResponseSchema, { chunk });
    }
  }

  async *#stallAfterFirstChunk(
    data: Uint8Array,
    signal: AbortSignal,
  ): AsyncGenerator<ReadResponse, void, undefined> {
    yield create(ReadResponseSchema, { chunk: data.subarray(0, READ_CHUNK_BYTES) });
    await new AsyncQueue<never>().next(signal);
  }

  #begin(request: WriteRequest): OpenWrite {
    this.#identity(request);
    const mode = request.mode ?? DEFAULT_FILE_MODE;
    if (mode > MODE_MAX) {
      throw invalid("mode exceeds 0o7777");
    }
    const path = normalize(request.path as string);
    const canonical = this.#target(request.path as string);
    if (this.tree.nodes.get(canonical) instanceof FakeDirectory) {
      throw invalid("destination is a directory");
    }
    this.tree.ensureParents(canonical);
    const metadata = lowercasedMetadata(request.metadata);
    this.writeMetadata.push(metadata);
    return { path, canonical, mode, metadata, parts: [] };
  }

  #commit(open: OpenWrite): EntryInfo {
    const total = open.parts.reduce((sum, part) => sum + part.byteLength, 0);
    const data = new Uint8Array(total);
    let offset = 0;
    for (const part of open.parts) {
      data.set(part, offset);
      offset += part.byteLength;
    }
    const node = new FakeFile(data, open.mode, open.metadata);
    this.tree.nodes.set(open.canonical, node);
    return entryInfo(open.path, node);
  }

  #snapshot(raw: string, username: string | undefined): { entry: EntryInfo; data: Uint8Array } {
    const owner = username === undefined ? undefined : create(UserSchema, { username });
    const data = this.#readable(create(ReadRequestSchema, { path: raw, user: owner }));
    return {
      entry: entryInfo(normalize(raw), this.#existing(this.#target(raw))),
      data: data.slice(),
    };
  }

  #importFile(
    raw: string,
    username: string | undefined,
    data: Uint8Array,
    mode: number | undefined,
    metadata: Readonly<Record<string, string>>,
  ): EntryInfo {
    const owner = username === undefined ? undefined : create(UserSchema, { username });
    const open = this.#begin(
      create(WriteRequestSchema, { path: raw, mode, user: owner, metadata: { ...metadata } }),
    );
    open.parts.push(data);
    return this.#commit(open);
  }

  #walk(root: string, canonicalRoot: string, depth: number): EntryInfo[] {
    const entries: EntryInfo[] = [];
    this.#visit(root, canonicalRoot, 1, depth, entries);
    return entries;
  }

  #visit(
    path: string,
    canonical: string,
    level: number,
    depth: number,
    entries: EntryInfo[],
  ): void {
    for (const name of this.tree.children(canonical)) {
      const childCanonical = join(canonical, name);
      const node = this.tree.nodes.get(childCanonical) as Node;
      const childPath = join(path, name);
      entries.push(entryInfo(childPath, node));
      if (entries.length > this.maxListEntries) {
        throw new ConnectError(
          `listing exceeds ${this.maxListEntries} entries; reduce depth`,
          Code.ResourceExhausted,
        );
      }
      const descend = node instanceof FakeDirectory && level < depth;
      if (descend && !isDenied(childCanonical)) {
        this.#visit(childPath, childCanonical, level + 1, depth, entries);
      }
    }
  }

  #checkMoveDestination(source: Node, destinationCanonical: string): void {
    const parent = this.tree.nodes.get(parentOf(destinationCanonical));
    if (parent === undefined) {
      throw notFound();
    }
    if (!(parent instanceof FakeDirectory)) {
      throw failedPrecondition("destination conflicts with an existing entry");
    }
    const existing = this.tree.nodes.get(destinationCanonical);
    if (existing === undefined) {
      return;
    }
    const sourceIsDir = source instanceof FakeDirectory;
    const existingIsDir = existing instanceof FakeDirectory;
    if (sourceIsDir !== existingIsDir) {
      throw failedPrecondition("destination conflicts with an existing entry");
    }
    if (existingIsDir && this.tree.children(destinationCanonical).length > 0) {
      throw failedPrecondition("destination conflicts with an existing entry");
    }
  }

  #relocate(sourceCanonical: string, destinationCanonical: string): void {
    const moved = new Map<string, Node>();
    for (const path of this.tree.subtree(sourceCanonical)) {
      moved.set(
        destinationCanonical + path.slice(sourceCanonical.length),
        this.tree.nodes.get(path) as Node,
      );
      this.tree.nodes.delete(path);
    }
    for (const [path, node] of moved) {
      this.tree.nodes.set(path, node);
    }
  }

  async *#watchStream(
    watch: FakeWatch,
    context: HandlerContext,
  ): AsyncGenerator<WatchDirResponse, void, undefined> {
    try {
      yield this.#firstWatchMessage();
      yield create(WatchDirResponseSchema, {
        event: { case: "keepalive", value: create(KeepAliveSchema, {}) },
      });
      while (!context.signal.aborted) {
        const item = await watch.events.next(context.signal);
        if (item === undefined) {
          return;
        }
        if (item instanceof StreamEnd) {
          abortWith(item);
        }
        yield item;
      }
    } finally {
      this.#release(watch);
    }
  }

  #firstWatchMessage(): WatchDirResponse {
    if (this.watchFirstMessage === "started") {
      return create(WatchDirResponseSchema, {
        event: { case: "started", value: create(WatchStartedSchema, {}) },
      });
    }
    return create(WatchDirResponseSchema, {
      event: { case: "keepalive", value: create(KeepAliveSchema, {}) },
    });
  }

  #release(watch: FakeWatch): void {
    const list = this.watches.get(watch.path);
    if (list?.includes(watch)) {
      this.watches.set(
        watch.path,
        list.filter((item) => item !== watch),
      );
      this.liveWatches -= 1;
    }
  }

  #broadcast(path: string, item: WatchDirResponse | StreamEnd): void {
    for (const watch of this.watches.get(normalize(path)) ?? []) {
      watch.events.push(item);
    }
  }
}

export function summarizeWrite(request: WriteRequest): WriteMessageSummary {
  return {
    path: request.path,
    chunk: request.chunk.byteLength,
    mode: request.mode,
    user: request.user?.username,
  };
}
