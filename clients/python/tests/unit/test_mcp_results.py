"""Mapeo puro de los modelos del SDK a los resultados MCP (design D5, D6, D8):
`truncate_text`, `execution_to_blocks` sobre `Execution` sintéticas y los
constructores de los `TypedDict`."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from mcp.types import EmbeddedResource, ImageContent, TextContent, TextResourceContents

from rayito import (
    CommandExitException,
    CommandResult,
    EntryInfo,
    Execution,
    ExecutionError,
    FileType,
    LineChart,
    Logs,
    Result,
    SandboxListItem,
)
from rayito.mcp._results import (
    MAX_OUTPUT_CHARS,
    command_output_from,
    directory_listing_from,
    execution_to_blocks,
    file_content_from,
    file_entry_from,
    sandbox_list_from,
    sandbox_summary_from,
    truncate_text,
    write_receipt_from,
)

SANDBOX_ID = "microvm-00000000-0000-0000-0000-000000000001"
PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/x8AAwMB/6X4zvQAAAAASUVORK5CYII="
)
SVG_TEXT = '<svg xmlns="http://www.w3.org/2000/svg"/>'
MODIFIED_AT = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)
STARTED_AT = datetime(2026, 9, 16, 11, 30, 0, tzinfo=UTC)
IMAGE_ARN = "arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base"


def summary_of(execution: Execution) -> dict[str, object]:
    blocks = execution_to_blocks(execution, sandbox_id=SANDBOX_ID)
    first = blocks[0]
    assert isinstance(first, TextContent)
    parsed: dict[str, object] = json.loads(first.text)
    return parsed


def text_result(text: str) -> Result:
    return Result(text=text, is_main_result=True, raw={"text/plain": text})


def test_truncate_text_below_at_and_above_the_limit() -> None:
    assert truncate_text("abc", limit=5) == ("abc", False)
    assert truncate_text("abcde", limit=5) == ("abcde", False)
    text, truncated = truncate_text("abcdefgh", limit=5)
    assert truncated is True
    assert text == "abcde\n… [truncado: 3 caracteres más]"


def test_truncate_text_default_limit_is_100_000() -> None:
    assert MAX_OUTPUT_CHARS == 100_000
    text, truncated = truncate_text("x" * 100_010)
    assert truncated is True
    assert text.endswith("… [truncado: 10 caracteres más]")


def test_execution_text_only() -> None:
    execution = Execution(
        results=[text_result("42")], logs=Logs(stdout=["hola\n"]), execution_count=3
    )
    blocks = execution_to_blocks(execution, sandbox_id=SANDBOX_ID)
    assert len(blocks) == 1
    summary = summary_of(execution)
    assert summary == {
        "text": "42",
        "stdout": "hola\n",
        "stderr": "",
        "error": None,
        "execution_count": 3,
        "results": [{"index": 0, "mime_types": ["text/plain"]}],
        "truncated": False,
    }


def test_execution_png_and_chart_attach_one_image_block() -> None:
    chart = LineChart(title="plot", elements=[])
    result = Result(png=PNG_BASE64, chart=chart, raw={"image/png": PNG_BASE64, "e2b/chart": "{}"})
    execution = Execution(results=[result], execution_count=1)
    blocks = execution_to_blocks(execution, sandbox_id=SANDBOX_ID)
    assert [type(block) for block in blocks] == [TextContent, ImageContent]
    image = blocks[1]
    assert isinstance(image, ImageContent)
    assert image.mime_type == "image/png"
    assert image.data == PNG_BASE64
    summary = summary_of(execution)
    assert summary["results"] == [{"index": 0, "mime_types": ["image/png", "e2b/chart"]}]
    assert summary["text"] is None


def test_execution_jpeg_attaches_a_jpeg_block() -> None:
    result = Result(jpeg=PNG_BASE64, raw={"image/jpeg": PNG_BASE64})
    blocks = execution_to_blocks(Execution(results=[result]), sandbox_id=SANDBOX_ID)
    image = blocks[1]
    assert isinstance(image, ImageContent)
    assert image.mime_type == "image/jpeg"


def test_execution_svg_attaches_an_embedded_resource() -> None:
    result = Result(svg=SVG_TEXT, raw={"image/svg+xml": SVG_TEXT})
    execution = Execution(results=[text_result("1"), result], execution_count=7)
    blocks = execution_to_blocks(execution, sandbox_id=SANDBOX_ID)
    assert [type(block) for block in blocks] == [TextContent, EmbeddedResource]
    resource = blocks[1]
    assert isinstance(resource, EmbeddedResource)
    assert isinstance(resource.resource, TextResourceContents)
    assert str(resource.resource.uri) == f"rayito://{SANDBOX_ID}/results/7/1.svg"
    assert resource.resource.mime_type == "image/svg+xml"
    assert resource.resource.text == SVG_TEXT


def test_execution_other_mime_types_are_listed_not_attached() -> None:
    result = Result(html="<b>x</b>", raw={"text/html": "<b>x</b>"})
    execution = Execution(results=[result], execution_count=2)
    blocks = execution_to_blocks(execution, sandbox_id=SANDBOX_ID)
    assert len(blocks) == 1
    assert summary_of(execution)["results"] == [{"index": 0, "mime_types": ["text/html"]}]


def test_execution_error_is_data_with_null_text() -> None:
    error = ExecutionError(name="ValueError", value="x", traceback="Traceback…")
    execution = Execution(error=error, execution_count=4)
    summary = summary_of(execution)
    assert summary["error"] == {"name": "ValueError", "value": "x", "traceback": "Traceback…"}
    assert summary["text"] is None


def test_execution_without_count_uses_unknown_in_svg_uri() -> None:
    result = Result(svg=SVG_TEXT, raw={"image/svg+xml": SVG_TEXT})
    blocks = execution_to_blocks(Execution(results=[result]), sandbox_id=SANDBOX_ID)
    resource = blocks[1]
    assert isinstance(resource, EmbeddedResource)
    assert str(resource.resource.uri).endswith("/results/unknown/0.svg")


def test_execution_long_stdout_is_truncated_with_flag() -> None:
    execution = Execution(logs=Logs(stdout=["x" * 50_005, "y" * 50_005]))
    summary = summary_of(execution)
    stdout = summary["stdout"]
    assert isinstance(stdout, str)
    assert stdout.endswith("… [truncado: 10 caracteres más]")
    assert summary["truncated"] is True


def test_execution_long_text_is_truncated_with_flag() -> None:
    execution = Execution(results=[text_result("z" * (MAX_OUTPUT_CHARS + 1))])
    summary = summary_of(execution)
    assert summary["truncated"] is True
    text = summary["text"]
    assert isinstance(text, str)
    assert text.endswith("… [truncado: 1 caracteres más]")


def test_command_output_from_result() -> None:
    output = command_output_from(CommandResult(stdout="hola\n", stderr="", exit_code=0))
    assert output == {"stdout": "hola\n", "stderr": "", "exit_code": 0, "truncated": False}


def test_command_output_from_exit_exception() -> None:
    exc = CommandExitException("exit 3", exit_code=3, stdout="out", stderr="err")
    assert command_output_from(exc) == {
        "stdout": "out",
        "stderr": "err",
        "exit_code": 3,
        "truncated": False,
    }


def test_command_output_truncates_each_stream() -> None:
    result = CommandResult(stdout="a" * 12, stderr="b", exit_code=0)
    output = command_output_from(result, limit=10)
    assert output["truncated"] is True
    assert output["stdout"].endswith("… [truncado: 2 caracteres más]")
    assert output["stderr"] == "b"


def test_file_content_keeps_the_full_size() -> None:
    content = file_content_from("/home/user/a.txt", "x" * 12, limit=10)
    assert content["size"] == 12
    assert content["truncated"] is True
    assert content["path"] == "/home/user/a.txt"


def entry(entry_type: FileType | None) -> EntryInfo:
    return EntryInfo(
        name="a.txt",
        type=entry_type,
        path="/home/user/a.txt",
        size=4,
        mode=0o644,
        permissions="-rw-r--r--",
        owner="user",
        group="user",
        modified_time=MODIFIED_AT,
    )


def test_file_entry_from_entry_info() -> None:
    assert file_entry_from(entry(FileType.FILE)) == {
        "name": "a.txt",
        "path": "/home/user/a.txt",
        "type": "file",
        "size": 4,
        "modified_time": "2026-09-16T12:00:00+00:00",
    }
    assert file_entry_from(entry(None))["type"] is None


def test_write_receipt_and_directory_listing() -> None:
    assert write_receipt_from(entry(FileType.FILE)) == {"path": "/home/user/a.txt", "size": 4}
    listing = directory_listing_from("/home/user", [entry(FileType.DIR)])
    assert listing["path"] == "/home/user"
    assert listing["entries"][0]["type"] == "dir"


def item(sandbox_id: str) -> SandboxListItem:
    return SandboxListItem(
        sandbox_id=sandbox_id,
        state="RUNNING",
        template=IMAGE_ARN,
        template_version="16.0",
        started_at=STARTED_AT,
    )


def test_sandbox_summary_from_item() -> None:
    assert sandbox_summary_from(item(SANDBOX_ID), current=True) == {
        "sandbox_id": SANDBOX_ID,
        "state": "RUNNING",
        "template": IMAGE_ARN,
        "template_version": "16.0",
        "started_at": "2026-09-16T11:30:00+00:00",
        "current": True,
    }


def test_sandbox_list_marks_only_the_current_sandbox() -> None:
    listing = sandbox_list_from([item("other"), item(SANDBOX_ID)], current_sandbox_id=SANDBOX_ID)
    assert [entry["current"] for entry in listing["sandboxes"]] == [False, True]
    none_current = sandbox_list_from([item("other")], current_sandbox_id=None)
    assert none_current["sandboxes"][0]["current"] is False
