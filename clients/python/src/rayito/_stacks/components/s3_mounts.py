"""Stub de componente para `m15-s3-mounts` (M15 foundations). La feature
sustituye `COMPONENT` por la definición real (`infra/s3-mounts.yaml`,
la política `RayitoS3MountAccess`) en su propio cambio; hasta entonces
`OptionalStacks.deploy/status/destroy` lanzan `UnimplementedError` para
este nombre (`supported=False`), y `list()`/`components()` ya lo muestran.
"""

from __future__ import annotations

from rayito._stacks._model import CostStatement, StackComponent

COMPONENT: StackComponent = StackComponent(
    name="s3-mounts",
    description="Pendiente de m15-s3-mounts: política RayitoS3MountAccess para mounts=.",
    supported=False,
    cost=CostStatement(creates=(), idle_monthly="$0 (sólo IAM, cuando exista)"),
)
