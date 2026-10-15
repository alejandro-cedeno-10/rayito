"""Componente `dashboard-read-role` (ADR-026, fase 3):
`infra/dashboard-read-role.yaml`, el rol de sólo lectura que una cuenta
*destino* crea para que `rayito dashboard --account NOMBRE=<arn>` muestre sus
sandboxes, imágenes y cuotas junto a los de la cuenta que ejecuta el panel. Se
despliega en cada cuenta destino, no en la del panel.
"""

from __future__ import annotations

from rayito._limits import DASHBOARD_ASSUME_ROLE_SESSION_SECONDS
from rayito._stacks._model import CostStatement, StackComponent, StackParameter

COMPONENT: StackComponent = StackComponent(
    name="dashboard-read-role",
    description=(
        "Rol de sólo lectura de una cuenta destino para la vista multicuenta de "
        "rayito dashboard --account. Se despliega en la cuenta que quieres ver."
    ),
    parameters=(
        StackParameter(
            "TrustedPrincipalArn",
            "ARN del rol o usuario IAM, en la cuenta que ejecuta el panel, que puede asumir "
            "este rol (nunca la raíz de la cuenta).",
            required=True,
        ),
        StackParameter(
            "ExternalId",
            "Opcional: si lo pones, el rol sólo se asume pasando este mismo valor "
            "(rayito dashboard --external-id).",
            default="",
            no_echo=True,
        ),
        StackParameter(
            "MaxSessionSeconds",
            "Sesión más larga que se puede pedir (IAM admite de 1 a 12 horas); el panel pide "
            f"{DASHBOARD_ASSUME_ROLE_SESSION_SECONDS} s.",
            default=str(DASHBOARD_ASSUME_ROLE_SESSION_SECONDS),
        ),
    ),
    capabilities=("CAPABILITY_IAM",),
    cost=CostStatement(
        creates=("AWS::IAM::Role (1, con una política en línea de sólo lectura)",),
        idle_monthly="$0: IAM no factura",
        per_use=(
            "Cada sesión del panel es un sts:AssumeRole (sin coste) y las lecturas son las "
            "mismas de rayito dashboard: ListMicrovms, ListMicrovmImages, "
            "Get/ListMicrovmImageVersion y ListServiceQuotas",
        ),
        removal=(
            "destroy() borra el rol; los accesos desde el panel dejan de funcionar al instante"
        ),
        source="IAM no tiene precio propio (https://aws.amazon.com/iam/pricing/, 2026-10-13)",
    ),
)
