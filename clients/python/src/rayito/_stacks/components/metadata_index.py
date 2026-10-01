"""Componente `metadata-index` (M14, adoptado por el convenio
`OptionalStack` en M15 foundations): la tabla DynamoDB y las dos políticas
IAM de `infra/metadata-index.yaml`. Ver `infra/README.md` para el detalle
completo; este módulo sólo describe la pila para `OptionalStacks`.
"""

from __future__ import annotations

from rayito._stacks._model import CostStatement, StackComponent, StackParameter

COMPONENT: StackComponent = StackComponent(
    name="metadata-index",
    description=(
        "Tabla DynamoDB (PAY_PER_REQUEST, TTL en expires_at) más las políticas "
        "RayitoIndexWriter/RayitoIndexReader, para index=DynamoDbIndex(...)."
    ),
    parameters=(
        StackParameter(
            "TableName",
            "Nombre de la tabla del índice (DynamoDbIndex('rayito-sandboxes') por defecto).",
            default="rayito-sandboxes",
        ),
        StackParameter(
            "PointInTimeRecovery",
            "Backups continuos (se facturan por GB-mes); 'false' por defecto.",
            default="false",
        ),
        StackParameter(
            "DeletionProtection",
            "Bloquea DeleteTable (y destroy()) mientras sea 'true'; 'false' por defecto.",
            default="false",
        ),
    ),
    capabilities=("CAPABILITY_IAM",),
    cost=CostStatement(
        creates=("AWS::DynamoDB::Table", "AWS::IAM::ManagedPolicy (x2)"),
        idle_monthly="$0 (on-demand, tabla vacía; IAM es gratis)",
        per_use=(
            "~1 WRU por sandbox creado con index= ($0,625 por millón de WRU)",
            "~0,5 RRU por sandbox candidato al listar ($0,125 por millón de RRU)",
            "$0,25/GB-mes de almacenamiento tras los primeros 25 GB",
        ),
        removal=(
            "destroy() borra la tabla (desactiva antes DeletionProtection si está en true) "
            "y las dos políticas"
        ),
        source="AWS_API_NOTES.md §20; precios de DynamoDB us-east-1 consultados 2026-09-30",
    ),
)
