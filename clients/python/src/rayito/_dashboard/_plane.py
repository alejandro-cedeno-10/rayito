"""El plano de control por defecto de los servicios del panel: el compartido
del proceso (`shared_control_plane`), construido en el primer uso y no al
construir el servicio."""

from __future__ import annotations

import boto3

from rayito._aws import shared_control_plane
from rayito._dashboard._ports import ManagementPlane


class LazyPlane:
    def __init__(
        self,
        *,
        region: str | None,
        session: boto3.session.Session | None,
        plane: ManagementPlane | None,
    ) -> None:
        self._region = region
        self._session = session
        self._plane = plane

    def get(self) -> ManagementPlane:
        if self._plane is None:
            self._plane = shared_control_plane(self._session, region=self._region)
        return self._plane
