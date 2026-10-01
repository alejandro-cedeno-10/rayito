"""Stub de componente para `m15-efs-volumes` (M15 foundations,
experimental). La feature sustituye `COMPONENT` por la definición real
(`infra/efs-volumes.yaml`: sistema de ficheros EFS, mount targets, grupos de
seguridad del conector, `NetworkConnector`) en su propio cambio, pendiente
de la campaña de medición EFS-1..EFS-20; hasta entonces
`OptionalStacks.deploy/status/destroy` lanzan `UnimplementedError` para
este nombre (`supported=False`).
"""

from __future__ import annotations

from rayito._stacks._model import CostStatement, StackComponent

COMPONENT: StackComponent = StackComponent(
    name="efs-volumes",
    description=(
        "Pendiente de m15-efs-volumes (experimental): sistema de ficheros EFS, "
        "mount targets y conector, para volumes=."
    ),
    supported=False,
    cost=CostStatement(creates=(), idle_monthly="pendiente de la medición EFS"),
)
