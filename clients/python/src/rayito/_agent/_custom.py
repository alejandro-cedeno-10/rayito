"""`CustomRuntime` y `HttpApp` (ADR-028, D2): el agente que escribes tú, con
cualquier framework (CrewAI, OpenAI Agents SDK, Google ADK, Claude Agent SDK,
Vercel AI SDK...), como un comando, una función de Python o una app HTTP.
Implementa los puertos `AgentRuntime`, `InstallableRuntime` y
`ServableRuntime` sobre `_host_runtime.HostBackedRuntime`, igual que Strands
y Hermes.

Exactamente uno de `command`, `entrypoint` o `app`:

- `command`: tu `argv`, que recibe la petición por stdin y habla el protocolo
  v1 por stdout (`protocol="rayito-v1"`) o escribe texto (`protocol="text"`:
  el prompt llega tal cual por stdin y la sesión, el modelo y el
  razonamiento por `RAYITO_SESSION_ID`, `RAYITO_MODEL` y `RAYITO_REASONING`).
- `entrypoint="modulo:funcion"`: una función de Python que recibe la
  petición (`{"prompt", "session_id", "model", "reasoning"}`) y devuelve el
  texto, un iterable de trozos de texto o un `dict` con `text`.
- `app=HttpApp(...)`: tu servidor HTTP; Rayito lo arranca, espera a su ruta de
  salud y traduce `/invocations`. Con `protocol="raw"` (un servidor MCP por
  Streamable HTTP, por ejemplo) sólo se expone, sin traducir.

El modelo llega a tu código por las variables estándar del proveedor
(`ANTHROPIC_BASE_URL`, `OPENAI_BASE_URL`, `AWS_ENDPOINT_URL_BEDROCK_RUNTIME`...)
y por las `RAYITO_MODEL_*`, apuntadas a la pasarela con un marcador como
clave: la clave real no está en el sandbox.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from rayito._agent._domain import AgentSpec
from rayito._agent._host_runtime import (
    PYTHON_FLAG_ENVS,
    HostBackedRuntime,
    HostSetup,
    model_base_url,
    standard_model_envs,
    user_requirements_steps,
)
from rayito._agent._recipes import AGENT_APP_DIR, AGENT_INSTALL_DIR
from rayito._agent._runtime import InstallTarget, TemplateStep, run_step
from rayito._limits import (
    AGENT_SERVICE_HEALTH_PATH,
    AGENT_SERVICE_INVOKE_PATH,
    MODEL_CREDENTIAL_PLACEHOLDER,
    PORT_MAX,
    PORT_MIN,
)
from rayito.exceptions import InvalidArgumentException

CustomProtocol = Literal["rayito-v1", "text"]
CUSTOM_PROTOCOLS: Final[tuple[str, ...]] = ("rayito-v1", "text")
HttpAppProtocol = Literal["agentcore", "openai-chat", "raw"]
HTTP_APP_PROTOCOLS: Final[tuple[str, ...]] = ("agentcore", "openai-chat", "raw")
CUSTOM_NAME: Final = "custom"
DEFAULT_CUSTOM_PROTOCOL: Final = "rayito-v1"
DEFAULT_HTTP_APP_PROTOCOL: Final = "agentcore"
#: El venv que crea `CustomRuntime(requirements=...)`.
CUSTOM_VENV_NAME: Final = "custom"
#: Variables con el modelo que ve tu código, además de las estándar del
#: proveedor.
MODEL_PROVIDER_ENV: Final = "RAYITO_MODEL_PROVIDER"
MODEL_ID_ENV: Final = "RAYITO_MODEL_ID"
MODEL_REGION_ENV: Final = "RAYITO_MODEL_REGION"
MODEL_BASE_URL_ENV: Final = "RAYITO_MODEL_BASE_URL"
MODEL_API_KEY_ENV: Final = "RAYITO_MODEL_API_KEY"
#: Variables que el driver `command` pone al hijo con `protocol="text"`.
SESSION_ID_ENV: Final = "RAYITO_SESSION_ID"
REQUEST_MODEL_ENV: Final = "RAYITO_MODEL"
REQUEST_REASONING_ENV: Final = "RAYITO_REASONING"
_PYTHON_ENTRYPOINT_PATTERN: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*")
_REQUIREMENTS_PATTERN: Final = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*")
_URL_PATH_PATTERN: Final = re.compile(r"/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*")


def _argv(value: object, label: str) -> tuple[str, ...]:
    if (
        isinstance(value, str)
        or not isinstance(value, Sequence)
        or not value
        or any(not isinstance(part, str) or not part for part in value)
    ):
        raise InvalidArgumentException(f"{label} debe ser una lista no vacía de cadenas")
    return tuple(value)


@dataclass(frozen=True)
class HttpApp:
    """Tu servidor HTTP (`CustomRuntime(app=HttpApp(...))`): `command` lo
    arranca, `port` es donde escucha (dentro del sandbox), `health_path` es
    la ruta que debe contestar 200 cuando está listo e `invoke_path` la que
    recibe `{"prompt": ...}`. `protocol` es `"agentcore"` (el contrato de
    AgentCore), `"openai-chat"` (Chat Completions con streaming) o `"raw"`
    (sólo se expone).

    Cuándo cada uno: con `"agentcore"` u `"openai-chat"` el host sirve
    `/ping` y `/invocations` y traduce a tu `invoke_path`, así que
    `deployment.invoke()` funciona pero tus rutas propias no se alcanzan
    desde fuera; con `"raw"` el despliegue reenvía tus rutas tal cual (tu
    `/health`, tu `/invoke`, un servidor MCP) y `invoke()` no existe: usa
    `deployment.url` y `deployment.headers` con tu cliente. Tu `command` se
    ejecuta con el directorio del código copiado como directorio de trabajo y
    en su `PYTHONPATH`; con `requirements`, `python` y `pip` son los del venv
    (sin él, `python` es el intérprete de la imagen)."""

    command: Sequence[str]
    port: int
    health_path: str = AGENT_SERVICE_HEALTH_PATH
    invoke_path: str = AGENT_SERVICE_INVOKE_PATH
    protocol: HttpAppProtocol = DEFAULT_HTTP_APP_PROTOCOL

    def __post_init__(self) -> None:
        object.__setattr__(self, "command", _argv(self.command, "HttpApp.command"))
        if isinstance(self.port, bool) or not isinstance(self.port, int):
            raise InvalidArgumentException("HttpApp.port debe ser un entero")
        if not PORT_MIN <= self.port <= PORT_MAX:
            raise InvalidArgumentException(
                f"HttpApp.port debe estar entre {PORT_MIN} y {PORT_MAX}, recibido {self.port}"
            )
        for label, path in (("health_path", self.health_path), ("invoke_path", self.invoke_path)):
            if not isinstance(path, str) or not _URL_PATH_PATTERN.fullmatch(path):
                raise InvalidArgumentException(
                    f"HttpApp.{label} debe ser una ruta que empiece por /"
                )
        if self.protocol not in HTTP_APP_PROTOCOLS:
            raise InvalidArgumentException(
                f"HttpApp.protocol debe ser uno de {list(HTTP_APP_PROTOCOLS)}"
            )

    @property
    def hides_own_routes(self) -> bool:
        """Con `protocol="agentcore"` o `"openai-chat"` el despliegue sólo
        expone `/ping` y `/invocations` (el host traduce a tu `invoke_path`):
        tus rutas propias (`health_path`, `invoke_path` distintas de esas
        dos) no se alcanzan desde fuera. Con `protocol="raw"` se exponen
        tal cual."""
        return self.protocol != "raw" and (
            self.health_path != AGENT_SERVICE_HEALTH_PATH
            or self.invoke_path != AGENT_SERVICE_INVOKE_PATH
        )

    @classmethod
    def from_mapping(cls, table: Mapping[str, object]) -> HttpApp:
        """La tabla `[runtime.app]` de `rayito-agent.toml`."""
        unknown = sorted(set(table) - {"command", "port", "health_path", "invoke_path", "protocol"})
        if unknown:
            raise InvalidArgumentException(f"runtime.app.{unknown[0]}: clave desconocida")
        kwargs = {key: table[key] for key in table if key != "command"}
        return cls(command=table.get("command"), **kwargs)  # type: ignore[arg-type]

    def to_mapping(self) -> dict[str, object]:
        """La tabla `[runtime.app]` canónica (sólo lo que no es el valor por
        defecto)."""
        table: dict[str, object] = {"command": list(self.command), "port": self.port}
        if self.health_path != AGENT_SERVICE_HEALTH_PATH:
            table["health_path"] = self.health_path
        if self.invoke_path != AGENT_SERVICE_INVOKE_PATH:
            table["invoke_path"] = self.invoke_path
        if self.protocol != DEFAULT_HTTP_APP_PROTOCOL:
            table["protocol"] = self.protocol
        return table


class CustomRuntime(HostBackedRuntime):
    """`AgentRuntime` del agente que escribes tú (ver el módulo).

    `install` son órdenes de shell que se ejecutan al construir la imagen
    (como root, antes de copiar tu `source`), por ejemplo un `npm ci` o un
    `dnf install`. `requirements` es un fichero de requisitos de tu `source`
    (`app/<requirements>`): se instala en un venv propio, que pasa a ser el
    intérprete del host y de tu función. `python` es el intérprete con el que
    corre todo (por defecto `python3`).

    Coste y activación
    -------------------
    Activa: pasar `CustomRuntime(...)` a `AgentTemplate`, `AgentProject` o
        `sbx.agent.run`.
    Recursos y llamadas AWS: ninguna por sí solo; la imagen que lo instala
        se construye con `AgentTemplate.build` (un build de imagen) y cada
        ejecución llama al modelo de la pasarela que elijas.
    Coste aproximado: el del build de la imagen (ver `AgentTemplate`) más el
        del modelo; lo que añada tu `install` y tus requisitos.
    IAM: la política `RayitoTemplateBuilder` para construir; ninguna para
        ejecutar (la pasarela guarda la clave).
    Cómo apagarla: no incluirlo en `runtimes` ni en el proyecto.
    Ejemplo:
        CustomRuntime(entrypoint="agente:responde", requirements="requirements.txt")
    """

    name = CUSTOM_NAME
    state_name = CUSTOM_NAME

    def __init__(
        self,
        install: Sequence[str] = (),
        requirements: str | None = None,
        command: Sequence[str] | None = None,
        entrypoint: str | None = None,
        app: HttpApp | None = None,
        protocol: CustomProtocol = DEFAULT_CUSTOM_PROTOCOL,
        python: str | None = None,
    ) -> None:
        if isinstance(install, str) or any(not isinstance(cmd, str) or not cmd for cmd in install):
            raise InvalidArgumentException("CustomRuntime.install debe ser una lista de órdenes")
        chosen = [
            label
            for label, value in (("command", command), ("entrypoint", entrypoint), ("app", app))
            if value is not None
        ]
        if len(chosen) != 1:
            raise InvalidArgumentException(
                "CustomRuntime necesita exactamente uno de command, entrypoint o app"
            )
        if protocol not in CUSTOM_PROTOCOLS:
            raise InvalidArgumentException(
                f"CustomRuntime.protocol debe ser uno de {list(CUSTOM_PROTOCOLS)}"
            )
        if protocol != DEFAULT_CUSTOM_PROTOCOL and command is None:
            raise InvalidArgumentException("CustomRuntime.protocol sólo se usa con command")
        if entrypoint is not None and not _PYTHON_ENTRYPOINT_PATTERN.fullmatch(entrypoint):
            raise InvalidArgumentException(
                "CustomRuntime.entrypoint debe tener la forma 'paquete.modulo:funcion'"
            )
        if app is not None and not isinstance(app, HttpApp):
            raise InvalidArgumentException("CustomRuntime.app debe ser un HttpApp")
        if requirements is not None and (
            not _REQUIREMENTS_PATTERN.fullmatch(requirements) or ".." in requirements.split("/")
        ):
            raise InvalidArgumentException(
                "CustomRuntime.requirements es una ruta relativa dentro de source"
            )
        if python is not None and (not isinstance(python, str) or not python):
            raise InvalidArgumentException("CustomRuntime.python no puede ser vacío")
        self.install = tuple(install)
        self.requirements = requirements
        self.command_argv = None if command is None else _argv(command, "CustomRuntime.command")
        self.entrypoint = entrypoint
        self.app = app
        self.protocol: CustomProtocol = protocol
        self.python = python

    def __repr__(self) -> str:
        return (
            f"CustomRuntime(command={self.command_argv!r}, entrypoint={self.entrypoint!r}, "
            f"app={self.app!r})"
        )

    @classmethod
    def from_options(cls, options: Mapping[str, object]) -> CustomRuntime:
        """El runtime de la tabla `[runtime]` de `rayito-agent.toml`."""
        app = options.get("app")
        if app is not None and not isinstance(app, Mapping):
            raise InvalidArgumentException("runtime.app debe ser una tabla")
        install = options.get("install", ())
        if not isinstance(install, (list, tuple)):
            raise InvalidArgumentException("runtime.install debe ser una lista")
        return cls(
            install=tuple(install),
            requirements=_optional_str(options, "requirements"),
            command=_optional_list(options, "command"),
            entrypoint=_optional_str(options, "entrypoint"),
            app=None if app is None else HttpApp.from_mapping(app),
            protocol=_optional_str(options, "protocol") or DEFAULT_CUSTOM_PROTOCOL,  # type: ignore[arg-type]
            python=_optional_str(options, "python"),
        )

    def to_options(self) -> dict[str, object]:
        """La tabla `[runtime]` canónica."""
        options: dict[str, object] = {"kind": CUSTOM_NAME}
        if self.install:
            options["install"] = list(self.install)
        if self.requirements is not None:
            options["requirements"] = self.requirements
        if self.command_argv is not None:
            options["command"] = list(self.command_argv)
        if self.entrypoint is not None:
            options["entrypoint"] = self.entrypoint
        if self.app is not None:
            options["app"] = self.app.to_mapping()
        if self.protocol != DEFAULT_CUSTOM_PROTOCOL:
            options["protocol"] = self.protocol
        if self.python is not None:
            options["python"] = self.python
        return options

    def venv_bin(self, install_dir: str = AGENT_INSTALL_DIR) -> str:
        """El `bin` del venv de `requirements`: va el primero en el `PATH` de
        tu app o tu comando, de modo que `python` y `pip` son los suyos."""
        return f"{install_dir}/{CUSTOM_VENV_NAME}/bin"

    def venv_python(self, install_dir: str = AGENT_INSTALL_DIR) -> str:
        return f"{self.venv_bin(install_dir)}/python"

    def _environment_options(self) -> dict[str, object]:
        """Dónde corre tu código: el directorio copiado (directorio de
        trabajo y `PYTHONPATH`) y, con `requirements`, el `bin` del venv."""
        options: dict[str, object] = {"app_dir": AGENT_APP_DIR}
        if self.requirements is not None:
            options["path_prepend"] = [self.venv_bin()]
        return options

    def host_python(self) -> str:
        if self.requirements is not None:
            return self.venv_python()
        return self.python or "python3"

    def flag_envs(self) -> Mapping[str, str]:
        return PYTHON_FLAG_ENVS

    def wiring_envs(self, spec: AgentSpec, urls: Mapping[str, str] | None) -> Mapping[str, str]:
        """Las variables estándar del proveedor más las `RAYITO_MODEL_*`."""
        envs = standard_model_envs(spec, urls)
        model = spec.model
        envs[MODEL_PROVIDER_ENV] = model.provider
        envs[MODEL_ID_ENV] = model.id
        if model.region is not None:
            envs[MODEL_REGION_ENV] = model.region
        if urls is not None:
            envs[MODEL_BASE_URL_ENV] = model_base_url(spec, urls)
            envs[MODEL_API_KEY_ENV] = MODEL_CREDENTIAL_PLACEHOLDER
        return envs

    def host_setup(
        self, spec: AgentSpec, urls: Mapping[str, str] | None, workdir: str
    ) -> HostSetup:
        if self.app is not None:
            return HostSetup(
                driver="http-app",
                options={
                    "command": list(self.app.command),
                    "port": self.app.port,
                    "health_path": self.app.health_path,
                    "invoke_path": self.app.invoke_path,
                    "protocol": self.app.protocol,
                    "workdir": workdir,
                    "model": spec.model.id,
                    **self._environment_options(),
                },
            )
        if self.command_argv is not None:
            return HostSetup(
                driver="command",
                options={
                    "command": list(self.command_argv),
                    "protocol": self.protocol,
                    "workdir": workdir,
                    **self._environment_options(),
                },
            )
        return HostSetup(
            driver="callable",
            options={"entrypoint": self.entrypoint, "app_dir": AGENT_APP_DIR, "workdir": workdir},
        )

    def install_steps(self, target: InstallTarget) -> Sequence[TemplateStep]:
        """Tus órdenes de `install` y, con `requirements`, el venv con tus
        requisitos."""
        steps: list[TemplateStep] = [run_step(command) for command in self.install]
        if self.requirements is not None:
            venv_dir = f"{target.install_dir}/{CUSTOM_VENV_NAME}"
            creator = self.python or target.python
            steps.append(run_step(f"{creator} -m venv {venv_dir}"))
            steps += user_requirements_steps(
                target, venv_dir=venv_dir, requirements=self.requirements, slug=CUSTOM_VENV_NAME
            )
        return steps

    def context_files(self) -> Mapping[str, bytes]:
        """Ninguno: tu código y tus requisitos van en `source`."""
        return {}

    def manifest_entry(self) -> Mapping[str, object]:
        """Qué se instaló: el driver, el hash de tus órdenes y tus
        requisitos."""
        driver = self.host_setup_driver()
        return {
            "driver": driver,
            "install_sha256": hashlib.sha256("\n".join(self.install).encode("utf-8")).hexdigest(),
            "requirements": self.requirements,
        }

    def host_setup_driver(self) -> str:
        if self.app is not None:
            return "http-app"
        return "command" if self.command_argv is not None else "callable"

    def smoke_checks(self) -> Sequence[str]:
        """Ninguna: no sabemos qué instalas."""
        return ()

    def prefetch_paths(self) -> Sequence[str]:
        return ()


def _optional_str(options: Mapping[str, object], key: str) -> str | None:
    value = options.get(key)
    if value is not None and not isinstance(value, str):
        raise InvalidArgumentException(f"runtime.{key} debe ser una cadena")
    return value


def _optional_list(options: Mapping[str, object], key: str) -> list[str] | None:
    value = options.get(key)
    if value is None:
        return None
    return list(_argv(value, f"runtime.{key}"))


__all__ = [
    "CUSTOM_NAME",
    "CUSTOM_PROTOCOLS",
    "HTTP_APP_PROTOCOLS",
    "CustomRuntime",
    "HttpApp",
]
