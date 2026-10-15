"""La configuración de la función, de variables de entorno que fija la
plantilla (`infra/dashboard.yaml`). Ninguna es un secreto: ids y nombres de
recursos de la propia pila."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

ENV_PREFIX: Final = "RAYITO_DASHBOARD_"
ENV_REGION: Final = "AWS_REGION"
ENV_USER_POOL_ID: Final = f"{ENV_PREFIX}USER_POOL_ID"
ENV_CLIENT_NAME: Final = f"{ENV_PREFIX}CLIENT_NAME"
ENV_COGNITO_DOMAIN: Final = f"{ENV_PREFIX}COGNITO_DOMAIN"
ENV_AUDIT_TABLE: Final = f"{ENV_PREFIX}AUDIT_TABLE"
ENV_CACHE_TABLE: Final = f"{ENV_PREFIX}CACHE_TABLE"
ENV_ACTIONS_ROLE_ARN: Final = f"{ENV_PREFIX}ACTIONS_ROLE_ARN"
ENV_COST_EXPLORER: Final = f"{ENV_PREFIX}COST_EXPLORER"
ENV_THEME_PARAMETER: Final = f"{ENV_PREFIX}THEME_PARAMETER"
ENV_LOGO_PARAMETER: Final = f"{ENV_PREFIX}LOGO_PARAMETER"
TRUE_VALUE: Final = "true"


@dataclass(frozen=True)
class StackConfig:
    region: str
    user_pool_id: str
    #: Nombre del cliente de aplicación de la SPA en el pool: el panel sólo
    #: acepta tokens de ese cliente y publica su id.
    client_name: str
    #: El host del Hosted UI de Cognito (sin esquema).
    cognito_domain: str
    audit_table: str
    cache_table: str
    #: `None` cuando la pila se desplegó sin acciones (`AllowActions=false`).
    actions_role_arn: str | None
    cost_explorer: bool
    #: Nombre del parámetro de SSM con el tema JSON, o `None` (aspecto de fábrica).
    theme_parameter: str | None
    #: Nombre del parámetro de SSM con el logo SVG, o `None`.
    logo_parameter: str | None

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> StackConfig:
        return cls(
            region=environ[ENV_REGION],
            user_pool_id=environ[ENV_USER_POOL_ID],
            client_name=environ[ENV_CLIENT_NAME],
            cognito_domain=environ[ENV_COGNITO_DOMAIN],
            audit_table=environ[ENV_AUDIT_TABLE],
            cache_table=environ[ENV_CACHE_TABLE],
            actions_role_arn=environ.get(ENV_ACTIONS_ROLE_ARN) or None,
            cost_explorer=environ.get(ENV_COST_EXPLORER, "") == TRUE_VALUE,
            theme_parameter=environ.get(ENV_THEME_PARAMETER) or None,
            logo_parameter=environ.get(ENV_LOGO_PARAMETER) or None,
        )
