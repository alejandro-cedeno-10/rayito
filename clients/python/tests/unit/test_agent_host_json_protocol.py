"""El protocolo `json` del driver `http-app` (0.14 D4): una app con su propio
contrato (`POST /chat {question}` -> `{answer}`) responde a `invoke()` por
`/invocations` y, a la vez, sus demás rutas (`/health`, las suyas) siguen
alcanzables por la misma URL. El `agentcore` explícito conserva su
comportamiento. El host se prueba una vez, aquí (`parity-checklist.md`)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rayito._agent._runner.agent_host.drivers import http_app
from rayito._agent._runner.agent_host.protocol import AgentFailure, Request, RunState
from tests.unit.agent_host_support import (
    invoke,
    json_app_driver,
    request,
    running_service,
)


def _run(driver: http_app.Driver, prompt: str = "hola") -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    driver.run(Request(prompt=prompt), events.append, RunState())
    return events


def test_json_translates_invocations_to_the_app_contract(tmp_path: Path) -> None:
    driver = json_app_driver(tmp_path)
    try:
        events = _run(driver, "buenas")
    finally:
        driver.cancel()
    assert [event["type"] for event in events] == [
        "session",
        "step_started",
        "text",
        "step_finished",
    ]
    assert events[2]["text"] == "eco: buenas"
    assert events[3]["usage"] is None


def test_the_session_id_travels_in_the_body(tmp_path: Path) -> None:
    driver = json_app_driver(tmp_path, response_field="session")
    session = "app_" + "ab" * 16
    events: list[dict[str, Any]] = []
    try:
        driver.run(Request(prompt="x", session_id=session), events.append, RunState())
    finally:
        driver.cancel()
    assert events[2]["text"] == session


def test_the_default_request_field_is_prompt(tmp_path: Path) -> None:
    driver = json_app_driver(tmp_path, request_field=None, response_field="answer")
    try:
        events = _run(driver, "x")
    finally:
        driver.cancel()
    assert events[2]["text"] == "eco: None"


def test_a_missing_response_field_is_an_invalid_response(tmp_path: Path) -> None:
    driver = json_app_driver(tmp_path, response_field="no_existe")
    try:
        with pytest.raises(AgentFailure) as error:
            _run(driver)
    finally:
        driver.cancel()
    assert (error.value.reason, error.value.detail_code) == ("protocol_error", "invalid_response")


def test_a_dotted_response_field_reads_nested_values() -> None:
    assert http_app.field_text({"data": {"reply": "hola"}}, "data.reply") == "hola"
    assert http_app.field_text({"data": {"reply": 3}}, "data.reply") is None
    assert http_app.field_text({"data": "x"}, "data.reply") is None


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"answer": "a", "text": "t"}, "a"),
        ({"output": "o", "text": "t"}, "o"),
        ({"text": "t", "response": "r"}, "t"),
        ({"result": "r", "content": "c"}, "r"),
        ({"content": "c"}, "c"),
        ({"otra": 1}, '{"otra": 1}'),
    ],
)
def test_the_first_candidate_field_wins(payload: dict[str, Any], expected: str) -> None:
    assert http_app.extract_text(payload, http_app.JSON_TEXT_KEYS) == expected


def test_json_forwards_the_apps_own_routes_through_the_host(tmp_path: Path) -> None:
    driver = json_app_driver(tmp_path)
    driver.warm()
    with running_service(driver) as (_, port):
        try:
            status, _, body = request(port, "GET", "/health")
            assert (status, json.loads(body)) == (200, {"ok": True})
            status, _, body = request(port, "POST", "/chat", {"question": "directo"})
            assert (status, json.loads(body)["answer"]) == (200, "eco: directo")
            status, _, body = invoke(port, "por el host")
            assert (status, body["text"]) == (200, "eco: por el host")
            assert request(port, "GET", "/ping")[0] == 200
        finally:
            driver.cancel()


def test_the_app_gets_its_port_from_the_configured_variable(tmp_path: Path) -> None:
    driver = json_app_driver(tmp_path)
    driver.warm()
    with running_service(driver) as (_, port):
        try:
            _, _, body = request(port, "GET", "/otra-ruta")
            assert json.loads(body)["port_env"] == str(driver._config["port"])
        finally:
            driver.cancel()


@pytest.mark.parametrize("protocol", ["agentcore", "openai-chat"])
def test_the_other_translating_protocols_do_not_forward(tmp_path: Path, protocol: str) -> None:
    driver = json_app_driver(tmp_path, protocol=protocol)
    driver.warm()
    assert driver.forwards is False
    with running_service(driver) as (_, port):
        try:
            assert request(port, "GET", "/health")[0] == 404
        finally:
            driver.cancel()


def test_raw_and_json_forward_and_only_raw_cannot_be_invoked(tmp_path: Path) -> None:
    raw = json_app_driver(tmp_path, protocol="raw")
    assert (raw.raw, raw.forwards) == (True, True)
    json_driver = json_app_driver(tmp_path, protocol="json")
    assert (json_driver.raw, json_driver.forwards) == (False, True)


def test_an_unknown_protocol_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(AgentFailure, match="invalid_protocol"):
        json_app_driver(tmp_path, protocol="grpc")
