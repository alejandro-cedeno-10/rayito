/**
 * Las cinco RPC de transferencia de `rayd` (diseño D8–D10) en el `rayd`
 * falso: `StartImport` arma un sondeo del GET prefirmado (cada `pollMs`) y al
 * encontrar el objeto comprueba `max_bytes` y `expected_sha256`, escribe el
 * fichero en el árbol falso y ejecuta el DELETE; `StartExport` hace el PUT o
 * los `UploadPart` de una foto del fichero; `WatchTransfer` emite la foto
 * actual, una foto por cambio y termina tras un estado final;
 * `GetTransfer("")` es la sonda de capacidad (`NotFound`). Con
 * `mode = "unimplemented"` se comporta como un `rayd` anterior a M9.
 */

import { createHash, randomBytes } from "node:crypto";
import { clone, create } from "@bufbuild/protobuf";
import { Code, ConnectError } from "@connectrpc/connect";
import {
  type EntryInfo,
  KeepAliveSchema,
  StreamErrorSchema,
} from "../../../src/gen/rayito/v1/common_pb.js";
import {
  type CancelTransferRequest,
  CancelTransferResponseSchema,
  type GetTransferRequest,
  type StartExportRequest,
  type StartImportRequest,
  StartTransferResponseSchema,
  TransferDirection,
  type TransferEvent,
  TransferEventSchema,
  TransferPhase,
  type TransferState,
  TransferStateSchema,
  type WatchTransferRequest,
} from "../../../src/gen/rayito/v1/filesystem_pb.js";
import { AsyncQueue, StreamEnd, sleep } from "./common.js";

export interface TransferFiles {
  readonly sandboxId: () => string;
  readonly snapshot: (
    path: string,
    user: string | undefined,
  ) => { readonly entry: EntryInfo; readonly data: Uint8Array };
  readonly commit: (
    path: string,
    user: string | undefined,
    data: Uint8Array,
    mode: number | undefined,
    metadata: Readonly<Record<string, string>>,
  ) => EntryInfo;
}

export interface ForcedFailure {
  readonly code: string;
  readonly message: string;
}

interface FakeTransferRecord {
  readonly state: TransferState;
  readonly watchers: Set<AsyncQueue<TransferEvent | StreamEnd>>;
  cancelled: boolean;
}

const FINISHED: ReadonlySet<TransferPhase> = new Set([
  TransferPhase.DONE,
  TransferPhase.FAILED,
  TransferPhase.CANCELLED,
]);
const EXPIRED_BODY = "Request has expired";
const S3_UNAVAILABLE: ForcedFailure = {
  code: "unavailable",
  message: "s3_unavailable: S3 no responde",
};

function sha256Hex(data: Uint8Array): string {
  return createHash("sha256").update(data).digest("hex");
}

/** Un S3 falso cerrado al terminar un test no deja rechazos sin capturar: la transferencia falla. */
async function fetchOrUndefined(url: string, init?: RequestInit): Promise<Response | undefined> {
  try {
    return await fetch(url, init);
  } catch {
    return undefined;
  }
}

function unimplemented(rpc: string): ConnectError {
  return new ConnectError(`${rpc} no existe en este rayd`, Code.Unimplemented);
}

export class FakeTransfers {
  mode: "m9" | "unimplemented" = "m9";
  pollMs = 20;
  phase: string | undefined;
  holdImports = false;
  holdExports = false;
  failNextExport: ForcedFailure | undefined;
  failNextImport: ForcedFailure | undefined;
  corruptNextExportSha = false;
  probes = 0;
  readonly imports: StartImportRequest[] = [];
  readonly exports: StartExportRequest[] = [];
  readonly cancels: string[] = [];
  readonly records = new Map<string, FakeTransferRecord>();
  readonly #files: TransferFiles;

  constructor(files: TransferFiles) {
    this.#files = files;
  }

  // ------------------------------------------------------------------ RPCs

  startImport(request: StartImportRequest) {
    this.#requireM9("StartImport");
    this.#gatePhase();
    const expectedKey = new RegExp(`/${this.#files.sandboxId()}/up/[0-9a-f]{32}$`);
    if (!expectedKey.test(request.object?.key ?? "") || request.get === undefined) {
      throw new ConnectError("la petición de transferencia no es válida", Code.InvalidArgument);
    }
    this.imports.push(request);
    const record = this.#register(TransferDirection.IMPORT, 0n);
    void this.#runImport(record, request);
    return create(StartTransferResponseSchema, { transferId: record.state.transferId });
  }

