"""Transferencias por S3 de un `Sandbox` (ADR-010): `upload_url`,
`download_url` y el enrutado de ficheros grandes de `files.write`/`files.read`.

El SDK firma con las credenciales del llamante (`rayito._s3.S3Gateway`) y
`rayd` mueve los bytes VM↔S3. `StartImport`, `StartExport`, `GetTransfer` y
`CancelTransfer` van por el canal de unarios; `WatchTransfer` por el de
streams, reabierto tras un `/suspend`, un reset o un EOF sin estado final
como un `WatchHandle`. La sonda de capacidad (`GetTransfer("")`) se hace una
vez por sandbox. Sólo se registran `transfer_id`, dirección, bytes,
duraciones y resultado, nunca una URL, un bucket, una clave ni una ruta.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypeVar

import grpc

from rayito._filesystem_base import (
    ReadFormat,
    decode_text,
    entry_info_from_proto,
    guarded_messages,
    require_regular_file,
    validate_path,
    validate_watch_timeout,
)
from rayito._models import DownloadLink, EntryInfo, S3Staging, TransferStatus, UploadTicket
from rayito._process_base import deadline_at, remaining_deadline
from rayito._s3 import ObjectFetch, S3Gateway
from rayito._sandbox_base import MAX_FUTILE_RECONNECTS, GateRetry, ReconnectBudget
from rayito._transfer_base import (
    CAPABILITY_PROBE_ID,
    DOWNLOAD_KEY_DIRECTION,
    UPLOAD_KEY_DIRECTION,
    CapabilityProbe,
    CompletedExport,
    ExportResult,
    HashingReader,
    KeyDirection,
    OperationDeadline,
    RoutedWrite,
    StagingObject,
    cancel_transfer_request,
    checksum_mismatch_error,
    completed_export,
    content_disposition,
    delete_lifetime,
    download_filename,
    effective_expires_in,
    expires_at,
    export_lifetime,
    failure_from_state,
    get_transfer_request,
    internal_get_lifetime,
    is_done,
    new_staging_object,
    outdated_image_error,
    part_plan,
    probe_supports_transfers,
    require_staging,
    staging_region,
    start_export_request,
    start_import_request,
    state_from_proto,
    terminal_state,
    transfer_deadline_error,
    transfer_timeout_error,
    translate_transfer_error,
    upload_method,
    upload_ticket_headers,
    validate_max_bytes,
    watch_starter,
    watch_transfer_request,
)
from rayito._transport import rpc_status
from rayito.exceptions import SandboxException, TimeoutException
from rayito.v1 import filesystem_pb2, filesystem_pb2_grpc

if TYPE_CHECKING:
    from rayito.sandbox_sync.main import Sandbox

FILES_STUB = filesystem_pb2_grpc.FilesystemServiceStub

logger = logging.getLogger("rayito.transfer")

TransferInvoke = Callable[[Any, float], Any]


class Transfers:
    """Las transferencias de un `Sandbox`; también es el `TicketOperations`
    de sus `UploadTicket`."""

    def __init__(self, sandbox: Sandbox) -> None:
        self._sandbox = sandbox
        self._probe = CapabilityProbe()
        self._probe_lock = threading.Lock()
        self._gateway: S3Gateway | None = None
        self._gateway_lock = threading.Lock()

    # ------------------------------------------------------------ capability

    def supports_transfers(self) -> bool:
        """Sonda `GetTransfer("")` cacheada: `NOT_FOUND` es un agente M9."""
        with self._probe_lock:
            if self._probe.supported is None:
                self._probe.supported = self._probe_agent()
            return self._probe.supported

    def require_support(self, feature: str) -> None:
        if not self.supports_transfers():
            raise outdated_image_error(feature)

    def _probe_agent(self) -> bool:
        request = get_transfer_request(CAPABILITY_PROBE_ID)
        timeout = self._sandbox._resolve_request_timeout(None)
        try:
            self._sandbox._call_unary(
                lambda: self._sandbox._files.GetTransfer(request, timeout=timeout)
            )
        except grpc.RpcError as exc:
            return probe_supports_transfers(exc)
        return True

    # ------------------------------------------------------------ public ops

    def upload_url(
        self,
        path: str,
        *,
        user: str | None,
        expires_in: int,
        max_bytes: int | None,
        form: bool,
        request_timeout: float | None,
    ) -> UploadTicket:
        staging = require_staging(self._sandbox.transfer, "upload_url")
        path = validate_path(path)
        limit = validate_max_bytes(max_bytes)
        lifetime = effective_expires_in(expires_in, staging)
        self.require_support("upload_url")
        target = self._new_object(staging, UPLOAD_KEY_DIRECTION)
        gateway = self._gateway_for(staging)
        signed_at = datetime.now(UTC)
        url, fields = gateway.presign_user_upload(
            target, form=form, max_bytes=limit, lifetime=lifetime
        )
        urls = gateway.presign_import(
            target, get_lifetime=lifetime, delete_lifetime=delete_lifetime(lifetime)
        )
        expiry = expires_at(signed_at, lifetime)
        request = start_import_request(
            path=path,
            user=user,
            mode=None,
            target=target,
            get_url=urls.get_url,
            delete_url=urls.delete_url,
            wait_for_object=True,
            expires_at=expiry,
            max_bytes=limit,
        )
        transfer_id = self._start(
            lambda stub, timeout: stub.StartImport(request, timeout=timeout),
            "upload_url",
            self._sandbox._resolve_request_timeout(request_timeout),
        )
        self._sandbox._logger_or(logger).debug(
            "transferencia %s armada (direction=import, ticket)", transfer_id
        )
        return UploadTicket(
            url,
            method=upload_method(form),
            headers=upload_ticket_headers(form),
            fields=fields,
            path=path,
            expires_at=expiry,
            transfer_id=transfer_id,
            operations=self,
        )

    def download_url(
        self,
        path: str,
        *,
        user: str | None,
        expires_in: int,
        filename: str | None,
        request_timeout: float | None,
    ) -> DownloadLink:
        staging = require_staging(self._sandbox.transfer, "download_url")
        path = validate_path(path)
        lifetime = effective_expires_in(expires_in, staging)
        name = download_filename(path, filename)
        self.require_support("download_url")
        entry = require_regular_file(
            self._sandbox.files.get_info(path, user=user, request_timeout=request_timeout)
        )
        deadline = OperationDeadline(time.monotonic(), request_timeout)
        result = self._export(staging, path, entry.size, user, deadline, None, "download_url")
        signed_at = datetime.now(UTC)
        url = self._gateway_for(staging).presign_download(
            result.target, lifetime, content_disposition(name)
        )
        return DownloadLink(
            url,
            path=path,
            expires_at=expires_at(signed_at, lifetime),
            transfer_id=result.transfer_id,
            size=result.size,
            sha256=result.sha256,
        )

    def write_routed(
        self,
        routed: RoutedWrite,
        *,
        user: str | None,
        metadata: Mapping[str, str],
        request_timeout: float | None,
    ) -> EntryInfo:
        """Sube con las credenciales del llamante (sha256 al vuelo) y pide a
        `rayd` una importación sin espera con ese sha256 y ese tamaño."""
        staging = require_staging(self._sandbox.transfer, "files.write")
        deadline = OperationDeadline(time.monotonic(), request_timeout)
        target = self._new_object(staging, UPLOAD_KEY_DIRECTION)
        gateway = self._gateway_for(staging)
        reader = HashingReader(routed.source)
        started = time.monotonic()
        try:
            run_bounded(
                lambda: gateway.upload(reader, target),
                deadline.budget(routed.size, started),
                reader.abort,
            )
        except TimeoutException:
            gateway.delete_quietly(target)
            raise
        try:
            transfer_id = self._start_large_import(
                staging, gateway, target, routed, reader, user, metadata, deadline
            )
        except BaseException:
            gateway.delete_quietly(target)
            raise
        state = self._follow(transfer_id, self._deadline_at(deadline, reader.size))
        if not is_done(state):
            raise failure_from_state(state, "import")
        self._sandbox._logger_or(logger).debug(
            "transferencia %s terminada (direction=import, bytes=%d, duration_ms=%d)",
            transfer_id,
            reader.size,
            int((time.monotonic() - started) * 1000),
        )
        return entry_info_from_proto(state.entry)

    def read_routed(
        self,
        path: str,
        entry: EntryInfo,
        *,
        read_format: ReadFormat,
        user: str | None,
        request_timeout: float | None,
        idle: float | None,
    ) -> str | bytes | Iterator[bytes]:
        """Exporta a S3 y descarga con las credenciales del llamante,
        verificando el sha256 de la exportación y borrando el objeto."""
        staging = require_staging(self._sandbox.transfer, "files.read")
        deadline = OperationDeadline(time.monotonic(), request_timeout)
        result = self._export(staging, path, entry.size, user, deadline, idle, "files.read")
        gateway = self._gateway_for(staging)
        if read_format == "stream":
            return self._stream_object(gateway, result, idle)
        fetch = ObjectFetch(gateway, result.target, idle)
        try:
            data = run_bounded(
                fetch.run, deadline.remaining(entry.size, time.monotonic()), fetch.cancel
            )
        finally:
            gateway.delete_quietly(result.target)
        if hashlib.sha256(data).hexdigest() != result.sha256:
            raise checksum_mismatch_error()
        return data if read_format == "bytes" else decode_text(data)

    # --------------------------------------------------- TicketOperations

    def wait_transfer(self, transfer_id: str, timeout: float | None) -> EntryInfo:
        state = self._follow(
            transfer_id, deadline_at(validate_watch_timeout(timeout), time.monotonic)
        )
        if not is_done(state):
            raise failure_from_state(state, "import")
        return entry_info_from_proto(state.entry)

    def transfer_status(self, transfer_id: str) -> TransferStatus:
        request = get_transfer_request(transfer_id)
        state = self._unary(
            lambda stub, timeout: stub.GetTransfer(request, timeout=timeout),
            "status",
            self._sandbox._resolve_request_timeout(None),
            filesystem=False,
        )
        return state_from_proto(state)

    def cancel_transfer(self, transfer_id: str) -> None:
        request = cancel_transfer_request(transfer_id)
        self._unary(
            lambda stub, timeout: stub.CancelTransfer(request, timeout=timeout),
            "cancel",
            self._sandbox._resolve_request_timeout(None),
            filesystem=False,
        )
        self._sandbox._logger_or(logger).debug("transferencia %s cancelada", transfer_id)

    # ------------------------------------------------------------- internals

    def _start_large_import(
        self,
        staging: S3Staging,
        gateway: S3Gateway,
        target: StagingObject,
        routed: RoutedWrite,
        reader: HashingReader,
        user: str | None,
        metadata: Mapping[str, str],
        deadline: OperationDeadline,
    ) -> str:
        get_lifetime = internal_get_lifetime(staging)
        signed_at = datetime.now(UTC)
        urls = gateway.presign_import(
            target, get_lifetime=get_lifetime, delete_lifetime=export_lifetime(reader.size)
        )
        request = start_import_request(
            path=routed.path,
            user=user,
            mode=routed.mode,
            target=target,
            get_url=urls.get_url,
            delete_url=urls.delete_url,
            wait_for_object=False,
            expires_at=expires_at(signed_at, get_lifetime),
            max_bytes=reader.size,
            expected_sha256=reader.sha256,
            metadata=metadata,
        )
        return self._start(
            lambda stub, timeout: stub.StartImport(request, timeout=timeout),
            "files.write",
            deadline.remaining(reader.size, time.monotonic()),
        )

    def _export(
        self,
        staging: S3Staging,
        path: str,
        size: int,
        user: str | None,
        deadline: OperationDeadline,
        idle: float | None,
        feature: str,
    ) -> ExportResult:
        """Un PUT por debajo de `multipart_threshold_bytes`; si no, el SDK
        abre la subida multiparte, firma una URL por parte y la completa (o
        la aborta ante cualquier fallo): `rayd` nunca ve una credencial."""
        target = self._new_object(staging, DOWNLOAD_KEY_DIRECTION)
        gateway = self._gateway_for(staging)
        plan = part_plan(size, staging)
        lifetime = export_lifetime(size)
        expiry = expires_at(datetime.now(UTC), lifetime)
        if plan is None:
            request = start_export_request(
                path=path,
                user=user,
                target=target,
                expires_at=expiry,
                put_url=gateway.presign_put(target, lifetime),
            )
            return self._run_export(request, target, size, deadline, idle, feature).export
        upload_id = gateway.create_multipart_upload(target)
        try:
            request = start_export_request(
                path=path,
                user=user,
                target=target,
                expires_at=expiry,
                part_urls=gateway.presign_upload_parts(
                    target, upload_id, plan.part_count, lifetime
                ),
                part_size=plan.part_size,
            )
            result = self._run_export(request, target, size, deadline, idle, feature)
            gateway.complete_multipart_upload(target, upload_id, list(result.part_etags))
        except BaseException:
            gateway.abort_multipart_upload_quietly(target, upload_id)
            raise
        return result.export

    def _run_export(
        self,
        request: filesystem_pb2.StartExportRequest,
        target: StagingObject,
        size: int,
        deadline: OperationDeadline,
        idle: float | None,
        feature: str,
    ) -> CompletedExport:
        started = time.monotonic()
        transfer_id = self._start(
            lambda stub, timeout: stub.StartExport(request, timeout=timeout),
            feature,
            deadline.remaining(size, started),
        )
        state = self._follow(transfer_id, self._deadline_at(deadline, size), idle)
        if not is_done(state):
            raise failure_from_state(state, "export")
        self._sandbox._logger_or(logger).debug(
            "transferencia %s terminada (direction=export, bytes=%d, duration_ms=%d)",
            transfer_id,
            int(state.bytes_done),
            int((time.monotonic() - started) * 1000),
        )
        return completed_export(target, transfer_id, state)

    def _stream_object(
        self, gateway: S3Gateway, result: ExportResult, idle: float | None
    ) -> Iterator[bytes]:
        chunks = gateway.open_chunks(result.target)
        digest = hashlib.sha256()
        try:
            for chunk in guarded_messages(chunks, idle, chunks.close):
                digest.update(chunk)
                yield chunk
        finally:
            chunks.close()
            gateway.delete_quietly(result.target)
        if digest.hexdigest() != result.sha256:
            raise checksum_mismatch_error()

    def _follow(
        self, transfer_id: str, at: float | None, idle: float | None = None
    ) -> filesystem_pb2.TransferState:
        """`WatchTransfer` hasta el estado final. Un corte reconectable espera
        al agente y reabre el stream (el primer mensaje es siempre la foto
        actual, así que no se pierde nada); un EOF sin estado final se
        reabre como mucho `MAX_FUTILE_RECONNECTS` veces."""
        request = watch_transfer_request(transfer_id)
        generation = self._sandbox.resume_generation
        budget = ReconnectBudget()
        empty_ends = 0
        while True:
            call, first = self._open_watch(request, transfer_id, at)
            try:
                state = self._terminal_state(call, first, idle)
            except grpc.RpcError as exc:
                generation = self._after_cut(exc, transfer_id, generation, budget)
                continue
            finally:
                call.cancel()
            if state is not None:
                return state
            empty_ends += 1
            if empty_ends > MAX_FUTILE_RECONNECTS:
                raise SandboxException(
                    f"WatchTransfer de {transfer_id} terminó sin estado final varias veces"
                )

    def _terminal_state(
        self, call: Any, first: Any, idle: float | None
    ) -> filesystem_pb2.TransferState | None:
        state = terminal_state(first)
        if state is not None:
            return state
        for event in guarded_messages(iter(call), idle, call.cancel):
            state = terminal_state(event)
            if state is not None:
                return state
        return None

    def _after_cut(
        self, exc: grpc.RpcError, transfer_id: str, generation: int, budget: ReconnectBudget
    ) -> int:
        if rpc_status(exc) is grpc.StatusCode.DEADLINE_EXCEEDED:
            raise transfer_deadline_error(transfer_id) from exc
        if not self._sandbox._is_reconnectable(exc):
            raise self._sandbox._stream_failure(exc) from exc
        outcome = self._sandbox._reconnect(
            exc, generation, wake=self._sandbox._foreground_stream_wakes()
        )
        if not outcome.resumed:
            raise self._sandbox._reconnect_error(outcome, exc) from exc
        if not budget.allows(outcome):
            raise self._sandbox._stream_failure(exc) from exc
        return outcome.resume_generation

    def _open_watch(
        self, request: filesystem_pb2.WatchTransferRequest, transfer_id: str, at: float | None
    ) -> tuple[Any, Any]:
        retry = GateRetry(self._sandbox._reconnect_timeout)
        while True:
            try:
                return self._sandbox._open_stream(
                    watch_starter(request, self._remaining(at, transfer_id)),
                    service=FILES_STUB,
                    stream=True,
                    translate=lambda exc: self._watch_open_error(exc, transfer_id),
                )
            except SandboxException as exc:
                delay = retry.retry_delay(exc)
                if delay is None:
                    raise
                time.sleep(delay)

    @staticmethod
    def _watch_open_error(exc: grpc.RpcError, transfer_id: str) -> Exception:
        if rpc_status(exc) is grpc.StatusCode.DEADLINE_EXCEEDED:
            return transfer_deadline_error(transfer_id)
        return translate_transfer_error(exc, "WatchTransfer")

    @staticmethod
    def _remaining(at: float | None, transfer_id: str) -> float | None:
        remaining = remaining_deadline(at, time.monotonic)
        if remaining is not None and remaining <= 0.0:
            raise transfer_deadline_error(transfer_id)
        return remaining

    @staticmethod
    def _deadline_at(deadline: OperationDeadline, size: int) -> float:
        now = time.monotonic()
        return now + deadline.remaining(size, now)

    def _start(self, invoke: TransferInvoke, feature: str, timeout: float) -> str:
        response = self._unary(invoke, feature, timeout, filesystem=True)
        return str(response.transfer_id)

    def _unary(
        self, invoke: TransferInvoke, feature: str, timeout: float, *, filesystem: bool
    ) -> Any:
        try:
            return self._sandbox._call_unary(lambda: invoke(self._sandbox._files, timeout))
        except grpc.RpcError as exc:
            raise translate_transfer_error(exc, feature, filesystem=filesystem) from exc

    def _new_object(self, staging: S3Staging, direction: KeyDirection) -> StagingObject:
        return new_staging_object(
            staging, self._sandbox.sandbox_id, self._sandbox.region, direction
        )

    def _gateway_for(self, staging: S3Staging) -> S3Gateway:
        with self._gateway_lock:
            if self._gateway is None:
                self._gateway = S3Gateway.from_session(
                    self._sandbox._session, staging_region(staging, self._sandbox.region)
                )
            return self._gateway


T = TypeVar("T")


def run_bounded(invoke: Callable[[], T], seconds: float | None, cancel: Callable[[], object]) -> T:
    """La pata S3 de una transferencia enrutada dentro de su plazo
    (`request_timeout` o `60 s + 1 s por MB`): corre en un hilo daemon y, si
    no termina a tiempo, `cancel` la corta (cierra el cuerpo o hace fallar
    la siguiente lectura de la subida) y se levanta `TimeoutException` sin
    esperar a los reintentos de botocore."""
    outcome: list[T] = []
    failure: list[BaseException] = []

    def work() -> None:
        try:
            outcome.append(invoke())
        except BaseException as exc:
            failure.append(exc)

    worker = threading.Thread(target=work, name="rayito-s3", daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        cancel()
        raise transfer_timeout_error()
    if failure:
        raise failure[0]
    return outcome[0]
