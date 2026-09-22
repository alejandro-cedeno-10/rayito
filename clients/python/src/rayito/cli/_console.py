"""Salida de la CLI: líneas con codificación tolerante, un documento JSON,
tablas y la traducción de errores a códigos de salida.

`rich` se importa dentro de `table()`: el modo `--json` y los módulos de
biblioteca no lo necesitan (es dependencia de `typer`, no del SDK).
"""

from __future__ import annotations

import io
import json
import shutil
import sys
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, NoReturn

from botocore.exceptions import ClientError

from rayito.cli._session import UsageError
from rayito.exceptions import SandboxException

STATUS_WIDTH = 4
MIN_TABLE_WIDTH = 120
EXIT_FAILURE = 1
EXIT_USAGE = 2


def echo(message: str = "", *, err: bool = False) -> None:
    """Los build logs y los `stateReason` traen UTF-8 arbitrario y una consola
    de Windows por defecto es cp1252: lo no codificable se sustituye en vez
    de abortar el informe."""
    stream = sys.stderr if err else sys.stdout
    encoding = stream.encoding or "utf-8"
    safe = message.encode(encoding, errors="replace").decode(encoding, errors="replace")
    print(safe, file=stream, flush=True)


def emit_json(document: Any) -> None:
    echo(json.dumps(document, indent=2, default=str))


def table(columns: Sequence[str], rows: Iterable[Sequence[object]]) -> None:
    from rich.console import Console
    from rich.table import Table

    rendered = Table(box=None, pad_edge=False, show_edge=False, header_style="bold")
    for column in columns:
        rendered.add_column(column, overflow="fold")
    for row in rows:
        rendered.add_row(*("" if cell is None else str(cell) for cell in row))
    buffer = io.StringIO()
    console = Console(file=buffer, width=table_width(), force_terminal=False, color_system=None)
    console.print(rendered)
    echo(buffer.getvalue().rstrip("\n"))


def table_width() -> int:
    return max(shutil.get_terminal_size(fallback=(MIN_TABLE_WIDTH, 24)).columns, MIN_TABLE_WIDTH)


def status_label(status: str) -> str:
    return f"{status:<{STATUS_WIDTH}}"


def age(started_at: datetime, now: datetime | None = None) -> str:
    """`1h23m`, `12m`, `45s`: edad de un sandbox desde `startedAt`."""
    current = now or datetime.now(UTC)
    seconds = max(0, int((current - started_at).total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def iso_utc(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def fail(message: str, *, code: int = EXIT_FAILURE) -> NoReturn:
    echo(f"rayito: {message}", err=True)
    raise SystemExit(code)


def client_error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", "Unknown"))


def client_error_message(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Message", exc))


@contextmanager
def translated_failures() -> Iterator[None]:
    """Un `ClientError` que ningún comando trató es `AWS error <Code>:
    <Message>` (salida 1); la familia `SandboxException`, su mensaje (1);
    `UsageError`, su mensaje (2)."""
    try:
        yield
    except UsageError as exc:
        fail(str(exc), code=EXIT_USAGE)
    except ClientError as exc:
        fail(f"AWS error {client_error_code(exc)}: {client_error_message(exc)}")
    except SandboxException as exc:
        fail(str(exc))
