"""Aceptación de `rayito sandbox proxy` contra AWS real
(`openspec/changes/archive/2026-10-01-m12-sizes-proxy`).

Arranca `python3 -m http.server` dentro de un sandbox real, lanza
`_proxy.run_proxy` (la misma función que usa el comando de la CLI, con un
`threading.Event` como `stop_event` porque SIGINT no cruza hilos) en un hilo
aparte y hace un `GET` a `http://127.0.0.1:<puerto>/` desde el proceso de
test: si vuelve 200, el proxy acuñó el JWE, reescribió la cabecera y abrió el
túnel TLS hasta el endpoint correctamente. No hace falta el access token del
sandbox en ningún momento: sólo el JWE del proxy.

No hay una segunda implementación del bucle de servir aquí: este test y la
CLI comparten exactamente `_proxy.run_proxy` (ver `sandbox.py::proxy_command`
y el hallazgo de revisión que pedía esto), así que no pueden divergir."""

from __future__ import annotations

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
        target=_proxy.run_proxy,
        kwargs=dict(
            control_plane=control_plane,
            sandbox_id=http_server_sandbox.sandbox_id,
            port=HTTP_SERVER_PORT,
            local_port=HTTP_SERVER_PORT,
            bind="127.0.0.1",
            on_ready=lambda _message: ready.set(),
            stop_event=stop,
        ),
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
