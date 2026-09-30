"""Aceptación de `rayito sandbox proxy` contra AWS real
(`openspec/changes/m12-sizes-proxy`).

Arranca `python3 -m http.server` dentro de un sandbox real, lanza el proxy
local (`_proxy.run_proxy`, la misma función que usa el comando de la CLI) en
un hilo aparte y hace un `GET` a `http://127.0.0.1:<puerto>/` desde el
proceso de test: si vuelve 200, el proxy acuñó el JWE, reescribió la
cabecera y abrió el túnel TLS hasta el endpoint correctamente. No hace falta
el access token del sandbox en ningún momento: sólo el JWE del proxy."""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
import urllib.request
from collections.abc import Iterator

import pytest

from rayito._aws import ControlPlane
from rayito.cli import _proxy
from rayito.sandbox_sync.main import Sandbox

pytestmark = pytest.mark.e2e

HTTP_SERVER_PORT = 8000
PROXY_READY_TIMEOUT_SECONDS = 30.0
GET_TIMEOUT_SECONDS = 20.0
HTTP_SERVER_BOOT_GRACE_SECONDS = 2.0


def _serve_until_stopped(
    control_plane: ControlPlane,
    sandbox_id: str,
    port: int,
    ready: threading.Event,
    stop: threading.Event,
) -> None:
    """Lo mismo que `_proxy.run_proxy`, pero parable desde otro hilo con un
    `threading.Event` en vez de Ctrl-C (SIGINT no cruza hilos)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_serve(control_plane, sandbox_id, port, ready, stop, loop=loop))
    finally:
        loop.close()


async def _serve(
    control_plane: ControlPlane,
    sandbox_id: str,
    port: int,
    ready: threading.Event,
    stop: threading.Event,
    *,
    loop: asyncio.AbstractEventLoop,
) -> None:
    endpoint = _proxy.resolve_endpoint(control_plane, sandbox_id)
    refresher = _proxy.build_refresher(control_plane, sandbox_id, port)
    refresher.start()
    spec = _proxy.ProxySpec(
        sandbox_id=sandbox_id, port=port, endpoint=endpoint, bind="127.0.0.1", local_port=port
    )
    connector = _proxy.default_connector(endpoint)

    def on_ready(_server: asyncio.Server) -> None:
        ready.set()

    task = asyncio.ensure_future(
        _proxy.serve_proxy(spec, lambda: refresher.store.jwe_for(port), connector, ready=on_ready)
    )
    try:
        await loop.run_in_executor(None, stop.wait)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        refresher.stop()


@pytest.fixture
def http_server_sandbox(sandbox: Sandbox) -> Iterator[Sandbox]:
    sandbox.commands.run(f"python3 -m http.server {HTTP_SERVER_PORT}", background=True)
    time.sleep(HTTP_SERVER_BOOT_GRACE_SECONDS)
    yield sandbox


def test_proxy_reaches_an_http_server_running_inside_the_sandbox(
    control_plane: ControlPlane, http_server_sandbox: Sandbox
) -> None:
    ready = threading.Event()
    stop = threading.Event()
    thread = threading.Thread(
        target=_serve_until_stopped,
        args=(control_plane, http_server_sandbox.sandbox_id, HTTP_SERVER_PORT, ready, stop),
        name="rayito-proxy-e2e",
        daemon=True,
    )
    thread.start()
    try:
        assert ready.wait(PROXY_READY_TIMEOUT_SECONDS), "el proxy no arrancó a tiempo"
        with urllib.request.urlopen(
            f"http://127.0.0.1:{HTTP_SERVER_PORT}/", timeout=GET_TIMEOUT_SECONDS
        ) as response:
            assert response.status == 200
    finally:
        stop.set()
        thread.join(timeout=15.0)
        assert not thread.is_alive(), "el hilo del proxy no terminó tras stop.set()"
