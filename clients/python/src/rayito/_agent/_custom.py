"""`CustomRuntime` y `HttpApp` (ADR-028, D2): el agente que escribes tú, con
cualquier framework (CrewAI, OpenAI Agents SDK, Google ADK, Claude Agent SDK,
Vercel AI SDK...), como un comando, una función de Python o una app HTTP.
Implementa los puertos `AgentRuntime`, `InstallableRuntime` y
`ServableRuntime` sobre `_host_runtime.HostBackedRuntime`, igual que Strands
y Hermes.

Exactamente uno de `command`, `entrypoint` o `app`. Con `package_manager`
(`pip` o `uv`: Python; `npm`, `pnpm` o `yarn`: Node) y `python_version` o
`node_version` el proyecto se instala con el gestor y la versión del
intérprete que pidas (`_install_plan.py`):

- `command`: tu `argv`, que recibe la petición por stdin y habla el protocolo
  v1 por stdout (`protocol="rayito-v1"`) o escribe texto (`protocol="text"`:
  el prompt llega tal cual por stdin y la sesión, el modelo y el
  razonamiento por `RAYITO_SESSION_ID`, `RAYITO_MODEL` y `RAYITO_REASONING`).
- `entrypoint="modulo:funcion"`: una función de Python que recibe la
  petición (`{"prompt", "session_id", "model", "reasoning"}`) y devuelve el
  texto, un iterable de trozos de texto o un `dict` con `text`. Con un gestor
  de Node es `ruta.ts#exportacion` y la ejecuta `node_bridge.mjs` con un
  `adapter`: `callable` (la misma función, en JavaScript), `langgraph` (un
  grafo de LangGraph.js o de deepagents-js) o `vercel-ai` (un agente del AI SDK).
- `app=HttpApp(...)`: tu servidor HTTP; Rayito lo arranca, espera a su ruta de
  salud y traduce `/invocations`. Con `protocol="json"` (una app con su
  propio contrato) también reenvía tus demás rutas; con `protocol="raw"` (un
  servidor MCP por Streamable HTTP, por ejemplo) sólo se expone, sin traducir.

Ese es el contrato mínimo de cualquier framework que no tiene su propio
adaptador (CrewAI, OpenAI Agents SDK, Google ADK, Claude Agent SDK...): una app
HTTP que contesta a su ruta de salud y a un POST con JSON, una función
`(petición) -> texto` o un comando que lee el prompt por stdin y escribe texto
por stdout. Los frameworks con adaptador (Strands, deepagents y LangGraph,
Hermes, OpenCode, y en JavaScript LangGraph.js, deepagents-js y el AI SDK) no
necesitan ninguno de los tres.

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
)
from rayito._agent._install_plan import (
    NODE_PACKAGE_MANAGERS,
    check_node_version,
    check_python_version,
    check_requirements_path,
    ecosystem,
    node_bin,
    node_dependency_steps,
    node_runtime_steps,
    python_dependency_steps,
    python_interpreter,
    python_manifest,
    python_runtime_steps,
)
from rayito._agent._recipes import (
    AGENT_APP_DIR,
    AGENT_INSTALL_DIR,
    NODE_BRIDGE_NAME,
    NODE_BRIDGE_PATH,
    node_bridge,
)
from rayito._agent._runtime import InstallTarget, TemplateStep, copy_step, run_step
from rayito._limits import (
    AGENT_DEFAULT_NODE_MAJOR,
    AGENT_NODE_BUILD_SHA256,
    AGENT_NODE_BUILD_VERSIONS,
    AGENT_PROTOCOL_VERSION,
    AGENT_SERVICE_HEALTH_PATH,
    AGENT_SERVICE_INVOKE_PATH,
    MODEL_CREDENTIAL_PLACEHOLDER,
    PORT_MAX,
    PORT_MIN,
)
from rayito.exceptions import InvalidArgumentException

CustomProtocol = Literal["rayito-v1", "text"]
CUSTOM_PROTOCOLS: Final[tuple[str, ...]] = ("rayito-v1", "text")
HttpAppProtocol = Literal["agentcore", "json", "openai-chat", "raw"]
HTTP_APP_PROTOCOLS: Final[tuple[str, ...]] = ("agentcore", "json", "openai-chat", "raw")
#: Los adaptadores del puente de Node que un `CustomRuntime` con un gestor
#: de Node puede elegir (el de Strands lo trae `StrandsRuntime`).
CustomAdapter = Literal["callable", "langgraph", "vercel-ai"]
CUSTOM_ADAPTERS: Final[tuple[str, ...]] = ("callable", "langgraph", "vercel-ai")
DEFAULT_CUSTOM_ADAPTER: Final = "callable"
CUSTOM_NAME: Final = "custom"
DEFAULT_CUSTOM_PROTOCOL: Final = "rayito-v1"
DEFAULT_HTTP_APP_PROTOCOL: Final = "agentcore"
HTTP_APP_KEYS: Final = frozenset(
    {
        "command",
        "port",
        "health_path",
        "invoke_path",
        "protocol",
        "request_field",
        "response_field",
        "port_env",
    }
)
#: El venv que crea `CustomRuntime(requirements=...)`.
CUSTOM_VENV_NAME: Final = "custom"
#: Carpeta de sesiones (bajo el estado del runtime) del historial que guarda
#: el puente de Node para los marcos sin memoria propia.
CUSTOM_SESSIONS_DIR_NAME: Final = "sessions"
#: El instalador de Python si `requirements` no trae `package_manager`.
DEFAULT_PYTHON_INSTALLER: Final = "pip"
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
#: `ruta/modulo.ts` o `ruta/modulo.ts#exportacion`.
_NODE_ENTRYPOINT_PATTERN: Final = re.compile(
    r"[A-Za-z0-9_][A-Za-z0-9_./-]*(#[A-Za-z_$][A-Za-z0-9_$]*)?"
)
_FIELD_PATTERN: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")
_ENV_NAME_PATTERN: Final = re.compile(r"[A-Z_][A-Z0-9_]*")
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
    AgentCore), `"json"` (una app con su propio contrato JSON),
    `"openai-chat"` (Chat Completions con streaming) o `"raw"` (sólo se
    expone).

    Cuándo cada uno: con `"agentcore"` u `"openai-chat"` el host sirve
    `/ping` y `/invocations` y traduce a tu `invoke_path`, así que
    `deployment.invoke()` funciona pero tus rutas propias no se alcanzan
    desde fuera; con `"json"` el host traduce igual (el prompt va en
    `request_field`, por defecto `prompt`, y el texto sale de
    `response_field`, o del primer campo de texto de `answer`, `output`,
    `text`, `response`, `result` o `content`) y además reenvía tus demás
    rutas, de modo que `invoke()` funciona y tu `/health` o tu `/chat`
    siguen alcanzables; con `"raw"` el despliegue reenvía tus rutas tal cual
    (un servidor MCP) y `invoke()` no existe: usa `deployment.url` y
    `deployment.headers` con tu cliente. `port_env` es la variable de
    entorno con la que se le dice a tu app en qué `port` escuchar (por
    ejemplo `PORT`). Tu `command` se
    ejecuta con el directorio del código copiado como directorio de trabajo y
    en su `PYTHONPATH`; con `requirements`, `python` y `pip` son los del venv
    (sin él, `python` es el intérprete de la imagen)."""

    command: Sequence[str]
    port: int
    health_path: str = AGENT_SERVICE_HEALTH_PATH
    invoke_path: str = AGENT_SERVICE_INVOKE_PATH
    protocol: HttpAppProtocol = DEFAULT_HTTP_APP_PROTOCOL
    request_field: str | None = None
    response_field: str | None = None
    port_env: str | None = None

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
        self._check_json_fields()
        if self.port_env is not None and not _ENV_NAME_PATTERN.fullmatch(str(self.port_env)):
            raise InvalidArgumentException(
                "HttpApp.port_env debe ser un nombre de variable de entorno (MAYÚSCULAS)"
            )

    def _check_json_fields(self) -> None:
        for label, value in (
            ("request_field", self.request_field),
            ("response_field", self.response_field),
        ):
            if value is None:
                continue
            if self.protocol != "json":
                raise InvalidArgumentException(f"HttpApp.{label} sólo se usa con protocol='json'")
            if not isinstance(value, str) or not _FIELD_PATTERN.fullmatch(value):
                raise InvalidArgumentException(
                    f"HttpApp.{label} debe ser un nombre de campo (con puntos para anidar)"
                )

    @property
    def hides_own_routes(self) -> bool:
        """Con `protocol="agentcore"` o `"openai-chat"` el despliegue sólo
        expone `/ping` y `/invocations` (el host traduce a tu `invoke_path`):
        tus rutas propias (`health_path`, `invoke_path` distintas de esas
        dos) no se alcanzan desde fuera. Con `protocol="json"` o `"raw"` se
        exponen tal cual."""
        return self.protocol not in ("raw", "json") and (
            self.health_path != AGENT_SERVICE_HEALTH_PATH
            or self.invoke_path != AGENT_SERVICE_INVOKE_PATH
        )

    @classmethod
    def from_mapping(cls, table: Mapping[str, object]) -> HttpApp:
        """La tabla `[runtime.app]` de `rayito-agent.toml`."""
        unknown = sorted(set(table) - HTTP_APP_KEYS)
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
        for key in ("request_field", "response_field", "port_env"):
            if getattr(self, key) is not None:
                table[key] = getattr(self, key)
        return table


