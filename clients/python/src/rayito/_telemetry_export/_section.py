"""Adapta `TelemetryExport` a `ConfigureRequest.telemetry_export` (M15,
m15-rayd-otlp). Pura salvo `resolve_bearer_token`, la única función de este
paquete que llama a AWS (`secretsmanager:GetSecretValue`, y sólo con
`OtlpAuth.bearer(...)`): construida ya con los hechos de imagen (sólo
conocidos tras `run-microvm`) y, si aplica, el valor del bearer ya resuelto
-- nunca su nombre de secreto, y nunca en un log o una excepción.

`require_telemetry_support` es el gate compartido por `Sandbox._apply_telemetry`
y `AsyncSandbox._apply_telemetry`: antes de esto, cada uno repetía el mismo
chequeo y el mismo mensaje de `UnimplementedError` por separado.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import boto3

from rayito._configure_base import AgentFeatures, require_configure_support
from rayito.exceptions import SecretException, UnimplementedError
from rayito.v1 import telemetry_export_pb2

from ._domain import TelemetryExport

if TYPE_CHECKING:
    from rayito.v1 import configure_pb2

_NAMES_WIRE = {
    "rayito": telemetry_export_pb2.TELEMETRY_EXPORT_NAME_STYLE_RAYITO,
    "e2b": telemetry_export_pb2.TELEMETRY_EXPORT_NAME_STYLE_E2B,
}

#: `_apply_telemetry`'s own feature name, for `require_configure_support`'s
#: message and for `UnimplementedError`'s first argument.
TELEMETRY_FEATURE_NAME = "telemetry="
_TELEMETRY_DOC = "docs/site/docs/funciones-opcionales/exportacion-otlp.md"


def require_telemetry_support(features: AgentFeatures | None) -> None:
    """`UnimplementedError` if the running agent never reports
    `telemetry_export = true`: absent entirely (an agent older than 0.6.0,
    via `require_configure_support`) or present but `False` (a 0.6.0 image
    without the OTLP exporter implemented, `features::slot::Unsupported` on
    `rayd`'s side). Shared verbatim by `Sandbox._apply_telemetry` and
    `AsyncSandbox._apply_telemetry` so the gate and its message can never
    drift between the two.
    """
    resolved = require_configure_support(features, TELEMETRY_FEATURE_NAME)
    if not resolved.telemetry_export:
        raise UnimplementedError(
            TELEMETRY_FEATURE_NAME,
            "esta imagen no tiene el exportador OTLP implementado todavía "
            "(pendiente de medición, docs/research/2026-10-e2b-out-of-scope.md §6)",
            doc=_TELEMETRY_DOC,
        )


def resolve_bearer_token(
    secret_name: str,
    *,
    region: str | None,
    session: boto3.session.Session | None = None,
) -> str:
    """Resuelve el valor de `secret_name` una vez, al enviar la sección (no
    en caché: a diferencia de `SecretCache`, el valor va directo a `rayd` y
    no se vuelve a leer hasta el siguiente `ConfigureSandbox`). El mensaje
    de error nunca repite el nombre del secreto ni su valor."""
    client = (session or boto3).client("secretsmanager", region_name=region)
    try:
        response = client.get_secret_value(SecretId=secret_name)
    except Exception as exc:
        raise SecretException("no se pudo leer el secreto de telemetry=") from exc
    value = response.get("SecretString")
    if not value:
        raise SecretException("el secreto de telemetry= no tiene SecretString")
    return str(value)


@dataclass(frozen=True)
class TelemetryExportSection:
    """`rayito._configure_base.ConfigureSection`: ya trae todo lo que
    `rayd` necesita, incluidos los hechos de imagen y (si aplica) el bearer
    ya resuelto."""

    telemetry: TelemetryExport
    image_arn: str
    image_version: str
    image_memory_mib: int
    bearer_token: str | None

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
    region: str | None,
    session: boto3.session.Session | None,
) -> TelemetryExportSection:
    """Resuelve `OtlpAuth.bearer` (si aplica) y construye la sección lista
    para `fill()`. Nunca se llama con `telemetry=None`."""
    bearer_token = (
        resolve_bearer_token(telemetry.auth.secret_name, region=region, session=session)
        if telemetry.auth.kind == "bearer" and telemetry.auth.secret_name is not None
        else None
    )
    return TelemetryExportSection(
        telemetry=telemetry,
        image_arn=image_arn,
        image_version=image_version,
        image_memory_mib=image_memory_mib,
        bearer_token=bearer_token,
    )
