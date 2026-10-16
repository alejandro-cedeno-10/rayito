"""Detalles de la app HTTP de un proyecto (ADR-029): su comando de arranque,
el puerto, las rutas de salud e invocación, el protocolo y los campos de la
petición. Todo sale de patrones de línea sobre el fichero de la app y de
`package.json`; nunca se ejecuta nada. Las reglas son datos de
`agent-detection.json`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from rayito._agent._detect_entry import EntryChoice
from rayito._agent._detect_report import APP_PROTOCOLS, DetectionApp
from rayito._agent._detect_scan import (
    PACKAGE_JSON,
    ProjectIndex,
    WarningSink,
    compiled_regex,
    render,
)
from rayito._agent._project_domain import AgentProjectException
from rayito._limits import AGENT_APP_INTERNAL_PORT, AGENT_APP_PORT_ENV, AGENT_SERVICE_PORT

PROTOCOL_AGENTCORE: Final = "agentcore"
PROTOCOL_JSON: Final = "json"
PROTOCOL_RAW: Final = "raw"
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


def _port(text: str, language: str, launcher: Mapping[str, Any], rules: Mapping[str, Any]) -> int:
    if not launcher["portLiteral"]:
        return AGENT_APP_INTERNAL_PORT
    app_rules = rules["app"]
    for template in app_rules["portEnvTemplates"][language]:
        if compiled_regex(template.replace("{env}", AGENT_APP_PORT_ENV)).search(text):
            return AGENT_APP_INTERNAL_PORT
    for pattern in app_rules["portLiterals"][language]:
        match = compiled_regex(pattern).search(text)
        if match is not None:
            return _checked_port(int(match.group(1)), rules)
    return AGENT_APP_INTERNAL_PORT


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
    port = _port(text, entry.language, launcher, rules)
    command = _command(index, launcher, entry, manager, port, rules)
    app = DetectionApp(
        framework=framework["id"],
        command=command,
        port=port,
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


def validate_protocol_override(protocol: str | None) -> None:
    """Un `--protocol` desconocido es `invalid_value`."""
    if protocol is not None and protocol not in APP_PROTOCOLS:
        raise AgentProjectException(
            f"{_PROTOCOL_PATH}: debe ser uno de {list(APP_PROTOCOLS)}",
            path=_PROTOCOL_PATH,
            code="invalid_value",
        )


__all__ = ["detect_app", "validate_protocol_override"]
