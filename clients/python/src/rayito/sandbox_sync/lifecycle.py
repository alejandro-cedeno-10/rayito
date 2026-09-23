"""Plazo lógico del `Sandbox` síncrono (ADR-011): el disparador del modo
`pause` y el `SetTimeout` de un solo uso de `Sandbox.set_timeout(sandbox_id)`.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
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


class DeadlineTrigger:
    """Un único `threading.Timer` daemon armado en el plazo que reportó `rayd`
    (design D7). Cada lifecycle registrado lo rearma (`arm`), `cancel` lo
    apaga para siempre (`close()`, `kill()`, `__exit__`).

    Mientras el disparo corre, los `arm` que provoca (el `Health` que lee
    registra un lifecycle nuevo) se aplazan: al terminar sólo se rearma un
    plazo que aún está por delante (`delay > 0`). Así un `expired` nunca
    rearma un disparo inmediato en bucle, ni tras suspender ni tras fallar:
    la política de idle de la plataforma es el respaldo."""

    def __init__(self, fire: Callable[[], None], *, name: str) -> None:
        self._fire = fire
        self._name = name
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._scheduled: float | None = None
        self._firing = False
        self._deferred: float | None = None
        self._cancelled = False

    @property
    def scheduled_delay(self) -> float | None:
        """El retardo del temporizador armado ahora mismo; `None` si no hay."""
        with self._lock:
            return self._scheduled

    def arm(self, delay: float | None) -> None:
        with self._lock:
            if self._cancelled:
                return
            if self._firing:
                self._deferred = delay
                return
            self._schedule(delay)

    def fire(self) -> None:
        """Ejecuta el disparo en el hilo que llama (el del temporizador)."""
        if not self._begin_firing():
            return
        try:
            self._fire()
        finally:
            self._end_firing()

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            self._schedule(None)

    def _begin_firing(self) -> bool:
        """Cancelar el temporizador pendiente es inocuo si es el que está
        disparando y evita un segundo disparo si `fire` llegó por otra vía."""
        with self._lock:
            if self._cancelled or self._firing:
                return False
            self._firing = True
            self._deferred = None
            self._schedule(None)
            return True

    def _end_firing(self) -> None:
        with self._lock:
            self._firing = False
            deferred, self._deferred = self._deferred, None
            if not self._cancelled and deferred is not None and deferred > 0:
                self._schedule(deferred)

    def _schedule(self, delay: float | None) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self._timer = None
        self._scheduled = delay
        if delay is None:
            return
        timer = threading.Timer(delay, self.fire)
        timer.name = self._name
        timer.daemon = True
        self._timer = timer
        timer.start()


def set_timeout_once(
    control_plane: ControlPlane,
    info: SandboxInfo,
    *,
    access_token: str,
    request: TimeoutRequest,
    transport: TransportSettings,
    request_timeout: float,
) -> Any:
    """Un `SetTimeout` a un sandbox `RUNNING` sin handle: acuña un JWE para
    el puerto 8080, abre un canal dedicado con el access token (cerrado al
    salir) y lo manda una vez, con un reintento tras un 403 del proxy.
    Devuelve el `LifecycleState` crudo; los errores salen como `RpcError`."""
    refresher = TokenRefresher(
        TokenStore(), lambda ports: control_plane.create_auth_token(info.sandbox_id, ports)
    )
    refresher.mint((PortSpec.single(DEFAULT_PORT),))
    plugin = ProxyAuthPlugin(
        refresher.store,
        port=DEFAULT_PORT,
        access_token=access_token,
        extra=transport.extra_metadata,
    )
    channel = transport.open_channel(info.endpoint, plugin)
    try:
        stub = lifecycle_pb2_grpc.LifecycleServiceStub(channel)
        try:
            return stub.SetTimeout(request.to_proto(), timeout=request_timeout)
        except grpc.RpcError as exc:
            if not is_proxy_forbidden(exc):
                raise
        refresher.refresh_all()
        return stub.SetTimeout(request.to_proto(), timeout=request_timeout)
    finally:
        channel.close()
