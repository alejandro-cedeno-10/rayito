"""`parse_build_failure`/`classify_ready_failure` (m15-templates, TPL-1/Q83,
TPL-5/Q85): releer el log de BuildKit para explicar un build fallido, sin
volver a correr nada."""

from __future__ import annotations

from rayito._templates._logs import classify_ready_failure, parse_build_failure


def test_parse_build_failure_extracts_the_last_step_command_and_exit_code() -> None:
    lines = [
        "#5 [3/6] RUN pip install --no-cache-dir not-a-real-package",
        "#5 1.234 ERROR: could not find a version that satisfies the requirement",
        "#5 ERROR: executor failed running [/bin/sh -c pip install --no-cache-dir "
        "not-a-real-package]: exit code: 1",
    ]
    detail = parse_build_failure(lines)
    assert detail.step == 3
    assert detail.command == "RUN pip install --no-cache-dir not-a-real-package"
    assert detail.exit_code == 1
    assert detail.log_tail is not None and lines[-1] in detail.log_tail


def test_parse_build_failure_with_no_lines_is_empty() -> None:
    detail = parse_build_failure([])
    assert detail == parse_build_failure([])
    assert detail.step is None
    assert detail.log_tail is None


def test_classify_ready_failure_distinguishes_client_and_server_errors() -> None:
    assert (
        classify_ready_failure(
            "Ready hook check failed: the application returned a server error (HTTP 5xx) response"
        )
        == "ready_server_error"
    )
    assert (
        classify_ready_failure(
            "Ready hook check failed: the application returned a client error (HTTP 4xx) response"
        )
        == "ready_client_error"
    )
    assert classify_ready_failure("The container image build failed.") is None
    assert classify_ready_failure(None) is None
