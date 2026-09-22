"""Configuración del servidor MCP: sólo variables de entorno (design D4).

Los hosts MCP configuran servidores con un bloque `env`, así que no hay flags
de línea de comandos que dupliquen estas variables. La región, el perfil y las
credenciales de AWS los resuelve boto3 por su cuenta. Este módulo no importa
`mcp`: `McpSettings` se puede validar sin el extra instalado.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal, cast

from rayito._limits import IDLE_MAX_IDLE_MIN_SECONDS, MAX_DURATION_SECONDS
from rayito._models import IdlePolicy
from rayito._sandbox_base import TEMPLATE_ENV_VAR, LoggingOption

TEMPLATE_VERSION_ENV_VAR: Final = "RAYITO_TEMPLATE_VERSION"
EXECUTION_ROLE_ENV_VAR: Final = "RAYITO_EXECUTION_ROLE_ARN"
TIMEOUT_ENV_VAR: Final = "RAYITO_MCP_TIMEOUT_SECONDS"
IDLE_ENV_VAR: Final = "RAYITO_MCP_IDLE_SECONDS"
LOG_LEVEL_ENV_VAR: Final = "RAYITO_MCP_LOG_LEVEL"

DEFAULT_TIMEOUT_SECONDS: Final = 3600
DEFAULT_IDLE_SECONDS: Final = 300
DEFAULT_LOG_LEVEL: Final = "INFO"
MIN_TIMEOUT_SECONDS: Final = 60
IDLE_DISABLED: Final = 0
LOG_LEVELS: Final = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


@dataclass(frozen=True)
class McpSettings:
    """Lo que el servidor necesita para crear su único sandbox.

    `template` puede ser `None`: no se valida al arrancar sino en la primera
    herramienta que lo necesita (design D7), para que `rayito-mcp --help` y
    `list_tools` funcionen sin AWS. `idle_seconds == 0` desactiva la
    auto-suspensión; si no, debe ser menor que `timeout_seconds` (lo exige
    `resolve_idle_policy` al crear el sandbox, y aquí se comprueba al
    arrancar para que no falle cada herramienta). `logging` es `cloudwatch`
    sólo con execution role, como en los e2e.
    """

    template: str | None = None
    template_version: str | None = None
    execution_role_arn: str | None = None
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    idle_seconds: int = DEFAULT_IDLE_SECONDS
    log_level: LogLevel = DEFAULT_LOG_LEVEL

    def __post_init__(self) -> None:
        if self.idle_seconds != IDLE_DISABLED and self.idle_seconds >= self.timeout_seconds:
            raise ValueError(
                f"{IDLE_ENV_VAR}={self.idle_seconds} debe ser menor que "
                f"{TIMEOUT_ENV_VAR}={self.timeout_seconds} (o {IDLE_DISABLED} para "
                "desactivar la auto-suspensión)"
            )

    @property
    def idle_policy(self) -> IdlePolicy | None:
        if self.idle_seconds == IDLE_DISABLED:
            return None
        return IdlePolicy(max_idle_seconds=self.idle_seconds, auto_resume=True)

    @property
    def logging(self) -> LoggingOption:
        return "cloudwatch" if self.execution_role_arn else "disabled"

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> McpSettings:
        """Lee las variables de D4; `ValueError` en español nombrando la
        variable ante un valor malformado, fuera de rango o un idle que no es
        menor que el timeout."""
        return cls(
            template=environ.get(TEMPLATE_ENV_VAR) or None,
            template_version=environ.get(TEMPLATE_VERSION_ENV_VAR) or None,
            execution_role_arn=environ.get(EXECUTION_ROLE_ENV_VAR) or None,
            timeout_seconds=parse_timeout_seconds(environ.get(TIMEOUT_ENV_VAR)),
            idle_seconds=parse_idle_seconds(environ.get(IDLE_ENV_VAR)),
            log_level=parse_log_level(environ.get(LOG_LEVEL_ENV_VAR)),
        )


def parse_integer(raw: str, *, variable: str) -> int:
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{variable} debe ser un entero, recibido {raw!r}") from None


def parse_timeout_seconds(raw: str | None) -> int:
    if raw is None or raw == "":
        return DEFAULT_TIMEOUT_SECONDS
    value = parse_integer(raw, variable=TIMEOUT_ENV_VAR)
    if not MIN_TIMEOUT_SECONDS <= value <= MAX_DURATION_SECONDS:
        raise ValueError(
            f"{TIMEOUT_ENV_VAR} debe estar entre {MIN_TIMEOUT_SECONDS} y "
            f"{MAX_DURATION_SECONDS} segundos, recibido {value}"
        )
    return value


def parse_idle_seconds(raw: str | None) -> int:
    if raw is None or raw == "":
        return DEFAULT_IDLE_SECONDS
    value = parse_integer(raw, variable=IDLE_ENV_VAR)
    if value != IDLE_DISABLED and value < IDLE_MAX_IDLE_MIN_SECONDS:
        raise ValueError(
            f"{IDLE_ENV_VAR} debe ser {IDLE_DISABLED} (sin auto-suspensión) o un entero "
            f">= {IDLE_MAX_IDLE_MIN_SECONDS}, recibido {value}"
        )
    return value


def parse_log_level(raw: str | None) -> LogLevel:
    if raw is None or raw == "":
        return DEFAULT_LOG_LEVEL
    level = raw.upper()
    if level not in LOG_LEVELS:
        raise ValueError(
            f"{LOG_LEVEL_ENV_VAR} debe ser uno de {', '.join(LOG_LEVELS)}, recibido {raw!r}"
        )
    return cast("LogLevel", level)