class CustomRuntime(HostBackedRuntime):
    """`AgentRuntime` del agente que escribes tú (ver el módulo).

    `install` son órdenes de shell que se ejecutan al construir la imagen
    (como root, antes de copiar tu `source`), por ejemplo un `npm ci` o un
    `dnf install`. `requirements` es un fichero de dependencias de tu `source`
    (`app/<requirements>`: `requirements.txt` o, con `package_manager="uv"`,
    `pyproject.toml` o `uv.lock`): se instala en un venv propio, que pasa a
    ser el intérprete del host y de tu función. `python` es el intérprete con
    el que corre todo (por defecto `python3`).

    `package_manager` elige el gestor: `pip` o `uv` (Python) o `npm`, `pnpm` o
    `yarn` (Node: `source` debe llevar `package.json` y su lockfile, y la
    instalación es la congelada del gestor). `python_version` (`3.11`...) y
    `node_version` (`22`, `24`) piden un intérprete distinto del de la imagen:
    se baja un build con sha256 fijado. `adapter` (`callable`, `langgraph` o
    `vercel-ai`) dice cómo el puente de Node ejecuta un `entrypoint`
    `ruta.ts#exportacion`.

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
        package_manager: str | None = None,
        python_version: str | None = None,
        node_version: str | None = None,
        adapter: str | None = None,
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
        if app is not None and not isinstance(app, HttpApp):
            raise InvalidArgumentException("CustomRuntime.app debe ser un HttpApp")
        if python is not None and (not isinstance(python, str) or not python):
            raise InvalidArgumentException("CustomRuntime.python no puede ser vacío")
        kind = _toolchain(package_manager, python, python_version, node_version)
        _check_entrypoint(entrypoint, kind)
        _check_requirements(requirements, kind, package_manager)
        _check_adapter(adapter, entrypoint, kind)
        self.install = tuple(install)
        self.requirements = requirements
        self.command_argv = None if command is None else _argv(command, "CustomRuntime.command")
        self.entrypoint = entrypoint
        self.app = app
        self.protocol: CustomProtocol = protocol
        self.python = python
        self.package_manager = package_manager
        self.python_version = python_version
        self.node_version = node_version
        self.adapter = adapter or (
            DEFAULT_CUSTOM_ADAPTER if kind == "node" and entrypoint else None
        )

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
            package_manager=_optional_str(options, "package_manager"),
            python_version=_optional_str(options, "python_version"),
            node_version=_optional_str(options, "node_version"),
            adapter=_optional_str(options, "adapter"),
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
        for key in ("python", "package_manager", "python_version", "node_version"):
            if getattr(self, key) is not None:
                options[key] = getattr(self, key)
        if self.adapter is not None and self.adapter != DEFAULT_CUSTOM_ADAPTER:
            options["adapter"] = self.adapter
        return options

    @property
    def _ecosystem(self) -> str | None:
        manager = self.package_manager
        return None if manager is None else ecosystem(manager)

    @property
    def _node(self) -> bool:
        return self._ecosystem == "node"

    @property
    def _bridged(self) -> bool:
        """El `entrypoint` es de JavaScript y lo ejecuta el puente de Node."""
        return self._node and self.entrypoint is not None

    def venv_bin(self, install_dir: str = AGENT_INSTALL_DIR) -> str:
        """El `bin` del venv de `requirements`: va el primero en el `PATH` de
        tu app o tu comando, de modo que `python` y `pip` son los suyos."""
        return f"{install_dir}/{CUSTOM_VENV_NAME}/bin"

    def venv_python(self, install_dir: str = AGENT_INSTALL_DIR) -> str:
        return f"{self.venv_bin(install_dir)}/python"

    def _pinned_python(self) -> str:
        return python_interpreter(InstallTarget(), self.python_version)

    def _environment_options(self) -> dict[str, object]:
        """Dónde corre tu código: el directorio copiado (directorio de
        trabajo y `PYTHONPATH`) y, con `requirements`, el `bin` del venv; con
        un gestor de Node, el `bin` de Node."""
        options: dict[str, object] = {"app_dir": AGENT_APP_DIR}
        prepend: list[str] = []
        if self.requirements is not None:
            prepend.append(self.venv_bin())
        if self._node:
            prepend.append(node_bin(InstallTarget()))
        if prepend:
            options["path_prepend"] = prepend
        return options

    def host_python(self) -> str:
        if self.requirements is not None:
            return self.venv_python()
        if self.python_version is not None:
            return self._pinned_python()
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
            return HostSetup(driver="http-app", options=self._app_options(spec, workdir))
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
        if self._bridged:
            return HostSetup(driver="node-callable", config=self._bridge_config(spec, workdir))
        return HostSetup(
            driver="callable",
            options={"entrypoint": self.entrypoint, "app_dir": AGENT_APP_DIR, "workdir": workdir},
        )

    def _app_options(self, spec: AgentSpec, workdir: str) -> dict[str, object]:
        assert self.app is not None
        options: dict[str, object] = {
            "command": list(self.app.command),
            "port": self.app.port,
            "health_path": self.app.health_path,
            "invoke_path": self.app.invoke_path,
            "protocol": self.app.protocol,
            "workdir": workdir,
            "model": spec.model.id,
            **self._environment_options(),
        }
        for key in ("request_field", "response_field", "port_env"):
            if getattr(self.app, key) is not None:
                options[key] = getattr(self.app, key)
        return options

    def _bridge_config(self, spec: AgentSpec, workdir: str) -> dict[str, object]:
        return {
            "v": AGENT_PROTOCOL_VERSION,
            "bridge": NODE_BRIDGE_PATH,
            "adapter": self.adapter,
            "entrypoint": self.entrypoint,
            "model": spec.model.id,
            "instructions": spec.instructions,
            "workdir": workdir,
            "app_dir": AGENT_APP_DIR,
            "state_dir": self.state_dir,
            "sessions_dir": f"{self.state_dir}/{CUSTOM_SESSIONS_DIR_NAME}",
        }

    def install_steps(self, target: InstallTarget) -> Sequence[TemplateStep]:
        """Tus órdenes de `install` y el toolchain que pidas: con
        `requirements`, el venv con tus dependencias; con un gestor de Node,
        Node y la instalación congelada de tu lockfile (y el puente si hay
        `entrypoint`)."""
        steps: list[TemplateStep] = [run_step(command) for command in self.install]
        if self._node:
            steps += node_runtime_steps(target, self.node_version)
            steps += node_dependency_steps(target, str(self.package_manager))
            if self._bridged:
                steps.append(copy_step(NODE_BRIDGE_NAME, NODE_BRIDGE_PATH))
            return steps
        steps += python_runtime_steps(target, self.python_version)
        if self.requirements is not None:
            steps += self._python_dependency_steps(target)
        return steps

    def _python_dependency_steps(self, target: InstallTarget) -> list[TemplateStep]:
        requirements = str(self.requirements)
        manifest = python_manifest(requirements)
        return python_dependency_steps(
            target,
            installer=self.package_manager or DEFAULT_PYTHON_INSTALLER,
            manifest=manifest,
            path=requirements if manifest == "requirements" else None,
            python=self.python or python_interpreter(target, self.python_version),
            venv_dir=f"{target.install_dir}/{CUSTOM_VENV_NAME}",
            slug=CUSTOM_VENV_NAME,
        )

    def context_files(self) -> Mapping[str, bytes]:
        """El puente de Node si hay un `entrypoint` de JavaScript; si no,
        ninguno: tu código y tus requisitos van en `source`."""
        return {NODE_BRIDGE_NAME: node_bridge()} if self._bridged else {}

    def manifest_entry(self) -> Mapping[str, object]:
        """Qué se instaló: el driver, el hash de tus órdenes y tus
        requisitos, y el gestor y las versiones de intérprete que pediste."""
        entry: dict[str, object] = {
            "driver": self.host_setup_driver(),
            "install_sha256": hashlib.sha256("\n".join(self.install).encode("utf-8")).hexdigest(),
            "requirements": self.requirements,
        }
        if self.package_manager is not None:
            entry["package_manager"] = self.package_manager
        if self.python_version is not None:
            entry["python_version"] = self.python_version
        if self._node:
            major = self.node_version or AGENT_DEFAULT_NODE_MAJOR
            entry["node_version"] = AGENT_NODE_BUILD_VERSIONS[major]
            entry["node_sha256"] = AGENT_NODE_BUILD_SHA256[major]
        return entry

    def host_setup_driver(self) -> str:
        if self.app is not None:
            return "http-app"
        if self.command_argv is not None:
            return "command"
        return "node-callable" if self._bridged else "callable"

    def smoke_checks(self) -> Sequence[str]:
        """Node o el CPython fijado, si los pediste; si no, ninguna: no
        sabemos qué instalas."""
        if self._node:
            return ("su user -c 'node --version'",)
        if self.python_version is not None:
            return (f"su user -c '{self._pinned_python()} --version'",)
        return ()

    def prefetch_paths(self) -> Sequence[str]:
        return ()


def _toolchain(
    package_manager: str | None,
    python: str | None,
    python_version: str | None,
    node_version: str | None,
) -> str | None:
    """El ecosistema (`python`, `node` o `None`) que piden las opciones del
    toolchain, validando que no se contradigan."""
    if package_manager is not None and not isinstance(package_manager, str):
        raise InvalidArgumentException("CustomRuntime.package_manager debe ser una cadena")
    kind = None if package_manager is None else ecosystem(package_manager)
    if python_version is not None:
        if kind == "node":
            raise InvalidArgumentException(
                "CustomRuntime.python_version no se combina con un gestor de Node"
            )
        if python is not None:
            raise InvalidArgumentException(
                "CustomRuntime.python y python_version no se combinan: usa uno"
            )
        check_python_version(python_version)
    if node_version is not None:
        if kind != "node":
            raise InvalidArgumentException(
                "CustomRuntime.node_version necesita package_manager en "
                f"{list(NODE_PACKAGE_MANAGERS)}"
            )
        check_node_version(node_version)
    return kind


def _check_entrypoint(entrypoint: str | None, kind: str | None) -> None:
    if entrypoint is None:
        return
    pattern = _NODE_ENTRYPOINT_PATTERN if kind == "node" else _PYTHON_ENTRYPOINT_PATTERN
    if not pattern.fullmatch(entrypoint) or ".." in entrypoint.split("/"):
        shape = "'ruta/modulo.ts#exportacion'" if kind == "node" else "'paquete.modulo:funcion'"
        raise InvalidArgumentException(f"CustomRuntime.entrypoint debe tener la forma {shape}")


def _check_requirements(requirements: str | None, kind: str | None, manager: str | None) -> None:
    if requirements is None:
        return
    if kind == "node":
        raise InvalidArgumentException(
            "CustomRuntime.requirements es de Python: con un gestor de Node las "
            "dependencias son las de package.json"
        )
    check_requirements_path("CustomRuntime", requirements, manager)


def _check_adapter(adapter: str | None, entrypoint: str | None, kind: str | None) -> None:
    if adapter is None:
        return
    if adapter not in CUSTOM_ADAPTERS:
        raise InvalidArgumentException(
            f"CustomRuntime.adapter debe ser uno de {list(CUSTOM_ADAPTERS)}"
        )
    if kind != "node" or entrypoint is None:
        raise InvalidArgumentException(
            "CustomRuntime.adapter sólo se usa con entrypoint y un gestor de Node"
        )


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
