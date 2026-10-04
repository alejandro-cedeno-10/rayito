"""Plano de control local de los tests `local` (nunca se publica: vive en
`tests/`, fuera de la wheel).

`LocalGuestControlPlane` es otro adaptador del puerto `ControlPlane`: un
decorador sobre el plano real (`LambdaMicrovmsControlPlane` contra Floci, el
emulador de AWS de `dev/local/compose.yaml`) que, donde AWS arrancaría un
MicroVM, apunta al contenedor `guest`, donde `rayd` corre como PID 1 sobre la
imagen de producto. Lo que Floci emula (`run-microvm`, `get-microvm`,
`list-microvms`, `terminate-microvm`, las imágenes) pasa por el adaptador
boto3 real, con sus formas de petición y su mapeo de errores; lo que Floci no
emula (el endpoint por VM, `create-microvm-auth-token`, suspend/resume) lo
sustituyen los hooks del guest, igual que los llama la plataforma
(`scripts/hooks-sim.py`, `AWS_API_NOTES.md` §8).

Solo se activa cuando los tests leen `RAYITO_LOCAL_GUEST`; el SDK no sabe que
existe.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, replace
from typing import Final

import grpc

from rayito._aws import ControlPlane, LaunchRequest, PortSpec
from rayito._limits import HOOK_PATH_PREFIX
from rayito._models import ImageVersionInfo, MicrovmListPage, SandboxInfo, SandboxListItem
from rayito._transport import TransportSettings
from rayito.exceptions import InvalidArgumentException, SandboxException

#: `host:puerto gRPC:puerto de hooks` del guest dentro del espacio de red que
#: comparte con el runner (`dev/local/compose.yaml`).
LOCAL_GUEST_VAR: Final = "RAYITO_LOCAL_GUEST"
#: El JWE que firmaría `create-microvm-auth-token`. Lo valida el proxy de AWS,
#: no `rayd` (ARCHITECTURE.md, Capa 3), así que en local basta un valor fijo
#: que no es un secreto.
LOCAL_PROXY_TOKEN: Final = "rayito-local-proxy-token"
#: Presupuesto de `/ready`: el de `scripts/hooks-sim.py`
#: (`READY_RETRY_BUDGET_S`), que cubre la salida forzada de 300 s de rayd,
#: más el rearranque del contenedor tras el `/terminate` anterior.
READY_BUDGET_SECONDS: Final = 330.0
READY_RETRY_INTERVAL_SECONDS: Final = 0.5
#: Plazo de cada hook salvo `/ready`: el `runTimeoutInSeconds` que declara la
#: imagen (`scripts/hooks-sim.py`, `DECLARED_TIMEOUT_S`).
HOOK_TIMEOUT_SECONDS: Final = 30.0
#: Lo que tarda como mucho el `rayd` anterior en soltar el puerto de hooks
#: tras `/terminate` (cierre ordenado de streams y procesos, ADR-011).
SHUTDOWN_BUDGET_SECONDS: Final = 15.0
SHUTDOWN_POLL_SECONDS: Final = 0.05
HTTP_OK: Final = 200
#: La respuesta de `/run` cuando `rayd` instala el token
#: (`crates/rayd/src/hooks/mod.rs`, `run_outcome`); `already_ran` o
#: `illegal` vienen del `rayd` anterior, que aún no ha salido.
RUN_INSTALLED: Final = "installed"
SUSPENDED_STATE: Final = "SUSPENDED"
RUNNING_STATE: Final = "RUNNING"


@dataclass(frozen=True)
class GuestAddress:
    """Dónde escucha el `rayd` del guest."""

    host: str
    grpc_port: int
    hooks_port: int

    @classmethod
    def parse(cls, raw: str) -> GuestAddress:
        parts = raw.strip().split(":")
        if len(parts) != 3 or not all(parts):
            raise InvalidArgumentException(
                f"{LOCAL_GUEST_VAR} debe ser host:puerto-grpc:puerto-hooks"
            )
        host, grpc_port, hooks_port = parts
        return cls(host=host, grpc_port=int(grpc_port), hooks_port=int(hooks_port))

    def hook_url(self, hook: str) -> str:
        return f"http://{self.host}:{self.hooks_port}{HOOK_PATH_PREFIX}/{hook}"

    def transport(self) -> TransportSettings:
        """h2c por loopback con credenciales locales de grpc: el mismo canal
        que usan los tests unitarios contra el `rayd` falso. Solo vale
        porque el runner y el guest comparten el espacio de red."""
        return TransportSettings(
            channel_credentials=grpc.local_channel_credentials(grpc.LocalConnectionType.LOCAL_TCP),
            port=self.grpc_port,
        )


class GuestHooks:
    """Adaptador HTTP de los hooks del guest (`POST`, HTTP/1.1, sólo `/run`
    lleva cuerpo)."""

    def __init__(self, address: GuestAddress) -> None:
        self._address = address

    def post(self, hook: str, body: dict[str, str] | None = None) -> tuple[int, str]:
        data = json.dumps(body).encode() if body is not None else b""
        headers = {"content-type": "application/json"} if body is not None else {}
        request = urllib.request.Request(
            self._address.hook_url(hook), data=data, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=HOOK_TIMEOUT_SECONDS) as response:
                return int(response.status), response.read().decode(errors="replace")
        except urllib.error.HTTPError as error:
            return int(error.code), error.read().decode(errors="replace")

    def wait_ready(self) -> None:
        """`/ready` responde 503 mientras el sidecar calienta el kernel, y la
        conexión se rechaza mientras Docker rearranca el contenedor."""
        deadline = time.monotonic() + READY_BUDGET_SECONDS
        while True:
            try:
                status, _ = self.post("ready")
                if status == HTTP_OK:
                    return
            except OSError:
                status = 0
            if time.monotonic() >= deadline:
                raise SandboxException(f"el guest local no respondió /ready (último: {status})")
            time.sleep(READY_RETRY_INTERVAL_SECONDS)

    def wait_gone(self) -> None:
        """Tras `/terminate`, hasta que el `rayd` anterior deja de responder
        (Docker lo rearranca enseguida; si la ventana se pierde, `run`
        detecta igualmente que contestó el anterior)."""
        deadline = time.monotonic() + SHUTDOWN_BUDGET_SECONDS
        while time.monotonic() < deadline:
            try:
                self.post("ready")
            except OSError:
                return
            time.sleep(SHUTDOWN_POLL_SECONDS)

    def run(self, microvm_id: str, payload: str) -> None:
        """`/ready` y `/run` hasta que un `rayd` recién arrancado instale el
        token; uno que ya aceptó un `/run` (el anterior, que aún no salió, o
        uno que dejó vivo otra sesión) se recicla con `/terminate`."""
        deadline = time.monotonic() + READY_BUDGET_SECONDS
        while True:
            self.wait_ready()
            reply = self.call("run", {"microvmId": microvm_id, "runHookPayload": payload})
            if RUN_INSTALLED in reply:
                return
            if time.monotonic() >= deadline:
                raise SandboxException("el guest local no instaló el token de /run")
            self.call("terminate")
            self.wait_gone()

    def call(self, hook: str, body: dict[str, str] | None = None) -> str:
        status, text = self.post(hook, body)
        if status != HTTP_OK:
            raise SandboxException(f"el hook /{hook} del guest local respondió {status}")
        return text


class LocalGuestControlPlane:
    """`ControlPlane` que lanza sobre Floci y ejecuta en el guest local.

    Un guest aloja un sandbox a la vez (como un MicroVM): `run_microvm`
    termina el anterior si un test lo dejó vivo, y `/terminate` hace salir a
    `rayd`, que Docker vuelve a arrancar limpio para el siguiente.
    """

    def __init__(
        self, inner: ControlPlane, address: GuestAddress, hooks: GuestHooks | None = None
    ) -> None:
        self._inner = inner
        self._address = address
        self._hooks = hooks or GuestHooks(address)
        self._lock = threading.Lock()
        self._live: str | None = None
        self._suspended: set[str] = set()

    @property
    def region(self) -> str:
        return self._inner.region

    @property
    def live_sandbox_id(self) -> str | None:
        return self._live

    def resolve_template_arn(self, template: str) -> str:
        return self._inner.resolve_template_arn(template)

    def run_microvm(self, request: LaunchRequest) -> SandboxInfo:
        with self._lock:
            if self._live is not None:
                self._terminate_guest(self._live)
            info = self._inner.run_microvm(request)
            self._hooks.run(info.sandbox_id, request.run_hook_payload)
            self._live = info.sandbox_id
            return self._localize(info)

    def get_microvm(self, sandbox_id: str) -> SandboxInfo:
        return self._localize(self._inner.get_microvm(sandbox_id))

    def list_microvms(
        self,
        *,
        image_arn: str | None = None,
        image_version: str | None = None,
        states: Iterable[str] | None = None,
    ) -> Iterator[SandboxListItem]:
        wanted = frozenset(states) if states is not None else None
        for item in self._inner.list_microvms(image_arn=image_arn, image_version=image_version):
            local = self._localize_item(item)
            if wanted is None or local.state in wanted:
                yield local

    def list_microvms_page(
        self,
        *,
        image_arn: str | None,
        image_version: str | None,
        max_results: int,
        next_token: str | None,
    ) -> MicrovmListPage:
        page = self._inner.list_microvms_page(
            image_arn=image_arn,
            image_version=image_version,
            max_results=max_results,
            next_token=next_token,
        )
        return replace(page, items=tuple(self._localize_item(item) for item in page.items))

    def terminate_microvm(self, sandbox_id: str) -> bool:
        with self._lock:
            if sandbox_id == self._live:
                self._terminate_guest(sandbox_id)
        return self._inner.terminate_microvm(sandbox_id)

    def suspend_microvm(self, sandbox_id: str) -> bool:
        with self._lock:
            if sandbox_id != self._live or sandbox_id in self._suspended:
                return False
            self._hooks.call("suspend")
            self._suspended.add(sandbox_id)
            return True

    def resume_microvm(self, sandbox_id: str) -> bool:
        with self._lock:
            if sandbox_id not in self._suspended:
                return False
            self._hooks.call("resume")
            self._suspended.discard(sandbox_id)
            return True

    def create_auth_token(self, sandbox_id: str, ports: Sequence[PortSpec]) -> str:
        if not ports:
            raise InvalidArgumentException("allowedPorts necesita al menos un puerto")
        return LOCAL_PROXY_TOKEN

    def get_microvm_image_version(self, image_arn: str, image_version: str) -> ImageVersionInfo:
        return self._inner.get_microvm_image_version(image_arn, image_version)

    def _terminate_guest(self, sandbox_id: str) -> None:
        self._hooks.call("terminate")
        self._hooks.wait_gone()
        self._suspended.discard(sandbox_id)
        self._live = None

    def _local_state(self, sandbox_id: str, state: str) -> str:
        if sandbox_id in self._suspended and state == RUNNING_STATE:
            return SUSPENDED_STATE
        return state

    def _localize(self, info: SandboxInfo) -> SandboxInfo:
        return replace(
            info,
            endpoint=self._address.host,
            state=self._local_state(info.sandbox_id, info.state),
        )

    def _localize_item(self, item: SandboxListItem) -> SandboxListItem:
        return replace(item, state=self._local_state(item.sandbox_id, item.state))
