"""Núcleo puro de `run_code`: deadlines, validación, requests, conversión de
protos, `Result`/`Execution` y el `ExecutionBuilder`."""

from __future__ import annotations

import json
from typing import Any

import pytest

from rayito import (
    ChartType,
    CodeContext,
    Execution,
    ExecutionError,
    LineChart,
    Logs,
    OutputMessage,
    Result,
)
from rayito._code_base import (
    CODE_STREAM_GRACE_SECONDS,
    DEFAULT_CODE_TIMEOUT_SECONDS,
    MAX_CODE_BYTES,
    ExecutionBuilder,
    build_create_context_request,
    build_execute_request,
    build_reattach_request,
    context_from_proto,
    error_from_proto,
    execute_deadline,
    fallback_context,
    language_default_context_id,
    normalize_language,
    reattach_failure,
    require_context_id,
    resolve_context_id,
    result_from_proto,
    validate_code,
    validate_cwd,
    validate_language,
)
from rayito._models import RESULT_FORMAT_ORDER
from rayito.exceptions import InvalidArgumentException, NotFoundException, SandboxException
from rayito.v1 import code_pb2, common_pb2

from .fake_code import LINE_CHART, ONE_PIXEL_PNG_BASE64

CONTEXT = CodeContext(id="ctx-000000000001", language="python", cwd="/tmp")


def started(execution_id: str = "exec-0123456789abcdef", count: int = 1) -> code_pb2.ExecuteEvent:
    return code_pb2.ExecuteEvent(
        started=code_pb2.ExecutionStarted(execution_id=execution_id, execution_count=count), seq=1
    )


def stdout(text: str, seq: int = 2) -> code_pb2.ExecuteEvent:
    return code_pb2.ExecuteEvent(
        stdout=code_pb2.OutputChunk(text=text, timestamp_unix_ns=1_700_000_000_000_000_001), seq=seq
    )


def stderr(text: str, seq: int = 3) -> code_pb2.ExecuteEvent:
    return code_pb2.ExecuteEvent(
        stderr=code_pb2.OutputChunk(text=text, timestamp_unix_ns=1_700_000_000_000_000_002), seq=seq
    )


def result_event(seq: int = 4, **fields: Any) -> code_pb2.ExecuteEvent:
    return code_pb2.ExecuteEvent(result=code_pb2.ExecutionResult(**fields), seq=seq)


def error_event(name: str, value: str, seq: int = 5) -> code_pb2.ExecuteEvent:
    return code_pb2.ExecuteEvent(
        error=code_pb2.ExecutionError(name=name, value=value, traceback=["l1", "l2"]), seq=seq
    )


def end(count: int, seq: int = 6) -> code_pb2.ExecuteEvent:
    return code_pb2.ExecuteEvent(end=code_pb2.ExecutionEnd(execution_count=count), seq=seq)


def keepalive() -> code_pb2.ExecuteEvent:
    return code_pb2.ExecuteEvent(keepalive=common_pb2.KeepAlive())


def test_execute_deadline_adds_the_grace_or_disables() -> None:
    assert execute_deadline(DEFAULT_CODE_TIMEOUT_SECONDS) == 300 + CODE_STREAM_GRACE_SECONDS
    assert execute_deadline(2) == 17
    assert execute_deadline(None) is None
    assert execute_deadline(0) is None
    with pytest.raises(InvalidArgumentException):
        execute_deadline(-1)


