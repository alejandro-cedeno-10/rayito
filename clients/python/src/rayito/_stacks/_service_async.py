"""`AsyncOptionalStacks` (M15 foundations, ADR-016): el mismo contrato que
`OptionalStacks` sobre `asyncio.to_thread`, igual que el resto de envoltorios
async del SDK sobre llamadas boto3 bloqueantes (`AsyncTokenRefresher` en
`_transport.py`). No reimplementa la lógica: la delega en una
`OptionalStacks` propia.
"""

from __future__ import annotations

import asyncio

import boto3

from rayito._stacks._model import ParameterChange, StackComponent, StackStatus
from rayito._stacks._port import StackProvisioner
from rayito._stacks._service import DEFAULT_WAIT_TIMEOUT_SECONDS, OptionalStacks


class AsyncOptionalStacks:
    def __init__(
        self,
        *,
        region: str | None = None,
        session: boto3.session.Session | None = None,
        provisioner: StackProvisioner | None = None,
    ) -> None:
        self._inner = OptionalStacks(region=region, session=session, provisioner=provisioner)

    def components(self) -> tuple[StackComponent, ...]:
        return self._inner.components()

    async def status(
        self, component: str | StackComponent, *, stack_name: str | None = None
    ) -> StackStatus | None:
        return await asyncio.to_thread(self._inner.status, component, stack_name=stack_name)

    async def parameter_changes(
        self,
        component: str | StackComponent,
        *,
        stack_name: str | None = None,
        parameters: dict[str, str] | None = None,
        artifact_bucket: str | None = None,
    ) -> tuple[ParameterChange, ...]:
        return await asyncio.to_thread(
            self._inner.parameter_changes,
            component,
            stack_name=stack_name,
            parameters=parameters,
            artifact_bucket=artifact_bucket,
        )

    async def deploy(
        self,
        component: str | StackComponent,
        *,
        stack_name: str | None = None,
        parameters: dict[str, str] | None = None,
        artifact_bucket: str | None = None,
        tags: dict[str, str] | None = None,
        wait: bool = True,
        wait_timeout: float = DEFAULT_WAIT_TIMEOUT_SECONDS,
    ) -> StackStatus:
        return await asyncio.to_thread(
            self._inner.deploy,
            component,
            stack_name=stack_name,
            parameters=parameters,
            artifact_bucket=artifact_bucket,
            tags=tags,
            wait=wait,
            wait_timeout=wait_timeout,
        )

    async def destroy(
        self,
        component: str | StackComponent,
        *,
        stack_name: str | None = None,
        wait: bool = True,
        wait_timeout: float = DEFAULT_WAIT_TIMEOUT_SECONDS,
    ) -> None:
        await asyncio.to_thread(
            self._inner.destroy,
            component,
            stack_name=stack_name,
            wait=wait,
            wait_timeout=wait_timeout,
        )
