"""Componente `secrets-access` (M13a, adoptado por el convenio
`OptionalStack` en M15 foundations): las dos políticas IAM de
`infra/secrets-access.yaml`. Ver `infra/README.md` para el detalle
completo; este módulo sólo describe la pila para `OptionalStacks`.
"""

from __future__ import annotations

from rayito._stacks._model import CostStatement, StackComponent, StackParameter

COMPONENT: StackComponent = StackComponent(
    name="secrets-access",
    description=(
        "Políticas IAM RayitoSecretsReader/RayitoSecretsAdmin sobre un prefijo de "
        "Secrets Manager, para secrets=/SecretStore/SecretCache."
    ),
    parameters=(
        StackParameter(
            "SecretPrefix",
            "Prefijo de nombre en Secrets Manager (SecretStore(prefix=)); nunca vacío.",
            default="rayito/",
        ),
        StackParameter(
            "KmsKeyArn",
            "ARN de una CMK propia; vacío usa la clave gestionada aws/secretsmanager.",
            default="",
        ),
    ),
    capabilities=("CAPABILITY_IAM",),
    cost=CostStatement(
        creates=("AWS::IAM::ManagedPolicy (x2)",),
        idle_monthly="$0 (sólo IAM; los secretos que crees facturan aparte)",
        per_use=(),
        removal=(
            "destroy() borra las dos políticas; no borra ningún secreto "
            "(usa SecretStore().destroy(...) antes)"
        ),
        source="AWS_API_NOTES.md §19",
    ),
)
