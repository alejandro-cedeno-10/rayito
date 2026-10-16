"""Plan de instalación y versiones de intérprete de un proyecto (ADR-029):
qué gestor instala sus dependencias (por su fichero de bloqueo o de
requisitos), si la instalación es reproducible y qué compilación fijada de
Python o de Node corresponde a la restricción del proyecto. Las reglas son
datos de `agent-detection.json`; aquí sólo se interpretan, sin tocar el
disco ni ejecutar nada.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from rayito._agent._detect_report import DetectionEvidence, DetectionInstall
from rayito._agent._detect_scan import (
    PACKAGE_JSON,
    PYTHON,
    ProjectIndex,
    WarningSink,
    compiled_regex,
    glob_match,
    render,
)
from rayito._agent._detect_versions import resolve_build
from rayito._agent._project_domain import AgentProjectException
from rayito._limits import (
    AGENT_DEFAULT_NODE_MAJOR,
    AGENT_DEFAULT_PYTHON_MINOR,
    AGENT_NODE_BUILD_VERSIONS,
    AGENT_PYTHON_BUILD_VERSIONS,
)

_INSTALL_PATH: Final = "install"
_REQUIREMENTS_PATH: Final = "source.requirements"


def _not_detected(key: str, rules: Mapping[str, Any]) -> AgentProjectException:
    text = rules["messages"][key]
    return AgentProjectException(
        f"{_INSTALL_PATH}: {text}", path=_INSTALL_PATH, code="not_detected"
    )


def _requirements_file(
    entry: Mapping[str, Any], index: ProjectIndex, rules: Mapping[str, Any]
) -> str | None:
    files = [
        path
        for path in index.root_matches(entry["glob"])
        if not any(glob_match(pattern, path) for pattern in entry["ignored"])
    ]
    for preferred in entry["preferred"]:
        if preferred in files:
            return str(preferred)
    if len(files) > 1:
        text = render(rules["messages"]["ambiguous_requirements"], {"candidates": ", ".join(files)})
        raise AgentProjectException(
            f"{_REQUIREMENTS_PATH}: {text}", path=_REQUIREMENTS_PATH, code="ambiguous"
        )
    return files[0] if files else None


class _Plan:
    """El gestor elegido: su fichero de bloqueo o de requisitos."""

    def __init__(
        self,
        manager: str | None,
        lockfile: str | None,
        requirements: str | None,
        require_hashes: bool,
        reproducible: bool,
    ) -> None:
        self.manager = manager
        self.lockfile = lockfile
        self.requirements = requirements
        self.require_hashes = require_hashes
        self.reproducible = reproducible


def _choose_manager(
    language: str, index: ProjectIndex, rules: Mapping[str, Any], sink: WarningSink
) -> _Plan:
    chosen: _Plan | None = None
    locks: list[str] = []
    for entry in rules["install"][language]:
        if chosen is None:
            chosen = _plan_for(entry, index, rules, sink)
        if entry.get("reproducible") and "file" in entry and index.has_root_file(entry["file"]):
            locks.append(entry["file"])
    if chosen is None:
        return _Plan(None, None, None, False, False)
    if len(locks) > 1:
        sink.add("multiple_lockfiles", files=", ".join(locks), used=chosen.lockfile or "")
    if not chosen.reproducible:
        sink.add("not_reproducible")
    return chosen


def _plan_for(
    entry: Mapping[str, Any], index: ProjectIndex, rules: Mapping[str, Any], sink: WarningSink
) -> _Plan | None:
    if "glob" in entry:
        path = _requirements_file(entry, index, rules)
        if path is None:
            return None
        text = index.read(path) or ""
        return _Plan(entry["manager"], None, path, entry["hashMarker"] in text, True)
    name = entry["file"]
    if not index.has_root_file(name):
        return None
    if entry.get("notDetected"):
        raise _not_detected(f"not_detected_{entry['manager']}", rules)
    if entry.get("unsupported"):
        sink.add("unsupported_lockfile", file=name)
        return None
    manager = entry["manager"]
    if "berryMarker" in entry and entry["berryMarker"] in (index.read(name) or ""):
        manager = entry["berryManager"]
    reproducible = bool(entry["reproducible"])
    return _Plan(manager, name if entry["reproducible"] else None, None, False, reproducible)


def _source_text(index: ProjectIndex, source: Mapping[str, Any]) -> str | None:
    name = source["file"]
    if "json" in source:
        document: Any = index.read_json(name)
        for key in source["json"]:
            document = document.get(key) if isinstance(document, dict) else None
        return str(document) if isinstance(document, str) and document else None
    text = index.read(name)
    if text is None:
        return None
    match = compiled_regex(source["pattern"]).search(text)
    return None if match is None else match.group(1)


def _resolve_version(
    language: str,
    index: ProjectIndex,
    rules: Mapping[str, Any],
    sink: WarningSink,
    evidence: list[DetectionEvidence],
) -> str:
    builds = AGENT_PYTHON_BUILD_VERSIONS if language == PYTHON else AGENT_NODE_BUILD_VERSIONS
    default = AGENT_DEFAULT_PYTHON_MINOR if language == PYTHON else str(AGENT_DEFAULT_NODE_MAJOR)
    for source in rules["versions"][language]["sources"]:
        text = _source_text(index, source)
        if text is None:
            continue
        resolution = resolve_build(source["kind"], text, builds, default)
        if not resolution.understood:
            sink.add("version_ignored", file=source["file"])
            continue
        if resolution.key is None:
            runtime = "Python" if language == PYTHON else "Node"
            message = render(rules["messages"]["unsupported_version"], {"runtime": runtime})
            path = "runtime.python_version" if language == PYTHON else "runtime.node_version"
            raise AgentProjectException(f"{path}: {message}", path=path, code="unsupported")
        evidence.append(DetectionEvidence(source["file"], f"version:{source['kind']}"))
        return resolution.key
    return default


def detect_install(
    language: str,
    index: ProjectIndex,
    rules: Mapping[str, Any],
    sink: WarningSink,
    evidence: list[DetectionEvidence],
) -> DetectionInstall:
    """El plan de instalación de un proyecto de `language`."""
    plan = _choose_manager(language, index, rules, sink)
    if plan.lockfile is not None or plan.requirements is not None:
        evidence.append(
            DetectionEvidence(plan.lockfile or plan.requirements, f"install:{plan.manager}")
        )
    version = _resolve_version(language, index, rules, sink, evidence)
    build_script = False
    if language != PYTHON:
        package = index.read_json(PACKAGE_JSON)
        scripts = package.get("scripts") if isinstance(package, dict) else None
        build_script = isinstance(scripts, dict) and rules["install"]["buildScript"] in scripts
    return DetectionInstall(
        manager=plan.manager,
        lockfile=plan.lockfile,
        requirements=plan.requirements,
        require_hashes=plan.require_hashes,
        reproducible=plan.reproducible,
        build_script=build_script,
        python_version=version if language == PYTHON else None,
        node_version=None if language == PYTHON else version,
    )


__all__ = ["detect_install"]
