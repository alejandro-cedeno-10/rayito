"""`AsyncQuotas`: el mismo contrato que `Quotas` sobre `asyncio.to_thread`;
delega en un `Quotas` propio."""

from __future__ import annotations

import asyncio

import boto3

from rayito._dashboard._domain import QuotaReport
from rayito._dashboard._ports import ManagementPlane, QuotaSource
from rayito._dashboard._quotas import Quotas
from rayito._size_catalog import DEFAULT_SIZE_CATALOG, SizeCatalog


class AsyncQuotas:
    def __init__(
        self,
        *,
        region: str | None = None,
        session: boto3.session.Session | None = None,
        control_plane: ManagementPlane | None = None,
        source: QuotaSource | None = None,
        catalog: SizeCatalog = DEFAULT_SIZE_CATALOG,
    ) -> None:
        self._inner = Quotas(
            region=region,
            session=session,
            control_plane=control_plane,
            source=source,
            catalog=catalog,
        )

    async def report(self) -> QuotaReport:
        return await asyncio.to_thread(self._inner.report)
