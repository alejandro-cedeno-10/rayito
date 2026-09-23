"""`FilesystemService` falso con las cinco RPCs de transferencia de M9
(`openspec/changes/m9-file-transfer/design.md` D8-D13), sobre el árbol en
memoria de `FakeFilesystemService` y un `FakeS3` compartido con el SDK.

Como `rayd`: `GetTransfer("")` responde `NOT_FOUND` (la sonda de
capacidad); una importación armada sondea el GET prefirmado hasta que el
objeto existe o vence `expires_at_unix_ms`, comprueba `max_bytes` y
`expected_sha256`, escribe el fichero con sus metadatos y borra el objeto
con el DELETE prefirmado; una exportación hace una foto del fichero al
aceptarla y la sube con el PUT o con las partes. `WatchTransfer` emite la
foto actual, un `keepalive` y el estado final, y un `/suspend` lo cierra con
`UNAVAILABLE suspending`. Nunca registra una URL.
"""

from __future__ import annotations

import hashlib
import math
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field

import grpc

from rayito.v1 import common_pb2, filesystem_pb2

from .fake_filesystem import (
    DEFAULT_FILE_MODE,
    FakeDirectory,
    FakeFile,
    FakeFilesystemService,
    FakeStatus,
    entry_info,
    invalid,
    normalize,
)
from .fake_s3 import FakeS3

POLL_SECONDS = 0.02
WAIT_SLICE_SECONDS = 0.05
TERMINAL_PHASES = frozenset(
    {
        filesystem_pb2.TRANSFER_PHASE_DONE,
        filesystem_pb2.TRANSFER_PHASE_FAILED,
        filesystem_pb2.TRANSFER_PHASE_CANCELLED,
    }
)


def now_unix_ms() -> int:
    return int(time.time() * 1000)


def transfer_error(code: str, reason: str) -> common_pb2.StreamError:
    return common_pb2.StreamError(code=code, message=f"{reason}: fallo simulado por el fake")


@dataclass
class TransferRecord:
    state: filesystem_pb2.TransferState
    import_request: filesystem_pb2.StartImportRequest | None = None
    export_request: filesystem_pb2.StartExportRequest | None = None
    snapshot: bytes = b""


