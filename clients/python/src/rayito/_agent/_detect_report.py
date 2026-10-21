"""Los valores de la detección de un proyecto de agente (ADR-029):
`DetectOverrides` (lo que el usuario fuerza) y `DetectionReport` (lo que se
detectó y de qué ficheros). Valores puros: ninguno guarda el contenido de un
fichero, sólo rutas relativas al proyecto y el identificador de la regla que
acertó. `DetectionReport.to_dict()` es la forma JSON que comparten los dos SDK
(`testdata/agent-detection/`, esquema `rayito.agent-detection/1`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final

from rayito._limits import AGENT_DETECTION_SCHEMA

#: Los protocolos que el informe puede nombrar: `framework` lo sirve el driver
#: del framework; el resto es una app HTTP (`agentcore`, `json`, `raw`).
PROTOCOL_FRAMEWORK: Final = "framework"
APP_PROTOCOLS: Final[tuple[str, ...]] = ("agentcore", "json", "raw")
#: Cómo se obtiene el objeto del agente: ya construido o con una fábrica.
ENTRY_OBJECT: Final = "object"
ENTRY_FACTORY: Final = "factory"


@dataclass(frozen=True)
class DetectOverrides:
    """Lo que el usuario fuerza por encima de la detección (`--framework`,
    `--language`, `--entry`, `--protocol`, `--model-id`, `--model-provider`,
    `--model-base-url`). `entrypoint="http"` sirve la app HTTP del proyecto
    en vez del framework del agente. `model_provider` es un preset de
    pasarela (`openai-compatible`, `litellm`...) y `model_base_url` la URL
    `https` de la pasarela de LLM, que pasa a ser su `upstream`."""

    framework: str | None = None
    language: str | None = None
    entrypoint: str | None = None
    protocol: str | None = None
    model_id: str | None = None
    model_provider: str | None = None
    model_base_url: str | None = None


@dataclass(frozen=True)
class DetectionEvidence:
    """Un fichero (ruta relativa al proyecto) y la regla que lo reconoció;
    nunca su contenido."""

    path: str | None
    rule: str

    def to_dict(self) -> dict[str, object]:
        return {"path": self.path, "rule": self.rule}


@dataclass(frozen=True)
class DetectionWarning:
    """Un aviso con código estable y el texto en español."""

    code: str
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True)
class DetectionProvider:
    """Un proveedor de modelo detectado: su preset de pasarela y la evidencia."""

    preset: str
    evidence: tuple[DetectionEvidence, ...]

    def to_dict(self) -> dict[str, object]:
        return {"preset": self.preset, "evidence": [item.to_dict() for item in self.evidence]}


@dataclass(frozen=True)
class DetectionApp:
    """La app HTTP del proyecto: el comando que la arranca, el puerto en el
    que escucha dentro del sandbox (y la variable de entorno con la que se le
    indica, si la lee de ella), sus rutas de salud e invocación y, con el
    protocolo `json`, los campos de la petición y la respuesta."""

    framework: str
    command: tuple[str, ...]
    port: int
    port_env: str | None
    health_path: str
    invoke_path: str | None
    request_field: str | None
    response_field: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "framework": self.framework,
            "command": list(self.command),
            "port": self.port,
            "port_env": self.port_env,
            "health_path": self.health_path,
            "invoke_path": self.invoke_path,
            "request_field": self.request_field,
            "response_field": self.response_field,
        }


@dataclass(frozen=True)
class DetectionInstall:
    """Cómo se instalan las dependencias: el gestor, el fichero de bloqueo (o
    los requisitos), si la instalación es reproducible, si hay un script de
    build y la versión de intérprete elegida entre las compilaciones fijadas."""

    manager: str | None
    lockfile: str | None
    requirements: str | None
    require_hashes: bool
    reproducible: bool
    build_script: bool
    python_version: str | None
    node_version: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "manager": self.manager,
            "lockfile": self.lockfile,
            "requirements": self.requirements,
            "require_hashes": self.require_hashes,
            "reproducible": self.reproducible,
            "build_script": self.build_script,
            "python_version": self.python_version,
            "node_version": self.node_version,
        }


@dataclass(frozen=True)
class DetectionAlternative:
    """Otra forma de servir el proyecto que la detección no eligió."""

    framework: str
    kind: str
    language: str | None
    entrypoint: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "framework": self.framework,
            "kind": self.kind,
            "language": self.language,
            "entrypoint": self.entrypoint,
        }


@dataclass(frozen=True)
class DetectionReport:
    """Lo que se detectó de un proyecto sin ejecutar su código: `language`,
    `framework` (la regla que acertó), `kind` (el `[runtime] kind` que lo
    sirve), el `entrypoint` (`modulo:atributo` en Python, `ruta#export` en
    TypeScript) y si es un objeto o una fábrica, el `protocol`, las `options`
    propias del runtime, la `app` HTTP, el plan de instalación, los
    proveedores de modelo y los literales de modelo vistos, la evidencia, los
    avisos y las alternativas. `model_envs` (`id_env`, `base_url_env`,
    `api_key_env`) son los nombres de variable con los que el código elige su
    modelo cuando no hay literal; sólo nombres, nunca valores."""

    language: str | None
    framework: str
    kind: str
    entrypoint: str | None
    entrypoint_kind: str | None
    protocol: str
    options: Mapping[str, str] = field(default_factory=dict)
    app: DetectionApp | None = None
    install: DetectionInstall | None = None
    providers: tuple[DetectionProvider, ...] = ()
    models: tuple[str, ...] = ()
    evidence: tuple[DetectionEvidence, ...] = ()
    warnings: tuple[DetectionWarning, ...] = ()
    alternatives: tuple[DetectionAlternative, ...] = ()
    model_envs: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "options", MappingProxyType(dict(self.options)))
        object.__setattr__(self, "model_envs", MappingProxyType(dict(self.model_envs)))

    def to_dict(self) -> dict[str, object]:
        """La forma JSON compartida con TypeScript; `model_envs` sólo si hay."""
        document: dict[str, object] = {
            "schema": AGENT_DETECTION_SCHEMA,
            "language": self.language,
            "framework": self.framework,
            "kind": self.kind,
            "entrypoint": self.entrypoint,
            "entrypoint_kind": self.entrypoint_kind,
            "protocol": self.protocol,
            "options": dict(self.options),
            "app": None if self.app is None else self.app.to_dict(),
            "install": None if self.install is None else self.install.to_dict(),
            "providers": [provider.to_dict() for provider in self.providers],
            "models": list(self.models),
            "evidence": [item.to_dict() for item in self.evidence],
            "warnings": [item.to_dict() for item in self.warnings],
            "alternatives": [item.to_dict() for item in self.alternatives],
        }
        if self.model_envs:
            document["model_envs"] = dict(self.model_envs)
        return document

    def runtime_table(self) -> dict[str, object]:
        """La tabla `[runtime]` de `rayito-agent.toml` que describe este
        informe: `kind`, `framework`, `language`, las opciones del runtime y
        el `entrypoint`, o `[runtime.app]` si el proyecto se sirve como app
        HTTP (que ya lleva su comando)."""
        table: dict[str, object] = {"kind": self.kind, "framework": self.framework}
        if self.language is not None:
            table["language"] = self.language
        if self.app is not None:
            table["app"] = _app_table(self.app, self.protocol)
        elif self.entrypoint is not None:
            table["entrypoint"] = self.entrypoint
        table.update(self.options)
        return table


def _app_table(app: DetectionApp, protocol: str) -> dict[str, object]:
    table: dict[str, object] = {
        "command": list(app.command),
        "port": app.port,
        "health_path": app.health_path,
        "protocol": protocol,
    }
    if app.port_env is not None:
        table["port_env"] = app.port_env
    if app.invoke_path is not None:
        table["invoke_path"] = app.invoke_path
    if app.request_field is not None:
        table["request_field"] = app.request_field
    if app.response_field is not None:
        table["response_field"] = app.response_field
    return table


__all__ = [
    "APP_PROTOCOLS",
    "ENTRY_FACTORY",
    "ENTRY_OBJECT",
    "PROTOCOL_FRAMEWORK",
    "DetectOverrides",
    "DetectionAlternative",
    "DetectionApp",
    "DetectionEvidence",
    "DetectionInstall",
    "DetectionProvider",
    "DetectionReport",
    "DetectionWarning",
]
