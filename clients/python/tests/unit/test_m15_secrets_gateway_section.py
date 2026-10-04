"""`rayito._secret_gateway._section`: `GatewaySection.fill` resuelve cada
cabecera por `SecretCache.get` (un acierto no llama a AWS) y construye el
`SecretGatewayConfig`; `GatewayHandle` expone el resultado y su
`refresh()`/`arefresh()`."""

from __future__ import annotations

from typing import Any, cast

import pytest

from rayito._secret_gateway._domain import GatewayStatus, SecretGateway
from rayito._secret_gateway._section import (
    EMPTY_GATEWAYS,
    GatewayHandle,
    GatewaySection,
    GatewaySectionFactory,
    gateway_statuses_from_proto,
)
from rayito._secrets import SecretCache, SecretStore
from rayito.v1 import configure_pb2, secret_gateway_pb2

from .fake_secrets import SENTINEL_VALUE, FakeSecretsManager, SpySession


def cache_over(api: FakeSecretsManager) -> SecretCache:
    return SecretCache(store=SecretStore(session=cast(Any, SpySession(api=api))))


@pytest.fixture
def api() -> FakeSecretsManager:
    fake = FakeSecretsManager()
    fake.put("rayito/anthropic", SENTINEL_VALUE)
    return fake


def test_fill_resolves_headers_and_builds_the_wire_message(api: FakeSecretsManager) -> None:
    cache = cache_over(api)
    gateways = {
        "anthropic": SecretGateway(
            upstream="https://api.anthropic.com",
            headers={"x-api-key": "anthropic"},
            allow=[("POST", "/v1/messages")],
            rate_per_minute=600,
        )
    }
    section = GatewaySection(gateways=gateways, cache=cache)
    request = configure_pb2.ConfigureRequest()
    section.fill(request)
    route = request.secret_gateway.routes[0]
    assert route.name == "anthropic"
    assert route.upstream == "https://api.anthropic.com"
    assert route.headers["x-api-key"] == SENTINEL_VALUE
    assert route.allow[0].method == "POST"
    assert route.allow[0].path == "/v1/messages"
    assert route.rate_per_minute == 600


def test_fill_with_a_cached_value_makes_no_second_aws_call(api: FakeSecretsManager) -> None:
    cache = cache_over(api)
    gateways = {
        "a": SecretGateway(
            upstream="https://example.com",
            headers={"x-api-key": "anthropic"},
            allow=[("GET", "/x")],
        )
    }
    section = GatewaySection(gateways=gateways, cache=cache)
    section.fill(configure_pb2.ConfigureRequest())
    calls_after_first = api.calls.get("GetSecretValue", 0)
    section.fill(configure_pb2.ConfigureRequest())
    assert api.calls.get("GetSecretValue", 0) == calls_after_first


def test_gateway_section_factory_binds_the_resolved_cache(api: FakeSecretsManager) -> None:
    cache = cache_over(api)
    factory = GatewaySectionFactory(
        {
            "a": SecretGateway(
                upstream="https://example.com",
                headers={"x-api-key": "anthropic"},
                allow=[("GET", "/x")],
            )
        }
    )
    section = factory(cache)
    assert isinstance(section, GatewaySection)
    assert section.cache is cache
    assert section.section == "secret_gateway"
    assert section.required_flag == "secret_gateway"


def test_gateway_statuses_from_proto_maps_routes_and_empty_error_to_none() -> None:
    status = secret_gateway_pb2.SecretGatewayStatus(
        routes=[
            secret_gateway_pb2.SecretGatewayRouteStatus(name="a", port=4321, last_error_class=""),
            secret_gateway_pb2.SecretGatewayRouteStatus(
                name="b", port=0, last_error_class="rate_limited"
            ),
        ]
    )
    result = gateway_statuses_from_proto(status)
    assert result == {
        "a": GatewayStatus(port=4321, last_error_class=None),
        "b": GatewayStatus(port=0, last_error_class="rate_limited"),
    }


def test_empty_handle_is_empty_and_refresh_is_a_no_op() -> None:
    assert len(EMPTY_GATEWAYS) == 0
    EMPTY_GATEWAYS.refresh()  # no debe lanzar


def test_handle_refresh_calls_the_sync_refresher_and_replaces_the_statuses() -> None:
    calls = []

    def refresher() -> dict[str, GatewayStatus]:
        calls.append(1)
        return {"a": GatewayStatus(port=999)}

    handle = GatewayHandle({"a": GatewayStatus(port=1)}, refresher=refresher)
    assert handle["a"].port == 1
    handle.refresh()
    assert handle["a"].port == 999
    assert len(calls) == 1


def test_handle_refresh_on_an_async_only_handle_raises_runtime_error() -> None:
    async def arefresher() -> dict[str, GatewayStatus]:
        return {}

    handle = GatewayHandle({}, async_refresher=arefresher)
    with pytest.raises(RuntimeError, match="arefresh"):
        handle.refresh()


@pytest.mark.asyncio
async def test_handle_arefresh_calls_the_async_refresher() -> None:
    async def arefresher() -> dict[str, GatewayStatus]:
        return {"a": GatewayStatus(port=42)}

    handle = GatewayHandle({}, async_refresher=arefresher)
    await handle.arefresh()
    assert handle["a"].port == 42


@pytest.mark.asyncio
async def test_handle_arefresh_wraps_a_sync_refresher_in_a_thread() -> None:
    def refresher() -> dict[str, GatewayStatus]:
        return {"a": GatewayStatus(port=7)}

    handle = GatewayHandle({}, refresher=refresher)
    await handle.arefresh()
    assert handle["a"].port == 7
