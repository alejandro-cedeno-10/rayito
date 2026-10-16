"""Vectores de `rayito sandbox proxy` compartidos con TypeScript
(`testdata/cli/proxy-vectors.json`, ADR-027): contrabando de cabeceras,
delimitación del cuerpo, reescritura de la cabecera y lista de Host/Origin.
La CLI de TypeScript reproduce estos valores, y este test fija que los de
Python no cambian sin que se regeneren a propósito."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import pytest

from rayito._authority import is_wildcard
from rayito.cli import _http_framing as framing
from rayito.cli import _proxy
from rayito.exceptions import InvalidArgumentException

REPO_ROOT_DEPTH: Final = 5
VECTORS: Final[dict[str, Any]] = json.loads(
    (
        Path(__file__).resolve().parents[REPO_ROOT_DEPTH]
        / "testdata"
        / "cli"
        / "proxy-vectors.json"
    ).read_text(encoding="utf-8")
)


def latin1(text: str) -> bytes:
    return text.encode("latin-1")


def described(framed: framing.BodyFraming) -> dict[str, Any]:
    return {"kind": framed.kind.value, "length": framed.length}


def access_of(entry: dict[str, Any]) -> _proxy.ProxyAccess:
    return _proxy.build_access(
        bind=entry["bind"],
        local_port=entry["localPort"],
        sandbox_id=entry["sandboxId"],
        allowed_hosts=entry["allowedHosts"],
        allowed_origins=entry["allowedOrigins"],
    )


@pytest.mark.parametrize("case", VECTORS["headAccepted"])
def test_accepted_heads(case: dict[str, Any]) -> None:
    head = _proxy.parse_http_head(latin1(case["raw"]))
    assert head.request_line == case["requestLine"]
    assert [list(pair) for pair in head.headers] == case["headers"]


@pytest.mark.parametrize("case", VECTORS["headRejected"], ids=lambda case: case["name"])
def test_rejected_heads(case: dict[str, Any]) -> None:
    with pytest.raises(_proxy.MalformedHttpHeadError):
        _proxy.parse_http_head(latin1(case["raw"]))


@pytest.mark.parametrize("case", VECTORS["rewriteHead"], ids=lambda case: case["name"])
def test_rewrite_head(case: dict[str, Any]) -> None:
    head = _proxy.parse_http_head(latin1(case["raw"]))
    rewritten = _proxy.rewrite_head(
        head, endpoint=case["endpoint"], jwe=case["jwe"], port=case["port"]
    )
    assert rewritten.decode("latin-1") == case["expected"]


@pytest.mark.parametrize("case", VECTORS["closingResponseHead"])
def test_closing_response_head(case: dict[str, Any]) -> None:
    head = _proxy.parse_http_head(latin1(case["raw"]))
    assert _proxy.closing_response_head(head).decode("latin-1") == case["expected"]


@pytest.mark.parametrize("case", VECTORS["requestFraming"])
def test_request_framing(case: dict[str, Any]) -> None:
    framed = framing.request_body_framing(case["transferEncodings"], case["contentLengths"])
    assert described(framed) == case["expected"]


@pytest.mark.parametrize("case", VECTORS["requestFramingRejected"])
def test_request_framing_rejected(case: dict[str, Any]) -> None:
    with pytest.raises(framing.MalformedFramingError):
        framing.request_body_framing(case["transferEncodings"], case["contentLengths"])


@pytest.mark.parametrize("case", VECTORS["responseFraming"])
def test_response_framing(case: dict[str, Any]) -> None:
    framed = framing.response_body_framing(
        case["status"],
        request_method=case["method"],
        transfer_encodings=case["transferEncodings"],
        content_lengths=case["contentLengths"],
    )
    assert described(framed) == case["expected"]


@pytest.mark.parametrize("case", VECTORS["responseStatus"])
def test_response_status(case: dict[str, Any]) -> None:
    assert framing.response_status(case["line"]) == case["expected"]


@pytest.mark.parametrize("line", VECTORS["responseStatusRejected"])
def test_response_status_rejected(line: str) -> None:
    with pytest.raises(framing.MalformedFramingError):
        framing.response_status(line)


@pytest.mark.parametrize("case", VECTORS["chunkSize"])
def test_chunk_size(case: dict[str, Any]) -> None:
    assert framing.chunk_size(latin1(case["line"])) == case["expected"]


@pytest.mark.parametrize("line", VECTORS["chunkSizeRejected"])
def test_chunk_size_rejected(line: str) -> None:
    with pytest.raises(framing.MalformedFramingError):
        framing.chunk_size(latin1(line))


@pytest.mark.parametrize("case", VECTORS["access"], ids=lambda case: case["name"])
def test_access(case: dict[str, Any]) -> None:
    access = access_of(case["input"])
    assert sorted(access.hosts) == case["hosts"]
    assert sorted(access.origins) == case["origins"]
    assert access.sandbox_host == case["sandboxHost"]
    for check in case["checks"]:
        assert access.admits_host(check["host"]) is check["hostAdmitted"]
        assert access.admits_origin(check["origin"]) is check["originAdmitted"]


@pytest.mark.parametrize("case", VECTORS["accessRejected"])
def test_access_rejected(case: dict[str, Any]) -> None:
    with pytest.raises(InvalidArgumentException):
        access_of(case["input"])


@pytest.mark.parametrize("case", VECTORS["wildcard"])
def test_wildcard(case: dict[str, Any]) -> None:
    assert is_wildcard(case["host"]) is case["expected"]


@pytest.mark.parametrize("case", VECTORS["loopbackBind"])
def test_loopback_bind(case: dict[str, Any]) -> None:
    assert _proxy.is_loopback_bind(case["bind"]) is case["expected"]


@pytest.mark.parametrize("case", VECTORS["sandboxLocalhostName"])
def test_sandbox_localhost_name(case: dict[str, Any]) -> None:
    assert _proxy.sandbox_localhost_name(case["id"]) == case["expected"]


@pytest.mark.parametrize(
    ("validate", "key"),
    [
        (_proxy.validate_proxy_port, "proxyPort"),
        (_proxy.validate_local_port, "localPort"),
        (_proxy.validate_max_connections, "maxConnections"),
    ],
)
def test_port_validation(validate: Any, key: str) -> None:
    for value in VECTORS["ports"][key]["valid"]:
        assert validate(value) == value
    for value in VECTORS["ports"][key]["invalid"]:
        with pytest.raises(InvalidArgumentException):
            validate(value)
