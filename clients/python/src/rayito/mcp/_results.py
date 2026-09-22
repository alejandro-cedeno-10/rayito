"""Formas de resultado del servidor MCP y su mapeo desde los modelos del SDK
(design D5, D6, D8). Funciones puras, sin I/O.

Los `TypedDict` son la salida estructurada de las cinco herramientas con
esquema; `execution_to_blocks` produce los bloques de contenido de
`run_code`. Se usa `typing_extensions.TypedDict` porque pydantic (el
validador del SDK `mcp`) rechaza los `typing.TypedDict` anidados en
Python 3.11.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any, Final

from mcp.types import EmbeddedResource, ImageContent, TextContent, TextResourceContents
from typing_extensions import TypedDict

from rayito._models import (
    CommandResult,
    EntryInfo,
    Execution,
    Result,
    SandboxListItem,
    execution_error_to_dict,
)
from rayito.exceptions import CommandExitException

MAX_OUTPUT_CHARS: Final = 100_000
PNG_MIME: Final = "image/png"
JPEG_MIME: Final = "image/jpeg"
SVG_MIME: Final = "image/svg+xml"
RESULT_URI_SCHEME: Final = "rayito"


class CommandOutput(TypedDict):
    stdout: str
    stderr: str
    exit_code: int
    truncated: bool


class FileContent(TypedDict):
    path: str
    content: str
    size: int
    truncated: bool


class WriteReceipt(TypedDict):
    path: str
    size: int


class FileEntry(TypedDict):
    name: str
    path: str
    type: str | None
    size: int
    modified_time: str


class DirectoryListing(TypedDict):
    path: str
    entries: list[FileEntry]


class SandboxSummary(TypedDict):
    sandbox_id: str
    state: str
    template: str
    template_version: str
    started_at: str
    current: bool


class SandboxList(TypedDict):
    sandboxes: list[SandboxSummary]


ContentBlock = TextContent | ImageContent | EmbeddedResource


def truncate_text(text: str, limit: int = MAX_OUTPUT_CHARS) -> tuple[str, bool]:
    """Corta `text` a `limit` caracteres con el sufijo `… [truncado: <n>
    caracteres más]`; el booleano dice si hubo corte."""
    if len(text) <= limit:
        return text, False
    remaining = len(text) - limit
    return f"{text[:limit]}\n… [truncado: {remaining} caracteres más]", True


def execution_to_blocks(
    execution: Execution, *, sandbox_id: str, limit: int = MAX_OUTPUT_CHARS
) -> list[ContentBlock]:
    """Primero un `TextContent` JSON con el resumen, después una imagen por
    `png`/`jpeg` y un recurso embebido por `svg` (design D6)."""
    summary = execution_summary(execution, limit)
    blocks: list[ContentBlock] = [
        TextContent(type="text", text=json.dumps(summary, ensure_ascii=False, indent=2))
    ]
    blocks.extend(image_blocks(execution.results))
    blocks.extend(
        svg_blocks(
            execution.results,
            sandbox_id=sandbox_id,
            execution_count=execution.execution_count,
        )
    )
    return blocks


def execution_summary(execution: Execution, limit: int) -> dict[str, Any]:
    text, text_cut = truncate_optional(execution.text, limit)
    stdout, stdout_cut = truncate_text("".join(execution.logs.stdout), limit)
    stderr, stderr_cut = truncate_text("".join(execution.logs.stderr), limit)
    return {
        "text": text,
        "stdout": stdout,
        "stderr": stderr,
        "error": None if execution.error is None else execution_error_to_dict(execution.error),
        "execution_count": execution.execution_count,
        "results": [
            {"index": index, "mime_types": list(result.raw)}
            for index, result in enumerate(execution.results)
        ],
        "truncated": text_cut or stdout_cut or stderr_cut,
    }


def truncate_optional(text: str | None, limit: int) -> tuple[str | None, bool]:
    if text is None:
        return None, False
    return truncate_text(text, limit)


def image_blocks(results: Iterable[Result]) -> Iterator[ImageContent]:
    for result in results:
        if result.png is not None:
            yield ImageContent(type="image", data=result.png, mime_type=PNG_MIME)
        if result.jpeg is not None:
            yield ImageContent(type="image", data=result.jpeg, mime_type=JPEG_MIME)


def svg_blocks(
    results: Iterable[Result], *, sandbox_id: str, execution_count: int | None
) -> Iterator[EmbeddedResource]:
    for index, result in enumerate(results):
        if result.svg is None:
            continue
        uri = svg_result_uri(sandbox_id, execution_count, index)
        yield EmbeddedResource(
            type="resource",
            resource=TextResourceContents(uri=uri, mime_type=SVG_MIME, text=result.svg),
        )


def svg_result_uri(sandbox_id: str, execution_count: int | None, index: int) -> str:
    count = "unknown" if execution_count is None else str(execution_count)
    return f"{RESULT_URI_SCHEME}://{sandbox_id}/results/{count}/{index}.svg"


def command_output_from(
    result: CommandResult | CommandExitException, limit: int = MAX_OUTPUT_CHARS
) -> CommandOutput:
    """Un exit code distinto de cero es dato: la excepción del SDK lleva los
    mismos campos que el resultado."""
    stdout, stdout_cut = truncate_text(result.stdout, limit)
    stderr, stderr_cut = truncate_text(result.stderr, limit)
    return CommandOutput(
        stdout=stdout,
        stderr=stderr,
        exit_code=result.exit_code,
        truncated=stdout_cut or stderr_cut,
    )


def file_content_from(path: str, content: str, limit: int = MAX_OUTPUT_CHARS) -> FileContent:
    text, cut = truncate_text(content, limit)
    return FileContent(path=path, content=text, size=len(content), truncated=cut)


def write_receipt_from(entry: EntryInfo) -> WriteReceipt:
    return WriteReceipt(path=entry.path, size=entry.size)


def file_entry_from(entry: EntryInfo) -> FileEntry:
    return FileEntry(
        name=entry.name,
        path=entry.path,
        type=None if entry.type is None else entry.type.value,
        size=entry.size,
        modified_time=entry.modified_time.isoformat(),
    )


def directory_listing_from(path: str, entries: Iterable[EntryInfo]) -> DirectoryListing:
    return DirectoryListing(path=path, entries=[file_entry_from(entry) for entry in entries])


def sandbox_summary_from(item: SandboxListItem, *, current: bool) -> SandboxSummary:
    return SandboxSummary(
        sandbox_id=item.sandbox_id,
        state=item.state,
        template=item.template,
        template_version=item.template_version,
        started_at=item.started_at.isoformat(),
        current=current,
    )


def sandbox_list_from(
    items: Iterable[SandboxListItem], *, current_sandbox_id: str | None
) -> SandboxList:
    return SandboxList(
        sandboxes=[
            sandbox_summary_from(item, current=item.sandbox_id == current_sandbox_id)
            for item in items
        ]
    )
