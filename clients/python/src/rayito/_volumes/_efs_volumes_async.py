"""`AsyncEfsVolumes`: el mismo contrato que `EfsVolumes` sobre
`asyncio.to_thread`, igual que `AsyncLifecycleEvents` y
`AsyncOptionalStacks`: no reimplementa nada, delega en un `EfsVolumes`
propio. `volume_store()` devuelve un `AsyncVolumeStore`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

import boto3

from rayito._volumes._efs_volumes import DEFAULT_STACK_NAME, EfsVolumes
from rayito._volumes._store_async import AsyncVolumeStore

if TYPE_CHECKING:
    from rayito._stacks._model import StackStatus
    from rayito._stacks._service import OptionalStacks
    from rayito._volumes._base import EfsFileSystemApi
    from rayito._volumes._network import EfsNetworkReport, NetworkInspector


class AsyncEfsVolumes:
    """Ver `EfsVolumes` (su bloque "Coste y activación" vale igual aquí)."""

    def __init__(
        self,
        *,
        stack_name: str = DEFAULT_STACK_NAME,
        region: str | None = None,
        session: boto3.session.Session | None = None,
        stacks: OptionalStacks | None = None,
        network: NetworkInspector | None = None,
        efs: Callable[[], EfsFileSystemApi] | None = None,
    ) -> None:
        self._region = region
        self._session = session
        self._inner = EfsVolumes(
            stack_name=stack_name,
            region=region,
            session=session,
            stacks=stacks,
            network=network,
            efs=efs,
        )

    @property
    def stack_name(self) -> str:
        return self._inner.stack_name

    async def check(self, *, vpc_id: str, subnet_ids: Sequence[str] | str) -> EfsNetworkReport:
        return await asyncio.to_thread(self._inner.check, vpc_id=vpc_id, subnet_ids=subnet_ids)

    async def deploy(
        self,
        *,
        vpc_id: str,
        subnet_ids: Sequence[str] | str,
        allow_write: bool = True,
        access_point_arns: Sequence[str] = (),
        connector_name: str | None = None,
        tags: dict[str, str] | None = None,
        wait: bool = True,
    ) -> StackStatus:
        return await asyncio.to_thread(
            self._inner.deploy,
            vpc_id=vpc_id,
            subnet_ids=subnet_ids,
            allow_write=allow_write,
            access_point_arns=access_point_arns,
            connector_name=connector_name,
            tags=tags,
            wait=wait,
        )

    async def status(self) -> StackStatus | None:
        return await asyncio.to_thread(self._inner.status)

    async def volume_store(self) -> AsyncVolumeStore:
        store = await asyncio.to_thread(self._inner.volume_store)
        return AsyncVolumeStore(
            file_system_id=store.file_system_id, region=self._region, session=self._session
        )

    async def delete_file_system(self, file_system_id: str) -> None:
        await asyncio.to_thread(self._inner.delete_file_system, file_system_id)

    async def destroy(self, *, delete_file_system: bool = False, wait: bool = True) -> None:
        await asyncio.to_thread(
            self._inner.destroy, delete_file_system=delete_file_system, wait=wait
        )
