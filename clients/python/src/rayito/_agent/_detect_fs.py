"""Adaptador de disco del puerto `ProjectTree` (ADR-029): lista y lee los
ficheros de un directorio local para la detección. Recorre por anchura (los
manifiestos de la raíz siempre caben), no entra en las carpetas excluidas
(`.git`, `node_modules`, `.venv`...) ni sigue enlaces simbólicos, y nunca lee
fuera del directorio del proyecto. No decide qué es un secreto: el dominio
descarta `.env` antes de leer nada.
"""

from __future__ import annotations

import os
from collections import deque
from pathlib import Path
from typing import Final

from rayito._agent._detect_scan import glob_match
from rayito._limits import AGENT_SOURCE_DEFAULT_EXCLUDES

NUL: Final = b"\0"


class LocalProjectTree:
    """Un directorio del disco visto como `ProjectTree`."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root).resolve()

    def list_files(self, max_depth: int, max_files: int) -> list[str]:
        """Rutas relativas con `/`, por anchura y en orden, como mucho
        `max_files`, sin bajar más de `max_depth` carpetas."""
        found: list[str] = []
        pending: deque[tuple[Path, int]] = deque([(self._root, 0)])
        while pending and len(found) < max_files:
            folder, depth = pending.popleft()
            for entry in _entries(folder):
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    if depth < max_depth and not _excluded_directory(entry.name):
                        pending.append((Path(entry.path), depth + 1))
                elif entry.is_file() and len(found) < max_files:
                    found.append(self._relative(Path(entry.path)))
        return found

    def read_text(self, path: str, max_bytes: int) -> str | None:
        """El texto de `path` (como mucho `max_bytes`), o `None` si no existe,
        sale del proyecto, es un enlace o es binario."""
        target = self._root / path
        try:
            if target.is_symlink() or not target.resolve().is_relative_to(self._root):
                return None
            with target.open("rb") as handle:
                data = handle.read(max_bytes)
        except OSError:
            return None
        if NUL in data:
            return None
        return data.decode("utf-8", errors="replace")

    def _relative(self, path: Path) -> str:
        return path.relative_to(self._root).as_posix()


def _entries(folder: Path) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(folder) as scan:
            return sorted(scan, key=lambda entry: entry.name)
    except OSError:
        return []


def _excluded_directory(name: str) -> bool:
    return any(glob_match(pattern, name) for pattern in AGENT_SOURCE_DEFAULT_EXCLUDES)


__all__ = ["LocalProjectTree"]
