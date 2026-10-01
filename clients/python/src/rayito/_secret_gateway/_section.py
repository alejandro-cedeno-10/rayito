"""Adaptador de `gateways=` sobre `ConfigureSandbox` (ADR-023): la única
parte de esta función que toca AWS (`SecretCache.get`, en `fill()`, justo
antes de cada `Configure`) o lee un mensaje de protobuf. `sandbox_sync`/
`sandbox_async`'s `main.py` son quienes de verdad llaman al RPC; este
módulo sólo construye la sección que les pasa y el mapeo `sbx.gateways`
que reciben de vuelta.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from rayito._secret_gateway._domain import GatewayStatus, SecretGateway

if TYPE_CHECKING:
    from rayito._secrets import SecretCache
    from rayito.v1 import configure_pb2, secret_gateway_pb2

#: `_configure_base.ConfigureSection.required_flag` / `AgentFeatures` field.
REQUIRED_FLAG: Final = "secret_gateway"
#: `_configure_base.ConfigureSection.section` / `rayito.v1.ConfigSection` name.
SECTION_NAME: Final = "secret_gateway"


@dataclass(frozen=True)
class GatewaySection:
    """Implementa el `Protocol` `ConfigureSection` de `_configure_base.py`
    para `gateways=`. Guarda sólo las entradas de `gateways=` tal como las
    escribió el llamante y la `SecretCache` con la que resolverlas: ningún
    valor de cabecera vive en esta instancia entre llamadas."""

    gateways: Mapping[str, SecretGateway]
    cache: SecretCache

    @property
    def section(self) -> str:
        return SECTION_NAME

    @property
    def required_flag(self) -> str:
        return REQUIRED_FLAG

    def fill(self, request: configure_pb2.ConfigureRequest) -> None:
        """Resuelve cada cabecera (`SecretCache.get`: un acierto no llama a
        AWS) y rellena `request.secret_gateway`. El valor resuelto sólo
        vive en este mensaje, que sale por el canal autenticado de
        `ConfigureSandbox` y que `ConfigureGrpc` nunca registra."""
        from rayito.v1 import secret_gateway_pb2  # perezoso: sólo si gateways= se usa

        def route(name: str, gateway: SecretGateway) -> secret_gateway_pb2.SecretGatewayRoute:
            headers = {header: self.cache.get(secret) for header, secret in gateway.headers.items()}
            allow = [
                secret_gateway_pb2.SecretGatewayAllowRule(method=method, path=path)
                for method, path in gateway.allow
            ]
            return secret_gateway_pb2.SecretGatewayRoute(
                name=name,
                upstream=gateway.upstream,
                headers=headers,
                allow=allow,
                rate_per_minute=gateway.rate_per_minute,
            )

        request.secret_gateway.CopyFrom(
            secret_gateway_pb2.SecretGatewayConfig(
                routes=[route(name, gateway) for name, gateway in self.gateways.items()]
            )
        )


def gateway_statuses_from_proto(
    status: secret_gateway_pb2.SecretGatewayStatus,
) -> dict[str, GatewayStatus]:
    """`SecretGatewayStatus` -> `{nombre: GatewayStatus}`, tal como lo
    devuelve `Configure` (en `results`, nada; el puerto sólo viaja en
    `ConfigureStatus`, que `create()` pide justo después de aplicar la
    sección) o `ConfigureStatus`."""
    return {
        route.name: GatewayStatus(
            port=route.port,
            last_error_class=route.last_error_class or None,
        )
        for route in status.routes
    }


@dataclass(frozen=True)
class GatewaySectionFactory:
    """Lo que `_feature_options.plan_features` pone en
    `FeaturePlan.configure_sections` por cada `gateways=`: un
    `ConfigureSection` todavía sin resolver, a la espera de la
    `SecretCache` que `create()`/`take()` ya calculan para `secrets=` (la
    misma, nunca una segunda). Fija la convención que sigue cualquier
    entrada de `configure_sections` que necesite algo resuelto más tarde
    que `plan_features` (`options.gateways is not None`, antes de abrir
    ningún canal): un invocable de un solo argumento, la `SecretCache`
    resuelta, que devuelve el `ConfigureSection` de verdad. `main.py` es
    quien lo invoca, justo antes de la llamada a `Configure`.
    """

    gateways: Mapping[str, SecretGateway]

    def __call__(self, cache: SecretCache) -> GatewaySection:
        return GatewaySection(gateways=self.gateways, cache=cache)


class GatewayHandle(Mapping[str, GatewayStatus]):
    """`sbx.gateways`: un mapeo de sólo lectura `nombre -> GatewayStatus`.
    Sin `gateways=` es siempre un `GatewayHandle` vacío cuyo `refresh()`/
    `arefresh()` no hacen nada, así que llamarlos nunca es una rama especial
    para quien los use. Con `gateways=`, resuelven otra vez cada cabecera
    (una `SecretCache` que ya venció la relee) y mandan un `Configure`
    nuevo — la forma de rotar un secreto sin recrear el sandbox.

    `Sandbox` (síncrono) pasa `refresher`; `AsyncSandbox` pasa
    `async_refresher` (mismo patrón que `SecretCache.get`/`aget`): cada uno
    sabe envolver al otro, así que da igual cuál dio el handle.
    """

    def __init__(
        self,
        statuses: Mapping[str, GatewayStatus],
        *,
        refresher: Callable[[], Mapping[str, GatewayStatus]] | None = None,
        async_refresher: Callable[[], Awaitable[Mapping[str, GatewayStatus]]] | None = None,
    ) -> None:
        self._statuses = dict(statuses)
        self._refresher = refresher
        self._async_refresher = async_refresher

    def __getitem__(self, name: str) -> GatewayStatus:
        return self._statuses[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._statuses)

    def __len__(self) -> int:
        return len(self._statuses)

    def __repr__(self) -> str:
        return f"GatewayHandle({self._statuses!r})"

    def refresh(self) -> None:
        """Para `Sandbox`. Un `AsyncSandbox` usa `arefresh()`: llamar a
        `refresh()` desde una corrutina bloquearía su propio loop."""
        if self._refresher is not None:
            self._statuses = dict(self._refresher())
        elif self._async_refresher is not None:
            raise RuntimeError(
                "este GatewayHandle es de un AsyncSandbox: usa 'await sbx.gateways.arefresh()'"
            )

    async def arefresh(self) -> None:
        """Para `AsyncSandbox`. Sobre un handle síncrono, corre el
        `refresher` en un hilo (como `SecretCache.aget` sobre `get`)."""
        if self._async_refresher is not None:
            self._statuses = dict(await self._async_refresher())
        elif self._refresher is not None:
            self._statuses = dict(await asyncio.to_thread(self._refresher))


EMPTY_GATEWAYS: Final = GatewayHandle({})
