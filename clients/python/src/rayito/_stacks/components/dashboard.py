"""Componente `dashboard` (ADR-026, fase 2): `infra/dashboard.yaml`, el panel de
gestión dentro de tu cuenta. Una Lambda tras una Function URL sirve la misma
SPA y la misma API `/api/v1` que `rayito dashboard`, con inicio de sesión de
Cognito, un rol de ejecución de sólo lectura, un rol de acciones aparte
(`AllowActions=true`), una tabla de auditoría sólo de añadir y una caché del
coste facturado. Sin CloudFront ni buckets S3. Ver `infra/README.md`; este
módulo sólo describe la pila para `OptionalStacks`.
"""

from __future__ import annotations

from typing import Final

from rayito._limits import (
    COST_EXPLORER_REQUEST_PRICE_USD,
    DASHBOARD_POLL_INTERVAL_SECONDS,
    DASHBOARD_STACK_COST_EXAMPLE_DAYS_PER_MONTH,
    DASHBOARD_STACK_COST_EXAMPLE_OPEN_HOURS_PER_DAY,
    DASHBOARD_STACK_COST_LOOKBACK_MONTHS,
    DASHBOARD_STACK_COST_MISSES_PER_DAY,
    DASHBOARD_STACK_SSM_ADVANCED_PARAMETER_MAX_BYTES,
    DASHBOARD_STACK_THEME_PARAMETER_MAX_BYTES,
    DASHBOARD_STACK_THEME_REFRESH_SECONDS,
    LAMBDA_FREE_TIER_GB_SECONDS_PER_MONTH,
    LAMBDA_FREE_TIER_REQUESTS_PER_MONTH,
    LAMBDA_REQUEST_PRICE_USD_PER_MILLION,
    SSM_ADVANCED_PARAMETER_PRICE_USD_PER_MONTH,
    SSM_API_INTERACTION_PRICE_USD_PER_TEN_THOUSAND,
)
from rayito._stacks._model import CostStatement, StackArtifact, StackComponent, StackParameter

SECONDS_PER_HOUR: Final = 3600
SECONDS_PER_DAY: Final = 86400
API_INTERACTIONS_PER_BILLING_UNIT: Final = 10000
#: La función lee el tema y el logo: dos parámetros por relectura.
SSM_PARAMETERS_READ: Final = 2


def usd_es(value: float, decimals: int) -> str:
    """Un precio con coma decimal; mismo formato que `usdEs` de TypeScript."""
    return f"${value:.{decimals}f}".replace(".", ",")


EXAMPLE_INVOCATIONS_PER_MONTH: Final = (
    DASHBOARD_STACK_COST_EXAMPLE_OPEN_HOURS_PER_DAY
    * SECONDS_PER_HOUR
    // DASHBOARD_POLL_INTERVAL_SECONDS
    * DASHBOARD_STACK_COST_EXAMPLE_DAYS_PER_MONTH
)
THEME_READS_PER_MONTH: Final = (
    DASHBOARD_STACK_COST_EXAMPLE_DAYS_PER_MONTH
    * SECONDS_PER_DAY
    // DASHBOARD_STACK_THEME_REFRESH_SECONDS
    * SSM_PARAMETERS_READ
)
SSM_ADVANCED_COST_NOTE: Final = (
    f"Un parámetro Advanced cuesta {usd_es(SSM_ADVANCED_PARAMETER_PRICE_USD_PER_MONTH, 2)} al mes "
    "más las interacciones de API (precios de SSM Parameter Store, 2026-10-13); la función "
    f"lo relee cada {DASHBOARD_STACK_THEME_REFRESH_SECONDS} s."
)
COST_EXPLORER_WORST_DAY_USD: Final = (
    DASHBOARD_STACK_COST_MISSES_PER_DAY * COST_EXPLORER_REQUEST_PRICE_USD
)
COST_EXPLORER_WORST_MONTH_USD: Final = (
    COST_EXPLORER_WORST_DAY_USD * DASHBOARD_STACK_COST_EXAMPLE_DAYS_PER_MONTH
)
THEME_READS_MONTHLY_USD: Final = (
    THEME_READS_PER_MONTH
    / API_INTERACTIONS_PER_BILLING_UNIT
    * SSM_API_INTERACTION_PRICE_USD_PER_TEN_THOUSAND
)

