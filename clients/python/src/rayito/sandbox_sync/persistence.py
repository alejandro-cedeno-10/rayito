"""`Checkpoint`/`Restore` síncronos sobre el canal de streams del sandbox.

Ambos RPCs abren un server-stream con deadline `timeout`; un 403 del proxy
antes del primer mensaje se reintenta una vez tras reacuñar (como
`WatchDir`); un corte a mitad (`suspending`, `UNAVAILABLE`, reset) **no** se
reconecta: la excepción dice que la operación se interrumpió y hay que
repetirla. Sólo se loguea la `uri` (bucket y prefijo son configuración, no
secretos); nunca la lista de exclusión ni el sha256 a nivel `info`.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
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
from rayito.v1 import filesystem_pb2_grpc

if TYPE_CHECKING:
    from rayito.sandbox_sync.main import Sandbox

FILES_STUB = filesystem_pb2_grpc.FilesystemServiceStub

logger = logging.getLogger("rayito.persistence")


class PersistenceClient:
    """Los dos RPCs de persistencia de un `Sandbox`."""

    def __init__(self, sandbox: Sandbox) -> None:
        self._sandbox = sandbox

    def checkpoint(
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
        self._sandbox._logger_or(logger).info(
            "sandbox %s: checkpoint hacia %s", self._sandbox.sandbox_id, destination.uri
        )
        call, first = self._open(
            lambda stub: stub.Checkpoint(request, timeout=deadline), rpc="Checkpoint"
        )
        try:
            for event in self._events(call, first):
                result = handle_checkpoint_event(event, destination, on_progress)
                if result is not None:
                    self._sandbox._logger_or(logger).info(
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

    def restore(
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
        self._sandbox._logger_or(logger).info(
            "sandbox %s: restore desde %s", self._sandbox.sandbox_id, origin.uri
        )
        call, first = self._open(
            lambda stub: stub.Restore(request, timeout=deadline), rpc="Restore"
        )
        try:
            for event in self._events(call, first):
                result = handle_restore_event(event, origin, on_progress)
                if result is not None:
                    self._sandbox._logger_or(logger).info(
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

    def _open(self, start: Any, *, rpc: str) -> tuple[Any, Any]:
        """Abre el stream en el canal de streams y consume `started`; un
        status antes del primer mensaje sigue la tabla D8."""
        call, first = self._sandbox._open_stream(
            start, service=FILES_STUB, stream=True, translate=status_exception
        )
        try:
            require_started(first, rpc=rpc)
        except Exception:
            call.cancel()
            raise
        return call, first

    @staticmethod
    def _events(call: Any, first: Any) -> Any:
        yield first
        yield from call
