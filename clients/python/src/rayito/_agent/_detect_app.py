"""Detalles de la app HTTP de un proyecto (ADR-029): su comando de arranque,
el puerto, las rutas de salud e invocación, el protocolo y los campos de la
petición. Todo sale de patrones de línea sobre el fichero de la app y de
`package.json`; nunca se ejecuta nada. Las reglas son datos de
`agent-detection.json`.

Con `--protocol langgraph-api` (ADR-033 D5) un proyecto con `langgraph.json`
se sirve con su propio servidor (`langgraph dev`, el launcher `langgraph-dev`)
como app `custom`, y `assistant_id` es el grafo elegido de `graphs`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from rayito._agent._detect_entry import (
    CURRENT_DIRECTORY,
    PYTHON_ATTRIBUTE_SEPARATOR,
    EntryChoice,
)
from rayito._agent._detect_report import APP_PROTOCOLS, DetectionApp
from rayito._agent._detect_scan import (
    PACKAGE_JSON,
    ProjectIndex,
    WarningSink,
    compiled_regex,
    render,
)
from rayito._agent._project_domain import AgentProjectException
from rayito._limits import (
    AGENT_APP_INTERNAL_PORT,
    AGENT_APP_PORT_ENV,
    AGENT_LANGGRAPH_HEALTH_PATH,
    AGENT_SERVICE_PORT,
)

PROTOCOL_AGENTCORE: Final = "agentcore"
PROTOCOL_JSON: Final = "json"
PROTOCOL_RAW: Final = "raw"
PROTOCOL_LANGGRAPH_API: Final = "langgraph-api"
#: El framework de `langgraph.json`, el launcher de `langgraph dev`, el
#: paquete de Python que lo trae y el `kind` con el que se sirve.
LANGGRAPH_JSON_FRAMEWORK: Final = "langgraph-json"
LANGGRAPH_DEV_LAUNCHER: Final = "langgraph-dev"
LANGGRAPH_CLI_DEPENDENCY: Final = "langgraph-cli"
CUSTOM_KIND: Final = "custom"
METHOD_GET: Final = "GET"
METHOD_POST: Final = "POST"
NODE_MAIN_COMMAND: Final = "node"
RUN_COMMAND: Final = "run"
#: El gestor de Node -> el ejecutable con el que se lanza un script.
SCRIPT_RUNNERS: Final[Mapping[str, str]] = {
    "npm": "npm",
    "pnpm": "pnpm",
    "yarn": "yarn",
    "yarn-berry": "yarn",
}
DEFAULT_RUNNER: Final = "npm"
_APP_COMMAND_PATH: Final = "runtime.app.command"
_INVOKE_PATH: Final = "runtime.app.invoke_path"
_PROTOCOL_PATH: Final = "runtime.app.protocol"
_ENTRY_PATH: Final = "runtime.entrypoint"


def _routes(
    text: str, language: str, rules: Mapping[str, Any], fixed: Sequence[Sequence[str]]
) -> list[tuple[str, str]]:
    routes: list[tuple[str, str]] = [(method, path) for method, path in fixed]
    for rule in rules["app"]["routes"][language]:
        for match in compiled_regex(rule["pattern"]).finditer(text):
            method = rule.get("method") or match.group(rule["methodGroup"]).upper()
            route = (method, match.group(rule["pathGroup"]))
            if route not in routes:
                routes.append(route)
    return routes


def _first_candidate(candidates: Sequence[str], available: Sequence[str]) -> str | None:
    return next((candidate for candidate in candidates if candidate in available), None)


def _health_path(get_paths: Sequence[str], rules: Mapping[str, Any], sink: WarningSink) -> str:
    candidates = rules["app"]["healthCandidates"]
    found = _first_candidate(candidates, get_paths)
    if found is not None:
        return found
    assumed = get_paths[0] if get_paths else candidates[-1]
    sink.add("health_path_assumed", path=assumed)
    return assumed


def _invoke_path(post_paths: Sequence[str], rules: Mapping[str, Any]) -> str | None:
    if len(post_paths) <= 1:
        return post_paths[0] if post_paths else None
    found = _first_candidate(rules["app"]["invokeCandidates"], post_paths)
    if found is None:
        text = render(rules["messages"]["ambiguous_route"], {"candidates": ", ".join(post_paths)})
        raise AgentProjectException(f"{_INVOKE_PATH}: {text}", path=_INVOKE_PATH, code="ambiguous")
    return found


def _field(text: str, candidates: Sequence[str], templates: Sequence[str]) -> str | None:
    for candidate in candidates:
        for template in templates:
            if compiled_regex(template.replace("{field}", candidate)).search(text):
                return candidate
    return None


def _port(
    text: str, language: str, launcher: Mapping[str, Any], rules: Mapping[str, Any]
) -> tuple[int, str | None]:
    if not launcher["portLiteral"]:
        return AGENT_APP_INTERNAL_PORT, None
    app_rules = rules["app"]
    for template in app_rules["portEnvTemplates"][language]:
        if compiled_regex(template.replace("{env}", AGENT_APP_PORT_ENV)).search(text):
            return AGENT_APP_INTERNAL_PORT, AGENT_APP_PORT_ENV
    for pattern in app_rules["portLiterals"][language]:
        match = compiled_regex(pattern).search(text)
        if match is not None:
            return _checked_port(int(match.group(1)), rules), None
    return AGENT_APP_INTERNAL_PORT, None


def _checked_port(port: int, rules: Mapping[str, Any]) -> int:
    reserved = {*rules["app"]["reservedPorts"], AGENT_SERVICE_PORT}
    if port in reserved:
        text = render(rules["messages"]["unsupported_port"], {"port": str(port)})
        raise AgentProjectException(
            f"{_APP_COMMAND_PATH}: {text}", path=_APP_COMMAND_PATH, code="unsupported"
        )
    return port


def _node_command(
    index: ProjectIndex, launcher: Mapping[str, Any], manager: str | None
) -> list[str]:
    package = index.read_json(PACKAGE_JSON)
    scripts = package.get("scripts") if isinstance(package, dict) else None
    start = launcher["startScript"]
    if isinstance(scripts, dict) and isinstance(scripts.get(start), str):
        return [SCRIPT_RUNNERS.get(manager or "", DEFAULT_RUNNER), RUN_COMMAND, start]
    main = package.get("main") if isinstance(package, dict) else None
    if isinstance(main, str) and main:
        return [NODE_MAIN_COMMAND, main]
    return []


def _command(
    index: ProjectIndex,
    launcher: Mapping[str, Any],
    entry: EntryChoice,
    manager: str | None,
    port: int,
    rules: Mapping[str, Any],
) -> tuple[str, ...]:
    if "command" in launcher:
        module = entry.entrypoint.split(":", 1)[0]
        values = {"module": module, "attr": entry.attr, "port": str(port)}
        return tuple(render(part, values) for part in launcher["command"])
    command = _node_command(index, launcher, manager)
    if not command:
        text = rules["messages"]["not_detected_start"]
        raise AgentProjectException(
            f"{_APP_COMMAND_PATH}: {text}", path=_APP_COMMAND_PATH, code="not_detected"
        )
    return tuple(command)


def _protocol(
    override: str | None,
    agentcore: bool,
    invoke: str | None,
    rules: Mapping[str, Any],
) -> str:
    if override is not None:
        return override
    if agentcore:
        return PROTOCOL_AGENTCORE
    return PROTOCOL_JSON if invoke is not None else PROTOCOL_RAW


def detect_app(
    index: ProjectIndex,
    rules: Mapping[str, Any],
    framework: Mapping[str, Any],
    entry: EntryChoice,
    manager: str | None,
    protocol_override: str | None,
    sink: WarningSink,
) -> tuple[DetectionApp, str]:
    """La app de `entry` y su protocolo. Sin ruta POST el protocolo es
    `raw`; `agentcore` si la app ya habla el contrato (`/ping` y
    `/invocations`) y `json` en otro caso."""
    if protocol_override == PROTOCOL_LANGGRAPH_API:
        raise _without_langgraph_json(rules)
    spec = framework["app"]
    launcher = rules["app"]["launchers"][spec["launcher"]]
    text = index.read(entry.path) or ""
    routes = _routes(text, entry.language, rules, spec["fixedRoutes"])
    get_paths = [path for method, path in routes if method == METHOD_GET]
    post_paths = [path for method, path in routes if method == METHOD_POST]
    contract = rules["app"]["agentcorePaths"]
    agentcore = contract["health"] in get_paths and contract["invoke"] in post_paths
    if protocol_override == PROTOCOL_AGENTCORE or (protocol_override is None and agentcore):
        health, invoke = contract["health"], contract["invoke"]
    else:
        health, invoke = _health_path(get_paths, rules, sink), _invoke_path(post_paths, rules)
    protocol = _protocol(protocol_override, agentcore, invoke, rules)
    if protocol in (PROTOCOL_AGENTCORE, PROTOCOL_JSON) and invoke is None:
        text_error = rules["messages"]["not_detected_route"]
        raise AgentProjectException(
            f"{_PROTOCOL_PATH}: {text_error}", path=_PROTOCOL_PATH, code="not_detected"
        )
    request_field, response_field = _fields(text, protocol, rules, sink)
    port, port_env = _port(text, entry.language, launcher, rules)
    command = _command(index, launcher, entry, manager, port, rules)
    app = DetectionApp(
        framework=framework["id"],
        command=command,
        port=port,
        port_env=port_env,
        health_path=health,
        invoke_path=invoke,
        request_field=request_field,
        response_field=response_field,
    )
    return app, protocol


def _fields(
    text: str, protocol: str, rules: Mapping[str, Any], sink: WarningSink
) -> tuple[str | None, str | None]:
    if protocol != PROTOCOL_JSON:
        return None, None
    app_rules = rules["app"]
    request = _field(text, app_rules["requestFields"], app_rules["requestFieldTemplates"])
    if request is None:
        request = app_rules["requestFields"][0]
        sink.add("request_field_assumed", field=request)
    response = _field(text, app_rules["responseFields"], app_rules["responseFieldTemplates"])
    return request, response


def _without_langgraph_json(rules: Mapping[str, Any]) -> AgentProjectException:
    text = rules["messages"]["langgraph_api_without_langgraph_json"]
    return AgentProjectException(
        f"{_PROTOCOL_PATH}: {text}", path=_PROTOCOL_PATH, code="invalid_value"
    )


def _graph_location(value: object) -> str | None:
    """`ruta:atributo` de un valor de `graphs`, sin el `./` inicial."""
    if not isinstance(value, str):
        return None
    return value[len(CURRENT_DIRECTORY) :] if value.startswith(CURRENT_DIRECTORY) else value


def _assistant_id(
    index: ProjectIndex,
    rules: Mapping[str, Any],
    spec_name: str,
    entry: EntryChoice,
    graph: str | None = None,
) -> str:
    """El grafo que se sirve: el de `--graph`, que `select_graph` ya validó
    al elegir el punto de entrada (`find_entry_in_file`), o, sin él, la clave
    de `graphs` que apunta al punto de entrada elegido: la preferida por
    nombre si hay varias, o la primera en orden."""
    if graph is not None:
        return graph
    spec = rules["entryFiles"][spec_name]
    document = index.read_json(spec["file"])
    graphs = document.get(spec["key"]) if isinstance(document, dict) else None
    wanted = f"{entry.path}{PYTHON_ATTRIBUTE_SEPARATOR}{entry.attr}"
    keys = sorted(key for key, value in (graphs or {}).items() if _graph_location(value) == wanted)
    if not keys:
        text = render(rules["messages"]["not_detected_entrypoint"], {"framework": spec["file"]})
        raise AgentProjectException(f"{_ENTRY_PATH}: {text}", path=_ENTRY_PATH, code="not_detected")
    return str(next((key for key in spec["preferKeys"] if key in keys), keys[0]))


def detect_langgraph_api(
    index: ProjectIndex,
    rules: Mapping[str, Any],
    framework: Mapping[str, Any],
    entry: EntryChoice | None,
    language: str | None,
    sink: WarningSink,
    graph: str | None = None,
) -> tuple[DetectionApp, str]:
    """La app `langgraph dev` de un proyecto con `langgraph.json` y el
    protocolo `langgraph-api`, con `assistant_id` el grafo de `graph`
    (`--graph`) o el elegido. Sin `langgraph.json` es `invalid_value`;
    avisa de que es un servidor de desarrollo y, si falta, de que
    `langgraph-cli` no está entre las dependencias."""
    if framework["id"] != LANGGRAPH_JSON_FRAMEWORK or entry is None:
        raise _without_langgraph_json(rules)
    assistant_id = _assistant_id(index, rules, framework["entryFile"], entry, graph)
    dependencies = index.dependencies(language) if language is not None else {}
    if LANGGRAPH_CLI_DEPENDENCY not in dependencies:
        sink.add("langgraph_cli_missing")
    sink.add("langgraph_dev_server")
    launcher = rules["app"]["launchers"][LANGGRAPH_DEV_LAUNCHER]
    port = AGENT_APP_INTERNAL_PORT
    app = DetectionApp(
        framework=framework["id"],
        command=tuple(render(part, {"port": str(port)}) for part in launcher["command"]),
        port=port,
        port_env=None,
        health_path=AGENT_LANGGRAPH_HEALTH_PATH,
        invoke_path=None,
        request_field=None,
        response_field=None,
        assistant_id=assistant_id,
    )
    return app, PROTOCOL_LANGGRAPH_API


def validate_protocol_override(protocol: str | None) -> None:
    """Un `--protocol` desconocido es `invalid_value`."""
    if protocol is not None and protocol not in APP_PROTOCOLS:
        raise AgentProjectException(
            f"{_PROTOCOL_PATH}: debe ser uno de {list(APP_PROTOCOLS)}",
            path=_PROTOCOL_PATH,
            code="invalid_value",
        )


__all__ = [
    "CUSTOM_KIND",
    "PROTOCOL_LANGGRAPH_API",
    "detect_app",
    "detect_langgraph_api",
    "validate_protocol_override",
]
