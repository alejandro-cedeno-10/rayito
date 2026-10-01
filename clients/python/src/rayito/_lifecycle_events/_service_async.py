"""`AsyncLifecycleEvents`: el mismo contrato que `LifecycleEvents` sobre
`asyncio.to_thread`, igual que `AsyncOptionalStacks`
(`_stacks/_service_async.py`) — no reimplementa nada, delega en una
`LifecycleEvents` propia.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import TYPE_CHECKING

import boto3

from rayito._lifecycle_events._domain import DEFAULT_STACK_NAME, EventRecord, WebhookInfo
from rayito._lifecycle_events._service import LifecycleEvents

if TYPE_CHECKING:
    from rayito._stacks._model import StackStatus
    from rayito._stacks._service import OptionalStacks


class AsyncLifecycleEvents:
    def __init__(
        self,
        *,
        stack_name: str = DEFAULT_STACK_NAME,
        region: str | None = None,
        session: boto3.session.Session | None = None,
        stacks: OptionalStacks | None = None,
    ) -> None:
        self._inner = LifecycleEvents(
            stack_name=stack_name, region=region, session=session, stacks=stacks
        )

    async def deploy(
        self,
        *,
        artifact_bucket: str,
        log_group_name: str,
        reconciler_interval_minutes: int = 5,
        tags: dict[str, str] | None = None,
        wait: bool = True,
    ) -> StackStatus:
        return await asyncio.to_thread(
            self._inner.deploy,
            artifact_bucket=artifact_bucket,
            log_group_name=log_group_name,
            reconciler_interval_minutes=reconciler_interval_minutes,
            tags=tags,
            wait=wait,
        )

    async def status(self) -> StackStatus | None:
        return await asyncio.to_thread(self._inner.status)

    async def destroy(self, *, wait: bool = True) -> None:
        await asyncio.to_thread(self._inner.destroy, wait=wait)

    async def register_webhook(
        self, url: str, *, secret_name: str, types: Sequence[str]
    ) -> WebhookInfo:
        return await asyncio.to_thread(
            self._inner.register_webhook, url, secret_name=secret_name, types=types
        )

    async def list_webhooks(self) -> list[WebhookInfo]:
        return await asyncio.to_thread(self._inner.list_webhooks)

    async def delete_webhook(self, webhook_id: str) -> None:
        await asyncio.to_thread(self._inner.delete_webhook, webhook_id)

    async def get_events(
        self,
        *,
        sandbox_id: str | None = None,
        types: Sequence[str] | None = None,
        limit: int = 100,
        order: str = "desc",
    ) -> list[EventRecord]:
        return await asyncio.to_thread(
            self._inner.get_events, sandbox_id=sandbox_id, types=types, limit=limit, order=order
        )
