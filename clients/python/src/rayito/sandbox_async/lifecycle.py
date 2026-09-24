"""Plazo lógico del `AsyncSandbox` (ADR-011): el disparador del modo `pause`
sobre `loop.call_later` y el `SetTimeout` de un solo uso de
`AsyncSandbox.set_timeout(sandbox_id)` sobre `grpc.aio`.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Coroutine
from typing import Any

import grpc

from rayito._aws import ControlPlane, PortSpec
from rayito._lifecycle_base import TimeoutRequest
from rayito._limits import DEFAULT_PORT
from rayito._models import SandboxInfo
from rayito._transport import (
    ProxyAuthPlugin,
    TokenRefresher,
    TokenStore,
    TransportSettings,
    is_proxy_forbidden,
)
from rayito.v1 import lifecycle_pb2_grpc

FireCallback = Callable[[], Coroutine[Any, Any, None]]


class AsyncDeadlineTrigger:
    """El `DeadlineTrigger` síncrono sobre el event loop: un único handle de
    `loop.call_later` que, al vencer, lanza el disparo como task. Mismas
    reglas: los `arm` durante el disparo se aplazan y al terminar sólo se
    rearma un plazo por delante (`delay > 0`); `cancel` lo apaga para
    siempre y cancela un disparo en curso."""

    def __init__(self, fire: FireCallback) -> None:
        self._fire = fire
        self._handle: asyncio.TimerHandle | None = None
        self._task: asyncio.Task[None] | None = None
        self._scheduled: float | None = None
        self._firing = False
        self._deferred: float | None = None
        self._cancelled = False

    @property
    def scheduled_delay(self) -> float | None:
        return self._scheduled

    def arm(self, delay: float | None) -> None:
        if self._cancelled:
            return
        if self._firing:
            self._deferred = delay
            return
        self._schedule(delay)

    async def fire(self) -> None:
        """Ejecuta el disparo en la coroutine que llama."""
        if self._cancelled or self._firing:
            return
        self._begin_firing()
        try:
            await self._fire()
        finally:
            self._end_firing()

    def cancel(self) -> None:
        self._cancelled = True
        self._schedule(None)
        task = self._task
        self._task = None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    def _begin_firing(self) -> None:
        """Mismo criterio que el síncrono: cancelar el handle pendiente es
        inocuo si ya corrió y evita un segundo disparo."""
        self._firing = True
        self._deferred = None
        self._schedule(None)

    def _end_firing(self) -> None:
        self._firing = False
        deferred, self._deferred = self._deferred, None
        if not self._cancelled and deferred is not None and deferred > 0:
            self._schedule(deferred)

    def _schedule(self, delay: float | None) -> None:
        if self._handle is not None:
            self._handle.cancel()
        self._handle = None
        self._scheduled = delay
        if delay is None:
            return
        self._handle = asyncio.get_running_loop().call_later(delay, self._start_task)

    def _start_task(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            await self.fire()


async def set_timeout_once_async(
    control_plane: ControlPlane,
    info: SandboxInfo,
    *,
    access_token: str,
    request: TimeoutRequest,
    transport: TransportSettings,
    request_timeout: float,
) -> Any:
    """`sandbox_sync.lifecycle.set_timeout_once` sobre `grpc.aio`: un JWE
    (boto3 en un hilo), un canal dedicado con el access token que se cierra
    al salir y un reintento tras un 403 del proxy."""
    refresher = TokenRefresher(
        TokenStore(), lambda ports: control_plane.create_auth_token(info.sandbox_id, ports)
    )
    await asyncio.to_thread(refresher.mint, (PortSpec.single(DEFAULT_PORT),))
    plugin = ProxyAuthPlugin(
        refresher.store,
        port=DEFAULT_PORT,
        access_token=access_token,
        extra=transport.extra_metadata,
    )
    channel = transport.open_aio_channel(info.endpoint, plugin)
    try:
        stub = lifecycle_pb2_grpc.LifecycleServiceStub(channel)
        try:
            return await stub.SetTimeout(request.to_proto(), timeout=request_timeout)
        except grpc.RpcError as exc:
            if not is_proxy_forbidden(exc):
                raise
        await asyncio.to_thread(refresher.refresh_all)
        return await stub.SetTimeout(request.to_proto(), timeout=request_timeout)
    finally:
        await channel.close(grace=None)
