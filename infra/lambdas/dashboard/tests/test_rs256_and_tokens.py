"""La verificación de tokens de Cognito: firma RS256 con sólo la biblioteca
estándar, y cada comprobación del token (emisor, uso, cliente, caducidad)."""

from __future__ import annotations

import _stack_path  # noqa: F401
import pytest
from dashboard_stack.rs256 import b64url_decode, verify_rs256
from dashboard_stack.tokens import (
    Caller,
    SigningKey,
    TokenError,
    TokenVerifier,
    signing_key_from_jwk,
)
from stack_signing import CLIENT_ID, ISSUER, KEY_ID, NOW, Signer, b64url

OTHER_SUBJECT = "subject-0002"


@pytest.fixture(scope="module")
def signer() -> Signer:
    return Signer()


class OneKey:
    def __init__(self, key: SigningKey | None) -> None:
        self._key = key

    def key(self, kid: str) -> SigningKey | None:
        return self._key if kid == KEY_ID else None


class Clients:
    def __init__(self, *ids: str) -> None:
        self.ids = frozenset(ids)

    def client_ids(self) -> frozenset[str]:
        return self.ids


def verifier(signer: Signer, *, clients: Clients | None = None) -> TokenVerifier:
    return TokenVerifier(
        issuer=ISSUER,
        keys=OneKey(signing_key_from_jwk(signer.jwk())),
        clients=clients or Clients(CLIENT_ID),
        clock=lambda: NOW,
    )


def reason(signer: Signer, token: str, **kwargs: Clients) -> str:
    with pytest.raises(TokenError) as raised:
        verifier(signer, **kwargs).verify(token)
    return raised.value.code


def test_a_valid_token_yields_the_caller_with_its_groups(signer: Signer) -> None:
    token = signer.token({"cognito:groups": ["rayito-dashboard-actions"]})
    assert verifier(signer).verify(token) == Caller(
        "subject-0001", "ada@example.com", frozenset({"rayito-dashboard-actions"})
    )


def test_the_signature_matches_the_standard_library_check(signer: Signer) -> None:
    message = b"header.payload"
    numbers = signer.numbers
    assert verify_rs256(message, signer.sign(message), modulus=numbers.n, exponent=numbers.e)
    assert not verify_rs256(b"other", signer.sign(message), modulus=numbers.n, exponent=numbers.e)


def test_a_tampered_payload_is_rejected(signer: Signer) -> None:
    head, _, tail = signer.token().split(".")
    forged = b64url(b'{"sub":"admin"}')
    assert reason(signer, f"{head}.{forged}.{tail}") == "signature"


def test_a_signature_with_the_wrong_length_or_value_is_rejected(signer: Signer) -> None:
    head, body, signature = signer.token().split(".")
    raw = b64url_decode(signature)
    assert reason(signer, f"{head}.{body}.{b64url(raw[:-1])}") == "signature"
    assert reason(signer, f"{head}.{body}.{b64url(bytes(len(raw)))}") == "signature"


def test_a_weak_key_is_never_trusted() -> None:
    assert not verify_rs256(b"m", b"\x01" * 64, modulus=(1 << 511) | 1, exponent=65537)


@pytest.mark.parametrize("alg", ["none", "HS256", "RS512"])
def test_only_rs256_is_accepted(signer: Signer, alg: str) -> None:
    assert reason(signer, signer.token(header={"alg": alg})) == "algorithm"


def test_an_unknown_key_id_is_rejected(signer: Signer) -> None:
    assert reason(signer, signer.token(header={"kid": "other"})) == "unknown_key"


@pytest.mark.parametrize(
    ("claims", "code"),
    [
        ({"iss": "https://example.com/pool"}, "issuer"),
        ({"token_use": "id"}, "token_use"),
        ({"exp": NOW - 3600}, "expired"),
        ({"nbf": NOW + 3600}, "not_yet_valid"),
        ({"sub": ""}, "subject"),
        ({"client_id": "someone-elses-client"}, "audience"),
        ({"exp": "soon"}, "malformed"),
    ],
)
def test_each_claim_is_checked(signer: Signer, claims: dict[str, object], code: str) -> None:
    assert reason(signer, signer.token(claims)) == code


def test_a_token_that_is_not_three_segments_or_too_large_is_rejected(signer: Signer) -> None:
    assert reason(signer, "a.b") == "malformed"
    assert reason(signer, "not base64!.b.c") == "malformed"
    assert reason(signer, "a" * 100_000) == "too_large"


def test_the_leeway_tolerates_a_small_clock_skew(signer: Signer) -> None:
    assert verifier(signer).verify(signer.token({"exp": NOW - 1})).subject == "subject-0001"
