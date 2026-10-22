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
from rayito._agent._detect_report import DetectionReport, DetectOverrides
from rayito._agent._detection_rules import DETECTION_RULES
from rayito._agent._project_domain import (
    _GATEWAY_PRESETS,
    ENTRYPOINT_KEY,
    GRAPH_KEY,
    GRAPH_RUNTIME_KIND,
    AgentProjectException,
    require_model_id,
)
from rayito._limits import AGENT_PROJECT_SCHEMA, AGENT_SECRET_NAME_TEMPLATE
from rayito._secrets import DEFAULT_SECRET_PREFIX

_MODEL_PATH: Final = "model"
_REGION_PATH: Final = "deploy.region"
_REGION_KEY: Final = "region"
_MODELS_KEY: Final = "models"
_UPSTREAM_KEY: Final = "upstream"
#: Cómo se da el `upstream` que la detección no puede saber.
UPSTREAM_HINT: Final = "pásalo con --model-base-url o "
_KIND_KEY: Final = "kind"
_ENTRYPOINT_KEY: Final = "entrypoint"
UV_MANAGER: Final = "uv"
UV_LOCKFILE: Final = "uv.lock"
PYTHON_LANGUAGE: Final = "python"
TYPESCRIPT_LANGUAGE: Final = "typescript"
ENTRY_EXPORT_SEPARATOR: Final = "#"
#: El secreto de la pasarela detectada, relativo al prefijo `rayito/` con el
#: que la pasarela lo lee (`SecretCache`) y `--create-secret` lo crea
#: (`SecretStore`): en Secrets Manager queda en
#: `AGENT_SECRET_NAME_TEMPLATE` (`rayito/agents/<proyecto>/<proveedor>`), sin
#: el prefijo duplicado de 0.16.
GATEWAY_SECRET_NAME_TEMPLATE: Final = AGENT_SECRET_NAME_TEMPLATE.removeprefix(DEFAULT_SECRET_PREFIX)


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
    preset: str,
    name: str,
    report: DetectionReport,
    region: str | None,
    base_url: str | None = None,
) -> dict[str, object]:
    """La tabla `[gateways.<preset>]` del proveedor detectado, con el secreto
    de la convención de nombre y lo que el preset exige (región, modelos, la
    URL de `--model-base-url` como `upstream`); lo opcional se deja fuera."""
    table: dict[str, object] = {
        "preset": preset,
        "secret": GATEWAY_SECRET_NAME_TEMPLATE.format(project=name, provider=preset),
    }
    spec = _GATEWAY_PRESETS[preset]
    if base_url is not None and _UPSTREAM_KEY not in spec.keys:
        raise _error(
            f"gateways.{preset}.{_UPSTREAM_KEY}",
            "invalid_value",
            f"el preset {preset!r} no admite --model-base-url: usa openai-compatible o litellm",
        )
    for key in spec.keys:
        if key == _REGION_KEY:
            table[key] = _required_region(region)
        elif key == _MODELS_KEY:
            table[key] = list(report.models)
        elif key == _UPSTREAM_KEY and base_url is not None:
            table[key] = base_url
        elif key not in spec.required:
            continue
        else:
            hint = UPSTREAM_HINT if key == _UPSTREAM_KEY else ""
            raise _error(
                f"gateways.{preset}.{key}",
                "missing_key",
                f"el preset {preset!r} necesita {key} y no se puede detectar: {hint}"
                "escríbelo en rayito-agent.toml (rayito agent detect --write)",
            )
    return table


def _model_tables(
    report: DetectionReport,
    name: str,
    region: str | None,
    bridge: Mapping[str, Any],
    runtime: Mapping[str, object],
    base_url: str | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    """`[model]` y `[gateways.<preset>]` de lo detectado. Sin literal de
    modelo el id sólo es obligatorio donde Rayito lo usa (`require_model_id`);
    si no, `[model]` lleva los nombres de variable de `model_envs`."""
    if not report.providers:
        raise _error(
            _MODEL_PATH,
            "not_detected",
            "no se detectó el proveedor del modelo: escribe [model] y [gateways] en "
            "rayito-agent.toml (rayito agent detect --write)",
        )
    preset = report.providers[0].preset
    model_id = report.models[0] if report.models else None
    require_model_id(
        model_id,
        runtime=str(runtime.get(_KIND_KEY, report.kind)),
        entrypoint=runtime.get(_ENTRYPOINT_KEY) is not None or "app" in runtime,
        gateway_preset=preset,
        detected=True,
    )
    gateway = _gateway_table(preset, name, report, region, base_url)
    model: dict[str, object] = {"provider": bridge["modelProviders"][preset]}
    if model_id is not None:
        model["id"] = model_id
    model["gateway"] = preset
    if _REGION_KEY in gateway:
        model[_REGION_KEY] = gateway[_REGION_KEY]
    model.update(report.model_envs)
    return model, {preset: gateway}


def detected_document(
    document: Mapping[str, object],
    report: DetectionReport,
    *,
    name: str,
    region: str | None,
    rules: Mapping[str, Any] = DETECTION_RULES,
    overrides: DetectOverrides | None = None,
) -> dict[str, object]:
    """El documento de proyecto con lo detectado donde el fichero calla.
    `overrides.model_base_url` es el `upstream` de la pasarela detectada."""
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
        base_url = None if overrides is None else overrides.model_base_url
        model, gateways = _model_tables(report, project_name, region, bridge, runtime, base_url)
        filled["model"] = model
        filled["gateways"] = {**_as_table(filled.get("gateways")), **gateways}
    return filled
