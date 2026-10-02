"""Componente `s3-mounts` (`m15-s3-mounts`, ADR-017): la política IAM
`RayitoS3MountAccess` de `infra/s3-mounts.yaml`. Ver ese fichero para el
contrato exacto de parámetros y statements; este módulo sólo describe la
pila para `OptionalStacks`.
"""

from __future__ import annotations

from rayito._stacks._model import CostStatement, StackComponent, StackParameter

COMPONENT: StackComponent = StackComponent(
    name="s3-mounts",
    description=(
        "Política IAM RayitoS3MountAccess (lectura, y escritura con ReadOnly=false) "
        "sobre un bucket, para el execution role de un sandbox con mounts=."
    ),
    parameters=(
        StackParameter(
            "BucketName",
            "El único bucket al que da acceso esta política (una pila por bucket).",
            required=True,
        ),
        StackParameter(
            "Prefixes",
            "Hasta 4 prefijos (coma-separados, acabados en '*') que el rol puede listar, "
            "leer y, con ReadOnly=false, escribir; '*' (por defecto) es todo el bucket.",
            default="*",
        ),
        StackParameter(
            "ReadOnly",
            "'true' (por defecto) sólo lectura; 'false' añade PutObject/DeleteObject.",
            default="true",
        ),
    ),
    cost=CostStatement(
        creates=("AWS::IAM::ManagedPolicy",),
        idle_monthly="$0 (sólo IAM)",
        per_use=(),
        removal="destroy() borra la política; no borra ningún objeto ni el bucket",
        source="AWS_API_NOTES.md §23",
    ),
    # La plantilla crea un `AWS::IAM::ManagedPolicy`: sin esta capacidad,
    # `CreateStack` falla con `InsufficientCapabilitiesException`.
    capabilities=("CAPABILITY_IAM",),
)
