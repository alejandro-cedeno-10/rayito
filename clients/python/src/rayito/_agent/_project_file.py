"""Lectura de `rayito-agent.toml` / `rayito-agent.json` (ADR-028, adaptador
de entrada de `AgentProject.load`): busca el fichero en un directorio, lo
convierte en un diccionario con `tomllib` o `json` y ofrece el lector con
el que `AgentProject.from_dict` resuelve `agent.instructions_file`. No
interpreta el documento: eso es el dominio (`_project_domain.py`)."""

from __future__ import annotations

import json
import os
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from rayito._agent._project_domain import AgentProjectException
from rayito._limits import AGENT_PROJECT_FILE_NAMES

#: Ruta que el error de un documento ilegible lleva: el documento entero.
DOCUMENT_PATH: Final = "<fichero>"
TOML_SUFFIX: Final = ".toml"
JSON_SUFFIX: Final = ".json"
INSTRUCTIONS_FILE_KEY: Final = "agent.instructions_file"


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
        code="file_not_found",
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
