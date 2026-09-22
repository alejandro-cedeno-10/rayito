"""`Checkpoint`/`Restore` asíncronos: la misma política que
`rayito.sandbox_sync.persistence` sobre `grpc.aio` (un 403 del proxy antes
del primer mensaje se reintenta una vez; un corte a mitad no se reconecta)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING, Any

import grpc

from rayito._models import CheckpointResult, RestoreResult, S3Prefix
from rayito._persistence_base import (
    CheckpointProgressCallback,
    RestoreProgressCallback,
    checkpoint_request,
    handle_checkpoint_event,
    handle_restore_event,
    mid_stream_exception,
    require_started,
    resolve_target,
    restore_request,
    status_exception,
    stream_ended_early,
    validate_persist_timeout,
)
from rayito._process_base import STREAM_EOF
from rayito.v1 import filesystem_pb2_grpc

if TYPE_CHECKING:
    from rayito.sandbox_async.main import AsyncSandbox

FILES_STUB = filesystem_pb2_grpc.FilesystemServiceStub

logger = logging.getLogger("rayito.persistence")


class AsyncPersistenceClient:
    """Los dos RPCs de persistencia de un `AsyncSandbox`."""

    def __init__(self, sandbox: AsyncSandbox) -> None:
        self._sandbox = sandbox

    async def checkpoint(
        self,
        *,
        target: S3Prefix | None,
        bound: S3Prefix | None,
        exclude: Sequence[str],
        timeout: float | None,
        on_progress: CheckpointProgressCallback | None,
        user: str | None,
    ) -> CheckpointResult:
        destination = resolve_target(target, bound)
        deadline = validate_persist_timeout(timeout)
        request = checkpoint_request(destination, exclude, user)
        logger.info("sandbox %s: checkpoint hacia %s", self._sandbox.sandbox_id, destination.uri)
        call, first = await self._open(
            lambda stub: stub.Checkpoint(request, timeout=deadline), rpc="Checkpoint"
        )
        try:
            async for event in self._events(call, first):
                result = handle_checkpoint_event(event, destination, on_progress)
                if result is not None:
                    logger.info(
                        "sandbox %s: checkpoint completo en %s (%d ficheros, %d bytes)",
                        self._sandbox.sandbox_id,
                        destination.uri,
                        result.files,
                        result.archive_bytes,
                    )
                    return result
        except grpc.RpcError as exc:
            raise mid_stream_exception(exc) from exc
        finally:
            call.cancel()
        raise stream_ended_early("Checkpoint")

    async def restore(
        self,
        *,
        source: S3Prefix | None,
        bound: S3Prefix | None,
        timeout: float | None,
        on_progress: RestoreProgressCallback | None,
        user: str | None,
    ) -> RestoreResult:
        origin = resolve_target(source, bound)
        deadline = validate_persist_timeout(timeout)
        request = restore_request(origin, user)
        logger.info("sandbox %s: restore desde %s", self._sandbox.sandbox_id, origin.uri)
        call, first = await self._open(
            lambda stub: stub.Restore(request, timeout=deadline), rpc="Restore"
        )
        try:
            async for event in self._events(call, first):
                result = handle_restore_event(event, origin, on_progress)
                if result is not None:
                    logger.info(
                        "sandbox %s: restore completo desde %s (%d ficheros, %d bytes)",
                        self._sandbox.sandbox_id,
                        origin.uri,
                        result.files,
                        result.bytes_written,
                    )
                    return result
        except grpc.RpcError as exc:
            raise mid_stream_exception(exc) from exc
        finally:
            call.cancel()
        raise stream_ended_early("Restore")

    async def _open(self, start: Any, *, rpc: str) -> tuple[Any, Any]:
        call, first = await self._sandbox._open_stream(
            start, service=FILES_STUB, stream=True, translate=status_exception
        )
        try:
            require_started(first, rpc=rpc)
        except Exception:
            call.cancel()
            raise
        return call, first

    @staticmethod
    async def _events(call: Any, first: Any) -> AsyncIterator[Any]:
        yield first
        while True:
            event = await call.read()
            if event is STREAM_EOF:
                return
            yield event
