"""Piezas que comparten los drivers de frameworks del host (`strands`,
`strands-ts`, `hermes`, `callable` y `command`): leer la configuración que
escribió el SDK, localizar el código del usuario, clasificar una excepción
sin mirar su mensaje y decidir un permiso. Sólo biblioteca estándar y nada
de `rayito`."""

from __future__ import annotations

import fnmatch
import importlib
import json
import re
import sys
from collections.abc import Mapping
from typing import Any, Final

from ..protocol import AgentFailure, new_id

#: Prefijo de los ids de sesión que el SDK acepta (`rda_` y 32 hex).
SESSION_ID_PREFIX: Final = "rda_"
SESSION_ID_PATTERN: Final = re.compile(r"rda_[0-9a-f]{32}")
#: `pkg.mod:attr`, importable desde el código del usuario.
ENTRYPOINT_PATTERN: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*")
#: Contenido con el que se deniega una herramienta: el modelo lo ve y puede
#: seguir sin ella.
DENIED_TOOL_MESSAGE: Final = "Permiso denegado por la configuración del agente."
#: Bibliotecas de modelo y de transporte cuyas excepciones son del modelo o
#: de la pasarela (`model_error`); cualquier otra es `runtime_error`.
MODEL_ERROR_MODULES: Final = ("anthropic", "boto3", "botocore", "google", "httpx", "openai")
#: Profundidad máxima de la cadena `__cause__`/`__context__` que se recorre.
MAX_CAUSE_DEPTH: Final = 6
#: Longitud máxima de un `detail_code`.
MAX_DETAIL_CODE_LENGTH: Final = 64
_DETAIL_CODE_PATTERN: Final = re.compile(r"[^a-z0-9_]")


def load_config(options: Mapping[str, Any]) -> Mapping[str, Any]:
    """La configuración del driver: con `config_path` es el JSON que escribió
    el SDK en esa ruta; sin él, `options` ya es la configuración."""
    path = options.get("config_path")
    if path is None:
        return options
    try:
        with open(str(path), encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, ValueError):
        raise AgentFailure("protocol_error", "invalid_input") from None
    if not isinstance(loaded, dict):
        raise AgentFailure("protocol_error", "invalid_input")
    return loaded


def new_session_id() -> str:
    return new_id(SESSION_ID_PREFIX)


def resolve_session_id(session_id: str | None) -> str:
    """El id de la petición, o uno nuevo si no hay; un id con otra forma es
    `invalid_session_id`."""
    if session_id is None:
        return new_session_id()
    if not SESSION_ID_PATTERN.fullmatch(session_id):
        raise AgentFailure("runtime_error", "invalid_session_id")
    return session_id


def add_import_roots(*directories: object) -> None:
    """Pone el código del usuario al principio de `sys.path`, en el orden
    dado y sin repetir."""
    for directory in reversed([str(item) for item in directories if item]):
        if directory not in sys.path:
            sys.path.insert(0, directory)


def load_attribute(entrypoint: str) -> Any:
    """El atributo de `pkg.mod:attr`; un fallo de importación es
    `entrypoint_not_found`."""
    if not ENTRYPOINT_PATTERN.fullmatch(entrypoint):
        raise AgentFailure("runtime_error", "invalid_entrypoint")
    module_name, attr = entrypoint.split(":", 1)
    try:
        return getattr(importlib.import_module(module_name), attr)
    except (ImportError, AttributeError):
        raise AgentFailure("runtime_error", "entrypoint_not_found") from None


def detail_code_of(exc: BaseException) -> str:
    """El nombre de la clase de `exc` como `detail_code`: nunca su mensaje."""
    return _DETAIL_CODE_PATTERN.sub("_", type(exc).__name__.lower())[:MAX_DETAIL_CODE_LENGTH]


def sanitize_detail_code(value: object, fallback: str) -> str:
    """Un `detail_code` aceptable a partir de un texto externo."""
    if not isinstance(value, str):
        return fallback
    cleaned = _DETAIL_CODE_PATTERN.sub("_", value.lower())[:MAX_DETAIL_CODE_LENGTH]
    return cleaned or fallback


def classify_chain(
    exc: BaseException, model_exception_names: frozenset[str] = frozenset()
) -> tuple[str, str]:
    """`model_error` si `exc` o alguna causa suya viene de una biblioteca de
    modelo o de transporte (o es una excepción de modelo del framework, por
    nombre de clase); si no, `runtime_error`. El detalle es el nombre de la
    clase de la excepción exterior."""
    current: BaseException | None = exc
    for _ in range(MAX_CAUSE_DEPTH):
        if current is None:
            break
        root = type(current).__module__.split(".", 1)[0]
        if root in MODEL_ERROR_MODULES or type(current).__name__ in model_exception_names:
            return "model_error", detail_code_of(exc)
        current = current.__cause__ or current.__context__
    return "runtime_error", detail_code_of(exc)


def decide(rules: Mapping[str, Any], tool: str, arguments: Mapping[str, Any]) -> str:
    """`allow` o `deny` para una llamada. Una herramienta sin regla usa
    `default`; con patrones gana el último que casa con el comando
    (`command`), y si ninguno casa, `default`."""
    default = str(rules.get("default", "allow"))
    rule = rules.get("tools", {}).get(tool)
    if rule is None:
        return default
    if isinstance(rule, str):
        return rule
    command = str(arguments.get("command", ""))
    decision = default
    for pattern, action in rule.items():
        if fnmatch.fnmatchcase(command, pattern):
            decision = str(action)
    return decision
