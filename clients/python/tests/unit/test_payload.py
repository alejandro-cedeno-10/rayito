from __future__ import annotations

import hashlib
import json

import pytest

from rayito._limits import RUN_HOOK_PAYLOAD_MAX_CHARS
from rayito._payload import (
    access_token_sha256,
    build_run_hook_payload,
    decode_access_token,
    encode_access_token,
    generate_access_token,
    validate_access_token,
)
from rayito.exceptions import InvalidArgumentException

TOKEN = encode_access_token(b"token")


def test_generated_token_is_urlsafe_and_unique() -> None:
    first, second = generate_access_token(), generate_access_token()
    assert first != second
    assert len(first) == 43
    assert "=" not in first and "+" not in first and "/" not in first


def test_token_sha256_hashes_the_decoded_bytes_like_rayd() -> None:
    secret = b"\x00\x01\x02" + bytes(range(3, 32))
    token = encode_access_token(secret)
    assert "=" not in token
    assert decode_access_token(token) == secret
    assert decode_access_token(token + "==") == secret
    assert access_token_sha256(token) == hashlib.sha256(secret).hexdigest()
    assert access_token_sha256(token) != hashlib.sha256(token.encode()).hexdigest()


@pytest.mark.parametrize(
    "token",
    ["", "t", "not base64!", "unit-test-access-token", "dG9rZW5=", "dG9rZW4+", "dG9rZW4/"],
)
def test_non_canonical_base64url_tokens_are_rejected(token: str) -> None:
    with pytest.raises(InvalidArgumentException, match="base64url"):
        validate_access_token(token)


def test_payload_carries_hash_not_token() -> None:
    secret = b"s3cret-token-bytes"
    token = encode_access_token(secret)
    payload = json.loads(build_run_hook_payload(access_token=token, envs={"A": "1"}))
    assert payload == {
        "v": 1,
        "token_sha256": hashlib.sha256(secret).hexdigest(),
        "user": "user",
        "workdir": "/home/user",
        "envs": {"A": "1"},
    }
    assert token not in json.dumps(payload)
    assert access_token_sha256(token) == payload["token_sha256"]


def test_payload_omits_envs_when_empty() -> None:
    assert "envs" not in json.loads(build_run_hook_payload(access_token=TOKEN, envs={}))


def test_payload_at_limit_is_accepted() -> None:
    base = len(build_run_hook_payload(access_token=TOKEN, envs={"K": ""}))
    filler = "x" * (RUN_HOOK_PAYLOAD_MAX_CHARS - base)
    text = build_run_hook_payload(access_token=TOKEN, envs={"K": filler})
    assert len(text) == RUN_HOOK_PAYLOAD_MAX_CHARS


def test_payload_over_limit_names_the_limit_and_the_escape_hatches() -> None:
    with pytest.raises(InvalidArgumentException, match="4096") as excinfo:
        build_run_hook_payload(access_token=TOKEN, envs={"K": "x" * RUN_HOOK_PAYLOAD_MAX_CHARS})
    assert "files.write" in str(excinfo.value)


def test_non_ascii_env_counts_escaped_characters() -> None:
    text = build_run_hook_payload(access_token=TOKEN, envs={"K": "ñ"})
    assert "\\u00f1" in text


@pytest.mark.parametrize("envs", [{"": "v"}, {"K": 1}])
def test_invalid_envs_rejected(envs: dict[str, object]) -> None:
    with pytest.raises(InvalidArgumentException):
        build_run_hook_payload(access_token=TOKEN, envs=envs)  # type: ignore[arg-type]


def test_empty_access_token_rejected() -> None:
    with pytest.raises(InvalidArgumentException):
        build_run_hook_payload(access_token="")


def test_payload_metadata_is_sorted_and_omitted_when_empty() -> None:
    payload = json.loads(
        build_run_hook_payload(access_token=TOKEN, envs={"E": "x"}, metadata={"b": "2", "a": "1"})
    )
    assert payload["v"] == 1
    assert payload["metadata"] == {"a": "1", "b": "2"}
    assert payload["envs"] == {"E": "x"}
    text = build_run_hook_payload(access_token=TOKEN, metadata={"b": "2", "a": "1"})
    assert text.index('"a":"1"') < text.index('"b":"2"')
    assert "metadata" not in json.loads(build_run_hook_payload(access_token=TOKEN, metadata={}))
    assert "metadata" not in json.loads(build_run_hook_payload(access_token=TOKEN))


def test_payload_budget_error_names_envs_and_metadata() -> None:
    half = "x" * (RUN_HOOK_PAYLOAD_MAX_CHARS // 2)
    with pytest.raises(InvalidArgumentException) as excinfo:
        build_run_hook_payload(access_token=TOKEN, envs={"E": half}, metadata={"m": half})
    message = str(excinfo.value)
    assert "envs" in message and "metadata" in message and "4096" in message


@pytest.mark.parametrize("metadata", [{"": "v"}, {"k": 1}, {1: "v"}])
def test_invalid_metadata_rejected(metadata: dict[object, object]) -> None:
    with pytest.raises(InvalidArgumentException, match="metadata"):
        build_run_hook_payload(access_token=TOKEN, metadata=metadata)  # type: ignore[arg-type]
