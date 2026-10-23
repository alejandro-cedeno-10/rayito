"""Higiene de logging del host del agente en modo `serve`: los loggers de
clientes HTTP (`httpx`, `httpcore`) escriben a nivel INFO la URL de cada
petición con su consulta, donde algunas APIs llevan credenciales. El host los
sube a WARNING al arrancar y sus propias líneas siguen saliendo."""

from __future__ import annotations

import argparse
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from rayito._agent._runner.agent_host import __main__ as host_main
from rayito._agent._runner.agent_host import serve

URL_WITH_TOKEN = "https://hooks.example.com/notify?token=not-a-real-token"


@pytest.fixture(autouse=True)
def restore_http_logger_levels() -> Iterator[None]:
    loggers = [logging.getLogger(name) for name in serve.QUIET_HTTP_LOGGERS]
    levels = [logger.level for logger in loggers]
    yield
    for logger, level in zip(loggers, levels, strict=True):
        logger.setLevel(level)


def _start_serve_with_invalid_config(tmp_path: Path) -> int:
    """Corre el arranque de `serve` hasta validar `host.json`, que falla: lo
    que interesa es la configuración de logging que deja montada."""
    args = argparse.Namespace(host_config=str(tmp_path / "no-existe.json"), port=1)
    return host_main.serve(host_main._package(), args)


def test_the_quiet_loggers_are_the_http_clients() -> None:
    assert serve.QUIET_HTTP_LOGGERS == ("httpx", "httpcore")


def test_http_client_info_records_with_credentials_are_not_emitted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    assert _start_serve_with_invalid_config(tmp_path) == 2
    for name in serve.QUIET_HTTP_LOGGERS:
        logging.getLogger(name).info('HTTP Request: GET %s "HTTP/1.1 200 OK"', URL_WITH_TOKEN)
        logging.getLogger(f"{name}.child").info("GET %s", URL_WITH_TOKEN)
    logging.getLogger(serve.LOGGER_NAME).info("ready")
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "token=" not in logged
    assert "invalid_host_config" in logged
    assert "ready" in logged


def test_http_client_warnings_still_reach_the_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    _start_serve_with_invalid_config(tmp_path)
    logging.getLogger("httpx").warning("retrying")
    assert [r.getMessage() for r in caplog.records if r.name == "httpx"] == ["retrying"]


def test_agent_code_can_turn_the_http_loggers_back_on(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    _start_serve_with_invalid_config(tmp_path)
    logging.getLogger("httpx").setLevel(logging.INFO)
    logging.getLogger("httpx").info("HTTP Request: GET https://example.com/")
    assert any(r.name == "httpx" for r in caplog.records)
