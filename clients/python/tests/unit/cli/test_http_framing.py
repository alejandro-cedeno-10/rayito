"""`cli/_http_framing`: dónde acaba el cuerpo de una petición o de una
respuesta (RFC 9112 §6.3) para que `rayito sandbox proxy` reenvíe un solo
mensaje."""

from __future__ import annotations

import pytest

from rayito.cli._http_framing import (
    NO_BODY,
    BodyFraming,
    BodyKind,
    MalformedFramingError,
    chunk_size,
    request_body_framing,
    response_body_framing,
    response_status,
)


@pytest.mark.parametrize(
    ("transfer_encodings", "content_lengths", "expected"),
    [
        ([], [], NO_BODY),
        ([], ["0"], NO_BODY),
        ([], ["12"], BodyFraming(BodyKind.LENGTH, 12)),
        ([], ["12", "12"], BodyFraming(BodyKind.LENGTH, 12)),
        ([], ["12, 12"], BodyFraming(BodyKind.LENGTH, 12)),
        (["chunked"], [], BodyFraming(BodyKind.CHUNKED)),
        (["gzip", "Chunked"], [], BodyFraming(BodyKind.CHUNKED)),
    ],
)
def test_request_body_framing(
    transfer_encodings: list[str], content_lengths: list[str], expected: BodyFraming
) -> None:
    assert request_body_framing(transfer_encodings, content_lengths) == expected


@pytest.mark.parametrize(
    ("transfer_encodings", "content_lengths"),
    [
        (["chunked"], ["3"]),
        (["gzip"], []),
        ([""], []),
        ([], ["3", "4"]),
        ([], ["+3"]),
        ([], [""]),
    ],
)
def test_ambiguous_request_framing_is_rejected(
    transfer_encodings: list[str], content_lengths: list[str]
) -> None:
    with pytest.raises(MalformedFramingError):
        request_body_framing(transfer_encodings, content_lengths)


@pytest.mark.parametrize(
    ("status", "method", "transfer_encodings", "content_lengths", "expected"),
    [
        (101, "GET", [], ["9"], NO_BODY),
        (204, "GET", [], [], NO_BODY),
        (304, "GET", [], ["9"], NO_BODY),
        (200, "HEAD", [], ["9"], NO_BODY),
        (200, "GET", ["chunked"], [], BodyFraming(BodyKind.CHUNKED)),
        (200, "GET", ["gzip"], [], BodyFraming(BodyKind.UNTIL_CLOSE)),
        (200, "GET", [], ["9"], BodyFraming(BodyKind.LENGTH, 9)),
        (200, "GET", [], ["0"], NO_BODY),
        (200, "GET", [], ["x"], BodyFraming(BodyKind.UNTIL_CLOSE)),
        (200, "GET", [], [], BodyFraming(BodyKind.UNTIL_CLOSE)),
    ],
)
def test_response_body_framing(
    status: int,
    method: str,
    transfer_encodings: list[str],
    content_lengths: list[str],
    expected: BodyFraming,
) -> None:
    assert (
        response_body_framing(
            status,
            request_method=method,
            transfer_encodings=transfer_encodings,
            content_lengths=content_lengths,
        )
        == expected
    )


def test_response_status() -> None:
    assert response_status("HTTP/1.1 101 Switching Protocols") == 101
    assert response_status("HTTP/1.0 200") == 200
    for line in ("garbage", "HTTP/2 200 OK", "HTTP/1.1 20 OK", ""):
        with pytest.raises(MalformedFramingError):
            response_status(line)


def test_chunk_size() -> None:
    assert chunk_size(b"1a\r\n") == 26
    assert chunk_size(b"0;ext=1\r\n") == 0
    for line in (b"\r\n", b"zz\r\n", b"-1\r\n", b"0x10\r\n"):
        with pytest.raises(MalformedFramingError):
            chunk_size(line)
