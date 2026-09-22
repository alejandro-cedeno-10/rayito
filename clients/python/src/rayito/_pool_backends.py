"""Backends de estado del pool: dónde viven los registros de plaza (y sus
secretos) entre `run-microvm` y `take()`.

`PoolBackend` es la costura para un almacén compartido futuro; aquí hay dos:
`InMemoryPoolBackend` (por defecto, muere con el proceso) y
`JsonFilePoolBackend` (un fichero JSON con esquema `rayito.pool/1`, escrito
de forma atómica con modo 0600). El de fichero es **de un solo proceso** —
sin bloqueo, sin coordinación entre hosts — y existe para los tests y para
recuperar un pool en el mismo host tras un reinicio. El fichero contiene los
access tokens de las plazas aparcadas en claro: es tan sensible como
`RAYITO_ACCESS_TOKEN`. El pool serializa toda llamada al backend bajo su
propio lock, así que un backend no necesita ser thread-safe.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any, Final, Protocol

from rayito._pool_base import POOL_SCHEMA, SlotRecord, record_from_dict, record_to_dict
from rayito.exceptions import InvalidArgumentException

FILE_MODE = 0o600
TEMP_SUFFIX = ".tmp"
NO_FOLLOW: Final = getattr(os, "O_NOFOLLOW", 0)
READ_FLAGS: Final = os.O_RDONLY | NO_FOLLOW


class PoolBackend(Protocol):
    """Registros de plaza indexados por `sandbox_id`."""

    @property
    def persistent(self) -> bool: ...

    def load(self) -> tuple[SlotRecord, ...]: ...

    def save(self, record: SlotRecord) -> None: ...

    def delete(self, sandbox_id: str) -> None: ...


class InMemoryPoolBackend:
    """Un dict: los registros (y sus secretos) mueren con el proceso."""

    persistent = False

    def __init__(self) -> None:
        self._records: dict[str, SlotRecord] = {}

    def load(self) -> tuple[SlotRecord, ...]:
        return tuple(self._records.values())

    def save(self, record: SlotRecord) -> None:
        self._records[record.sandbox_id] = record

    def delete(self, sandbox_id: str) -> None:
        self._records.pop(sandbox_id, None)


class JsonFilePoolBackend:
    """`{"schema": "rayito.pool/1", "slots": [...]}` en `path`.

    Cada escritura vuelca el fichero completo a un temporal de nombre
    aleatorio en el mismo directorio, creado en exclusiva y con el modo
    fijado sobre el descriptor (en Windows el modo no tiene efecto), y lo
    promueve con `os.replace`, así un corte a mitad deja el fichero anterior
    intacto y nadie puede precrear el temporal para quedarse con los tokens.
    La lectura se niega a seguir un enlace en la propia ruta del estado. Un
    fichero ausente carga `()`; otro `schema` es `InvalidArgumentException`.
    El mismo esquema lo lee y escribe el SDK TypeScript.
    """

    persistent = True

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> tuple[SlotRecord, ...]:
        return tuple(self._read().values())

    def save(self, record: SlotRecord) -> None:
        records = self._read()
        records[record.sandbox_id] = record
        self._write(records)

    def delete(self, sandbox_id: str) -> None:
        records = self._read()
        if records.pop(sandbox_id, None) is not None:
            self._write(records)

    def _read(self) -> dict[str, SlotRecord]:
        try:
            descriptor = os.open(self._path, READ_FLAGS)
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise not_a_regular_file(self._path) from exc
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            text = handle.read()
        return records_from_document(parse_document(text, self._path))

    def _write(self, records: dict[str, SlotRecord]) -> None:
        document = {
            "schema": POOL_SCHEMA,
            "slots": [record_to_dict(record) for record in records.values()],
        }
        descriptor, temp = tempfile.mkstemp(
            dir=self._path.parent, prefix=f"{self._path.name}.", suffix=TEMP_SUFFIX
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                restrict_to_owner(handle.fileno())
                json.dump(document, handle, indent=2, sort_keys=True)
            os.replace(temp, self._path)
        except BaseException:
            discard(temp)
            raise


def restrict_to_owner(descriptor: int) -> None:
    """`mkstemp` ya crea 0600 en POSIX; el `fchmod` explícito fija el modo
    sobre el descriptor —no sobre un nombre que otro uid podría haber
    cambiado entre medias— y es lo que hace cierta la frase de T14. Windows
    no tiene `fchmod` y tampoco el modelo de modos POSIX."""
    if hasattr(os, "fchmod"):
        os.fchmod(descriptor, FILE_MODE)


def discard(path: str) -> None:
    with suppress(OSError):
        os.unlink(path)


def not_a_regular_file(path: Path) -> InvalidArgumentException:
    return InvalidArgumentException(
        f"{path}: el fichero de estado del pool debe ser un fichero regular, no un enlace"
    )


def parse_document(text: str, path: Path) -> dict[str, Any]:
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidArgumentException(f"{path}: no es JSON válido ({exc.msg})") from exc
    if not isinstance(document, dict):
        raise InvalidArgumentException(f"{path}: el documento del pool debe ser un objeto")
    schema = document.get("schema")
    if schema != POOL_SCHEMA:
        raise InvalidArgumentException(
            f"{path}: schema {schema!r} no reconocido; este SDK lee {POOL_SCHEMA!r}"
        )
    return document


def records_from_document(document: dict[str, Any]) -> dict[str, SlotRecord]:
    slots = document.get("slots") or []
    if not isinstance(slots, list):
        raise InvalidArgumentException("`slots` debe ser una lista")
    records = [record_from_dict(item) for item in slots]
    return {record.sandbox_id: record for record in records}


__all__ = ["InMemoryPoolBackend", "JsonFilePoolBackend", "PoolBackend"]
