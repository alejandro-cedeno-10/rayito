"""Versiones de intérprete de un proyecto (ADR-029): lee una restricción de
Python (PEP 440: `>=3.11,<3.14`, `~=3.12`, `==3.12.*`, `^3.11`) o de Node
(semver: `>=20`, `^22`, `22.x`, `20 || 22`) y elige entre las compilaciones
fijadas de `limits.json`. Puro y sin dependencias: los dos SDK implementan lo
mismo y lo comprueban con `testdata/agent-detection/versions.json`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

Version = tuple[int, ...]

SPECIFIER_KIND: Final = "specifier"
SEMVER_KIND: Final = "semver"
MINOR_KIND: Final = "minor"
MAJOR_KIND: Final = "major"
_CLAUSE: Final = re.compile(r"^(~=|==|!=|>=|<=|>|<|\^|~)?\s*v?(\d+(?:\.\d+)*)(\.\*)?$", re.ASCII)
_COMPARATOR: Final = re.compile(
    r"(>=|<=|>|<|=|\^|~)?\s*v?(\d+|[xX*])(?:\.(\d+|[xX*]))?(?:\.(\d+|[xX*]))?", re.ASCII
)
_WILDCARDS: Final = frozenset({"x", "X", "*"})
_OR: Final = "||"
_MINOR_COMPONENTS: Final = 2
_SEMVER_COMPONENTS: Final = 3


def parse_version(text: str) -> Version:
    return tuple(int(part) for part in text.split("."))


def _pad(left: Version, right: Version) -> tuple[Version, Version]:
    size = max(len(left), len(right))
    return left + (0,) * (size - len(left)), right + (0,) * (size - len(right))


def compare(left: Version, right: Version) -> int:
    a, b = _pad(left, right)
    return -1 if a < b else 1 if a > b else 0


def _bump(version: Version, index: int) -> Version:
    """`version` con el componente `index` aumentado y los siguientes a cero."""
    return (*version[:index], version[index] + 1, *((0,) * (len(version) - index - 1)))


def _next_partial(numbers: Version) -> Version:
    """El primer `major.minor.patch` por encima de una versión parcial
    (`22` -> `23.0.0`, `22.1` -> `22.2.0`)."""
    bumped = _bump(numbers, len(numbers) - 1)
    return bumped + (0,) * (_SEMVER_COMPONENTS - len(bumped))


def _caret_upper(version: Version) -> Version:
    padded = version + (0,) * (_SEMVER_COMPONENTS - len(version))
    index = next((i for i, part in enumerate(padded) if part != 0), len(padded) - 1)
    return _bump(padded, index)


def _tilde_upper(version: Version) -> Version:
    index = 0 if len(version) == 1 else 1
    return _bump(version, index)


def _clause_satisfied(operator: str, version: Version, wildcard: bool, candidate: Version) -> bool:
    if operator in ("", "=="):
        if wildcard:
            return candidate[: len(version)] == version
        return compare(candidate, version) == 0
    if operator == "!=":
        if wildcard:
            return candidate[: len(version)] != version
        return compare(candidate, version) != 0
    if operator == ">=":
        return compare(candidate, version) >= 0
    if operator == ">":
        return compare(candidate, version) > 0
    if operator == "<=":
        return compare(candidate, version) <= 0
    if operator == "<":
        return compare(candidate, version) < 0
    if operator == "~=":
        return len(version) >= _MINOR_COMPONENTS and (
            compare(candidate, version) >= 0 and candidate[: len(version) - 1] == version[:-1]
        )
    if operator == "^":
        return compare(candidate, version) >= 0 and compare(candidate, _caret_upper(version)) < 0
    return compare(candidate, version) >= 0 and compare(candidate, _tilde_upper(version)) < 0


def specifier_satisfied(specifier: str, version: str) -> bool | None:
    """`version` cumple la restricción de Python `specifier` (cláusulas
    separadas por comas), o `None` si no se entiende."""
    candidate = parse_version(version)
    for raw in specifier.split(","):
        match = _CLAUSE.match(raw.strip())
        if match is None:
            return None
        operator, text, wildcard = match.group(1) or "", match.group(2), bool(match.group(3))
        if not _clause_satisfied(operator, parse_version(text), wildcard, candidate):
            return False
    return True


def _expand_comparator(operator: str, parts: tuple[str, ...]) -> list[tuple[str, Version]] | None:
    """Un comparador de semver como pares `(operador, versión)` simples."""
    numbers = [int(part) for part in parts if part not in _WILDCARDS]
    exact = len(numbers) == _SEMVER_COMPONENTS and len(parts) == _SEMVER_COMPONENTS
    if not numbers:
        return [] if operator in ("", "=", "^", "~") else None
    base = tuple(numbers) + (0,) * (_SEMVER_COMPONENTS - len(numbers))
    if operator in ("", "="):
        if exact:
            return [("==", base)]
        return [(">=", base), ("<", _next_partial(tuple(numbers)))]
    if operator in ("^", "~"):
        upper = _caret_upper(base) if operator == "^" else _tilde_upper(tuple(numbers))
        return [(">=", base), ("<", upper + (0,) * (_SEMVER_COMPONENTS - len(upper)))]
    if exact or operator in (">=", "<"):
        return [(operator, base)]
    next_base = _next_partial(tuple(numbers))
    return [(">=", next_base)] if operator == ">" else [("<", next_base)]


def _group_satisfied(group: str, candidate: Version) -> bool | None:
    comparators = list(_COMPARATOR.finditer(group))
    leftover = _COMPARATOR.sub("", group).replace(",", "").strip()
    if not comparators or leftover:
        return None
    for match in comparators:
        parts = tuple(part for part in match.groups()[1:] if part is not None)
        clauses = _expand_comparator(match.group(1) or "", parts)
        if clauses is None:
            return None
        for operator, version in clauses:
            if not _clause_satisfied(operator, version, False, candidate):
                return False
    return True


def semver_satisfied(spec: str, version: str) -> bool | None:
    """`version` cumple el rango de semver `spec` (grupos con `||`), o `None`
    si no se entiende."""
    candidate = parse_version(version)
    outcome: bool | None = False
    for group in spec.split(_OR):
        result = _group_satisfied(group.strip(), candidate)
        if result is None:
            return None
        if result:
            outcome = True
    return outcome


def satisfied(kind: str, constraint: str, version: str) -> bool | None:
    """Despacha por tipo de restricción: `specifier` (PEP 440), `semver`, o
    `minor` / `major` (el prefijo de una versión fijada en un fichero)."""
    if kind == SPECIFIER_KIND:
        return specifier_satisfied(constraint, version)
    if kind == SEMVER_KIND:
        return semver_satisfied(constraint, version)
    wanted = parse_version(constraint)
    return parse_version(version)[: len(wanted)] == wanted


@dataclass(frozen=True)
class Resolution:
    """El resultado de elegir una compilación: su clave (`"3.12"`, `"22"`) o
    `None` si ninguna cumple, y si la restricción se entendió."""

    key: str | None
    understood: bool


def _numeric_key(key: str) -> Version:
    return parse_version(key)


def resolve_build(
    kind: str, constraint: str, builds: Mapping[str, str], default: str
) -> Resolution:
    """La compilación fijada que cumple `constraint`: la de por defecto si
    cumple (sin descarga) y, si no, la más alta que cumpla."""

    def check(key: str) -> bool | None:
        return satisfied(kind, constraint, builds[key])

    if check(default) is None:
        return Resolution(None, False)
    if check(default):
        return Resolution(default, True)
    for key in sorted(builds, key=_numeric_key, reverse=True):
        if check(key):
            return Resolution(key, True)
    return Resolution(None, True)


__all__ = [
    "MAJOR_KIND",
    "MINOR_KIND",
    "SEMVER_KIND",
    "SPECIFIER_KIND",
    "Resolution",
    "compare",
    "parse_version",
    "resolve_build",
    "satisfied",
    "semver_satisfied",
    "specifier_satisfied",
]
