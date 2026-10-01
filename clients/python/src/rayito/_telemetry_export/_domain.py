"""Dominio puro de m15-rayd-otlp (ADR-021): `OtlpAuth`/`TelemetryExport`
validan al construirse, sin tocar AWS ni `grpc`; `plan()` añade la única
comprobación que sí puede hacerse antes de `run-microvm` (la variante caps,
vía `rayito._role_policy`). El resto (resolver un `OtlpAuth.bearer`, rellenar
los hechos de imagen, enviar `ConfigureSandbox`) vive en `_section.py`, que sí
puede tocar AWS, y corre después de que el sandbox esté `RUNNING`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Literal

from rayito._role_policy import require_caps_for
from rayito.exceptions import InvalidArgumentException

#: 15..=300 s (research `docs/research/2026-10-e2b-out-of-scope.md` §6.3/§6.8,
#: OT2 medirá los bytes facturados a este ritmo contra la cuota real de la
#: cuenta); provisional hasta que la etapa de aceptación lo reconfirme.
MIN_INTERVAL_S: Final = 15
MAX_INTERVAL_S: Final = 300
DEFAULT_INTERVAL_S: Final = 60
DEFAULT_SERVICE_NAME: Final = "rayito"

NameStyle = Literal["rayito", "e2b"]
_NAME_STYLES: Final = ("rayito", "e2b")


@dataclass(frozen=True)
class OtlpAuth:
    """Cómo firma `rayd` cada exportación (research §6.3: opciones B1/B1').

    `execution_role()` es SigV4 sobre las credenciales IMDS del execution
    role: exige `rayito-base-caps` (o una variante derivada, por tamaño).
    `bearer(secret_name=...)` es experimental (B1'): el valor del secreto se
    resuelve una vez, al enviar la sección (`_section.resolve_bearer_token`),
    y viaja a `rayd` por `ConfigureSandbox`, nunca por variables de entorno
    (regla 4 de ADR-014) ni en texto plano en ningún lado; funciona en
    `rayito-base`, sin caps.
    """

    kind: Literal["execution_role", "bearer"]
    secret_name: str | None = None

    @classmethod
    def execution_role(cls) -> OtlpAuth:
        return cls(kind="execution_role")

    @classmethod
    def bearer(cls, secret_name: str) -> OtlpAuth:
        if not secret_name.strip():
            raise InvalidArgumentException("OtlpAuth.bearer(secret_name=...) no puede estar vacío")
        return cls(kind="bearer", secret_name=secret_name)


@dataclass(frozen=True)
class TelemetryExport:
    """`telemetry=` de `Sandbox.create()`/`AsyncSandbox.create()`. Validado
    al construirse: un valor fuera de rango es `InvalidArgumentException`
    antes de `run-microvm`, nunca una sección rechazada por `rayd` después
    de lanzar la `MicroVM`.
    """

    interval_s: int = DEFAULT_INTERVAL_S
    service_name: str = DEFAULT_SERVICE_NAME
    names: NameStyle = "rayito"
    auth: OtlpAuth = field(default_factory=OtlpAuth.execution_role)

    def __post_init__(self) -> None:
        if not (MIN_INTERVAL_S <= self.interval_s <= MAX_INTERVAL_S):
            raise InvalidArgumentException(
                f"telemetry=TelemetryExport(interval_s={self.interval_s}) debe estar entre "
                f"{MIN_INTERVAL_S} y {MAX_INTERVAL_S}"
            )
        if not self.service_name.strip():
            raise InvalidArgumentException(
                "telemetry=TelemetryExport(service_name=...) no puede estar vacío"
            )
        if self.names not in _NAME_STYLES:
            raise InvalidArgumentException(
                f"telemetry=TelemetryExport(names={self.names!r}) no reconocido: usa "
                f"{' o '.join(_NAME_STYLES)!r}"
            )


@dataclass(frozen=True)
class TelemetryHealth:
    """`sbx.get_telemetry_status()`: espejo de `TelemetryExportStatus`
    (`ConfigureService.ConfigureStatus`). Todo ceros/`None` cuando `rayd`
    nunca aplicó una sección (no hay `telemetry=`, o la imagen no soporta
    la función)."""

    exported: int = 0
    dropped: int = 0
    last_error_class: str | None = None


def plan(telemetry: object, *, image_variant: str | None) -> TelemetryExport:
    """Único chequeo previo a `run-microvm` además del tipo: la variante
    caps, cuando el nombre de imagen ya permite saberlo
    (`_role_policy.require_caps_for`, diferido a `Health.features` en otro
    caso). `telemetry` llega como `object` porque `FeatureOptions.telemetry`
    es deliberadamente laxo (`Any`); cualquier cosa que no sea un
    `TelemetryExport` ya construido (y por tanto ya validado por su propio
    `__post_init__`) es `InvalidArgumentException` antes de tocar nada más.
    """
    if not isinstance(telemetry, TelemetryExport):
        raise InvalidArgumentException(
            f"telemetry= debe ser un TelemetryExport, no {type(telemetry).__name__}"
        )
    if telemetry.auth.kind == "execution_role":
        require_caps_for("telemetry=", image_variant)
    return telemetry
