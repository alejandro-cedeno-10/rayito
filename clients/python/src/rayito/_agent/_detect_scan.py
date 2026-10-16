"""Lectura acotada del árbol de un proyecto para la detección (ADR-029):
el puerto `ProjectTree`, el índice de ficheros que filtra lo excluido y lo
demasiado profundo, y la extracción de dependencias de los manifiestos
(`requirements*.txt`, `pyproject.toml`, `package.json`) sin ejecutar nada ni
interpretar TOML. Todo es puro: la E/S está detrás del puerto.
"""

from __future__ import annotations

import functools
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Final, Protocol

from rayito._agent._detect_report import DetectionWarning
from rayito._limits import (
    AGENT_DETECTION_MAX_DEPTH,
    AGENT_DETECTION_MAX_FILE_BYTES,
    AGENT_DETECTION_MAX_FILES,
    AGENT_SOURCE_DEFAULT_EXCLUDES,
)

PATH_SEPARATOR: Final = "/"
#: Los identificadores de lenguaje de las reglas y el manifiesto de Node.
PYTHON: Final = "python"
TYPESCRIPT: Final = "typescript"
PACKAGE_JSON: Final = "package.json"
#: Las expresiones de las reglas se evalúan por líneas y sin Unicode, igual
#: que `RegExp` con la bandera `m`.
REGEX_FLAGS: Final = re.MULTILINE | re.ASCII
#: Las cuatro secciones de dependencias de un `package.json`.
PACKAGE_JSON_SECTIONS: Final = (
    "dependencies",
    "devDependencies",
    "peerDependencies",
    "optionalDependencies",
)
_REQUIREMENT: Final = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[([^\]]*)\])?", re.ASCII)
_SECTION_HEADER: Final = re.compile(r"^\[+([^\]]+)\]+\s*$", re.ASCII)
_ARRAY_OPEN: Final = re.compile(r"^\s*[A-Za-z0-9_-]+\s*=\s*\[", re.ASCII)
_POETRY_DEPENDENCIES: Final = re.compile(
    r"^tool\.poetry(?:\.group\.[\w-]+)?\.dependencies$", re.ASCII
)
_POETRY_KEY: Final = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*=", re.ASCII)
_QUOTED: Final = re.compile(r"\"([^\"]*)\"|'([^']*)'")
_NAME_SEPARATORS: Final = re.compile(r"[-_.]+", re.ASCII)
PROJECT_SECTION: Final = "project"
OPTIONAL_DEPENDENCIES_SECTION: Final = "project.optional-dependencies"
DEPENDENCY_GROUPS_SECTION: Final = "dependency-groups"
DEPENDENCIES_KEY: Final = re.compile(r"^\s*dependencies\s*=\s*\[", re.ASCII)
POETRY_PYTHON: Final = "python"


class ProjectTree(Protocol):
    """El puerto de lectura del proyecto: `list_files` devuelve rutas
    relativas con `/` (como mucho `max_files`, hasta `max_depth` carpetas de
    profundidad) y `read_text` el texto de un fichero (como mucho
    `max_bytes`), o `None` si no existe o no es texto."""

    def list_files(self, max_depth: int, max_files: int) -> Sequence[str]: ...

    def read_text(self, path: str, max_bytes: int) -> str | None: ...


@functools.cache
def compiled_regex(pattern: str) -> re.Pattern[str]:
    """La expresión de una regla, compilada una vez."""
    return re.compile(pattern, REGEX_FLAGS)


def compare_paths(left: str, right: str) -> int:
    """Orden de rutas por código de carácter, el mismo en los dos SDK."""
    return -1 if left < right else 1 if left > right else 0


def path_depth(path: str) -> int:
    return path.count(PATH_SEPARATOR)


@functools.cache
def glob_regex(pattern: str) -> re.Pattern[str]:
    """`*` es la única comodín: cualquier cosa menos `/`."""
    body = "[^/]*".join(re.escape(part) for part in pattern.split("*"))
    return re.compile(f"^{body}$", re.ASCII)


def glob_match(pattern: str, name: str) -> bool:
    return glob_regex(pattern).match(name) is not None


