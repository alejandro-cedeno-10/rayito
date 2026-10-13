"""`Quotas`: las cuotas aplicadas de Lambda MicroVMs (Service Quotas), la
memoria asignada contra la cuota de la región (RUNNING más suspendidos, de
`ListMicrovms` por la memoria de cada imagen) y los avisos cerca del límite.
Construirlo no hace ninguna llamada a AWS.
"""

from __future__ import annotations

import boto3

from rayito._aws import LazyClient
from rayito._dashboard._aws import ServiceQuotasSource
from rayito._dashboard._domain import MEMORY_COUNTED_STATES, QuotaReport, build_quota_report
from rayito._dashboard._inventory import InventoryReader
from rayito._dashboard._plane import LazyPlane
from rayito._dashboard._ports import ManagementPlane, QuotaSource
from rayito._size_catalog import DEFAULT_SIZE_CATALOG, SizeCatalog


class Quotas:
    """Cuotas y uso de memoria de una región.

    Coste y activación
    -------------------
    Activa: una llamada explícita a `report()`; construir `Quotas` no llama
        a AWS.
    Recursos y llamadas AWS: sólo lecturas, ninguna crea recursos:
        `ListServiceQuotas` (código `lambda`), `ListMicrovms` y, como
        mucho una vez por versión de imagen y proceso,
        `GetMicrovmImageVersion`.
    Coste aproximado: $0. `GetMicrovmImageVersion` no factura ni tiene
        cuota propia (AWS_API_NOTES.md §24) y Service Quotas no cobra por
        consulta (a confirmar en la aceptación, tarea 1.1 del cambio
        `dashboard`); `ListMicrovms` cuenta contra un TPS no documentado.
    IAM: `servicequotas:ListServiceQuotas`, `lambda:ListMicrovms` y
        `lambda:GetMicrovmImageVersion`.
    Cómo apagarla: no llames a `report()`; no deja recursos que borrar.
    Ejemplo:
        report = Quotas(region="us-east-1").report()
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
        self._session = session
        self._plane = LazyPlane(region=region, session=session, plane=control_plane)
        self._source = source
        self._catalog = catalog

    def report(self) -> QuotaReport:
        plane = self._plane.get()
        applied = self._quota_source(plane.region).list_applied_quotas()
        views = InventoryReader(plane, self._catalog).views(states=MEMORY_COUNTED_STATES)
        return build_quota_report(plane.region, applied, (view.memory_mib for view in views))

    def _quota_source(self, region: str) -> QuotaSource:
        if self._source is None:
            client = LazyClient("service-quotas", region=region, session=self._session)
            self._source = ServiceQuotasSource(client.get)
        return self._source