COMPONENT: StackComponent = StackComponent(
    name="dashboard",
    description=(
        "Panel de gestión de MicroVMs en tu cuenta: Lambda con Function URL, Cognito, "
        "roles de lectura y de acciones separados, auditoría append-only en DynamoDB."
    ),
    parameters=(
        StackParameter(
            "ArtifactBucket",
            "Bucket S3 donde OptionalStacks.deploy(artifact_bucket=...) subió el código.",
            required=True,
        ),
        StackParameter(
            "AllowActions",
            "true crea el rol de acciones (matar, suspender, reanudar), el grupo de Cognito "
            "que puede usarlo y la auditoría; sin él el panel es de sólo lectura.",
            default="false",
        ),
        StackParameter(
            "EnableCostExplorer",
            "true permite consultar el coste facturado (ce:GetCostAndUsage, 0,01 USD por "
            "consulta, cacheada 24 h en la tabla de caché). Sólo se aceptan ventanas de meses "
            f"completos de los últimos {DASHBOARD_STACK_COST_LOOKBACK_MONTHS} meses y, en total, "
            f"como mucho {DASHBOARD_STACK_COST_MISSES_PER_DAY} consultas de pago por día UTC.",
            default="false",
        ),
        StackParameter(
            "ImageNamePrefix",
            "El rol de acciones sólo actúa sobre imágenes cuyo nombre empieza así.",
            default="rayito-",
        ),
        StackParameter(
            "InitialUserEmail",
            "Opcional: crea este usuario de Cognito (invitación por correo) y, con "
            "AllowActions=true, lo añade al grupo de acciones.",
            default="",
        ),
        StackParameter(
            "DomainPrefix",
            "Opcional: prefijo del dominio del Hosted UI de Cognito (único en la región).",
            default="",
        ),
        StackParameter(
            "ThemeJson",
            "Opcional: tema de color propio en JSON (hasta "
            f"{DASHBOARD_STACK_THEME_PARAMETER_MAX_BYTES} bytes; formato de "
            "examples/dashboard-theme). La pila lo guarda en un parámetro de SSM.",
            default="",
        ),
        StackParameter(
            "ThemeParameterName",
            "Opcional: nombre (con «/» inicial) de un parámetro de SSM tuyo con el tema JSON, "
            f"para uno de más de {DASHBOARD_STACK_THEME_PARAMETER_MAX_BYTES} bytes. "
            f"Tiene prioridad sobre ThemeJson. {SSM_ADVANCED_COST_NOTE}",
            default="",
        ),
        StackParameter(
            "LogoParameterName",
            "Opcional: nombre (con «/» inicial) de un parámetro de SSM con un logo SVG "
            f"(hasta {DASHBOARD_STACK_SSM_ADVANCED_PARAMETER_MAX_BYTES} bytes). "
            f"{SSM_ADVANCED_COST_NOTE}",
            default="",
        ),
        StackParameter(
            "ReservedConcurrency",
            "Opcional: concurrencia reservada de la función; acota lo que cuesta una "
            "avalancha de peticiones sin autenticar.",
            default="",
        ),
        StackParameter(
            "LogRetentionDays",
            "Retención del log group de la función (días); nunca contiene tokens.",
            default="30",
        ),
    ),
    artifacts=(
        StackArtifact(
            name="dashboard",
            parameter_key="ArtifactS3Key",
            bucket_parameter_key="ArtifactBucket",
        ),
    ),
    capabilities=("CAPABILITY_IAM",),
    cost=CostStatement(
        creates=(
            "AWS::Lambda::Function (1) + AWS::Lambda::Url + AWS::Logs::LogGroup",
            "AWS::Cognito::UserPool + UserPoolClient + UserPoolDomain "
            "(+ UserPoolGroup con AllowActions=true)",
            "AWS::DynamoDB::Table (x2: auditoría y caché de coste, bajo demanda)",
            "AWS::IAM::Role (x1 de lectura; +1 de acciones con AllowActions=true)",
            "AWS::SSM::Parameter (Standard, sin coste; sólo con ThemeJson)",
        ),
        idle_monthly=(
            "~$0 en reposo: sin tráfico no hay invocaciones, y Lambda, DynamoDB bajo demanda "
            "y Cognito (capa gratuita de usuarios activos) sólo facturan por uso. Un "
            "parámetro SSM Advanced propio (tema o logo) cuesta "
            f"{usd_es(SSM_ADVANCED_PARAMETER_PRICE_USD_PER_MONTH, 2)} al mes aunque no haya tráfico"
        ),
        per_use=(
            "Lambda: una invocación por petición de la SPA, que sondea cada "
            f"{DASHBOARD_POLL_INTERVAL_SECONDS} s con la pestaña abierta. Ejemplo: una pestaña "
            f"abierta {DASHBOARD_STACK_COST_EXAMPLE_OPEN_HOURS_PER_DAY} h al día durante "
            f"{DASHBOARD_STACK_COST_EXAMPLE_DAYS_PER_MONTH} días son unas "
            f"{EXAMPLE_INVOCATIONS_PER_MONTH} invocaciones al mes, dentro de las "
            f"{LAMBDA_FREE_TIER_REQUESTS_PER_MONTH} peticiones y los "
            f"{LAMBDA_FREE_TIER_GB_SECONDS_PER_MONTH} GB-s gratuitos al mes; pasada la capa "
            f"gratuita, {usd_es(LAMBDA_REQUEST_PRICE_USD_PER_MILLION, 2)} por millón de "
            "peticiones más la duración. La Function URL es pública (AuthType NONE): el "
            "tráfico sin autenticar también factura invocaciones, así que fija "
            "ReservedConcurrency para acotarlo",
            "Cost Explorer (sólo con EnableCostExplorer=true): "
            f"{usd_es(COST_EXPLORER_REQUEST_PRICE_USD, 2)} por consulta, una cada 24 h por "
            "ventana gracias a la caché y, en total, como mucho "
            f"{DASHBOARD_STACK_COST_MISSES_PER_DAY} al día UTC (tope de la pila): en el peor "
            f"caso {usd_es(COST_EXPLORER_WORST_DAY_USD, 2)} al día, unos "
            f"{usd_es(COST_EXPLORER_WORST_MONTH_USD, 2)} al mes",
            "DynamoDB: una escritura por sandbox y acción (auditoría)",
            "SSM (sólo con tema o logo en parámetros Advanced): "
            f"{usd_es(SSM_API_INTERACTION_PRICE_USD_PER_TEN_THOUSAND, 2)} por cada "
            f"{API_INTERACTIONS_PER_BILLING_UNIT} interacciones de API; la función relee cada "
            f"{DASHBOARD_STACK_THEME_REFRESH_SECONDS} s por contenedor activo, hasta unas "
            f"{THEME_READS_PER_MONTH} interacciones al mes "
            f"({usd_es(THEME_READS_MONTHLY_USD, 2)})",
        ),
        removal=(
            "destroy() borra todo lo que la pila creó, incluidas las filas de auditoría "
            "(expórtalas antes si las necesitas) y los usuarios de Cognito"
        ),
        source=(
            "AWS_API_NOTES.md §10; precios de Lambda, DynamoDB, Cognito, SSM Parameter Store "
            "y Cost Explorer us-east-1, 2026-10-13"
        ),
    ),
)
