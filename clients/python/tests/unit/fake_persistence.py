"""`FilesystemService.Checkpoint`/`Restore` falsos con el contrato de `rayd`
en M7 (`openspec/changes/m7-s3-persistence/design.md` D1-D8).

Un "S3" en memoria por `bucket/key_prefix` (un manifest con `files`,
`archive_bytes` y `sha256`), validación de `bucket`/`key_prefix`/`exclude`
como la de `rayd`, `user=root` rechazado, un solo checkpoint o restore a la
vez (`FAILED_PRECONDITION`), credenciales que se pueden retirar
(`PERMISSION_DENIED` antes del primer mensaje), y un guion de eventos
(`started`, `progress`..., `done` | `error`) que un test puede alterar:
`fail_after_started`, `slow_events`, `unimplemented`.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Generator, Iterator
from dataclasses import dataclass, field
from typing import Any

import grpc

from rayito.v1 import common_pb2, filesystem_pb2

PERSIST_EXCLUDE_MAX = 64
BUCKET_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789.-")
KEY_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!_.*'()-/")


@dataclass
class FakeCheckpoint:
    files: int
    bytes: int
    archive_bytes: int
    sha256: str
    excluded: tuple[str, ...]
    sandbox_id: str


@dataclass
class FakePersistence:
    """Estado y guion compartidos por los dos RPCs del `FakeFilesystemService`."""

    objects: dict[str, FakeCheckpoint] = field(default_factory=dict)
    home_files: int = 4
    home_bytes: int = 52_428_800
    has_credentials: bool = True
    busy: bool = False
    unimplemented: bool = False
    progress_events: int = 2
    slow_events: float = 0.0
    fail_after_started: tuple[str, str] | None = None
    checkpoint_requests: list[filesystem_pb2.CheckpointRequest] = field(default_factory=list)
    restore_requests: list[filesystem_pb2.RestoreRequest] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    # --------------------------------------------------------- test controls

    def seed(
        self, bucket: str, key_prefix: str, *, files: int = 3, sha256: str = "ab" * 32
    ) -> None:
        self.objects[f"{bucket}/{key_prefix}"] = FakeCheckpoint(
            files=files,
            bytes=1234,
            archive_bytes=999,
            sha256=sha256,
            excluded=(),
            sandbox_id="mvm-old",
        )

    def stored(self, bucket: str, key_prefix: str) -> FakeCheckpoint | None:
        return self.objects.get(f"{bucket}/{key_prefix}")

    # ------------------------------------------------------------------ RPCs

    def checkpoint(
        self, request: filesystem_pb2.CheckpointRequest, context: grpc.ServicerContext
    ) -> Iterator[filesystem_pb2.CheckpointEvent]:
        with self.lock:
            self.checkpoint_requests.append(request)
        if self.unimplemented:
            context.abort(grpc.StatusCode.UNIMPLEMENTED, "Unimplemented")
        self._validate(request.target, request.user if request.HasField("user") else None, context)
        if len(request.exclude) > PERSIST_EXCLUDE_MAX:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "more than 64 exclude entries")
        for entry in request.exclude:
            if entry.startswith("/") or ".." in entry.split("/") or not entry:
                context.abort(grpc.StatusCode.INVALID_ARGUMENT, "exclude entry rejected")
        self._require_credentials(context)
        self._acquire(context)
        return self._checkpoint_events(request, context)

    def restore(
        self, request: filesystem_pb2.RestoreRequest, context: grpc.ServicerContext
    ) -> Iterator[filesystem_pb2.RestoreEvent]:
        with self.lock:
            self.restore_requests.append(request)
        if self.unimplemented:
            context.abort(grpc.StatusCode.UNIMPLEMENTED, "Unimplemented")
        self._validate(request.source, request.user if request.HasField("user") else None, context)
        self._require_credentials(context)
        stored = self.stored(request.source.bucket, request.source.key_prefix)
        if stored is None:
            context.abort(grpc.StatusCode.NOT_FOUND, "no checkpoint under the prefix")
        self._acquire(context)
        return self._restore_events(stored, context)

    # ------------------------------------------------------------- internals

    def _validate(
        self, location: filesystem_pb2.S3Location, user: Any, context: grpc.ServicerContext
    ) -> None:
        bucket = location.bucket
        if not 3 <= len(bucket) <= 63 or any(char not in BUCKET_CHARS for char in bucket):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "bucket rejected")
        prefix = location.key_prefix
        if (
            not prefix
            or prefix.startswith("/")
            or prefix.endswith("/")
            or "//" in prefix
            or any(char not in KEY_CHARS for char in prefix)
        ):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "key_prefix rejected")
        if user is not None and user.username == "root":
            context.abort(
                grpc.StatusCode.PERMISSION_DENIED, "persistence never archives the root home"
            )

    def _require_credentials(self, context: grpc.ServicerContext) -> None:
        if not self.has_credentials:
            context.abort(grpc.StatusCode.PERMISSION_DENIED, "no execution role credentials")

    def _acquire(self, context: grpc.ServicerContext) -> None:
        with self.lock:
            if self.busy:
                context.abort(grpc.StatusCode.FAILED_PRECONDITION, "persistence busy")
            self.busy = True

    def _release(self) -> None:
        with self.lock:
            self.busy = False

    def _checkpoint_events(
        self, request: filesystem_pb2.CheckpointRequest, context: grpc.ServicerContext
    ) -> Generator[filesystem_pb2.CheckpointEvent, None, None]:
        try:
            yield filesystem_pb2.CheckpointEvent(
                started=filesystem_pb2.CheckpointStarted(
                    files=self.home_files, bytes=self.home_bytes
                )
            )
            for index in range(self.progress_events):
                if self._sleep_or_cancelled(context):
                    return
                yield filesystem_pb2.CheckpointEvent(
                    progress=filesystem_pb2.CheckpointProgress(
                        files_done=index + 1,
                        bytes_read=(index + 1) * 1000,
                        bytes_uploaded=index * 1000,
                    )
                )
            yield filesystem_pb2.CheckpointEvent(keepalive=common_pb2.KeepAlive())
            if self.fail_after_started is not None:
                code, message = self.fail_after_started
                yield filesystem_pb2.CheckpointEvent(
                    error=common_pb2.StreamError(code=code, message=message)
                )
                return
            archive_bytes = self.home_bytes // 3
            sha256 = hashlib.sha256(
                f"{request.target.bucket}/{request.target.key_prefix}".encode()
            ).hexdigest()
            self.objects[f"{request.target.bucket}/{request.target.key_prefix}"] = FakeCheckpoint(
                files=self.home_files,
                bytes=self.home_bytes,
                archive_bytes=archive_bytes,
                sha256=sha256,
                excluded=tuple(request.exclude),
                sandbox_id="mvm-test",
            )
            yield filesystem_pb2.CheckpointEvent(
                done=filesystem_pb2.CheckpointDone(
                    files=self.home_files,
                    bytes_read=self.home_bytes,
                    archive_bytes=archive_bytes,
                    sha256=sha256,
                    skipped=1,
                    duration_ms=1500,
                )
            )
        finally:
            self._release()

    def _restore_events(
        self, stored: FakeCheckpoint, context: grpc.ServicerContext
    ) -> Generator[filesystem_pb2.RestoreEvent, None, None]:
        try:
            yield filesystem_pb2.RestoreEvent(
                started=filesystem_pb2.RestoreStarted(
                    archive_bytes=stored.archive_bytes, files=stored.files
                )
            )
            for index in range(self.progress_events):
                if self._sleep_or_cancelled(context):
                    return
                yield filesystem_pb2.RestoreEvent(
                    progress=filesystem_pb2.RestoreProgress(
                        files_done=index + 1, bytes_downloaded=(index + 1) * 500
                    )
                )
            if self.fail_after_started is not None:
                code, message = self.fail_after_started
                yield filesystem_pb2.RestoreEvent(
                    error=common_pb2.StreamError(code=code, message=message)
                )
                return
            yield filesystem_pb2.RestoreEvent(
                done=filesystem_pb2.RestoreDone(
                    files=stored.files,
                    bytes_written=stored.bytes,
                    archive_bytes=stored.archive_bytes,
                    sha256=stored.sha256,
                    skipped=0,
                    duration_ms=800,
                )
            )
        finally:
            self._release()

    def _sleep_or_cancelled(self, context: grpc.ServicerContext) -> bool:
        if self.slow_events <= 0:
            return False
        deadline = time.monotonic() + self.slow_events
        while time.monotonic() < deadline:
            if not context.is_active():
                return True
            time.sleep(0.01)
        return False
