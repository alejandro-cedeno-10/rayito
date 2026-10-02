"""Parte pura de `ConfigureSandbox` (M15 foundations, ADR-015): qué dice
`Health.features` sobre un agente, y cómo se traduce el resultado de una
sección de `Configure` a una excepción. Las llamadas gRPC viven en los
adaptadores (`sandbox_sync/configure.py`, `sandbox_async/configure.py`);
este módulo no importa `grpc`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from rayito.exceptions import SandboxException, UnimplementedError
from rayito.v1 import configure_pb2

if TYPE_CHECKING:
    from rayito.v1 import features_pb2, health_pb2

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
    orden), el flag de `AgentFeatures` que debe estar activo, cómo rellena
    su campo del `ConfigureRequest` compartido y cómo traduce el
    `SectionResult` que le corresponde en su propia excepción (`fill`)."""

    @property
    def section(self) -> str: ...

    @property
    def required_flag(self) -> str: ...

    def fill(self, request: configure_pb2.ConfigureRequest) -> None: ...

    def check_result(self, code: int, error_class: str) -> None: ...


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


#: Wire `ConfigSection` -> the string every `ConfigureSection.section`
#: property uses; grows by one entry each time a feature's own section
#: joins (`efs_volumes`, ...), never by renaming an existing one.
_WIRE_SECTION_NAMES: dict[int, str] = {
    configure_pb2.CONFIG_SECTION_S3_MOUNTS: "s3_mounts",
    configure_pb2.CONFIG_SECTION_EFS_VOLUMES: "efs_volumes",
    configure_pb2.CONFIG_SECTION_LIFECYCLE_EVENTS: "lifecycle_events",
    configure_pb2.CONFIG_SECTION_TELEMETRY_EXPORT: "telemetry_export",
    configure_pb2.CONFIG_SECTION_SECRET_GATEWAY: "secret_gateway",
}


def require_capabilities(sections: Sequence[ConfigureSection], features: AgentFeatures) -> None:
    """Puerta de capacidad previa al envío: la primera sección cuyo propio
    `required_flag` esté en `False` en `features` lanza `UnimplementedError`
    nombrándola, antes de construir un solo `ConfigureRequest`. Se llama
    justo tras el primer `Health`, dentro del mismo `try`/`except` que
    `_open` ya usa para terminar el sandbox ante cualquier fallo anterior a
    `agent_ready` (salvo `keep_on_failure`): esta puerta reutiliza esa
    terminación, no implementa la suya propia.
    """
    for entry in sections:
        if not getattr(features, entry.required_flag, False):
            raise UnimplementedError(
                entry.section,
                "esta imagen no tiene un adaptador real para esta función "
                "(agente anterior a 0.6.0, o variante de imagen sin el caps que necesita)",
                doc=CONFIGURE_DOC,
            )


def build_configure_request(
    sections: Sequence[ConfigureSection],
) -> configure_pb2.ConfigureRequest:
    """Un único `ConfigureRequest` con todas las secciones de `sections`
    rellenas; `request_id` es nuevo en cada llamada (idempotencia nunca
    pedida por `create()`/`take()`, que sólo llaman una vez por sandbox)."""
    request = configure_pb2.ConfigureRequest(request_id=uuid.uuid4().hex)
    for entry in sections:
        entry.fill(request)
    return request


def check_configure_response(
    response: configure_pb2.ConfigureResponse, sections: Sequence[ConfigureSection]
) -> None:
    """Traduce cada `SectionResult` de `response` a la excepción de su
    propia sección (`ConfigureSection.check_result`); un resultado para una
    sección que `sections` no contiene (no debería ocurrir: el agente sólo
    responde por lo que `ConfigureRequest` llevaba) se ignora en vez de
    fallar de forma opaca.
    """
    by_name = {entry.section: entry for entry in sections}
    for result in response.results:
        name = _WIRE_SECTION_NAMES.get(result.section)
        entry = by_name.get(name) if name is not None else None
        if entry is None:
            continue
        entry.check_result(result.code, result.error_class)
