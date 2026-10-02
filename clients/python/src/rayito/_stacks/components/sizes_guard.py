"""Componente `sizes-guard` (m15-sizes-catalog): la política IAM
`RayitoRunAllowedSizes` de `infra/sizes-guard.yaml`, que limita
`lambda:RunMicrovm` a los ARN de imagen que el operador liste
explícitamente (Q90) con un Deny sobre `NotResource: ImageArns` (no sólo un
Allow: el Allow por sí solo no restringe nada una vez que la identidad ya
tiene el `microvm-image:*` de la `CallerPolicy` estándar de
`infra/iam.yaml`). Sólo IAM: ningún recurso facturable."""

from __future__ import annotations

from rayito._stacks._model import CostStatement, StackComponent, StackParameter

COMPONENT: StackComponent = StackComponent(
    name="sizes-guard",
    description=(
        "Política RayitoRunAllowedSizes: Deny de lambda:RunMicrovm fuera de los ARN de "
        "imagen listados (el baseline más cada sufijo de --sizes que se quiera permitir), "
        "efectivo sea cual sea el resto de permisos de la identidad."
    ),
    parameters=(
        StackParameter(
            "ImageArns",
            "ARN completos (CommaDelimitedList) de cada versión de imagen permitida.",
            required=True,
        ),
    ),
    capabilities=("CAPABILITY_IAM",),
    cost=CostStatement(
        creates=("AWS::IAM::ManagedPolicy",),
        idle_monthly="$0 (sólo IAM)",
        removal="destroy() borra la política; ninguna imagen se toca",
        source="AWS_API_NOTES.md §24 (Q90)",
    ),
)