def render(template: str, values: Mapping[str, str]) -> str:
    """Rellena los `{nombre}` de una plantilla de mensaje."""
    text = template
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text


class WarningSink:
    """Acumula los avisos de una detección con el texto de las reglas."""

    def __init__(self, rules: Mapping[str, Any]) -> None:
        self._templates: Mapping[str, str] = rules["warnings"]
        self.items: list[DetectionWarning] = []

    def add(self, code: str, **values: str) -> None:
        warning = DetectionWarning(code, render(self._templates[code], values))
        if warning not in self.items:
            self.items.append(warning)


def normalize_python_name(name: str) -> str:
    """El nombre de un paquete de Python normalizado (PEP 503)."""
    return _NAME_SEPARATORS.sub("-", name).lower()


def requirement_names(spec: str) -> list[str]:
    """Los nombres con que un requisito cuenta como dependencia: el del
    paquete y uno por extra (`strands-agents[openai]`)."""
    match = _REQUIREMENT.match(spec)
    if match is None:
        return []
    name = normalize_python_name(match.group(1))
    extras = [extra.strip() for extra in (match.group(2) or "").split(",") if extra.strip()]
    return [name, *(f"{name}[{normalize_python_name(extra)}]" for extra in extras)]


def requirements_dependencies(text: str) -> list[str]:
    names: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped and not stripped.startswith(("#", "-")):
            names.extend(requirement_names(stripped))
    return names


def _quoted_strings(line: str) -> list[str]:
    return [double or single for double, single in _QUOTED.findall(line)]


def _closes_array(line: str) -> bool:
    return "]" in _QUOTED.sub("", line)


def pyproject_dependencies(text: str) -> list[str]:
    """Las dependencias declaradas en un `pyproject.toml` (`[project]`,
    sus extras, `[dependency-groups]` y las tablas de Poetry), leídas por
    líneas: sin intérprete de TOML, igual en los dos SDK."""
    names: list[str] = []
    section = ""
    in_array = False
    for line in text.split("\n"):
        stripped = line.strip()
        header = _SECTION_HEADER.match(stripped)
        if header is not None and not in_array:
            section = header.group(1).strip()
        elif in_array or _opens_dependency_array(section, line):
            names.extend(_names_of_strings(line))
            in_array = not _closes_array(line)
        elif _POETRY_DEPENDENCIES.match(section):
            key = _POETRY_KEY.match(stripped)
            if key is not None and key.group(1) != POETRY_PYTHON:
                names.append(normalize_python_name(key.group(1)))
    return names


def _opens_dependency_array(section: str, line: str) -> bool:
    if section == PROJECT_SECTION:
        return DEPENDENCIES_KEY.match(line) is not None
    if section in (OPTIONAL_DEPENDENCIES_SECTION, DEPENDENCY_GROUPS_SECTION):
        return _ARRAY_OPEN.match(line) is not None
    return False


def _names_of_strings(line: str) -> list[str]:
    names: list[str] = []
    for text in _quoted_strings(line):
        names.extend(requirement_names(text))
    return names


def package_json_dependencies(document: object) -> list[str]:
    if not isinstance(document, dict):
        return []
    names: list[str] = []
    for section in PACKAGE_JSON_SECTIONS:
        entries = document.get(section)
        if isinstance(entries, dict):
            names.extend(str(name) for name in entries)
    return names


