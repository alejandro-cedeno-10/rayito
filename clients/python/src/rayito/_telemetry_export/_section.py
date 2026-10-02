"""Adapta `TelemetryExport` a `ConfigureRequest.telemetry_export` (M15,
m15-rayd-otlp). Puro: `build_section` recibe los hechos de imagen (sólo
conocidos tras `run-microvm`) y, si aplica, el bearer ya resuelto -- nunca
su nombre de secreto, y nunca en un log, un `repr` o una excepción.

`resolve_bearer_token`/`aresolve_bearer_token` leen el bearer de
`OtlpAuth.bearer(...)` por la misma `SecretCache` que `secrets=` (y por
tanto el mismo `SecretStore`): la misma regla de prefijo (`rayito/` por
defecto, `resolve_secret_id`), la misma traducción de errores y como mucho
un `GetSecretValue` por TTL. Esta es la única llamada a AWS del paquete, y
sólo con `OtlpAuth.bearer(...)`.

`require_telemetry_support` es el gate compartido por `Sandbox._apply_telemetry`
y `AsyncSandbox._apply_telemetry`: antes de esto, cada uno repetía el mismo
chequeo y el mismo mensaje de `UnimplementedError` por separado.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from rayito._configure_base import AgentFeatures, require_configure_support
from rayito.exceptions import UnimplementedError
from rayito.v1 import telemetry_export_pb2

from ._domain import TelemetryExport

if TYPE_CHECKING:
    from rayito._secrets import SecretCache
    from rayito.v1 import configure_pb2

_NAMES_WIRE = {
    "rayito": telemetry_export_pb2.TELEMETRY_EXPORT_NAME_STYLE_RAYITO,
    "e2b": telemetry_export_pb2.TELEMETRY_EXPORT_NAME_STYLE_E2B,
}

#: `_apply_telemetry`'s own feature name, for `require_configure_support`'s
#: message and for `UnimplementedError`'s first argument.
TELEMETRY_FEATURE_NAME = "telemetry="
_TELEMETRY_DOC = "docs/site/docs/funciones-opcionales/exportacion-otlp.md"
#: La primera imagen cuyo `rayd` trae el exportador OTLP (m15-rayd-otlp).
_REQUIRED_AGENT = "rayd 0.6.0"


def require_telemetry_support(features: AgentFeatures | None) -> None:
    """`UnimplementedError` if the running agent never reports
    `telemetry_export = true`: absent entirely (an agent older than 0.6.0,
    via `require_configure_support`) or present but `False` (a 0.6 `rayd`
    started without `AWS_REGION`, so it has no CloudWatch endpoint to
    export to). Shared verbatim by `Sandbox._apply_telemetry` and
    `AsyncSandbox._apply_telemetry` so the gate and its message can never
    drift between the two.
    """
    resolved = require_configure_support(features, TELEMETRY_FEATURE_NAME)
    if not resolved.telemetry_export:
        raise UnimplementedError(
            TELEMETRY_FEATURE_NAME,
            f"el rayd de esta imagen no admite exportar telemetría: es anterior a "
            f"{_REQUIRED_AGENT} o arrancó sin AWS_REGION. Usa una imagen rayito-base "
            "(o rayito-base-caps para OtlpAuth.execution_role()) publicada con "
            f"{_REQUIRED_AGENT} o posterior",
            doc=_TELEMETRY_DOC,
        )


def _bearer_secret_name(telemetry: TelemetryExport) -> str | None:
    return telemetry.auth.secret_name if telemetry.auth.kind == "bearer" else None


def resolve_bearer_token(telemetry: TelemetryExport, cache: SecretCache) -> str | None:
    """El valor del secreto de `OtlpAuth.bearer(...)` (vía `cache`, con su
    prefijo `rayito/` y su TTL), o `None` con `OtlpAuth.execution_role()`.
    Los errores son los de `SecretCache.get`: nunca repiten el nombre ni el
    valor."""
    secret_name = _bearer_secret_name(telemetry)
    return None if secret_name is None else cache.get(secret_name)


async def aresolve_bearer_token(telemetry: TelemetryExport, cache: SecretCache) -> str | None:
    """`resolve_bearer_token` para asyncio (`SecretCache.aget`: un acierto no
    sale del bucle; un fallo corre en un hilo)."""
    secret_name = _bearer_secret_name(telemetry)
    return None if secret_name is None else await cache.aget(secret_name)


@dataclass(frozen=True)
class TelemetryExportSection:
    """`rayito._configure_base.ConfigureSection`: ya trae todo lo que
    `rayd` necesita, incluidos los hechos de imagen y (si aplica) el bearer
    ya resuelto."""

    telemetry: TelemetryExport
    image_arn: str
    image_version: str
    image_memory_mib: int
    #: Nunca en `repr` (tracebacks con locales, Sentry, depuradores).
    bearer_token: str | None = field(repr=False)

    @property
    def section(self) -> str:
        return "telemetry_export"

    @property
    def required_flag(self) -> str:
        return "telemetry_export"

    def fill(self, request: configure_pb2.ConfigureRequest) -> None:
        config = telemetry_export_pb2.TelemetryExportConfig(
            interval_s=self.telemetry.interval_s,
            service_name=self.telemetry.service_name,
            names=_NAMES_WIRE[self.telemetry.names],
            image_arn=self.image_arn,
            image_version=self.image_version,
            image_memory_mib=self.image_memory_mib,
        )
        if self.bearer_token is not None:
            config.bearer.token = self.bearer_token
        else:
            config.execution_role.SetInParent()
        request.telemetry_export.CopyFrom(config)


def build_section(
    telemetry: TelemetryExport,
    *,
    image_arn: str,
    image_version: str,
    image_memory_mib: int,
    bearer_token: str | None,
) -> TelemetryExportSection:
    """Construye la sección lista para `fill()` con el bearer ya resuelto
    (`resolve_bearer_token`/`aresolve_bearer_token`). Nunca se llama con
    `telemetry=None`."""
    return TelemetryExportSection(
        telemetry=telemetry,
        image_arn=image_arn,
        image_version=image_version,
        image_memory_mib=image_memory_mib,
        bearer_token=bearer_token,
    )
