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
    """El mismo contrato y el mismo coste que `Quotas`.

    Coste y activación
    -------------------
    Activa: una llamada explícita a `report()`; construir `AsyncQuotas` no
        llama a AWS.
    Recursos y llamadas AWS: sólo lecturas, ninguna crea recursos:
        `ListServiceQuotas`, `ListMicrovms` y `GetMicrovmImageVersion`.
    Coste aproximado: $0, sin ninguna llamada de pago (AWS_API_NOTES.md §24
        y §30; Service Quotas no publica tarifa por consulta, consultado
        2026-10-13).
    IAM: `servicequotas:ListServiceQuotas`, `lambda:ListMicrovms` y
        `lambda:GetMicrovmImageVersion`.
    Cómo apagarla: no llames a `report()`; no deja recursos que borrar.
    Ejemplo:
        report = await AsyncQuotas(region="us-east-1").report()
        print(report.memory.level)
    """

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
        """Gratuita. Ver `Quotas.report`."""
        return await asyncio.to_thread(self._inner.report)
