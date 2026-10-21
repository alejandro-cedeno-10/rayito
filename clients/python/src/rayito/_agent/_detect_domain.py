"""Detección de un proyecto de agente (ADR-029, D1): servicio de dominio
puro sobre el puerto `ProjectTree`. Lee manifiestos y busca patrones de
línea; nunca importa, ejecuta ni evalúa el código del usuario, así que es
seguro sobre cualquier repositorio. La precedencia es fichero de proyecto >
bandera o argumento (`DetectOverrides`) > detección > valor por defecto con
nombre. Un caso poco claro falla con `ambiguous` o `not_detected` y nombra la
clave o la bandera que lo resuelve; nunca adivina.

Las reglas son datos (`agent-detection.json`, generado a `_detection_rules.py`)
y los mismos vectores (`testdata/agent-detection/`) los comprueban en Python y
en TypeScript.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from rayito._agent._detect_app import detect_app, validate_protocol_override
from rayito._agent._detect_entry import (
    EntryChoice,
    explicit_entry,
    find_entry,
    find_entry_in_file,
)
from rayito._agent._detect_install import detect_install
from rayito._agent._detect_providers import detect_providers
from rayito._agent._detect_report import (
    PROTOCOL_FRAMEWORK,
    DetectionAlternative,
    DetectionEvidence,
    DetectionReport,
    DetectOverrides,
)
from rayito._agent._detect_scan import ProjectIndex, ProjectTree, WarningSink, render
from rayito._agent._detection_rules import DETECTION_RULES
from rayito._agent._project_domain import AGENT_LANGUAGES, AgentProjectException

HTTP_KEYWORD_KEY: Final = "httpEntryKeyword"
_FRAMEWORK_PATH: Final = "runtime.framework"
_LANGUAGE_PATH: Final = "runtime.language"
_KIND_PATH: Final = "runtime.kind"
_ENTRYPOINT_PATH: Final = "runtime.entrypoint"
_SOURCE_PATH: Final = "source"


@dataclass(frozen=True)
class _Candidate:
    order: int
    framework: Mapping[str, Any]
    language: str | None
    evidence: tuple[DetectionEvidence, ...]


def _error(path: str, code: str, text: str) -> AgentProjectException:
    return AgentProjectException(f"{path}: {text}", path=path, code=code)


def _invalid_override(
    option: str, path: str, value: str, allowed: Sequence[str], rules: Mapping[str, Any]
) -> AgentProjectException:
    text = render(
        rules["messages"]["invalid_override"],
        {"value": value, "option": option, "allowed": ", ".join(allowed)},
    )
    return _error(path, "invalid_value", text)


def _framework_names(rules: Mapping[str, Any]) -> list[str]:
    names = {name for item in rules["frameworks"] for name in (item["id"], item["framework"])}
    return sorted({*names, rules[HTTP_KEYWORD_KEY]})


def _check_overrides(overrides: DetectOverrides, rules: Mapping[str, Any]) -> None:
    if overrides.language is not None and overrides.language not in AGENT_LANGUAGES:
        raise _invalid_override(
            "--language", _LANGUAGE_PATH, overrides.language, AGENT_LANGUAGES, rules
        )
    names = _framework_names(rules)
    if overrides.framework is not None and overrides.framework not in names:
        raise _invalid_override("--framework", _FRAMEWORK_PATH, overrides.framework, names, rules)
    validate_protocol_override(overrides.protocol)


def _present_languages(
    index: ProjectIndex, rules: Mapping[str, Any], overrides: DetectOverrides
) -> list[str]:
    if overrides.language is not None:
        return [overrides.language]
    return [
        language
        for language, spec in rules["languages"].items()
        if any(index.root_matches(marker) for marker in spec["markers"])
    ]


def _file_evidence(
    framework: Mapping[str, Any], index: ProjectIndex
) -> list[DetectionEvidence] | None:
    """La evidencia de un framework que se reconoce por ficheros de la raíz,
    o `None` si no tiene reglas de ficheros."""
    anywhere = framework.get("agnosticFiles", [])
    every = framework.get("agnosticAllFiles", [])
    some = framework.get("agnosticAnyFiles", [])
    if not (anywhere or every or some):
        return None
    if every and not all(index.has_root_file(name) for name in every):
        return []
    found = [name for name in [*anywhere, *every, *some] if index.has_root_file(name)]
    if (some and not any(index.has_root_file(name) for name in some)) or not found:
        return []
    return [DetectionEvidence(name, f"file:{name}") for name in found]


def _forced(framework: Mapping[str, Any], overrides: DetectOverrides) -> bool:
    name = overrides.framework
    return name is not None and name in (framework["id"], framework["framework"])


def _candidates(
    index: ProjectIndex,
    rules: Mapping[str, Any],
    languages: Sequence[str],
    overrides: DetectOverrides,
) -> list[_Candidate]:
    http_only = overrides.framework == rules[HTTP_KEYWORD_KEY] or (
        overrides.entrypoint == rules[HTTP_KEYWORD_KEY]
    )
    found: list[_Candidate] = []
    for order, framework in enumerate(rules["frameworks"]):
        forced = _forced(framework, overrides)
        if (overrides.framework is not None and not http_only and not forced) or (
            http_only and not framework.get("webOnly")
        ):
            continue
        files = _file_evidence(framework, index)
        if files is not None:
            if files or forced:
                found.append(_Candidate(order, framework, None, tuple(files or _forced_evidence())))
            continue
        for language in languages or (list(framework["deps"]) if forced else []):
            found.extend(_dependency_candidates(index, order, framework, language, forced))
    return found


def _forced_evidence() -> list[DetectionEvidence]:
    return [DetectionEvidence(None, "override:framework")]


def _dependency_candidates(
    index: ProjectIndex, order: int, framework: Mapping[str, Any], language: str, forced: bool
) -> list[_Candidate]:
    wanted = framework["deps"].get(language)
    if wanted is None:
        return []
    dependencies = index.dependencies(language)
    evidence = [
        DetectionEvidence(dependencies[name], f"dep:{name}")
        for name in wanted
        if name in dependencies
    ]
    if not evidence and not forced:
        return []
    return [_Candidate(order, framework, language, tuple(evidence or _forced_evidence()))]


def _entry_for(
    candidate: _Candidate,
    index: ProjectIndex,
    rules: Mapping[str, Any],
    overrides: DetectOverrides,
) -> EntryChoice | None:
    framework = candidate.framework
    if overrides.entrypoint is not None and overrides.entrypoint != rules[HTTP_KEYWORD_KEY]:
        return explicit_entry(overrides.entrypoint, candidate.language)
    if "entryFile" in framework:
        return find_entry_in_file(index, rules, framework["entryFile"], framework["id"])
    if "entries" in framework and candidate.language is not None:
        return find_entry(index, rules, framework, candidate.language)
    return None


def _needs_entry(framework: Mapping[str, Any]) -> bool:
    return "entries" in framework or "entryFile" in framework


def _choose(
    candidates: list[_Candidate],
    index: ProjectIndex,
    rules: Mapping[str, Any],
    overrides: DetectOverrides,
    sink: WarningSink,
) -> tuple[_Candidate, EntryChoice | None]:
    remaining = sorted(
        candidates,
        key=lambda item: (item.order, "" if item.language is None else item.language),
    )
    while remaining:
        ties = [item for item in remaining if item.order == remaining[0].order]
        if len({item.language for item in ties if item.language is not None}) > 1:
            raise _error(_LANGUAGE_PATH, "ambiguous", rules["messages"]["ambiguous_language"])
        head = ties[0]
        entry = _entry_for(head, index, rules, overrides)
        if entry is not None or not _needs_entry(head.framework):
            return head, entry
        sink.add("entrypoint_not_found", framework=head.framework["framework"])
        remaining.remove(head)
    raise _error(_KIND_PATH, "not_detected", rules["messages"]["not_detected_agent"])


def _kind(framework: Mapping[str, Any], language: str | None) -> str:
    kinds = framework["kind"]
    return str(kinds.get(language) or next(iter(kinds.values())))


def _alternatives(
    index: ProjectIndex,
    rules: Mapping[str, Any],
    languages: Sequence[str],
    sink: WarningSink,
) -> tuple[DetectionAlternative, ...]:
    found: list[DetectionAlternative] = []
    for order, framework in enumerate(rules["frameworks"]):
        if not framework.get("webOnly"):
            continue
        for language in languages:
            for candidate in _dependency_candidates(index, order, framework, language, False):
                entry = _quiet_entry(candidate, index, rules)
                if entry is not None:
                    found.append(
                        DetectionAlternative(
                            framework["framework"],
                            _kind(framework, language),
                            language,
                            entry.entrypoint,
                        )
                    )
    if found:
        sink.add("http_app_alternative", framework=found[0].framework)
    return tuple(found)


def _quiet_entry(
    candidate: _Candidate, index: ProjectIndex, rules: Mapping[str, Any]
) -> EntryChoice | None:
    try:
        return find_entry(index, rules, candidate.framework, candidate.language or "")
    except AgentProjectException:
        return None


def _skills_options(framework: Mapping[str, Any], index: ProjectIndex) -> dict[str, str]:
    options: dict[str, str] = {}
    for key, spec in framework.get("options", {}).items():
        if index.has_directory(spec["directory"]):
            options[key] = spec["directory"]
    return options


def detect(
    tree: ProjectTree,
    overrides: DetectOverrides | None = None,
    rules: Mapping[str, Any] = DETECTION_RULES,
) -> DetectionReport:
    """Detecta lenguaje, framework, punto de entrada, protocolo, app HTTP,
    plan de instalación y proveedor de modelo de un proyecto, sin ejecutar su
    código. `overrides` fuerza lo que el usuario indicó."""
    overrides = overrides or DetectOverrides()
    _check_overrides(overrides, rules)
    sink = WarningSink(rules)
    index = ProjectIndex(tree, rules, sink)
    languages = _present_languages(index, rules, overrides)
    candidates = _candidates(index, rules, languages, overrides)
    if not candidates:
        key = "not_detected_agent" if languages else "not_detected_manifest"
        raise _error(
            _SOURCE_PATH if not languages else _KIND_PATH, "not_detected", rules["messages"][key]
        )
    winner, entry = _choose(candidates, index, rules, overrides, sink)
    framework = winner.framework
    language = entry.language if entry is not None and winner.language is None else winner.language
    evidence = list(winner.evidence)
    if entry is not None:
        evidence.append(DetectionEvidence(entry.path or None, f"entry:{entry.rule}"))
    install = (
        detect_install(language, index, rules, sink, evidence) if language is not None else None
    )
    app, protocol = None, PROTOCOL_FRAMEWORK
    if "app" in framework and entry is not None:
        manager = None if install is None else install.manager
        app, protocol = detect_app(
            index, rules, framework, entry, manager, overrides.protocol, sink
        )
    elif overrides.protocol is not None:
        sink.add("protocol_ignored")
    alternatives = (
        ()
        if "app" in framework
        else _alternatives(index, rules, [language] if language else languages, sink)
    )
    deps = index.dependencies(language) if language is not None else {}
    found = detect_providers(index, rules, language, framework["framework"], deps, sink, overrides)
    return DetectionReport(
        language=language,
        framework=framework["framework"],
        kind=_kind(framework, language),
        entrypoint=None if entry is None else entry.entrypoint,
        entrypoint_kind=None if entry is None else entry.kind,
        protocol=protocol,
        options=_skills_options(framework, index),
        app=app,
        install=install,
        providers=found.providers,
        models=found.models,
        evidence=tuple(evidence),
        warnings=tuple(sink.items),
        alternatives=alternatives,
        model_envs=found.model_envs,
    )


def fill_runtime(document: Mapping[str, object], report: DetectionReport) -> dict[str, object]:
    """El documento de proyecto con la tabla `[runtime]` que falta rellenada
    por la detección. Lo que el fichero ya dice gana, clave por clave; si el
    fichero declara otro `kind` que el detectado, se deja tal cual."""
    filled = dict(document)
    detected = report.runtime_table()
    current = document.get("runtime")
    if not isinstance(current, Mapping):
        filled["runtime"] = detected
        return filled
    if "kind" in current and current["kind"] != detected["kind"]:
        return filled
    merged: dict[str, object] = {**detected, **current}
    detected_app, current_app = detected.get("app"), current.get("app")
    if isinstance(detected_app, Mapping) and isinstance(current_app, Mapping):
        merged["app"] = {**detected_app, **current_app}
    filled["runtime"] = merged
    return filled


__all__ = [
    "DetectOverrides",
    "DetectionReport",
    "ProjectTree",
    "detect",
    "fill_runtime",
]
