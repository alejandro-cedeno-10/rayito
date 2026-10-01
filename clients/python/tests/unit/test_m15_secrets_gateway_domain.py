"""`rayito._secret_gateway._domain`: validación pura de `SecretGateway`,
`gateways=` y `GatewayStatus.url`. Nada de AWS: ninguna de estas pruebas
construye un cliente ni toca la red."""

from __future__ import annotations

import pytest

from rayito._secret_gateway._domain import (
    MAX_ALLOW_RULES_PER_ROUTE,
    MAX_HEADERS_PER_ROUTE,
    MAX_RATE_PER_MINUTE,
    MAX_ROUTES_PER_GATEWAY,
    GatewayStatus,
    SecretGateway,
    validate_gateways,
    validate_route_name,
)
from rayito.exceptions import InvalidArgumentException


def gateway(**overrides: object) -> SecretGateway:
    defaults: dict[str, object] = {
        "upstream": "https://api.anthropic.com",
        "headers": {"x-api-key": "anthropic"},
        "allow": [("POST", "/v1/messages")],
    }
    defaults.update(overrides)
    return SecretGateway(**defaults)  # type: ignore[arg-type]


def test_a_well_formed_gateway_is_accepted() -> None:
    gw = gateway(rate_per_minute=600)
    assert gw.upstream == "https://api.anthropic.com"
    assert gw.rate_per_minute == 600


def test_upstream_must_be_https_with_no_path_or_query() -> None:
    for bad in [
        "http://api.anthropic.com",
        "https://api.anthropic.com/v1",
        "https://api.anthropic.com?x=1",
        "https://",
        "not-a-url",
    ]:
        with pytest.raises(InvalidArgumentException):
            gateway(upstream=bad)


def test_headers_must_be_non_empty_and_bounded() -> None:
    with pytest.raises(InvalidArgumentException):
        gateway(headers={})
    with pytest.raises(InvalidArgumentException):
        gateway(headers={f"h{i}": "s" for i in range(MAX_HEADERS_PER_ROUTE + 1)})


def test_allow_must_be_non_empty_and_bounded_with_valid_entries() -> None:
    with pytest.raises(InvalidArgumentException):
        gateway(allow=[])
    with pytest.raises(InvalidArgumentException):
        gateway(allow=[("POST", "/x")] * (MAX_ALLOW_RULES_PER_ROUTE + 1))
    with pytest.raises(InvalidArgumentException):
        gateway(allow=[("get", "/x")])  # lowercase method
    with pytest.raises(InvalidArgumentException):
        gateway(allow=[("GET", "x")])  # relative path


def test_rate_per_minute_zero_means_default_and_out_of_range_is_rejected() -> None:
    assert gateway(rate_per_minute=0).rate_per_minute == 0
    with pytest.raises(InvalidArgumentException):
        gateway(rate_per_minute=MAX_RATE_PER_MINUTE + 1)
    with pytest.raises(InvalidArgumentException):
        gateway(rate_per_minute=-1)


def test_validate_route_name_accepts_lowercase_kebab_and_rejects_the_rest() -> None:
    assert validate_route_name("anthropic-prod") == "anthropic-prod"
    for bad in ["", "Anthropic", "a_b", "a b", "a" * 65]:
        with pytest.raises(InvalidArgumentException):
            validate_route_name(bad)


def test_validate_gateways_rejects_empty_too_many_and_bad_values() -> None:
    with pytest.raises(InvalidArgumentException):
        validate_gateways({})
    with pytest.raises(InvalidArgumentException):
        validate_gateways({f"g{i}": gateway() for i in range(MAX_ROUTES_PER_GATEWAY + 1)})
    with pytest.raises(InvalidArgumentException):
        validate_gateways({"a": object()})  # type: ignore[dict-item]


def test_validate_gateways_accepts_a_well_formed_mapping() -> None:
    mapping = {"anthropic": gateway()}
    assert validate_gateways(mapping) is mapping


def test_gateway_status_url_is_loopback_and_never_prints_a_secret() -> None:
    status = GatewayStatus(port=54321)
    assert status.url == "http://127.0.0.1:54321"
    assert status.last_error_class is None
