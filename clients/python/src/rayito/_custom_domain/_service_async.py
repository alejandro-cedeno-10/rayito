"""`AsyncCustomDomain` (m15-custom-domain): el mismo contrato que
`CustomDomain` sobre `asyncio.to_thread`, igual que `AsyncOptionalStacks`
(`_stacks/_service_async.py`) y `AsyncTokenRefresher` (`_transport.py`). No
reimplementa la lógica: la delega en una `CustomDomain` propia.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence

import boto3

from rayito._custom_domain._kvs import KeyValueStoreWriter
from rayito._custom_domain._service import (
    CUSTOM_DOMAIN_WAIT_TIMEOUT_SECONDS,
    CustomDomain,
    CustomDomainRoute,
)
from rayito._stacks._model import StackStatus
from rayito._stacks._port import StackProvisioner


class AsyncCustomDomain:
    """`CustomDomain` para `asyncio` (experimental): los mismos métodos,
    cada uno en un hilo porque boto3 es bloqueante. Construirlo no hace
    ninguna llamada a AWS. El bloque "Coste y activación" y el alcance de lo
    verificado en AWS real son los de `CustomDomain`.
    """

    def __init__(
        self,
        *,
        public_domain: str,
        stack_name: str | None = None,
        kvs_arn: str | None = None,
        region: str | None = None,
        session: boto3.session.Session | None = None,
        provisioner: StackProvisioner | None = None,
        kvs_writer: KeyValueStoreWriter | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._inner = CustomDomain(
            public_domain=public_domain,
            stack_name=stack_name,
            kvs_arn=kvs_arn,
            region=region,
            session=session,
            provisioner=provisioner,
            kvs_writer=kvs_writer,
            clock=clock,
        )

    @property
    def public_domain(self) -> str:
        return self._inner.public_domain

    def host_for(self, alias: str, port: int) -> str:
        return self._inner.host_for(alias, port)

    async def deploy(
        self,
        *,
        certificate_arn: str,
        alternate_domain_names: Sequence[str] | None = None,
        tags: dict[str, str] | None = None,
        wait: bool = True,
        wait_timeout: float = CUSTOM_DOMAIN_WAIT_TIMEOUT_SECONDS,
    ) -> StackStatus:
        return await asyncio.to_thread(
            self._inner.deploy,
            certificate_arn=certificate_arn,
            alternate_domain_names=alternate_domain_names,
            tags=tags,
            wait=wait,
            wait_timeout=wait_timeout,
        )

    async def status(self) -> StackStatus | None:
        return await asyncio.to_thread(self._inner.status)

    async def destroy(
        self, *, wait: bool = True, wait_timeout: float = CUSTOM_DOMAIN_WAIT_TIMEOUT_SECONDS
    ) -> None:
        await asyncio.to_thread(self._inner.destroy, wait=wait, wait_timeout=wait_timeout)

    async def register(
        self,
        alias: str,
        port: int,
        *,
        endpoint: str,
        jwe: str,
        traffic_token: str | None = None,
        public: bool = False,
        ttl_seconds: int,
    ) -> CustomDomainRoute:
        return await asyncio.to_thread(
            self._inner.register,
            alias,
            port,
            endpoint=endpoint,
            jwe=jwe,
            traffic_token=traffic_token,
            public=public,
            ttl_seconds=ttl_seconds,
        )

    async def refresh(
        self, route: CustomDomainRoute, *, jwe: str, ttl_seconds: int
    ) -> CustomDomainRoute:
        return await asyncio.to_thread(self._inner.refresh, route, jwe=jwe, ttl_seconds=ttl_seconds)

    async def unregister(self, alias: str, port: int) -> None:
        await asyncio.to_thread(self._inner.unregister, alias, port)
