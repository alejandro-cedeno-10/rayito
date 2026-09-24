"""Captura de registros por logger para los tests de `logger=` y de higiene."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager


class RecordList(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def messages(self) -> list[str]:
        return [record.getMessage() for record in self.records]

    def text(self) -> str:
        """Mensajes y trazas formateados: lo que acabaría en un fichero de log."""
        formatter = logging.Formatter("%(name)s %(message)s")
        return "\n".join(formatter.format(record) for record in self.records)


@contextmanager
def capture_logs(name: str) -> Iterator[RecordList]:
    """Todos los registros (DEBUG incluido) que llegan al logger `name` o a
    sus descendientes."""
    target = logging.getLogger(name)
    handler = RecordList()
    previous_level = target.level
    target.addHandler(handler)
    target.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        target.removeHandler(handler)
        target.setLevel(previous_level)
