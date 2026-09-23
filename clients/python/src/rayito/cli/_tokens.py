"""El access token de `rayito sandbox create|connect|exec|metrics` y los
pares `K=V` de sus opciones. Sólo biblioteca estándar: se importa sin el
extra `cli`.

El token nunca llega por argv (se ve en `ps`), nunca se imprime y nunca se
loguea: viene de `--token-file` o de `RAYITO_ACCESS_TOKEN`, y `create
--detach` lo guarda en un fichero nuevo con modo `0600` (en Windows el modo
lo ignora el sistema; el fichero hereda los permisos del directorio).
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

TOKEN_ENV_VAR: Final = "RAYITO_ACCESS_TOKEN"
TOKEN_FILE_MODE: Final = 0o600
TOKEN_FILE_FLAGS: Final = os.O_WRONLY | os.O_CREAT | os.O_EXCL


class TokenFileError(Exception):
    """El fichero de token no se pudo leer o ya existía al crearlo; el
    mensaje nombra la ruta y nunca el contenido."""


class InvalidPairError(ValueError):
    """Un `--env`/`--metadata` sin `=` o sin clave; el mensaje no repite el
    valor (podría ser un secreto mal pegado)."""


def resolve_token(token_file: Path | None, environ: Mapping[str, str]) -> str | None:
    """`--token-file` gana a `RAYITO_ACCESS_TOKEN`; un fichero vacío o una
    variable vacía cuentan como ausentes."""
    if token_file is not None:
        return read_token_file(token_file) or None
    return environ.get(TOKEN_ENV_VAR) or None


def read_token_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise TokenFileError(
            f"no se pudo leer el fichero de token {path}: {type(exc).__name__}"
        ) from None


def write_token_file(path: Path, token: str) -> None:
    """Crea el fichero (`O_EXCL`, `0600`); uno existente es `TokenFileError`
    y no se toca."""
    try:
        descriptor = os.open(path, TOKEN_FILE_FLAGS, TOKEN_FILE_MODE)
    except FileExistsError:
        raise TokenFileError(f"el fichero de token ya existe: {path}") from None
    except OSError as exc:
        raise TokenFileError(
            f"no se pudo crear el fichero de token {path}: {type(exc).__name__}"
        ) from None
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(token)


def parse_pairs(values: Sequence[str] | None, *, option: str) -> dict[str, str]:
    """`["A=1", "B=x=y"]` → `{"A": "1", "B": "x=y"}`."""
    pairs: dict[str, str] = {}
    for position, value in enumerate(values or (), start=1):
        key, separator, content = value.partition("=")
        if not separator or not key:
            raise InvalidPairError(f"{option}: se esperaba K=V (entrada n.º {position})")
        pairs[key] = content
    return pairs
