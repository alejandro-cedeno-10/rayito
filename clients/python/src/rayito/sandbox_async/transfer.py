"""Transferencias por S3 de un `AsyncSandbox` (ADR-010): la misma política
que `sandbox_sync.transfer` sobre `grpc.aio`. Las llamadas a boto3 (firmas,
subidas y descargas con las credenciales del llamante) son bloqueantes y
corren en `asyncio.to_thread`. Sólo se registran `transfer_id`, dirección,
bytes, duraciones y resultado, nunca una URL, un bucket, una clave ni una
ruta.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypeVar

import grpc

from rayito._filesystem_base import (
    ReadFormat,
    decode_text,
    entry_info_from_proto,
    idle_timeout_error,
    read_guarded,
    require_regular_file,
    validate_path,
    validate_watch_timeout,
)
from rayito._models import (
    AsyncUploadTicket,
    DownloadLink,
    EntryInfo,
    S3Staging,
    TransferStatus,
)
from rayito._process_base import STREAM_EOF, deadline_at, remaining_deadline
from rayito._s3 import ObjectChunks, ObjectFetch, S3Gateway
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
    from rayito.sandbox_async.main import AsyncSandbox

FILES_STUB = filesystem_pb2_grpc.FilesystemServiceStub

logger = logging.getLogger("rayito.transfer")

AsyncTransferInvoke = Callable[[Any, float], Awaitable[Any]]


class AsyncTransfers:
    """Las transferencias de un `AsyncSandbox`; también es el
    `AsyncTicketOperations` de sus `AsyncUploadTicket`."""

    def __init__(self, sandbox: AsyncSandbox) -> None:
        self._sandbox = sandbox
        self._probe = CapabilityProbe()
        self._probe_lock = asyncio.Lock()
        self._gateway: S3Gateway | None = None
        self._gateway_lock = asyncio.Lock()

    # ------------------------------------------------------------ capability

    async def supports_transfers(self) -> bool:
        """Sonda `GetTransfer("")` cacheada: `NOT_FOUND` es un agente M9."""
        async with self._probe_lock:
            if self._probe.supported is None:
                self._probe.supported = await self._probe_agent()
            return self._probe.supported

    async def require_support(self, feature: str) -> None:
        if not await self.supports_transfers():
            raise outdated_image_error(feature)

    async def _probe_agent(self) -> bool:
        request = get_transfer_request(CAPABILITY_PROBE_ID)
        timeout = self._sandbox._resolve_request_timeout(None)
        try:
            await self._sandbox._call_unary(
                lambda: self._sandbox._files.GetTransfer(request, timeout=timeout)
            )
        except grpc.RpcError as exc:
            return probe_supports_transfers(exc)
        return True

    # ------------------------------------------------------------ public ops

    async def upload_url(
        self,
        path: str,
        *,
        user: str | None,
        expires_in: int,
        max_bytes: int | None,
        form: bool,
        request_timeout: float | None,
    ) -> AsyncUploadTicket:
        staging = require_staging(self._sandbox.transfer, "upload_url")
        path = validate_path(path)
        limit = validate_max_bytes(max_bytes)
        lifetime = effective_expires_in(expires_in, staging)
        await self.require_support("upload_url")
        target = self._new_object(staging, UPLOAD_KEY_DIRECTION)
        gateway = await self._gateway_for(staging)
        signed_at = datetime.now(UTC)
        url, fields = await asyncio.to_thread(
            lambda: gateway.presign_user_upload(
                target, form=form, max_bytes=limit, lifetime=lifetime
            )
        )
        urls = await asyncio.to_thread(
            lambda: gateway.presign_import(
                target, get_lifetime=lifetime, delete_lifetime=delete_lifetime(lifetime)
            )
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
        transfer_id = await self._start(
            lambda stub, timeout: stub.StartImport(request, timeout=timeout),
            "upload_url",
            self._sandbox._resolve_request_timeout(request_timeout),
        )
        self._sandbox._logger_or(logger).debug(
            "transferencia %s armada (direction=import, ticket)", transfer_id
        )
        return AsyncUploadTicket(
            url,
            method=upload_method(form),
            headers=upload_ticket_headers(form),
            fields=fields,
            path=path,
            expires_at=expiry,
            transfer_id=transfer_id,
            operations=self,
        )

    async def download_url(
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
        await self.require_support("download_url")
        entry = require_regular_file(
            await self._sandbox.files.get_info(path, user=user, request_timeout=request_timeout)
        )
        deadline = OperationDeadline(time.monotonic(), request_timeout)
        result = await self._export(staging, path, entry.size, user, deadline, None, "download_url")
        signed_at = datetime.now(UTC)
        gateway = await self._gateway_for(staging)
        url = await asyncio.to_thread(
            gateway.presign_download, result.target, lifetime, content_disposition(name)
        )
        return DownloadLink(
            url,
            path=path,
            expires_at=expires_at(signed_at, lifetime),
            transfer_id=result.transfer_id,
            size=result.size,
            sha256=result.sha256,
        )

    async def write_routed(
        self,
        routed: RoutedWrite,
        *,
        user: str | None,
        metadata: Mapping[str, str],
        request_timeout: float | None,
    ) -> EntryInfo:
        """Misma política que `Transfers.write_routed`: la subida (y el
        sha256 al vuelo) corre en un hilo."""
        staging = require_staging(self._sandbox.transfer, "files.write")
        deadline = OperationDeadline(time.monotonic(), request_timeout)
        target = self._new_object(staging, UPLOAD_KEY_DIRECTION)
        gateway = await self._gateway_for(staging)
        reader = HashingReader(routed.source)
        started = time.monotonic()
        try:
            await run_bounded(
                lambda: gateway.upload(reader, target),
                deadline.budget(routed.size, started),
                reader.abort,
            )
        except TimeoutException:
            await asyncio.to_thread(gateway.delete_quietly, target)
            raise
        try:
            transfer_id = await self._start_large_import(
                staging, gateway, target, routed, reader, user, metadata, deadline
            )
        except BaseException:
            await asyncio.to_thread(gateway.delete_quietly, target)
            raise
        state = await self._follow(transfer_id, self._deadline_at(deadline, reader.size))
        if not is_done(state):
            raise failure_from_state(state, "import")
        self._sandbox._logger_or(logger).debug(
            "transferencia %s terminada (direction=import, bytes=%d, duration_ms=%d)",
            transfer_id,
            reader.size,
            int((time.monotonic() - started) * 1000),
        )
        return entry_info_from_proto(state.entry)

    async def read_routed(
        self,
        path: str,
        entry: EntryInfo,
        *,
        read_format: ReadFormat,
        user: str | None,
        request_timeout: float | None,
        idle: float | None,
    ) -> str | bytes | AsyncIterator[bytes]:
        """Misma política que `Transfers.read_routed`; `stream` devuelve un
        `AsyncIterator[bytes]` sobre el cuerpo de `get_object`."""
        staging = require_staging(self._sandbox.transfer, "files.read")
        deadline = OperationDeadline(time.monotonic(), request_timeout)
        result = await self._export(staging, path, entry.size, user, deadline, idle, "files.read")
        gateway = await self._gateway_for(staging)
        if read_format == "stream":
            return self._stream_object(gateway, result, idle)
        fetch = ObjectFetch(gateway, result.target, idle)
        try:
            data = await run_bounded(
                fetch.run, deadline.remaining(entry.size, time.monotonic()), fetch.cancel
            )
        finally:
            await asyncio.to_thread(gateway.delete_quietly, result.target)
        if hashlib.sha256(data).hexdigest() != result.sha256:
            raise checksum_mismatch_error()
        return data if read_format == "bytes" else decode_text(data)

    # ----------------------------------------------- AsyncTicketOperations

    async def wait_transfer(self, transfer_id: str, timeout: float | None) -> EntryInfo:
        state = await self._follow(
            transfer_id, deadline_at(validate_watch_timeout(timeout), time.monotonic)
        )
        if not is_done(state):
            raise failure_from_state(state, "import")
        return entry_info_from_proto(state.entry)

    async def transfer_status(self, transfer_id: str) -> TransferStatus:
        request = get_transfer_request(transfer_id)
        state = await self._unary(
            lambda stub, timeout: stub.GetTransfer(request, timeout=timeout),
            "status",
            self._sandbox._resolve_request_timeout(None),
            filesystem=False,
        )
        return state_from_proto(state)

    async def cancel_transfer(self, transfer_id: str) -> None:
        request = cancel_transfer_request(transfer_id)
        await self._unary(
            lambda stub, timeout: stub.CancelTransfer(request, timeout=timeout),
            "cancel",
            self._sandbox._resolve_request_timeout(None),
            filesystem=False,
        )
        self._sandbox._logger_or(logger).debug("transferencia %s cancelada", transfer_id)

    # ------------------------------------------------------------- internals

    async def _start_large_import(
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
        urls = await asyncio.to_thread(
            lambda: gateway.presign_import(
                target, get_lifetime=get_lifetime, delete_lifetime=export_lifetime(reader.size)
            )
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
        return await self._start(
            lambda stub, timeout: stub.StartImport(request, timeout=timeout),
            "files.write",
            deadline.remaining(reader.size, time.monotonic()),
        )

    async def _export(
        self,
        staging: S3Staging,
        path: str,
        size: int,
        user: str | None,
        deadline: OperationDeadline,
        idle: float | None,
        feature: str,
    ) -> ExportResult:
        """Misma política que `Transfers._export`: un PUT o una subida
        multiparte que el SDK abre, completa o aborta."""
        target = self._new_object(staging, DOWNLOAD_KEY_DIRECTION)
        gateway = await self._gateway_for(staging)
        plan = part_plan(size, staging)
        lifetime = export_lifetime(size)
        expiry = expires_at(datetime.now(UTC), lifetime)
        if plan is None:
            put_url = await asyncio.to_thread(gateway.presign_put, target, lifetime)
            request = start_export_request(
                path=path, user=user, target=target, expires_at=expiry, put_url=put_url
            )
            completed = await self._run_export(request, target, size, deadline, idle, feature)
            return completed.export
        upload_id = await asyncio.to_thread(gateway.create_multipart_upload, target)
        try:
            part_urls = await asyncio.to_thread(
                gateway.presign_upload_parts, target, upload_id, plan.part_count, lifetime
            )
            request = start_export_request(
                path=path,
                user=user,
                target=target,
                expires_at=expiry,
                part_urls=part_urls,
                part_size=plan.part_size,
            )
            result = await self._run_export(request, target, size, deadline, idle, feature)
            await asyncio.to_thread(
                gateway.complete_multipart_upload, target, upload_id, list(result.part_etags)
            )
        except BaseException:
            await asyncio.to_thread(gateway.abort_multipart_upload_quietly, target, upload_id)
            raise
        return result.export

    async def _run_export(
        self,
        request: filesystem_pb2.StartExportRequest,
        target: StagingObject,
        size: int,
        deadline: OperationDeadline,
        idle: float | None,
        feature: str,
    ) -> CompletedExport:
        started = time.monotonic()
        transfer_id = await self._start(
            lambda stub, timeout: stub.StartExport(request, timeout=timeout),
            feature,
            deadline.remaining(size, started),
        )
        state = await self._follow(transfer_id, self._deadline_at(deadline, size), idle)
        if not is_done(state):
            raise failure_from_state(state, "export")
        self._sandbox._logger_or(logger).debug(
            "transferencia %s terminada (direction=export, bytes=%d, duration_ms=%d)",
            transfer_id,
            int(state.bytes_done),
            int((time.monotonic() - started) * 1000),
        )
        return completed_export(target, transfer_id, state)

    async def _stream_object(
        self, gateway: S3Gateway, result: ExportResult, idle: float | None
    ) -> AsyncIterator[bytes]:
        chunks = await asyncio.to_thread(gateway.open_chunks, result.target)
        digest = hashlib.sha256()
        try:
            while True:
                chunk = await next_object_chunk(chunks, idle)
                if chunk is None:
                    break
                digest.update(chunk)
                yield chunk
        finally:
            await asyncio.to_thread(chunks.close)
            await asyncio.to_thread(gateway.delete_quietly, result.target)
        if digest.hexdigest() != result.sha256:
            raise checksum_mismatch_error()

    async def _follow(
        self, transfer_id: str, at: float | None, idle: float | None = None
    ) -> filesystem_pb2.TransferState:
        """Misma política que `Transfers._follow`."""
        request = watch_transfer_request(transfer_id)
        generation = self._sandbox.resume_generation
        budget = ReconnectBudget()
        empty_ends = 0
        while True:
            call, first = await self._open_watch(request, transfer_id, at)
            try:
                state = await self._terminal_state(call, first, idle)
            except grpc.RpcError as exc:
                generation = await self._after_cut(exc, transfer_id, generation, budget)
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

    @staticmethod
    async def _terminal_state(
        call: Any, first: Any, idle: float | None
    ) -> filesystem_pb2.TransferState | None:
        state = terminal_state(first)
        while state is None:
            event = await read_guarded(call, idle)
            if event is STREAM_EOF:
                return None
            state = terminal_state(event)
        return state

    async def _after_cut(
        self, exc: grpc.RpcError, transfer_id: str, generation: int, budget: ReconnectBudget
    ) -> int:
        if rpc_status(exc) is grpc.StatusCode.DEADLINE_EXCEEDED:
            raise transfer_deadline_error(transfer_id) from exc
        if not self._sandbox._is_reconnectable(exc):
            raise await self._sandbox._stream_failure(exc) from exc
        outcome = await self._sandbox._reconnect(
            exc, generation, wake=self._sandbox._foreground_stream_wakes()
        )
        if not outcome.resumed:
            raise self._sandbox._reconnect_error(outcome, exc) from exc
        if not budget.allows(outcome):
            raise await self._sandbox._stream_failure(exc) from exc
        return outcome.resume_generation

    async def _open_watch(
        self, request: filesystem_pb2.WatchTransferRequest, transfer_id: str, at: float | None
    ) -> tuple[Any, Any]:
        retry = GateRetry(self._sandbox._reconnect_timeout)
        while True:
            try:
                return await self._sandbox._open_stream(
                    watch_starter(request, self._remaining(at, transfer_id)),
                    service=FILES_STUB,
                    stream=True,
                    translate=lambda exc: self._watch_open_error(exc, transfer_id),
                )
            except SandboxException as exc:
                delay = retry.retry_delay(exc)
                if delay is None:
                    raise
                await asyncio.sleep(delay)

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

    async def _start(self, invoke: AsyncTransferInvoke, feature: str, timeout: float) -> str:
        response = await self._unary(invoke, feature, timeout, filesystem=True)
        return str(response.transfer_id)

    async def _unary(
        self, invoke: AsyncTransferInvoke, feature: str, timeout: float, *, filesystem: bool
    ) -> Any:
        try:
            return await self._sandbox._call_unary(lambda: invoke(self._sandbox._files, timeout))
        except grpc.RpcError as exc:
            raise translate_transfer_error(exc, feature, filesystem=filesystem) from exc

    def _new_object(self, staging: S3Staging, direction: KeyDirection) -> StagingObject:
        return new_staging_object(
            staging, self._sandbox.sandbox_id, self._sandbox.region, direction
        )

    async def _gateway_for(self, staging: S3Staging) -> S3Gateway:
        async with self._gateway_lock:
            if self._gateway is None:
                self._gateway = await asyncio.to_thread(
                    S3Gateway.from_session,
                    self._sandbox._session,
                    staging_region(staging, self._sandbox.region),
                )
            return self._gateway


async def next_object_chunk(chunks: ObjectChunks, idle: float | None) -> bytes | None:
    """El siguiente trozo del cuerpo de S3 (en un hilo) o `None` al final;
    con `idle`, una espera más larga cierra el cuerpo y levanta
    `TimeoutException("stream_idle_timeout: ...")`."""
    pending = asyncio.to_thread(next, chunks, None)
    if idle is None:
        return await pending
    try:
        return await asyncio.wait_for(pending, idle)
    except TimeoutError as exc:
        await asyncio.to_thread(chunks.close)
        raise idle_timeout_error(idle) from exc


T = TypeVar("T")


async def run_bounded(
    invoke: Callable[[], T], seconds: float | None, cancel: Callable[[], object]
) -> T:
    """Misma política que `rayito.sandbox_sync.transfer.run_bounded`: la
    pata S3 corre en un hilo y, si agota el plazo, `cancel` la corta y se
    levanta `TimeoutException`; el resultado tardío del hilo se descarta."""
    pending = asyncio.ensure_future(asyncio.to_thread(invoke))
    try:
        return await asyncio.wait_for(asyncio.shield(pending), seconds)
    except TimeoutError:
        cancel()
        pending.add_done_callback(discard_outcome)
        raise transfer_timeout_error() from None


def discard_outcome(future: asyncio.Future[Any]) -> None:
    if not future.cancelled():
        future.exception()