def test_validate_code_accepts_empty_and_rejects_large_or_non_str() -> None:
    assert validate_code("") == ""
    assert validate_code("x = 1") == "x = 1"
    assert validate_code("é" * (MAX_CODE_BYTES // 2)) == "é" * (MAX_CODE_BYTES // 2)
    with pytest.raises(InvalidArgumentException, match="1 MiB"):
        validate_code("é" * (MAX_CODE_BYTES // 2 + 1))
    with pytest.raises(InvalidArgumentException, match="str"):
        validate_code(b"x = 1")  # type: ignore[arg-type]


def test_resolve_context_id_for_context_str_and_none() -> None:
    assert resolve_context_id(CONTEXT) == "ctx-000000000001"
    assert resolve_context_id("ctx-abc") == "ctx-abc"
    assert resolve_context_id(None) is None
    assert require_context_id(CONTEXT) == "ctx-000000000001"
    with pytest.raises(InvalidArgumentException):
        resolve_context_id("")
    with pytest.raises(InvalidArgumentException):
        require_context_id(None)
    with pytest.raises(InvalidArgumentException):
        require_context_id(7)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("given", "canonical"),
    [
        (None, None),
        ("", None),
        ("python", "python"),
        ("Python", "python"),
        ("bash", "bash"),
        ("Bash", "bash"),
        ("javascript", "javascript"),
        ("JS", "javascript"),
        ("js", "javascript"),
    ],
)
def test_normalize_language_table(given: str | None, canonical: str | None) -> None:
    assert normalize_language(given) == canonical
    assert validate_language(given) == (canonical or "python")


@pytest.mark.parametrize("rejected", ["r", "java", "ruby", "typescript", " bash"])
def test_normalize_language_rejects_other_kernels(rejected: str) -> None:
    with pytest.raises(InvalidArgumentException, match="language"):
        normalize_language(rejected)
    with pytest.raises(InvalidArgumentException, match="language"):
        validate_language(rejected)


def test_validate_language_and_cwd() -> None:
    assert validate_language(None) == "python"
    assert validate_language("") == "python"
    assert validate_language("python") == "python"
    assert validate_language("javascript") == "javascript"
    with pytest.raises(InvalidArgumentException, match="language"):
        validate_language(7)  # type: ignore[arg-type]
    assert validate_cwd(None) is None
    assert validate_cwd("") is None
    assert validate_cwd("/tmp") == "/tmp"
    with pytest.raises(InvalidArgumentException, match="cwd"):
        validate_cwd("relative")
    with pytest.raises(InvalidArgumentException, match="cwd"):
        validate_cwd("/nul\x00")


def test_build_execute_request_shape() -> None:
    request = build_execute_request("x")
    assert request.code == "x"
    assert request.timeout_ms == 300_000
    assert not request.HasField("context_id")
    assert dict(request.envs) == {}
    custom = build_execute_request("x", context_id="ctx-1", envs={"A": "1"}, timeout=2)
    assert custom.context_id == "ctx-1"
    assert custom.timeout_ms == 2000
    assert dict(custom.envs) == {"A": "1"}
    assert build_execute_request("x", timeout=None).timeout_ms == 0
    assert build_execute_request("x", timeout=0).timeout_ms == 0
    with pytest.raises(InvalidArgumentException):
        build_execute_request("x", envs={"": "1"})
    with pytest.raises(InvalidArgumentException):
        build_execute_request("x", timeout=-1)


def test_build_execute_request_language_presence_and_exclusivity() -> None:
    assert not build_execute_request("x").HasField("language")
    assert not build_execute_request("x", language=None).HasField("language")
    assert not build_execute_request("x", language="").HasField("language")
    python = build_execute_request("x", language="Python")
    assert python.HasField("language")
    assert python.language == "python"
    bash = build_execute_request("echo 1", language="bash")
    assert bash.language == "bash"
    assert not bash.HasField("context_id")
    assert build_execute_request("1", language="JS").language == "javascript"
    with pytest.raises(InvalidArgumentException, match="excluyentes"):
        build_execute_request("x", context_id="default", language="bash")
    with pytest.raises(InvalidArgumentException, match="excluyentes"):
        build_execute_request("x", context_id="ctx-1", language="python")
    with pytest.raises(InvalidArgumentException, match="language"):
        build_execute_request("x", language="r")
    assert language_default_context_id(bash) == "default-bash"
    assert language_default_context_id(python) == "default"
    assert language_default_context_id(build_execute_request("x")) is None


def test_build_create_context_request_shape() -> None:
    request = build_create_context_request()
    assert request.language == "python"
    assert not request.HasField("cwd")
    custom = build_create_context_request(language="", cwd="/tmp", envs={"M4": "1"})
    assert custom.language == "python"
    assert custom.cwd == "/tmp"
    assert dict(custom.envs) == {"M4": "1"}
    with pytest.raises(InvalidArgumentException):
        build_create_context_request(language="ruby")
    assert build_create_context_request(language="js").language == "javascript"
    assert build_create_context_request(language="Bash").language == "bash"


def test_result_from_proto_maps_every_mime_field() -> None:
    proto = code_pb2.ExecutionResult(
        is_main_result=True,
        text="t",
        html="<b>h</b>",
        markdown="# m",
        latex="\\alpha",
        json='{"a": 1}',
        javascript="1+1",
        png=ONE_PIXEL_PNG_BASE64,
        jpeg="/9j/",
        svg="<svg/>",
        pdf="JVBERi0=",
        chart=json.dumps(LINE_CHART),
        data='{"a": [1, 2]}',
        extra={"application/vnd.x": "v", "rayito/omitted": "image/png: 9000000 bytes"},
    )
    result = result_from_proto(proto)
    assert result.is_main_result is True
    assert (result.text, result.html, result.markdown, result.latex) == (
        "t",
        "<b>h</b>",
        "# m",
        "\\alpha",
    )
    assert result.json == {"a": 1}
    assert result.data == {"a": [1, 2]}
    assert result.javascript == "1+1"
    assert (result.png, result.jpeg, result.svg, result.pdf) == (
        ONE_PIXEL_PNG_BASE64,
        "/9j/",
        "<svg/>",
        "JVBERi0=",
    )
    assert isinstance(result.chart, LineChart)
    assert result.chart.type is ChartType.LINE
    assert result.extra == {"application/vnd.x": "v", "rayito/omitted": "image/png: 9000000 bytes"}
    assert result.raw["text/plain"] == "t"
    assert result.raw["application/json"] == '{"a": 1}'
    assert result.raw["e2b/chart"] == json.dumps(LINE_CHART)
    assert result.raw["rayito/omitted"] == "image/png: 9000000 bytes"
    assert result.formats() == [
        *RESULT_FORMAT_ORDER,
        "application/vnd.x",
        "rayito/omitted",
    ]


def test_result_from_proto_keeps_raw_strings_when_json_is_invalid() -> None:
    result = result_from_proto(code_pb2.ExecutionResult(json="{not json", data="[1,"))
    assert result.json == "{not json"
    assert result.data == "[1,"
    assert result.is_main_result is False
    assert result.formats() == ["json", "data"]
    assert result.text is None
    assert result_from_proto(code_pb2.ExecutionResult()).formats() == []
    unknown = result_from_proto(code_pb2.ExecutionResult(chart="{not json"))
    assert unknown.chart is not None
    assert unknown.chart.type is ChartType.UNKNOWN


def test_result_repr_str_and_ipython_hooks() -> None:
    result = Result(text="42", png="iVBOR", json={"a": 1}, is_main_result=True)
    assert str(result) == "42"
    assert repr(result) == "Result(formats=['text', 'png', 'json'], is_main_result=True)"
    assert result._repr_png_() == "iVBOR"
    assert result._repr_json_() == {"a": 1}
    assert result._repr_html_() is None
    assert result._repr_markdown_() is None
    assert result._repr_svg_() is None
    assert result._repr_jpeg_() is None
    assert result._repr_pdf_() is None
    assert result._repr_latex_() is None
    assert result._repr_javascript_() is None
    assert str(Result()) == ""
    assert "png" in Result(png="x", chart=LineChart()).formats()
    assert "chart" in Result(png="x", chart=LineChart()).formats()


def test_error_and_context_from_proto() -> None:
    error = error_from_proto(
        code_pb2.ExecutionError(
            name="ZeroDivisionError", value="division by zero", traceback=["a", "b"]
        )
    )
    assert error == ExecutionError(
        name="ZeroDivisionError", value="division by zero", traceback="a\nb"
    )
    synthetic = error_from_proto(code_pb2.ExecutionError(name="ExecutionTimeout", value="x"))
    assert synthetic.traceback == ""
    info = code_pb2.ContextInfo(context_id="ctx-1", language="python", cwd="/tmp")
    assert context_from_proto(info) == CodeContext(id="ctx-1", language="python", cwd="/tmp")
    bare = context_from_proto(code_pb2.ContextInfo(context_id="default"))
    assert bare == CodeContext(id="default", language="python", cwd="/home/user")
    assert fallback_context("ctx-2", language=None, cwd=None) == CodeContext(
        id="ctx-2", language="python", cwd="/home/user"
    )


def test_execution_text_and_to_json() -> None:
    execution = Execution(
        results=[Result(png="x"), Result(text="42", is_main_result=True, raw={"text/plain": "42"})],
        logs=Logs(stdout=["a\n"], stderr=[]),
        error=ExecutionError(name="E", value="v", traceback="t"),
        execution_count=3,
    )
    assert execution.text == "42"
    assert Execution().text is None
    assert Execution(results=[Result(text="x")]).text is None
    document = json.loads(execution.to_json())
    assert document == {
        "results": [
            {"is_main_result": False},
            {"is_main_result": True, "text/plain": "42"},
        ],
        "logs": {"stdout": ["a\n"], "stderr": []},
        "error": {"name": "E", "value": "v", "traceback": "t"},
        "execution_count": 3,
    }
    assert json.loads(Execution().to_json())["error"] is None


def test_output_message_str() -> None:
    message = OutputMessage(line="hola\n", timestamp=1, error=True)
    assert str(message) == "hola\n"
    assert message.error is True
    assert OutputMessage(line="x", timestamp=2).error is False


def test_builder_feeds_events_in_order_and_calls_back() -> None:
    seen: list[str] = []
    builder = ExecutionBuilder(
        on_stdout=lambda m: seen.append(f"out:{m.line}"),
        on_stderr=lambda m: seen.append(f"err:{m.line}"),
        on_result=lambda r: seen.append(f"res:{r.text}"),
        on_error=lambda e: seen.append(f"error:{e.name}"),
    )
    assert builder.feed(keepalive()) is False
    assert builder.feed(started(count=7)) is False
    assert builder.execution_id == "exec-0123456789abcdef"
    assert builder.execution.execution_count == 7
    assert builder.feed(stdout("a")) is False
    assert builder.feed(keepalive()) is False
    assert builder.feed(stderr("b")) is False
    assert builder.feed(result_event(text="42", is_main_result=True)) is False
    assert builder.feed(error_event("ValueError", "boom")) is False
    assert builder.feed(end(7)) is True
    execution = builder.finish()
    assert execution.logs.stdout == ["a"]
    assert execution.logs.stderr == ["b"]
    assert execution.text == "42"
    assert execution.error == ExecutionError(name="ValueError", value="boom", traceback="l1\nl2")
    assert execution.execution_count == 7
    assert seen == ["out:a", "err:b", "res:42", "error:ValueError"]


def test_builder_keeps_started_count_on_synthetic_end_and_reports_violations() -> None:
    builder = ExecutionBuilder()
    builder.feed(started(count=4))
    builder.feed(error_event("ExecutionTimeout", "execution exceeded 2000 ms"))
    assert builder.feed(end(0)) is True
    assert builder.finish().execution_count == 4
    with pytest.raises(SandboxException, match="protocol violation"):
        builder.feed(stdout("late"))
    assert builder.feed(keepalive()) is False
    with pytest.raises(SandboxException, match="protocol violation"):
        builder.feed(end(0))
    twice = ExecutionBuilder()
    twice.feed(started())
    with pytest.raises(SandboxException, match="started"):
        twice.feed(started())
    with pytest.raises(SandboxException, match="ExecutionEnd"):
        ExecutionBuilder().finish()
    with pytest.raises(SandboxException, match="desconocido"):
        ExecutionBuilder().feed(code_pb2.ExecuteEvent())


def test_builder_tracks_ids_and_seq_for_reattach() -> None:
    builder = ExecutionBuilder(context_id="ctx-000000000001")
    assert builder.context_id == "ctx-000000000001"
    assert ExecutionBuilder().context_id == "default"
    assert builder.last_seq == 0
    with pytest.raises(SandboxException, match="started"):
        builder.reattach_request()
    builder.feed(
        code_pb2.ExecuteEvent(
            started=code_pb2.ExecutionStarted(execution_id="exec-0123456789abcdef"), seq=1
        )
    )
    builder.feed(code_pb2.ExecuteEvent(keepalive=common_pb2.KeepAlive()))
    builder.feed(code_pb2.ExecuteEvent(stdout=code_pb2.OutputChunk(text="a"), seq=2))
    assert builder.last_seq == 2
    request = builder.reattach_request()
    assert (request.context_id, request.execution_id, request.from_seq) == (
        "ctx-000000000001",
        "exec-0123456789abcdef",
        3,
    )
    assert builder.reattached == 0


def test_build_reattach_request_validates_its_arguments() -> None:
    request = build_reattach_request("default", "exec-0123456789abcdef", 0)
    assert (request.context_id, request.execution_id, request.from_seq) == (
        "default",
        "exec-0123456789abcdef",
        0,
    )
    with pytest.raises(InvalidArgumentException, match="execution_id"):
        build_reattach_request("default", "exec-xyz", 0)
    with pytest.raises(InvalidArgumentException, match="from_seq"):
        build_reattach_request("default", "exec-0123456789abcdef", -1)
    with pytest.raises(InvalidArgumentException):
        build_reattach_request("", "exec-0123456789abcdef", 0)
    failure = reattach_failure(NotFoundException("execution not found"))
    assert isinstance(failure, SandboxException)
    assert "execution not found" in str(failure)


def test_builder_propagates_callback_exceptions() -> None:
    def boom(message: OutputMessage) -> None:
        raise RuntimeError(message.line)

    builder = ExecutionBuilder(on_stdout=boom)
    builder.feed(started())
    with pytest.raises(RuntimeError, match="x"):
        builder.feed(stdout("x"))
    assert builder.execution.logs.stdout == ["x"]
