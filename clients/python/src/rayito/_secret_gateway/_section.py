"""Adaptador de `gateways=` sobre `ConfigureSandbox` (ADR-023): la única
parte de esta función que toca AWS (`SecretCache.get`, en `fill()`, justo
antes de cada `Configure`) o lee un mensaje de protobuf. `sandbox_sync`/
`sandbox_async`'s `main.py` son quienes de verdad llaman al RPC; este
módulo sólo construye la sección que les pasa y el mapeo `sbx.gateways`
que reciben de vuelta (`GatewaySection.after_apply`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from rayito._configure_base import ImmediateSection
from rayito._secret_gateway._domain import GatewayStatus, SecretGateway

if TYPE_CHECKING:
    from rayito._configure_base import AgentFeatures, AsyncReapply, Reapply, SectionApplied
    from rayito._secrets import SecretCache
    from rayito.v1 import configure_pb2, secret_gateway_pb2

#: `_configure_base.ConfigureSection.required_flag` / `AgentFeatures` field.
REQUIRED_FLAG: Final = "secret_gateway"
#: `_configure_base.ConfigureSection.section` / `rayito.v1.ConfigSection` name.
SECTION_NAME: Final = "secret_gateway"


@dataclass(frozen=True)
class GatewaySection(ImmediateSection):
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

    def forget_cached_values(self) -> None:
        """Descarta de `cache` cada secreto que estas rutas inyectan, para que
        el próximo `fill()` los relea de Secrets Manager aunque su TTL
        (`SecretCache.ttl_seconds`, 300 s por defecto) no haya vencido: sin
        esto, `sbx.gateways.refresh()` justo después de
        `SecretStore.update()` volvería a mandar el valor viejo."""
        for gateway in self.gateways.values():
            for secret in gateway.headers.values():
                self.cache.invalidate(secret)

    def after_apply(self, applied: SectionApplied) -> GatewayHandle:
        """`PostApplySection`: el `sbx.gateways` que `create()`/`take()`
        guardan. Su `refresh()`/`arefresh()` olvida los valores en caché y
        vuelve a mandar esta sección (`applied.reapply`/`areapply`), así que
        siempre empuja la versión actual de cada secreto."""
        return GatewayHandle(
            gateway_statuses_from_proto(applied.status.secret_gateway),
            refresher=None if applied.reapply is None else self._refresher(applied.reapply),
            async_refresher=(
                None if applied.areapply is None else self._async_refresher(applied.areapply)
            ),
        )

    def _refresher(self, reapply: Reapply) -> Callable[[], Mapping[str, GatewayStatus]]:
        def refresh() -> Mapping[str, GatewayStatus]:
            self.forget_cached_values()
            return gateway_statuses_from_proto(reapply().secret_gateway)

        return refresh

    def _async_refresher(
        self, areapply: AsyncReapply
    ) -> Callable[[], Awaitable[Mapping[str, GatewayStatus]]]:
        async def refresh() -> Mapping[str, GatewayStatus]:
            self.forget_cached_values()
            return gateway_statuses_from_proto((await areapply()).secret_gateway)

        return refresh


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
    `ConfigureSection` todavía sin resolver
    (`_configure_base.SectionFactory`), a la espera de la `SecretCache` que
    `create()`/`take()` ya calculan para `secrets=` (la misma, nunca una
    segunda). `_configure_base.resolve_sections` lo invoca justo antes de
    la llamada a `Configure`.
    """

    gateways: Mapping[str, SecretGateway]

    def __call__(self, cache: SecretCache) -> GatewaySection:
        return GatewaySection(gateways=self.gateways, cache=cache)


class GatewayHandle(Mapping[str, GatewayStatus]):
    """`sbx.gateways`: un mapeo de sólo lectura `nombre -> GatewayStatus`.
    Sin `gateways=` es siempre un `GatewayHandle` vacío cuyo `refresh()`/
    `arefresh()` no hacen nada, así que llamarlos nunca es una rama especial
    para quien los use. Con `gateways=`, releen cada cabecera de Secrets
    Manager (aunque la `SecretCache` no haya vencido) y mandan un
    `Configure` nuevo — la forma de rotar un secreto sin recrear el
    sandbox. Cada ruta conserva su puerto; si `rayd` rechaza la sección,
    lanzan y el estado anterior sigue en pie.

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
        recovered: bool = False,
    ) -> None:
        self._statuses = dict(statuses)
        self._refresher = refresher
        self._async_refresher = async_refresher
        self._recovered = recovered

    @property
    def recovered(self) -> bool:
        """`True` si `connect()` lo reconstruyó desde `ConfigureStatus` (no
        sabe rotar secretos); `False` si viene de `create(gateways=)`."""
        return self._recovered

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

#: Relee `ConfigureStatus` sin mandar ningún `Configure`: el `refresh()` de
#: un `sbx.gateways` recuperado por `connect()`.
StatusReader = Callable[[], "configure_pb2.ConfigureStatusResponse"]
AsyncStatusReader = Callable[[], Awaitable["configure_pb2.ConfigureStatusResponse"]]


def gateways_recoverable(features: AgentFeatures | None) -> bool:
    """`connect()` sólo pregunta por las pasarelas a un agente que anuncia
    `ConfigureService` y la función `secret_gateway`: sobre uno anterior,
    `sbx.gateways` queda vacío sin ninguna llamada extra."""
    return features is not None and features.configure and features.secret_gateway


def owns_gateways(handle: object | None) -> bool:
    """`True` sólo para el `sbx.gateways` del handle que aplicó `gateways=`:
    ése se conserva en cada `connect()`. Uno recuperado (o ninguno) se
    vuelve a leer de `ConfigureStatus`."""
    return isinstance(handle, GatewayHandle) and not handle.recovered


def recovered_gateways(
    status: configure_pb2.ConfigureStatusResponse,
    *,
    reader: StatusReader | None = None,
    async_reader: AsyncStatusReader | None = None,
) -> GatewayHandle:
    """El `sbx.gateways` que `connect()` reconstruye desde el
    `ConfigureStatus` de un sandbox en marcha: sólo nombre, puerto y último
    error de cada ruta, porque `rayd` nunca devuelve el upstream, las
    cabeceras ni sus valores. Sin la definición original no hay nada que
    rotar, así que su `refresh()`/`arefresh()` sólo relee el estado; rotar
    un secreto (`refresh()` o `reincarnate()`) sigue siendo cosa del handle
    que llamó a `create(gateways=)`. Vacío (`EMPTY_GATEWAYS`) si el sandbox no tiene
    pasarelas."""
    statuses = gateway_statuses_from_proto(status.secret_gateway)
    if not statuses:
        return EMPTY_GATEWAYS
    return GatewayHandle(
        statuses,
        refresher=None if reader is None else _status_refresher(reader),
        async_refresher=None if async_reader is None else _async_status_refresher(async_reader),
        recovered=True,
    )


def _status_refresher(reader: StatusReader) -> Callable[[], Mapping[str, GatewayStatus]]:
    def refresh() -> Mapping[str, GatewayStatus]:
        return gateway_statuses_from_proto(reader().secret_gateway)

    return refresh


def _async_status_refresher(
    async_reader: AsyncStatusReader,
) -> Callable[[], Awaitable[Mapping[str, GatewayStatus]]]:
    async def refresh() -> Mapping[str, GatewayStatus]:
        return gateway_statuses_from_proto((await async_reader()).secret_gateway)

    return refresh