class ProjectIndex:
    """Los ficheros del proyecto ya filtrados (sin lo excluido, sin pasar de
    `AGENT_DETECTION_MAX_DEPTH` ni de `AGENT_DETECTION_MAX_FILES`) y su
    lectura con caché. Los manifiestos y ficheros de configuración se buscan
    sólo en la raíz del proyecto; el código fuente, en todo el árbol."""

    def __init__(self, tree: ProjectTree, rules: Mapping[str, Any], warnings: WarningSink) -> None:
        self._tree = tree
        self._rules = rules
        self._warnings = warnings
        self._texts: dict[str, str | None] = {}
        self._json: dict[str, object] = {}
        self.paths: tuple[str, ...] = self._filtered(tree)
        self.root_files: tuple[str, ...] = tuple(
            path for path in self.paths if PATH_SEPARATOR not in path
        )

    def _filtered(self, tree: ProjectTree) -> tuple[str, ...]:
        exempt = set(self._rules["envExampleFiles"])
        kept = [
            path
            for path in tree.list_files(AGENT_DETECTION_MAX_DEPTH, AGENT_DETECTION_MAX_FILES + 1)
            if (path in exempt or not self._excluded(path))
            and path_depth(path) <= AGENT_DETECTION_MAX_DEPTH
        ]
        ordered = sorted(set(kept), key=functools.cmp_to_key(_by_depth_then_path))
        if len(ordered) > AGENT_DETECTION_MAX_FILES:
            self._warnings.add("scan_truncated")
        return tuple(ordered[:AGENT_DETECTION_MAX_FILES])

    @staticmethod
    def _excluded(path: str) -> bool:
        return any(
            glob_match(pattern, segment)
            for segment in path.split(PATH_SEPARATOR)
            for pattern in AGENT_SOURCE_DEFAULT_EXCLUDES
        )

    def read(self, path: str) -> str | None:
        if path not in self._texts:
            text = self._tree.read_text(path, AGENT_DETECTION_MAX_FILE_BYTES)
            self._texts[path] = None if text is None else text.replace("\r\n", "\n")
        return self._texts[path]

    def read_json(self, path: str) -> object | None:
        """El JSON de un fichero, o `None` (con un aviso) si no se entiende."""
        if path not in self._json:
            text = self.read(path)
            parsed: object | None = None
            if text is not None:
                try:
                    parsed = json.loads(text)
                except ValueError:
                    self._warnings.add("file_unreadable", file=path)
            self._json[path] = parsed
        return self._json[path]

    def root_matches(self, pattern: str) -> list[str]:
        return [path for path in self.root_files if glob_match(pattern, path)]

    def has_root_file(self, name: str) -> bool:
        return name in self.root_files

    def has_directory(self, name: str) -> bool:
        prefix = f"{name}{PATH_SEPARATOR}"
        return any(path.startswith(prefix) for path in self.paths)

    def sources(self, language: str) -> list[str]:
        """Los ficheros de código del lenguaje, de menos a más profundos,
        sin las carpetas de pruebas, ejemplos ni documentación."""
        spec = self._rules["languages"][language]
        ignored = set(self._rules["ignoredPathSegments"])
        found: list[str] = []
        for path in self.paths:
            *folders, name = path.split(PATH_SEPARATOR)
            if ignored.intersection(folders):
                continue
            if any(glob_match(pattern, name) for pattern in spec["sources"]) and not any(
                glob_match(pattern, name) for pattern in spec["sourceExcludes"]
            ):
                found.append(path)
        return found

    def dependencies(self, language: str) -> Mapping[str, str]:
        """Nombre de dependencia -> el primer manifiesto de la raíz que la
        declara."""
        found: dict[str, str] = {}
        for entry in self._rules["languages"][language]["dependencyFiles"]:
            for path in self.root_matches(entry["glob"]):
                for name in self._names_in(path, entry["format"]):
                    found.setdefault(name, path)
        return found

    def _names_in(self, path: str, manifest_format: str) -> list[str]:
        if manifest_format == "package-json":
            return package_json_dependencies(self.read_json(path))
        text = self.read(path)
        if text is None:
            return []
        if manifest_format == "pyproject":
            return pyproject_dependencies(text)
        return requirements_dependencies(text)


def _by_depth_then_path(left: str, right: str) -> int:
    return path_depth(left) - path_depth(right) or compare_paths(left, right)


__all__ = [
    "PACKAGE_JSON",
    "PACKAGE_JSON_SECTIONS",
    "PATH_SEPARATOR",
    "PYTHON",
    "TYPESCRIPT",
    "ProjectIndex",
    "ProjectTree",
    "WarningSink",
    "compare_paths",
    "compiled_regex",
    "glob_match",
    "normalize_python_name",
    "package_json_dependencies",
    "path_depth",
    "pyproject_dependencies",
    "render",
    "requirement_names",
    "requirements_dependencies",
]
