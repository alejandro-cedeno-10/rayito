"""Del informe de detección a un `AgentProject` (ADR-029, D1 y D7).

`detected_document` completa un documento de proyecto (el fichero, si lo hay)
con lo que la detección sabe y el fichero no dice: el runtime (con el gestor
de paquetes, los requisitos y las versiones del plan de instalación, y el
adaptador de Node de los frameworks de JavaScript), el código (`source`), el
modelo y su pasarela. Lo que el fichero ya dice gana, clave por clave, y lo
que no se puede saber sin adivinar (el proveedor, el id del modelo, la región)
es un error que nombra la clave que lo resuelve. Puro: no lee el disco ni
llama a AWS. `rayito._agent._detect_project` y `agent/detect-project.ts` leen
las mismas reglas (`bridge` de `agent-detection.json`) y los mismos vectores
(`testdata/agent-samples/manifest.json`).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from rayito._agent._detect_domain import fill_runtime
from rayito._agent._detect_report import DetectionReport
from rayito._agent._detection_rules import DETECTION_RULES
from rayito._agent._project_domain import (
    _GATEWAY_PRESETS,
    ENTRYPOINT_KEY,
    GRAPH_KEY,
    GRAPH_RUNTIME_KIND,
    AgentProjectException,
)
from rayito._limits import AGENT_PROJECT_SCHEMA, AGENT_SECRET_NAME_TEMPLATE

_MODEL_PATH: Final = "model"
_MODEL_ID_PATH: Final = "model.id"
_REGION_PATH: Final = "deploy.region"
_REGION_KEY: Final = "region"
_MODELS_KEY: Final = "models"
UV_MANAGER: Final = "uv"
UV_LOCKFILE: Final = "uv.lock"
PYTHON_LANGUAGE: Final = "python"
TYPESCRIPT_LANGUAGE: Final = "typescript"
ENTRY_EXPORT_SEPARATOR: Final = "#"


def _error(path: str, code: str, text: str) -> AgentProjectException:
    return AgentProjectException(f"{path}: {text}", path=path, code=code)


def _as_table(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


def _complete_runtime(
    runtime: dict[str, object],
    declared: Mapping[str, object],
    report: DetectionReport,
    bridge: Mapping[str, Any],
) -> None:
    """Rellena, sólo donde el fichero (`declared`) calla, las claves del
    runtime que salen del plan de instalación y del adaptador de Node."""
    node_runtime = bridge["nodeRuntimes"].get(report.framework)
    if report.language == TYPESCRIPT_LANGUAGE and node_runtime is not None:
        if "kind" not in declared:
            runtime["kind"] = node_runtime["kind"]
        runtime.setdefault("adapter", node_runtime["adapter"])
    kind = str(runtime.get("kind", report.kind))
    _keep_graph(runtime, declared, kind)
    entrypoint = runtime.get("entrypoint")
    if (
        isinstance(entrypoint, str)
        and kind in bridge["modulePathKinds"].get(report.language, ())
        and ENTRY_EXPORT_SEPARATOR in entrypoint
    ):
        runtime["entrypoint"] = entrypoint.partition(ENTRY_EXPORT_SEPARATOR)[0]
    install = report.install
    if install is None or kind not in bridge["installKinds"]:
        return
    runtime.setdefault("package_manager", install.manager)
    if report.language == PYTHON_LANGUAGE:
        requirements = install.requirements or (
            install.lockfile if install.lockfile == UV_LOCKFILE else bridge["defaultUvManifest"]
        )
        runtime.setdefault("requirements", requirements)
        if install.python_version is not None:
            runtime.setdefault("python_version", install.python_version)
    elif install.node_version is not None:
        runtime.setdefault("node_version", install.node_version)


def _keep_graph(runtime: dict[str, object], declared: Mapping[str, object], kind: str) -> None:
    """Con un grafo elegido (`--graph` o `[runtime] graph`) y sin un
    `entrypoint` del fichero, el runtime `langgraph` guarda el nombre del grafo
    y no el `entrypoint` que la detección resolvió; otro runtime (un proyecto
    de LangGraph.js se sirve como `custom`) guarda el `entrypoint` resuelto y
    no el nombre, salvo que el fichero lo escriba."""
    if GRAPH_KEY not in runtime or ENTRYPOINT_KEY in declared:
        return
    if kind == GRAPH_RUNTIME_KIND:
        runtime.pop(ENTRYPOINT_KEY, None)
    elif GRAPH_KEY not in declared:
        runtime.pop(GRAPH_KEY)


def _required_region(region: str | None) -> str:
    if region is None:
        raise _error(
            _REGION_PATH,
            "missing_key",
            "no hay región de AWS para la pasarela del modelo: pasa --region, exporta "
            "AWS_REGION o escribe [deploy] region en rayito-agent.toml",
        )
    return region


def _gateway_table(
    preset: str, name: str, report: DetectionReport, region: str | None
) -> dict[str, object]:
    """La tabla `[gateways.<preset>]` del proveedor detectado, con el secreto
    de la convención de nombre y lo que el preset exige (región, modelos)."""
    table: dict[str, object] = {
        "preset": preset,
        "secret": AGENT_SECRET_NAME_TEMPLATE.format(project=name, provider=preset),
    }
    for key in _GATEWAY_PRESETS[preset].keys:
        if key == _REGION_KEY:
            table[key] = _required_region(region)
        elif key == _MODELS_KEY:
            table[key] = list(report.models)
        else:
            raise _error(
                f"gateways.{preset}.{key}",
                "missing_key",
                f"el preset {preset!r} necesita {key} y no se puede detectar: "
                "escríbelo en rayito-agent.toml (rayito agent detect --write)",
            )
    return table


def _model_tables(
    report: DetectionReport, name: str, region: str | None, bridge: Mapping[str, Any]
) -> tuple[dict[str, object], dict[str, object]]:
    if not report.providers:
        raise _error(
            _MODEL_PATH,
            "not_detected",
            "no se detectó el proveedor del modelo: escribe [model] y [gateways] en "
            "rayito-agent.toml (rayito agent detect --write)",
        )
    preset = report.providers[0].preset
    if not report.models:
        raise _error(
            _MODEL_ID_PATH,
            "not_detected",
            "no se encontró el id del modelo en el código: escríbelo como literal o define "
            "[model] id en rayito-agent.toml (rayito agent detect --write)",
        )
    gateway = _gateway_table(preset, name, report, region)
    model: dict[str, object] = {
        "provider": bridge["modelProviders"][preset],
        "id": report.models[0],
        "gateway": preset,
    }
    if _REGION_KEY in gateway:
        model[_REGION_KEY] = gateway[_REGION_KEY]
    return model, {preset: gateway}


def detected_document(
    document: Mapping[str, object],
    report: DetectionReport,
    *,
    name: str,
    region: str | None,
    rules: Mapping[str, Any] = DETECTION_RULES,
) -> dict[str, object]:
    """El documento de proyecto con lo detectado donde el fichero calla."""
    bridge = rules["bridge"]
    filled = fill_runtime(document, report)
    filled.setdefault("schema", AGENT_PROJECT_SCHEMA)
    filled.setdefault("name", name)
    runtime = _as_table(filled.get("runtime"))
    _complete_runtime(runtime, _as_table(document.get("runtime")), report, bridge)
    filled["runtime"] = runtime
    filled.setdefault("source", {"path": bridge["defaultSource"]})
    if "model" not in filled:
        project_name = str(filled["name"])
        model, gateways = _model_tables(report, project_name, region, bridge)
        filled["model"] = model
        filled["gateways"] = {**_as_table(filled.get("gateways")), **gateways}
    return filled
