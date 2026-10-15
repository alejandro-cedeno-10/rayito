"""La configuración de la función, leída del entorno que fija la plantilla."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from rayito._percent import PERCENT
from rayito._limits import QUOTA_CRITICAL_FRACTION, QUOTA_WARNING_FRACTION


class ConfigError(Exception):
    """Falta una variable o un umbral no es coherente: la función falla al
    arrancar en vez de callar un aviso."""


@dataclass(frozen=True)
class AlertConfig:
    region: str
    topic_arn: str
    state_table: str
    warning_fraction: float
    critical_fraction: float

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> AlertConfig:
        try:
            region = environ["AWS_REGION"]
            topic_arn = environ["RAYITO_ALERTS_TOPIC_ARN"]
            state_table = environ["RAYITO_ALERTS_STATE_TABLE"]
        except KeyError as exc:
            raise ConfigError(f"falta la variable {exc.args[0]}") from exc
        warning = _percent(environ, "RAYITO_ALERTS_WARNING_PERCENT", QUOTA_WARNING_FRACTION)
        critical = _percent(environ, "RAYITO_ALERTS_CRITICAL_PERCENT", QUOTA_CRITICAL_FRACTION)
        if not 0 < warning < critical <= 1:
            raise ConfigError("los umbrales deben cumplir 0 < aviso < crítico <= 100")
        return cls(region, topic_arn, state_table, warning, critical)


def _percent(environ: Mapping[str, str], name: str, default: float) -> float:
    raw = environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw) / PERCENT
    except ValueError as exc:
        raise ConfigError(f"{name} no es un número") from exc
