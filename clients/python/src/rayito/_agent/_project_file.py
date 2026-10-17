"""Lectura de `rayito-agent.toml` / `rayito-agent.json` (ADR-028, adaptador
de entrada de `AgentProject.load`): busca el fichero en un directorio, lo
convierte en un diccionario con `tomllib` o `json` y ofrece el lector con
el que `AgentProject.from_dict` resuelve `agent.instructions_file`. No
interpreta el documento: eso es el dominio (`_project_domain.py`)."""

from __future__ import annotations

import json
import os
import re
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from rayito._agent._project_domain import AgentProject, AgentProjectException
from rayito._aws_region import resolve_region
from rayito._limits import AGENT_PROJECT_FILE_NAMES

if TYPE_CHECKING:
    from rayito._agent._detect_report import DetectionReport, DetectOverrides

#: Ruta que el error de un documento ilegible lleva: el documento entero.
DOCUMENT_PATH: Final = "<fichero>"
TOML_SUFFIX: Final = ".toml"
JSON_SUFFIX: Final = ".json"
INSTRUCTIONS_FILE_KEY: Final = "agent.instructions_file"
FILE_NOT_FOUND_CODE: Final = "file_not_found"
NOT_DETECTED_CODE: Final = "not_detected"
#: Lo que no es válido en el nombre de un proyecto (`[a-z0-9-]`) y el nombre
#: que sale de un directorio sin nombre utilizable. Una sola regla para el
#: andamiaje de `rayito agent init` y para la detección.
_INVALID_NAME_CHARS: Final = re.compile(r"[^a-z0-9]+")
DEFAULT_PROJECT_NAME: Final = "my-agent"
MAX_PROJECT_NAME_CHARS: Final = 63


@dataclass(frozen=True)
class ProjectDocument:
    """El documento leído: su contenido, el directorio del fichero y el
    lector de ficheros de texto que no puede salir de ese directorio."""

    data: Mapping[str, object]
    base_dir: str
    read_text: Callable[[str], str]


def read_project_document(path: str | os.PathLike[str]) -> ProjectDocument:
    """Lee el fichero de proyecto de `path`: un fichero, o un directorio
    donde se usa el primero de `AGENT_PROJECT_FILE_NAMES` que exista."""
    file = _locate(Path(path))
    text = file.read_text(encoding="utf-8")
    data = _parse(file, text)
    base_dir = str(file.resolve().parent)
    return ProjectDocument(data=data, base_dir=base_dir, read_text=_reader(base_dir))


def project_name_of(directory: Path) -> str:
    """El nombre de proyecto que sale del nombre del directorio: en minúsculas,
    con `-` en lugar de cualquier otro carácter, como mucho
    `MAX_PROJECT_NAME_CHARS`, y `DEFAULT_PROJECT_NAME` si no queda nada."""
    cleaned = _INVALID_NAME_CHARS.sub("-", directory.resolve().name.lower()).strip("-")
    return cleaned[:MAX_PROJECT_NAME_CHARS].strip("-") or DEFAULT_PROJECT_NAME


def detect_project(
    path: str | os.PathLike[str],
    overrides: DetectOverrides | None = None,
    *,
    region: str | None = None,
) -> tuple[AgentProject, DetectionReport | None]:
    """El `AgentProject` de `path` con lo que el fichero de proyecto (si lo
    hay) no dice completado por la detección, y su informe. Lee ficheros
    locales y no ejecuta nada del proyecto; no llama a AWS. Un fichero que ya
    declara `[runtime] kind` basta por sí solo: si el directorio no tiene nada
    que detectar, el informe es `None` y el proyecto es el del fichero."""
    from rayito._agent._detect_domain import detect
    from rayito._agent._detect_fs import LocalProjectTree
    from rayito._agent._detect_project import detected_document

    target = Path(path)
    try:
        located = read_project_document(target)
        document, base_dir, read_text = dict(located.data), located.base_dir, located.read_text
        directory = Path(base_dir)
    except AgentProjectException as exc:
        if exc.code != FILE_NOT_FOUND_CODE or not target.is_dir():
            raise
        document, directory = {}, target
        base_dir = str(directory.resolve())
        read_text = _reader(base_dir)
    try:
        report = detect(LocalProjectTree(directory), overrides)
    except AgentProjectException as exc:
        if exc.code != NOT_DETECTED_CODE or not _declares_runtime(document):
            raise
        return AgentProject.from_dict(document, base_dir=base_dir, read_text=read_text), None
    chosen_region = region or _document_region(document) or resolve_region(None)
    filled = detected_document(
        document, report, name=project_name_of(directory), region=chosen_region
    )
    return AgentProject.from_dict(filled, base_dir=base_dir, read_text=read_text), report


def _declares_runtime(document: Mapping[str, object]) -> bool:
    runtime = document.get("runtime")
    return isinstance(runtime, Mapping) and "kind" in runtime


def _document_region(document: Mapping[str, object]) -> str | None:
    deploy = document.get("deploy")
    value = deploy.get("region") if isinstance(deploy, Mapping) else None
    return value if isinstance(value, str) and value else None


def _locate(target: Path) -> Path:
    if target.is_file():
        return target
    if target.is_dir():
        for name in AGENT_PROJECT_FILE_NAMES:
            candidate = target / name
            if candidate.is_file():
                return candidate
    names = " ni ".join(AGENT_PROJECT_FILE_NAMES)
    raise AgentProjectException(
        f"{DOCUMENT_PATH}: no hay ningún fichero {names} en la ruta",
        path=DOCUMENT_PATH,
        code=FILE_NOT_FOUND_CODE,
    )


def _parse(file: Path, text: str) -> Mapping[str, object]:
    try:
        if file.suffix == TOML_SUFFIX:
            return tomllib.loads(text)
        if file.suffix == JSON_SUFFIX:
            document = json.loads(text)
            if isinstance(document, dict):
                return document
            raise ValueError("la raíz no es un objeto")
    except (tomllib.TOMLDecodeError, ValueError) as exc:
        raise AgentProjectException(
            f"{DOCUMENT_PATH}: el fichero no es un documento de proyecto válido",
            path=DOCUMENT_PATH,
            code="invalid_value",
        ) from exc
    raise AgentProjectException(
        f"{DOCUMENT_PATH}: la extensión debe ser {TOML_SUFFIX} o {JSON_SUFFIX}",
        path=DOCUMENT_PATH,
        code="invalid_value",
    )


def _reader(base_dir: str) -> Callable[[str], str]:
    root = Path(base_dir).resolve()

    def read_text(path: str) -> str:
        target = Path(path)
        if not target.is_file():
            raise AgentProjectException(
                f"{INSTRUCTIONS_FILE_KEY}: el fichero no existe",
                path=INSTRUCTIONS_FILE_KEY,
                code="file_not_found",
            )
        if not target.resolve().is_relative_to(root):
            raise AgentProjectException(
                f"{INSTRUCTIONS_FILE_KEY}: sale del directorio del fichero",
                path=INSTRUCTIONS_FILE_KEY,
                code="path_escape",
            )
        return target.read_text(encoding="utf-8")

    return read_text