@dataclass
class FakeTransferFilesystemService(FakeFilesystemService):
    """`FakeFilesystemService` más transferencias (ver módulo).

    `export_failure` hace que la próxima exportación termine `FAILED` con ese
    `(code, reason)`; `import_failure`, lo mismo para la próxima importación
    en cuanto ve el objeto. `unimplemented` responde `UNIMPLEMENTED` a las
    cinco RPCs, como un `rayd` anterior a M9."""

    s3: FakeS3 = field(default_factory=FakeS3)
    transfers: dict[str, TransferRecord] = field(default_factory=dict)
    import_requests: list[filesystem_pb2.StartImportRequest] = field(default_factory=list)
    export_requests: list[filesystem_pb2.StartExportRequest] = field(default_factory=list)
    transfer_rpcs: list[str] = field(default_factory=list)
    export_failure: tuple[str, str] | None = None
    import_failure: tuple[str, str] | None = None
    unimplemented: bool = False
    watch_cuts: int = 0
    changed: threading.Condition = field(default_factory=threading.Condition)

    # ------------------------------------------------------------------ RPCs

    def StartImport(
        self, request: filesystem_pb2.StartImportRequest, context: grpc.ServicerContext
    ) -> filesystem_pb2.StartTransferResponse:
        self._enter_transfer("StartImport", context)
        self._gate_phase(context)
        try:
            self._identity(request)
            path = normalize(request.path)
            self._check_import(request, path)
        except FakeStatus as status:
            context.abort(status.code, status.message)
        record = self._admit(filesystem_pb2.TRANSFER_DIRECTION_IMPORT)
        record.import_request = request
        with self.lock:
            self.import_requests.append(request)
        threading.Thread(target=self._run_import, args=(record, path), daemon=True).start()
        return filesystem_pb2.StartTransferResponse(transfer_id=record.state.transfer_id)

    def StartExport(
        self, request: filesystem_pb2.StartExportRequest, context: grpc.ServicerContext
    ) -> filesystem_pb2.StartTransferResponse:
        self._enter_transfer("StartExport", context)
        self._gate_phase(context)
        try:
            self._identity(request)
            path = normalize(request.path)
            node = self._existing(self._target(request.path))
            if not isinstance(node, FakeFile):
                raise invalid("export source is not a regular file")
            self._check_export(request, len(node.data))
        except FakeStatus as status:
            context.abort(status.code, status.message)
        record = self._admit(filesystem_pb2.TRANSFER_DIRECTION_EXPORT)
        record.export_request = request
        record.snapshot = node.data
        record.state.bytes_total = len(node.data)
        record.state.entry.CopyFrom(entry_info(path, node))
        with self.lock:
            self.export_requests.append(request)
        threading.Thread(target=self._run_export, args=(record,), daemon=True).start()
        return filesystem_pb2.StartTransferResponse(transfer_id=record.state.transfer_id)

    def GetTransfer(
        self, request: filesystem_pb2.GetTransferRequest, context: grpc.ServicerContext
    ) -> filesystem_pb2.TransferState:
        self._enter_transfer("GetTransfer", context)
        record = self._record(request.transfer_id, context)
        with self.changed:
            return self._snapshot(record)

    def WatchTransfer(
        self, request: filesystem_pb2.WatchTransferRequest, context: grpc.ServicerContext
    ) -> Iterator[filesystem_pb2.TransferEvent]:
        self._enter_transfer("WatchTransfer", context)
        self._gate_phase(context)
        record = self._record(request.transfer_id, context)
        return self._watch(record, context)

    def CancelTransfer(
        self, request: filesystem_pb2.CancelTransferRequest, context: grpc.ServicerContext
    ) -> filesystem_pb2.CancelTransferResponse:
        self._enter_transfer("CancelTransfer", context)
        record = self._record(request.transfer_id, context)
        with self.changed:
            if record.state.phase not in TERMINAL_PHASES:
                record.state.phase = filesystem_pb2.TRANSFER_PHASE_CANCELLED
                record.state.error.CopyFrom(transfer_error("cancelled", "cancelled"))
                self.changed.notify_all()
        return filesystem_pb2.CancelTransferResponse()

    # --------------------------------------------------------- test controls

    def suspend(self) -> None:
        super().suspend()
        with self.changed:
            self.changed.notify_all()

    def resume(self) -> None:
        super().resume()
        with self.changed:
            self.changed.notify_all()

    def state_of(self, transfer_id: str) -> filesystem_pb2.TransferState:
        with self.changed:
            return self._snapshot(self.transfers[transfer_id])

    def rpc_names(self) -> list[str]:
        with self.lock:
            return list(self.transfer_rpcs)

    # -------------------------------------------------------------- internals

    def _enter_transfer(self, rpc: str, context: grpc.ServicerContext) -> None:
        self._enter(rpc, context)
        with self.lock:
            self.transfer_rpcs.append(rpc)
        if self.unimplemented:
            context.abort(grpc.StatusCode.UNIMPLEMENTED, "Method not found!")

    def _record(self, transfer_id: str, context: grpc.ServicerContext) -> TransferRecord:
        with self.changed:
            record = self.transfers.get(transfer_id) if transfer_id else None
        if record is None:
            context.abort(grpc.StatusCode.NOT_FOUND, "transferencia desconocida")
        return record

    def _admit(self, direction: filesystem_pb2.TransferDirection) -> TransferRecord:
        transfer_id = uuid.uuid4().hex
        state = filesystem_pb2.TransferState(
            transfer_id=transfer_id,
            direction=direction,
            phase=filesystem_pb2.TRANSFER_PHASE_WAITING,
        )
        record = TransferRecord(state=state)
        with self.changed:
            self.transfers[transfer_id] = record
        return record

    def _check_import(self, request: filesystem_pb2.StartImportRequest, path: str) -> None:
        if not request.get.url or not request.HasField("delete"):
            raise invalid("import needs get and delete")
        if "/up/" not in request.object.key or request.expires_at_unix_ms <= now_unix_ms():
            raise invalid("import request refused by policy")
        if isinstance(self.tree.nodes.get(self._target(path)), FakeDirectory):
            raise invalid("destination is a directory")

    def _check_export(self, request: filesystem_pb2.StartExportRequest, size: int) -> None:
        if "/down/" not in request.object.key:
            raise invalid("export request refused by policy")
        if request.WhichOneof("target") == "multipart":
            multipart = request.multipart
            expected = max(1, math.ceil(size / multipart.part_size))
            if len(multipart.parts) != expected:
                raise FakeStatus(grpc.StatusCode.FAILED_PRECONDITION, "file_changed")

    def _run_import(self, record: TransferRecord, path: str) -> None:
        request = record.import_request
        assert request is not None
        data = self._poll_object(record, request)
        if data is None:
            return
        failure = self._import_failure(request, data)
        if failure is not None:
            self.s3.delete_url(request.delete.url)
            self._fail(record, *failure)
            return
        entry = self._commit_import(request, path, data)
        self.s3.delete_url(request.delete.url)
        with self.changed:
            if record.state.phase in TERMINAL_PHASES:
                return
            record.state.phase = filesystem_pb2.TRANSFER_PHASE_DONE
            record.state.bytes_done = len(data)
            record.state.bytes_total = len(data)
            record.state.sha256 = hashlib.sha256(data).hexdigest()
            record.state.entry.CopyFrom(entry)
            self.changed.notify_all()

    def _poll_object(
        self, record: TransferRecord, request: filesystem_pb2.StartImportRequest
    ) -> bytes | None:
        while True:
            with self.changed:
                if record.state.phase in TERMINAL_PHASES:
                    return None
            if now_unix_ms() >= request.expires_at_unix_ms:
                self._fail(record, "deadline_exceeded", "expired")
                return None
            if self.phase is None:
                status, data = self.s3.get_url(request.get.url)
                with self.changed:
                    record.state.probes += 1
                if status == 200:
                    return data
                if not request.wait_for_object:
                    self._fail(record, "not_found", "no_object")
                    return None
            time.sleep(POLL_SECONDS)

    def _import_failure(
        self, request: filesystem_pb2.StartImportRequest, data: bytes
    ) -> tuple[str, str] | None:
        if self.import_failure is not None:
            failure, self.import_failure = self.import_failure, None
            return failure
        if request.max_bytes and len(data) > request.max_bytes:
            return "invalid_argument", "too_large"
        expected = request.expected_sha256
        if expected and hashlib.sha256(data).hexdigest() != expected:
            return "failed_precondition", "checksum_mismatch"
        return None

    def _commit_import(
        self, request: filesystem_pb2.StartImportRequest, path: str, data: bytes
    ) -> common_pb2.EntryInfo:
        mode = int(request.mode) if request.HasField("mode") else DEFAULT_FILE_MODE
        metadata = {str(key).lower(): str(value) for key, value in request.metadata.items()}
        node = FakeFile(data, mode, metadata)
        with self.lock:
            canonical = self._target(path)
            self.tree.ensure_parents(canonical)
            self.tree.nodes[canonical] = node
        return entry_info(path, node)

    def _run_export(self, record: TransferRecord) -> None:
        request = record.export_request
        assert request is not None
        if self.export_failure is not None:
            failure, self.export_failure = self.export_failure, None
            self._fail(record, *failure)
            return
        data = record.snapshot
        etags = self._upload(request, data)
        if etags is None:
            self._fail(record, "permission_denied", "access_denied")
            return
        with self.changed:
            if record.state.phase in TERMINAL_PHASES:
                return
            record.state.phase = filesystem_pb2.TRANSFER_PHASE_DONE
            record.state.bytes_done = len(data)
            record.state.sha256 = hashlib.sha256(data).hexdigest()
            record.state.part_etags.extend(etags)
            self.changed.notify_all()

    def _upload(self, request: filesystem_pb2.StartExportRequest, data: bytes) -> list[str] | None:
        if request.WhichOneof("target") == "put":
            status, _ = self.s3.put_url(request.put.url, data)
            return [] if status == 200 else None
        part_size = int(request.multipart.part_size)
        etags: list[str] = []
        for index, part in enumerate(request.multipart.parts):
            chunk = data[index * part_size : (index + 1) * part_size]
            status, etag = self.s3.put_url(part.url, chunk)
            if status != 200:
                return None
            etags.append(etag)
        return etags

    def _fail(self, record: TransferRecord, code: str, reason: str) -> None:
        with self.changed:
            if record.state.phase in TERMINAL_PHASES:
                return
            record.state.phase = filesystem_pb2.TRANSFER_PHASE_FAILED
            record.state.error.CopyFrom(transfer_error(code, reason))
            self.changed.notify_all()

    @staticmethod
    def _snapshot(record: TransferRecord) -> filesystem_pb2.TransferState:
        state = filesystem_pb2.TransferState()
        state.CopyFrom(record.state)
        return state

    def _watch(
        self, record: TransferRecord, context: grpc.ServicerContext
    ) -> Iterator[filesystem_pb2.TransferEvent]:
        with self.changed:
            first = self._snapshot(record)
        yield filesystem_pb2.TransferEvent(state=first)
        if first.phase in TERMINAL_PHASES:
            return
        yield filesystem_pb2.TransferEvent(keepalive=common_pb2.KeepAlive())
        while context.is_active():
            with self.changed:
                if self.phase is not None:
                    self.watch_cuts += 1
                    context.abort(grpc.StatusCode.UNAVAILABLE, self.phase)
                if record.state.phase in TERMINAL_PHASES:
                    final = self._snapshot(record)
                    break
                self.changed.wait(WAIT_SLICE_SECONDS)
        else:
            return
        yield filesystem_pb2.TransferEvent(state=final)
