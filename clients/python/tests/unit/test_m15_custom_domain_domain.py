"""`rayito._custom_domain._domain` (m15-custom-domain, ADR-024): dominio puro,
sin `boto3`. Los casos de hostname vienen de `testdata/custom-domain/hostnames.json`
(compartidos con `m15-custom-domain.test.ts`, TypeScript)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rayito._custom_domain._domain import (
    MAX_ALTERNATE_DOMAIN_NAMES,
    MAX_KVS_VALUE_BYTES,
    RouteMetadata,
    check_kvs_value_size,
    kvs_json_key,
    kvs_meta_key,
    route_host,
    route_label,
    traffic_token_digest,
    validate_alias,
    validate_alternate_domain_names,
    validate_public_domain,
    validate_route_port,
)
from rayito.exceptions import CustomDomainException, InvalidArgumentException

TESTDATA_PATH = (
    Path(__file__).resolve().parents[4] / "testdata" / "custom-domain" / "hostnames.json"
)
TESTDATA = json.loads(TESTDATA_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", TESTDATA["valid"], ids=lambda c: c["host"])
def test_route_host_matches_the_shared_fixture(case: dict[str, object]) -> None:
    assert route_host(case["alias"], case["port"], case["publicDomain"]) == case["host"]  # type: ignore[arg-type]


@pytest.mark.parametrize("alias", TESTDATA["invalidAlias"])
def test_invalid_aliases_are_rejected(alias: str) -> None:
    with pytest.raises(InvalidArgumentException):
        validate_alias(alias)


@pytest.mark.parametrize("port", TESTDATA["invalidPort"])
def test_invalid_ports_are_rejected(port: int) -> None:
    with pytest.raises(InvalidArgumentException):
        validate_route_port(port)


@pytest.mark.parametrize("public_domain", TESTDATA["invalidPublicDomain"])
def test_invalid_public_domains_are_rejected(public_domain: str) -> None:
    with pytest.raises(InvalidArgumentException):
        validate_public_domain(public_domain)


ALTERNATE = TESTDATA["alternateDomainNames"]


@pytest.mark.parametrize("names", ALTERNATE["valid"], ids=repr)
def test_valid_alternate_domain_names_match_the_shared_fixture(names: list[str]) -> None:
    assert validate_alternate_domain_names(names, ALTERNATE["publicDomain"]) == tuple(names)


@pytest.mark.parametrize("names", ALTERNATE["invalid"], ids=repr)
def test_invalid_alternate_domain_names_are_rejected(names: list[str]) -> None:
    with pytest.raises(InvalidArgumentException):
        validate_alternate_domain_names(names, ALTERNATE["publicDomain"])


def test_a_bare_string_is_not_a_list_of_alternate_domain_names() -> None:
    # Un `str` también es una `Sequence[str]`: sin esta guarda, cada letra
    # se validaría como un hostname.
    with pytest.raises(InvalidArgumentException):
        validate_alternate_domain_names("8000-ws-7.sbx.example.com", "sbx.example.com")


def test_more_alternate_domain_names_than_cloudfront_allows_are_rejected() -> None:
    names = [f"{index}-a.sbx.example.com" for index in range(1, MAX_ALTERNATE_DOMAIN_NAMES + 2)]
    with pytest.raises(InvalidArgumentException, match="demasiados"):
        validate_alternate_domain_names(names, "sbx.example.com")


def test_route_label_is_port_dash_alias() -> None:
    assert route_label("ws-7", 8000) == "8000-ws-7"


def test_kvs_keys_are_prefixed_and_distinct() -> None:
    label = route_label("ws-7", 8000)
    assert kvs_json_key(label) == "j:8000-ws-7"
    assert kvs_meta_key(label) == "m:8000-ws-7"
    assert kvs_json_key(label) != kvs_meta_key(label)


def test_traffic_token_digest_is_sha256_hex_or_empty() -> None:
    assert traffic_token_digest(None) == ""
    digest = traffic_token_digest("un-token")
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)
    # Determinista: el mismo token siempre produce el mismo hash.
    assert traffic_token_digest("un-token") == digest


def test_route_metadata_round_trips_through_json() -> None:
    metadata = RouteMetadata(
        endpoint="10.0.0.1.lambda-url", traffic_token_sha256="", expires_at=1234
    )
    encoded = metadata.encode()
    decoded = RouteMetadata.decode(encoded)
    assert decoded == metadata


def test_route_metadata_uses_short_keys_to_fit_the_kvs_limit() -> None:
    metadata = RouteMetadata(endpoint="e" * 1100, traffic_token_sha256="t" * 64, expires_at=1)
    with pytest.raises(CustomDomainException, match="kvs_value_too_large"):
        metadata.encode()


def test_check_kvs_value_size_accepts_a_real_jwe_sized_value() -> None:
    # DOM-1: un JWE real mide 823 B; muy por debajo del límite.
    check_kvs_value_size("x" * 823)
    assert MAX_KVS_VALUE_BYTES == 1024


def test_check_kvs_value_size_rejects_oversized_values() -> None:
    with pytest.raises(CustomDomainException):
        check_kvs_value_size("x" * (MAX_KVS_VALUE_BYTES + 1))
