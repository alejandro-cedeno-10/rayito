/**
 * `FilesystemService.Checkpoint`/`Restore` falsos con el contrato de `rayd`
 * en M7 (`openspec/changes/m7-s3-persistence/design.md` D1-D8): un "S3" en
 * memoria por `bucket/key_prefix`, la validación de `rayd`, `user=root`
 * rechazado, un solo checkpoint o restore a la vez (`FailedPrecondition`),
 * credenciales que se pueden retirar (`PermissionDenied` antes del primer
 * mensaje) y un guion de eventos alterable por el test.
 */

import { create } from "@bufbuild/protobuf";
import { Code, ConnectError, type HandlerContext } from "@connectrpc/connect";
import { KeepAliveSchema, StreamErrorSchema } from "../../../src/gen/rayito/v1/common_pb.js";
import {
  type CheckpointEvent,
  CheckpointEventSchema,
  type CheckpointRequest,
  type RestoreEvent,
  RestoreEventSchema,
  type RestoreRequest,
  type S3Location,
} from "../../../src/gen/rayito/v1/filesystem_pb.js";

const PERSIST_EXCLUDE_MAX = 64;
const BUCKET_CHARS = /^[a-z0-9.-]+$/;
const KEY_CHARS = /^[A-Za-z0-9!_.*'()/-]+$/;

export interface FakeCheckpoint {
  readonly files: number;
  readonly bytes: number;
  readonly archiveBytes: number;
  readonly sha256: string;
  readonly excluded: readonly string[];
}

async function sha256Hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export class FakePersistence {
  readonly objects = new Map<string, FakeCheckpoint>();
  readonly checkpointRequests: CheckpointRequest[] = [];
  readonly restoreRequests: RestoreRequest[] = [];
  homeFiles = 4;
  homeBytes = 52_428_800;
  hasCredentials = true;
  busy = false;
  unimplemented = false;
  progressEvents = 2;
  slowEventsMs = 0;
  failAfterStarted: { readonly code: string; readonly message: string } | undefined;
  /** Como `failAfterStarted` pero sólo para `Restore`: el `Checkpoint` previo sigue funcionando. */
  failRestoreAfterStarted: { readonly code: string; readonly message: string } | undefined;

  seed(bucket: string, keyPrefix: string, options: { files?: number; sha256?: string } = {}): void {
    this.objects.set(`${bucket}/${keyPrefix}`, {
      files: options.files ?? 3,
      bytes: 1234,
      archiveBytes: 999,
      sha256: options.sha256 ?? "ab".repeat(32),
      excluded: [],
    });
  }

  stored(bucket: string, keyPrefix: string): FakeCheckpoint | undefined {
    return this.objects.get(`${bucket}/${keyPrefix}`);
  }

  checkpoint(request: CheckpointRequest, context: HandlerContext): AsyncIterable<CheckpointEvent> {
    this.checkpointRequests.push(request);
    if (this.unimplemented) {
      throw new ConnectError("Unimplemented", Code.Unimplemented);
    }
    this.#validate(request.target, request.user?.username);
    if (request.exclude.length > PERSIST_EXCLUDE_MAX) {
      throw new ConnectError("more than 64 exclude entries", Code.InvalidArgument);
    }
    for (const entry of request.exclude) {
      if (entry.startsWith("/") || entry.split("/").includes("..") || entry === "") {
        throw new ConnectError("exclude entry rejected", Code.InvalidArgument);
      }
    }
    this.#requireCredentials();
    this.#acquire();
    return this.#checkpointEvents(request, context);
  }

  restore(request: RestoreRequest, context: HandlerContext): AsyncIterable<RestoreEvent> {
    this.restoreRequests.push(request);
    if (this.unimplemented) {
      throw new ConnectError("Unimplemented", Code.Unimplemented);
    }
    this.#validate(request.source, request.user?.username);
    this.#requireCredentials();
    const stored =
      request.source === undefined
        ? undefined
        : this.stored(request.source.bucket, request.source.keyPrefix);
    if (stored === undefined) {
      throw new ConnectError("no checkpoint under the prefix", Code.NotFound);
    }
    this.#acquire();
    return this.#restoreEvents(stored, context);
  }

  #validate(location: S3Location | undefined, username: string | undefined): void {
    const bucket = location?.bucket ?? "";
    if (bucket.length < 3 || bucket.length > 63 || !BUCKET_CHARS.test(bucket)) {
      throw new ConnectError("bucket rejected", Code.InvalidArgument);
    }
    const prefix = location?.keyPrefix ?? "";
    if (
      prefix === "" ||
      prefix.startsWith("/") ||
      prefix.endsWith("/") ||
      prefix.includes("//") ||
      !KEY_CHARS.test(prefix)
    ) {
      throw new ConnectError("key_prefix rejected", Code.InvalidArgument);
    }
    if (username === "root") {
      throw new ConnectError("persistence never archives the root home", Code.PermissionDenied);
    }
  }

  #requireCredentials(): void {
    if (!this.hasCredentials) {
      throw new ConnectError("no execution role credentials", Code.PermissionDenied);
    }
  }

  #acquire(): void {
    if (this.busy) {
      throw new ConnectError("persistence busy", Code.FailedPrecondition);
    }
    this.busy = true;
  }

  async #pause(context: HandlerContext): Promise<boolean> {
    if (this.slowEventsMs <= 0) {
      return false;
    }
    const deadline = performance.now() + this.slowEventsMs;
    while (performance.now() < deadline) {
      if (context.signal.aborted) {
        return true;
      }
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    return false;
  }

  async *#checkpointEvents(
    request: CheckpointRequest,
    context: HandlerContext,
  ): AsyncGenerator<CheckpointEvent, void, undefined> {
    try {
      yield create(CheckpointEventSchema, {
        event: {
          case: "started",
          value: { files: BigInt(this.homeFiles), bytes: BigInt(this.homeBytes) },
        },
      });
      for (let index = 0; index < this.progressEvents; index += 1) {
        if (await this.#pause(context)) {
          return;
        }
        yield create(CheckpointEventSchema, {
          event: {
            case: "progress",
            value: {
              filesDone: BigInt(index + 1),
              bytesRead: BigInt((index + 1) * 1000),
              bytesUploaded: BigInt(index * 1000),
            },
          },
        });
      }
      yield create(CheckpointEventSchema, {
        event: { case: "keepalive", value: create(KeepAliveSchema, {}) },
      });
      if (this.failAfterStarted !== undefined) {
        yield create(CheckpointEventSchema, {
          event: { case: "error", value: create(StreamErrorSchema, this.failAfterStarted) },
        });
        return;
      }
      const target = request.target;
      const key = `${target?.bucket ?? ""}/${target?.keyPrefix ?? ""}`;
      const archiveBytes = Math.floor(this.homeBytes / 3);
      const sha256 = await sha256Hex(key);
      this.objects.set(key, {
        files: this.homeFiles,
        bytes: this.homeBytes,
        archiveBytes,
        sha256,
        excluded: [...request.exclude],
      });
      yield create(CheckpointEventSchema, {
        event: {
          case: "done",
          value: {
            files: BigInt(this.homeFiles),
            bytesRead: BigInt(this.homeBytes),
            archiveBytes: BigInt(archiveBytes),
            sha256,
            skipped: 1n,
            durationMs: 1500,
          },
        },
      });
    } finally {
      this.busy = false;
    }
  }

  async *#restoreEvents(
    stored: FakeCheckpoint,
    context: HandlerContext,
  ): AsyncGenerator<RestoreEvent, void, undefined> {
    try {
      yield create(RestoreEventSchema, {
        event: {
          case: "started",
          value: { archiveBytes: BigInt(stored.archiveBytes), files: BigInt(stored.files) },
        },
      });
      for (let index = 0; index < this.progressEvents; index += 1) {
        if (await this.#pause(context)) {
          return;
        }
        yield create(RestoreEventSchema, {
          event: {
            case: "progress",
            value: { filesDone: BigInt(index + 1), bytesDownloaded: BigInt((index + 1) * 500) },
          },
        });
      }
      const failure = this.failRestoreAfterStarted ?? this.failAfterStarted;
      if (failure !== undefined) {
        yield create(RestoreEventSchema, {
          event: { case: "error", value: create(StreamErrorSchema, failure) },
        });
        return;
      }
      yield create(RestoreEventSchema, {
        event: {
          case: "done",
          value: {
            files: BigInt(stored.files),
            bytesWritten: BigInt(stored.bytes),
            archiveBytes: BigInt(stored.archiveBytes),
            sha256: stored.sha256,
            skipped: 0n,
            durationMs: 800,
          },
        },
      });
    } finally {
      this.busy = false;
    }
  }
}
