"""`AsyncVolumeStore` (`m15-efs-volumes`, ADR-018, experimental): el mismo
contrato que `VolumeStore` sobre `asyncio.to_thread`, igual que
`AsyncOptionalStacks` (`_stacks/_service_async.py`). No reimplementa la
lógica: la delega en un `VolumeStore` propio.
"""

from __future__ import annotations

import asyncio

import boto3

from rayito._volumes._domain import EfsVolume
from rayito._volumes._store import VolumeStore


class AsyncVolumeStore:
    def __init__(
        self,
        *,
        file_system_id: str,
        region: str | None = None,
        session: boto3.session.Session | None = None,
    ) -> None:
        self._inner = VolumeStore(file_system_id=file_system_id, region=region, session=session)

    @property
    def file_system_id(self) -> str:
        return self._inner.file_system_id

    @property
    def region(self) -> str | None:
        return self._inner.region

    def __repr__(self) -> str:
        return (
            f"AsyncVolumeStore(file_system_id={self._inner.file_system_id!r}, "
            f"region={self.region!r})"
        )

    async def create(self, name: str) -> EfsVolume:
        return await asyncio.to_thread(self._inner.create, name)

    async def get(self, name: str) -> EfsVolume:
        return await asyncio.to_thread(self._inner.get, name)

    async def list(self) -> tuple[EfsVolume, ...]:
        return await asyncio.to_thread(self._inner.list)

    async def destroy(self, name: str) -> bool:
        return await asyncio.to_thread(self._inner.destroy, name)