  startExport(request: StartExportRequest) {
    this.#requireM9("StartExport");
    this.#gatePhase();
    const expectedKey = new RegExp(`/${this.#files.sandboxId()}/down/[0-9a-f]{32}$`);
    if (!expectedKey.test(request.object?.key ?? "")) {
      throw new ConnectError("la petición de transferencia no es válida", Code.InvalidArgument);
    }
    const { entry, data } = this.#files.snapshot(request.path, request.user?.username);
    if (request.target.case === "multipart") {
      const partSize = Number(request.target.value.partSize);
      const expected = Math.max(1, Math.ceil(data.byteLength / partSize));
      if (request.target.value.parts.length !== expected) {
        throw new ConnectError("file_changed: el fichero cambió", Code.FailedPrecondition);
      }
    }
    this.exports.push(request);
    const record = this.#register(TransferDirection.EXPORT, BigInt(data.byteLength));
    record.state.entry = entry;
    void this.#runExport(record, request, data);
    return create(StartTransferResponseSchema, { transferId: record.state.transferId });
  }

  getTransfer(request: GetTransferRequest): TransferState {
    this.#requireM9("GetTransfer");
    if (request.transferId === "") {
      this.probes += 1;
    }
    return clone(TransferStateSchema, this.#record(request.transferId).state);
  }

  watchTransfer(request: WatchTransferRequest, signal: AbortSignal): AsyncIterable<TransferEvent> {
    this.#requireM9("WatchTransfer");
    this.#gatePhase();
    const record = this.#record(request.transferId);
    const queue = new AsyncQueue<TransferEvent | StreamEnd>();
    record.watchers.add(queue);
    return this.#watchStream(record, queue, signal);
  }

  cancelTransfer(request: CancelTransferRequest) {
    this.#requireM9("CancelTransfer");
    const record = this.#record(request.transferId);
    this.cancels.push(request.transferId);
    if (!FINISHED.has(record.state.phase)) {
      record.cancelled = true;
      this.#finish(record, TransferPhase.CANCELLED, {
        code: "cancelled",
        message: "cancelled: transferencia cancelada",
      });
    }
    return create(CancelTransferResponseSchema, {});
  }

  // --------------------------------------------------------- test controls

  /** Lo que hace `/suspend`: cada `WatchTransfer` vivo termina con `Unavailable suspending`. */
  suspend(): void {
    for (const record of this.records.values()) {
      for (const queue of record.watchers) {
        queue.push(new StreamEnd(Code.Unavailable, "suspending"));
      }
    }
    this.phase = "suspending";
  }

  resume(): void {
    this.phase = undefined;
  }

  get liveWatchers(): number {
    let count = 0;
    for (const record of this.records.values()) {
      count += record.watchers.size;
    }
    return count;
  }

  // -------------------------------------------------------------- internals

  #requireM9(rpc: string): void {
    if (this.mode === "unimplemented") {
      throw unimplemented(rpc);
    }
  }

  #gatePhase(): void {
    if (this.phase !== undefined) {
      throw new ConnectError(this.phase, Code.Unavailable);
    }
  }

  #record(transferId: string): FakeTransferRecord {
    const record = this.records.get(transferId);
    if (record === undefined) {
      throw new ConnectError("transferencia desconocida", Code.NotFound);
    }
    return record;
  }

  #register(direction: TransferDirection, bytesTotal: bigint): FakeTransferRecord {
    const transferId = randomBytes(16).toString("hex");
    const record: FakeTransferRecord = {
      state: create(TransferStateSchema, {
        transferId,
        direction,
        phase: TransferPhase.WAITING,
        bytesTotal,
      }),
      watchers: new Set(),
      cancelled: false,
    };
    this.records.set(transferId, record);
    return record;
  }

  #publish(record: FakeTransferRecord): void {
    const snapshot = create(TransferEventSchema, {
      event: { case: "state", value: clone(TransferStateSchema, record.state) },
    });
    for (const queue of record.watchers) {
      queue.push(snapshot);
    }
  }

  #finish(record: FakeTransferRecord, phase: TransferPhase, failure?: ForcedFailure): void {
    record.state.phase = phase;
    if (failure !== undefined) {
      record.state.error = create(StreamErrorSchema, failure);
    }
    this.#publish(record);
  }

  async #runImport(record: FakeTransferRecord, request: StartImportRequest): Promise<void> {
    const deadline = Number(request.expiresAtUnixMs);
    while (!record.cancelled) {
      if (this.holdImports) {
        await sleep(this.pollMs);
        continue;
      }
      const response = await fetchOrUndefined(request.get?.url ?? "");
      if (response === undefined) {
        this.#finish(record, TransferPhase.FAILED, S3_UNAVAILABLE);
        return;
      }
      record.state.probes += 1;
      if (response.status === 200) {
        await this.#landImport(record, request, response);
        return;
      }
      const body = await response.text();
      if (body.includes(EXPIRED_BODY)) {
        this.#finish(record, TransferPhase.FAILED, {
          code: "deadline_exceeded",
          message: "expired: la URL caducó",
        });
        return;
      }
      if (!request.waitForObject) {
        this.#finish(record, TransferPhase.FAILED, {
          code: "not_found",
          message: "no_object: el objeto no existe",
        });
        return;
      }
      if (Date.now() >= deadline) {
        this.#finish(record, TransferPhase.FAILED, {
          code: "deadline_exceeded",
          message: "expired: el ticket caducó sin objeto",
        });
        return;
      }
      await sleep(this.pollMs);
    }
  }

  async #landImport(
    record: FakeTransferRecord,
    request: StartImportRequest,
    response: Response,
  ): Promise<void> {
    const length = Number(response.headers.get("content-length"));
    record.state.bytesTotal = BigInt(length);
    const maxBytes = Number(request.maxBytes);
    if (maxBytes > 0 && length > maxBytes) {
      await response.body?.cancel();
      this.#finish(record, TransferPhase.FAILED, {
        code: "invalid_argument",
        message: "too_large: el objeto supera max_bytes",
      });
      await this.#deleteStaged(request);
      return;
    }
    const data = new Uint8Array(await response.arrayBuffer());
    record.state.phase = TransferPhase.RUNNING;
    this.#publish(record);
    const forced = this.failNextImport;
    this.failNextImport = undefined;
    const digest = sha256Hex(data);
    if (forced !== undefined) {
      this.#finish(record, TransferPhase.FAILED, forced);
    } else if (request.expectedSha256 !== "" && request.expectedSha256 !== digest) {
      this.#finish(record, TransferPhase.FAILED, {
        code: "failed_precondition",
        message: "checksum_mismatch: el sha256 no coincide",
      });
    } else {
      this.#commitImport(record, request, data, digest);
    }
    await this.#deleteStaged(request);
  }

  #commitImport(
    record: FakeTransferRecord,
    request: StartImportRequest,
    data: Uint8Array,
    digest: string,
  ): void {
    try {
      record.state.entry = this.#files.commit(
        request.path,
        request.user?.username,
        data,
        request.mode,
        request.metadata,
      );
    } catch (error) {
      const connect = ConnectError.from(error);
      this.#finish(record, TransferPhase.FAILED, {
        code: connect.code === Code.PermissionDenied ? "permission_denied" : "internal",
        message: `unexpected_response: ${connect.rawMessage}`,
      });
      return;
    }
    record.state.sha256 = digest;
    record.state.bytesDone = BigInt(data.byteLength);
    this.#finish(record, TransferPhase.DONE);
  }

  async #deleteStaged(request: StartImportRequest): Promise<void> {
    if (request.delete !== undefined) {
      await fetchOrUndefined(request.delete.url, { method: "DELETE" });
    }
  }

  async #runExport(
    record: FakeTransferRecord,
    request: StartExportRequest,
    data: Uint8Array,
  ): Promise<void> {
    await sleep(0);
    while (this.holdExports && !record.cancelled) {
      await sleep(this.pollMs);
    }
    const forced = this.failNextExport;
    this.failNextExport = undefined;
    if (forced !== undefined) {
      this.#finish(record, TransferPhase.FAILED, forced);
      return;
    }
    record.state.phase = TransferPhase.RUNNING;
    this.#publish(record);
    const target = request.target;
    const pieces =
      target.case === "multipart"
        ? target.value.parts.map((part, index) => {
            const size = Number(target.value.partSize);
            return { url: part.url, body: data.subarray(index * size, (index + 1) * size) };
          })
        : [{ url: target.value?.url ?? "", body: data }];
    const etags: string[] = [];
    for (const piece of pieces) {
      const response = await fetchOrUndefined(piece.url, {
        method: "PUT",
        body: piece.body,
        headers: { "content-type": "application/octet-stream" },
      });
      if (response === undefined) {
        this.#finish(record, TransferPhase.FAILED, S3_UNAVAILABLE);
        return;
      }
      if (response.status !== 200) {
        this.#finish(record, TransferPhase.FAILED, {
          code: "permission_denied",
          message: "access_denied: S3 rechazó el PUT",
        });
        return;
      }
      etags.push(response.headers.get("etag") ?? "");
      record.state.bytesDone += BigInt(piece.body.byteLength);
    }
    record.state.sha256 = this.corruptNextExportSha ? "0".repeat(64) : sha256Hex(data);
    this.corruptNextExportSha = false;
    if (target.case === "multipart") {
      record.state.partEtags = etags;
    }
    this.#finish(record, TransferPhase.DONE);
  }

  async *#watchStream(
    record: FakeTransferRecord,
    queue: AsyncQueue<TransferEvent | StreamEnd>,
    signal: AbortSignal,
  ): AsyncGenerator<TransferEvent, void, undefined> {
    try {
      yield create(TransferEventSchema, {
        event: { case: "state", value: clone(TransferStateSchema, record.state) },
      });
      if (FINISHED.has(record.state.phase)) {
        return;
      }
      yield create(TransferEventSchema, {
        event: { case: "keepalive", value: create(KeepAliveSchema, {}) },
      });
      while (!signal.aborted) {
        const item = await queue.next(signal);
        if (item === undefined) {
          return;
        }
        if (item instanceof StreamEnd) {
          throw new ConnectError(item.message, item.code);
        }
        yield item;
        if (item.event.case === "state" && FINISHED.has(item.event.value.phase)) {
          return;
        }
      }
    } finally {
      record.watchers.delete(queue);
    }
  }
}
