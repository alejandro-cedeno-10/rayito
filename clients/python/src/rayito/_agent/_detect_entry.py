"""Búsqueda del punto de entrada de un agente en el código del proyecto
(ADR-029): patrones de línea sobre asignaciones y exportaciones de nivel de
módulo, nunca importando ni ejecutando el código del usuario. Las reglas
(`agent-detection.json`) son datos; aquí sólo se interpretan.

Un candidato tiene un nivel: 1 un objeto ya construido, 2 una fábrica sin
argumentos y 3 una fábrica con argumentos (que nunca se elige sola).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from rayito._agent._detect_report import ENTRY_FACTORY, ENTRY_OBJECT
from rayito._agent._detect_scan import (
    PATH_SEPARATOR,
    PYTHON,
    TYPESCRIPT,
    ProjectIndex,
    compiled_regex,
    path_depth,
    render,
)
from rayito._agent._project_domain import AgentProjectException

TIER_OBJECT: Final = 1
TIER_FACTORY_WITHOUT_ARGS: Final = 2
TIER_FACTORY_WITH_ARGS: Final = 3
_OVERRIDE_RULE: Final = "override:entrypoint"
PYTHON_SUFFIX: Final = ".py"
PACKAGE_INIT: Final = "__init__"
MODULE_SEPARATOR: Final = "."
TYPESCRIPT_EXPORT_SEPARATOR: Final = "#"
PYTHON_ATTRIBUTE_SEPARATOR: Final = ":"
CURRENT_DIRECTORY: Final = "./"
_IDENTIFIER: Final = re.compile(r"^[A-Za-z_]\w*$", re.ASCII)
_ENTRY_PATH: Final = "runtime.entrypoint"


@dataclass(frozen=True)
class EntryChoice:
    """El punto de entrada elegido: su fichero, el atributo (o export), si es
    un objeto o una fábrica, la regla que acertó, el lenguaje y la cadena
    `modulo:atributo` / `ruta#export` que el proyecto escribe."""

    path: str
    attr: str
    factory: bool
    rule: str
    language: str
    entrypoint: str

    @property
    def kind(self) -> str:
        return ENTRY_FACTORY if self.factory else ENTRY_OBJECT


@dataclass(frozen=True)
class _Candidate:
    path: str
    attr: str
    tier: int
    rule: str


def python_module(path: str) -> str | None:
    """El módulo importable de una ruta `.py`, o `None` si algún componente
    no es un identificador válido."""
    stem = path[: -len(PYTHON_SUFFIX)] if path.endswith(PYTHON_SUFFIX) else path
    parts = stem.split(PATH_SEPARATOR)
    if parts[-1] == PACKAGE_INIT and len(parts) > 1:
        parts = parts[:-1]
    if not all(_IDENTIFIER.match(part) for part in parts):
        return None
    return MODULE_SEPARATOR.join(parts)


def entrypoint_string(language: str, path: str, attr: str) -> str | None:
    """`modulo:atributo` en Python, `ruta#export` en TypeScript."""
    if language == PYTHON:
        module = python_module(path)
        return None if module is None else f"{module}{PYTHON_ATTRIBUTE_SEPARATOR}{attr}"
    return f"{path}{TYPESCRIPT_EXPORT_SEPARATOR}{attr}"


def _stem(path: str) -> str:
    name = path.rsplit(PATH_SEPARATOR, 1)[-1]
    return name.rsplit(MODULE_SEPARATOR, 1)[0]


def _collect(
    index: ProjectIndex, rules: Mapping[str, Any], framework: Mapping[str, Any], language: str
) -> list[_Candidate]:
    found: dict[tuple[str, str], _Candidate] = {}
    sources = index.sources(language)
    for rule in framework["entries"].get(language, []):
        pattern = compiled_regex(rule["pattern"])
        requires = compiled_regex(rule["requires"]) if rule.get("requires") else None
        for path in sources:
            text = index.read(path)
            if text is None or (requires is not None and not requires.search(text)):
                continue
            for match in pattern.finditer(text):
                attr = match.group(1) if pattern.groups else rule["name"]
                if entrypoint_string(language, path, attr) is None:
                    continue
                candidate = _Candidate(path, attr, rule["tier"], rule["id"])
                known = found.get((path, attr))
                if known is None or candidate.tier < known.tier:
                    found[(path, attr)] = candidate
    return list(found.values())


def _narrow(pool: list[_Candidate], keep: Callable[[_Candidate], bool]) -> list[_Candidate]:
    preferred = [candidate for candidate in pool if keep(candidate)]
    return preferred if preferred else pool


def _listing(language: str, candidates: list[_Candidate], rules: Mapping[str, Any]) -> str:
    names = sorted({entrypoint_string(language, item.path, item.attr) or "" for item in candidates})
    return ", ".join(names[: rules["maxCandidatesListed"]])


def _select(
    candidates: list[_Candidate], rules: Mapping[str, Any], language: str
) -> _Candidate | None:
    if not candidates:
        return None
    best = min(candidate.tier for candidate in candidates)
    pool = sorted(
        (candidate for candidate in candidates if candidate.tier == best),
        key=lambda item: (path_depth(item.path), item.path, item.attr),
    )
    names, files = set(rules["preferNames"]), set(rules["preferFiles"])
    if len(pool) > 1:
        pool = _narrow(pool, lambda item: item.attr in names)
    if len(pool) > 1:
        pool = _narrow(pool, lambda item: _stem(item.path) in files)
    if len(pool) > 1:
        raise _ambiguous("ambiguous_entrypoint", language, pool, rules)
    chosen = pool[0]
    if chosen.tier == TIER_FACTORY_WITH_ARGS:
        raise _ambiguous("ambiguous_factory", language, pool, rules)
    return chosen


def _ambiguous(
    message: str, language: str, pool: list[_Candidate], rules: Mapping[str, Any]
) -> AgentProjectException:
    text = render(rules["messages"][message], {"candidates": _listing(language, pool, rules)})
    return AgentProjectException(f"{_ENTRY_PATH}: {text}", path=_ENTRY_PATH, code="ambiguous")


def find_entry(
    index: ProjectIndex, rules: Mapping[str, Any], framework: Mapping[str, Any], language: str
) -> EntryChoice | None:
    """El punto de entrada de `framework` en el código de `language`, o `None`
    si no hay ninguno. Varios candidatos del mismo nivel sin una preferencia
    clara son `ambiguous`, y una fábrica con argumentos también."""
    chosen = _select(_collect(index, rules, framework, language), rules, language)
    if chosen is None:
        return None
    entrypoint = entrypoint_string(language, chosen.path, chosen.attr)
    assert entrypoint is not None
    return EntryChoice(
        path=chosen.path,
        attr=chosen.attr,
        factory=chosen.tier != TIER_OBJECT,
        rule=chosen.rule,
        language=language,
        entrypoint=entrypoint,
    )


def _from_graph_value(value: object, spec: Mapping[str, Any], rule: str) -> EntryChoice | None:
    if not isinstance(value, str) or PYTHON_ATTRIBUTE_SEPARATOR not in value:
        return None
    location, attr = value.rsplit(PYTHON_ATTRIBUTE_SEPARATOR, 1)
    path = (
        location[len(CURRENT_DIRECTORY) :] if location.startswith(CURRENT_DIRECTORY) else location
    )
    suffix = MODULE_SEPARATOR + path.rsplit(MODULE_SEPARATOR, 1)[-1]
    language = spec["languagesBySuffix"].get(suffix)
    entrypoint = None if language is None or not attr else entrypoint_string(language, path, attr)
    if language is None or entrypoint is None:
        return None
    return EntryChoice(path, attr, False, rule, language, entrypoint)


def find_entry_in_file(
    index: ProjectIndex, rules: Mapping[str, Any], spec_name: str, rule: str
) -> EntryChoice | None:
    """El punto de entrada que declara un fichero de configuración (el
    `graphs` de `langgraph.json`): uno solo, el preferido por nombre, o
    `ambiguous`."""
    spec = rules["entryFiles"][spec_name]
    document = index.read_json(spec["file"])
    graphs = document.get(spec["key"]) if isinstance(document, dict) else None
    if not isinstance(graphs, dict) or not graphs:
        return None
    choices = {key: _from_graph_value(value, spec, rule) for key, value in sorted(graphs.items())}
    usable = {key: choice for key, choice in choices.items() if choice is not None}
    if not usable:
        return None
    if len(usable) == 1:
        return next(iter(usable.values()))
    for key in spec["preferKeys"]:
        if key in usable:
            return usable[key]
    listing = ", ".join(
        sorted(choice.entrypoint for choice in usable.values())[: rules["maxCandidatesListed"]]
    )
    text = render(rules["messages"]["ambiguous_entrypoint"], {"candidates": listing})
    raise AgentProjectException(f"{_ENTRY_PATH}: {text}", path=_ENTRY_PATH, code="ambiguous")


def explicit_entry(entrypoint: str, language: str | None) -> EntryChoice:
    """Un punto de entrada que da el usuario: se usa tal cual. `ruta#export`
    es TypeScript, `modulo:atributo` Python y una ruta sin más se reconoce por
    su extensión."""
    if TYPESCRIPT_EXPORT_SEPARATOR in entrypoint:
        path, attr = entrypoint.split(TYPESCRIPT_EXPORT_SEPARATOR, 1)
        return EntryChoice(path, attr, False, _OVERRIDE_RULE, TYPESCRIPT, entrypoint)
    if PYTHON_ATTRIBUTE_SEPARATOR in entrypoint:
        module, attr = entrypoint.split(PYTHON_ATTRIBUTE_SEPARATOR, 1)
        path = module.replace(MODULE_SEPARATOR, PATH_SEPARATOR) + PYTHON_SUFFIX
        return EntryChoice(path, attr, False, _OVERRIDE_RULE, PYTHON, entrypoint)
    resolved = language or (PYTHON if entrypoint.endswith(PYTHON_SUFFIX) else TYPESCRIPT)
    return EntryChoice(entrypoint, "", False, _OVERRIDE_RULE, resolved, entrypoint)


__all__ = [
    "EntryChoice",
    "entrypoint_string",
    "explicit_entry",
    "find_entry",
    "find_entry_in_file",
    "python_module",
]
