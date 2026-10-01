"""Parte pura de `ConfigureSandbox` (M15 foundations, ADR-015): qué dice
`Health.features` sobre un agente, y cómo se traduce el resultado de una
sección de `Configure` a una excepción. Las llamadas gRPC viven en los
adaptadores (`sandbox_sync/configure.py`, `sandbox_async/configure.py`);
este módulo no importa `grpc`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from rayito.exceptions import SandboxException, UnimplementedError

if TYPE_CHECKING:
    from rayito.v1 import configure_pb2, features_pb2, health_pb2

CONFIGURE_DOC: str = "docs/site/docs/funciones-opcionales/pilas-opcionales.md"


@dataclass(frozen=True)
class AgentFeatures:
    """Espejo de `AgentFeatures` (`features.proto`): qué funciones 0.6
    soporta el agente en ejecución. Ningún flag es secreto."""

    configure: bool = False
    s3_mounts: bool = False
    efs_volumes: bool = False
    lifecycle_events: bool = False
    telemetry_export: bool = False
    secret_gateway: bool = False
    template_start: bool = False

    @classmethod
    def from_proto(cls, features: features_pb2.AgentFeatures) -> AgentFeatures:
        return cls(
            configure=features.configure,
            s3_mounts=features.s3_mounts,
            efs_volumes=features.efs_volumes,
            lifecycle_events=features.lifecycle_events,
            telemetry_export=features.telemetry_export,
            secret_gateway=features.secret_gateway,
            template_start=features.template_start,
        )


def agent_features_from_health(response: health_pb2.HealthResponse) -> AgentFeatures | None:
    """`None` cuando el agente es anterior a 0.6.0 (el campo `features` está
    ausente, presencia de mensaje de proto3: no basta con mirar los flags,
    que todos valdrían `False` igual que en un 0.6.0 sin ninguna función
    real todavía). Quien llama exige 0.6.0 con
    `require_configure_support` antes de usar cualquier flag.
    """
    if not response.HasField("features"):
        return None
    return AgentFeatures.from_proto(response.features)


def require_configure_support(features: AgentFeatures | None, feature: str) -> AgentFeatures:
    """`UnimplementedError` nombrando la imagen 0.6.0 si `features` es
    `None` (agente anterior a `ConfigureService`); devuelve `features` tal
    cual en otro caso, para que el llamante siga comprobando su propio flag.
    """
    if features is None:
        raise UnimplementedError(
            feature,
            "necesita una imagen 0.6.0 o posterior (ConfigureSandbox)",
            doc=CONFIGURE_DOC,
        )
    return features


class ConfigureSection(Protocol):
    """Lo que una función 0.6 implementa para participar en una sola
    llamada a `Configure`: el nombre de su sección (para los logs y el
    orden), el flag de `AgentFeatures` que debe estar activo y cómo rellena
    su campo del `ConfigureRequest` compartido."""

    @property
    def section(self) -> str: ...

    @property
    def required_flag(self) -> str: ...

    def fill(self, request: configure_pb2.ConfigureRequest) -> None: ...


def section_error(
    section: str, code_name: str, error_class: str
) -> SandboxException | UnimplementedError | None:
    """`None` cuando la sección se aplicó o sigue asentándose (`PENDING`);
    en otro caso la excepción que describe por qué no. `code_name` es el
    nombre del enum `SectionCode` tal cual lo da `Name()` de protobuf
    (`"SECTION_CODE_APPLIED"`, ...), para no acoplar este módulo al tipo
    generado.
    """
    if code_name in ("SECTION_CODE_APPLIED", "SECTION_CODE_PENDING"):
        return None
    if code_name == "SECTION_CODE_UNSUPPORTED":
        return UnimplementedError(
            section, "esta imagen no tiene esta función implementada todavía", doc=CONFIGURE_DOC
        )
    reason = error_class or code_name.removeprefix("SECTION_CODE_").lower()
    return SandboxException(f"{section}: {reason}")
