"""Parte pura de `ConfigureSandbox` (M15 foundations, ADR-015): qué dice
`Health.features` sobre un agente, y cómo se traduce el resultado de una
sección de `Configure` a una excepción. Las llamadas gRPC viven en los
adaptadores (`sandbox_sync/configure.py`, `sandbox_async/configure.py`);
este módulo no importa `grpc`.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol, runtime_checkable

from rayito.exceptions import SandboxException, UnimplementedError
from rayito.v1 import configure_pb2

if TYPE_CHECKING:
    from rayito._secrets import SecretCache
    from rayito.v1 import features_pb2, health_pb2

CONFIGURE_DOC: str = "docs/site/docs/funciones-opcionales/pilas-opcionales.md"

#: Cadencia del sondeo de `ConfigureStatus` mientras alguna sección sigue
#: `SECTION_CODE_PENDING` tras `Configure`: un montaje local responde en
#: decenas de milisegundos una vez listo, así que un cuarto de segundo no
#: añade latencia apreciable a `create()` ni martillea al agente.
CONFIGURE_SETTLE_POLL_S: Final = 0.25


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
    su campo del `ConfigureRequest` compartido (`fill`), cómo traduce el
    `SectionResult` que le corresponde en su propia excepción
    (`check_result`) y, si el agente la deja en `SECTION_CODE_PENDING`,
    cuánto esperar (`settle_timeout_s`) y cómo leer su estado en
    `ConfigureStatus` (`check_status`)."""

    @property
    def section(self) -> str: ...

    @property
    def required_flag(self) -> str: ...

    def fill(self, request: configure_pb2.ConfigureRequest) -> None: ...

    def check_result(self, code: int, error_class: str) -> None: ...

    @property
    def settle_timeout_s(self) -> float: ...

    def check_status(self, status: configure_pb2.ConfigureStatusResponse, *, final: bool) -> bool:
        """`True` si la sección ya se asentó con éxito; lanza su propia
        excepción si falló, o si `final` y todavía no se ha asentado."""
        ...


#: Vuelve a mandar una sola sección ya aplicada (`fill` otra vez, `Configure`,
#: `check_configure_response`) y devuelve el `ConfigureStatus` resultante; síncrono
#: en `Sandbox`, asíncrono en `AsyncSandbox`.
Reapply = Callable[[], configure_pb2.ConfigureStatusResponse]
AsyncReapply = Callable[[], Awaitable[configure_pb2.ConfigureStatusResponse]]


@dataclass(frozen=True)
class SectionApplied:
    """Lo que `PostApplySection.after_apply` recibe una vez su `Configure`
    se aplicó: el `ConfigureStatus` de ese momento y cómo volver a mandar
    esa misma sección más tarde (`reapply` en `Sandbox`, `areapply` en
    `AsyncSandbox`; el otro es `None`). `main.py` lo construye igual para
    cualquier función, sin saber cuál es."""

    status: configure_pb2.ConfigureStatusResponse
    reapply: Reapply | None = None
    areapply: AsyncReapply | None = None


@runtime_checkable
class PostApplySection(ConfigureSection, Protocol):
    """Un `ConfigureSection` que además necesita algo después de aplicarse
    (hoy `gateways=`: leer los puertos de `ConfigureStatus` y poder rotar).
    `create()`/`take()` piden `ConfigureStatus` una sola vez si alguna
    sección lo implementa y guardan lo que devuelve `after_apply` bajo su
    `section`; la propiedad pública de esa función (`sbx.gateways`, ...) lo
    lee de ahí. Así `main.py` nunca nombra una función concreta."""

    def after_apply(self, applied: SectionApplied) -> object: ...


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


def raise_section_error(section: str, code: int, error_class: str) -> None:
    """`ConfigureSection.check_result` para una sección sin excepción propia
    (hoy `gateways=`): lanza lo que `section_error` diga del `SectionResult`
    de `section`, y nada si se aplicó o sigue `PENDING`. Así un `INVALID`/
    `FAILED`/`UNSUPPORTED` nunca pasa en silencio, ni en el `Configure` de
    `create()`/`take()` ni en uno posterior de una sola sección (un
    `refresh()`), que pasan los dos por `check_configure_response`.
    """
    error = section_error(section, configure_pb2.SectionCode.Name(code), error_class)
    if error is not None:
        raise error


class ImmediateSection(ABC):
    """La parte común de un `ConfigureSection` que `rayd` aplica en el acto
    (nunca `PENDING`) y sin error propio (`gateways=`, `telemetry=`):
    `check_result` lanza lo que `raise_section_error` diga y no hay espera.
    Cada subclase aporta `section`, `required_flag` y `fill`."""

    @property
    @abstractmethod
    def section(self) -> str: ...

    def check_result(self, code: int, error_class: str) -> None:
        raise_section_error(self.section, code, error_class)

    @property
    def settle_timeout_s(self) -> float:
        return 0.0

    def check_status(self, status: configure_pb2.ConfigureStatusResponse, *, final: bool) -> bool:
        del status, final
        return True


class SectionFactory(Protocol):
    """Una entrada de `FeaturePlan.configure_sections` que todavía no es un
    `ConfigureSection`: necesita la `SecretCache` del handle, que
    `create()`/`take()` sólo conocen después de `plan_features` (la misma
    que ya calculan para `secrets=`, nunca una segunda). Hoy
    `GatewaySectionFactory`; `resolve_sections` la invoca justo antes de
    construir el `ConfigureRequest`."""

    def __call__(self, cache: SecretCache) -> ConfigureSection: ...


#: Lo que `plan_features` pone en `FeaturePlan.configure_sections`: una
#: sección ya lista (`mounts=`) o una que espera la `SecretCache`
#: (`gateways=`).
PlannedSection = ConfigureSection | SectionFactory


def resolve_sections(
    planned: Sequence[PlannedSection], cache: Callable[[], SecretCache]
) -> tuple[ConfigureSection, ...]:
    """Cada entrada de `planned` como `ConfigureSection`: las que ya lo son
    tal cual, cada `SectionFactory` invocada con `cache()` — que sólo se
    llama (y sólo crea la caché compartida del proceso) si alguna entrada
    la necesita."""
    resolved: list[ConfigureSection] = []
    for entry in planned:
        resolved.append(entry(cache()) if callable(entry) else entry)
    return tuple(resolved)


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


@runtime_checkable
class CapabilityGate(Protocol):
    """Un `ConfigureSection` que explica él mismo por qué el agente no lo
    soporta (hoy `telemetry=`: un `rayd` 0.6 sin `AWS_REGION`), en lugar
    del mensaje genérico de `require_capabilities`."""

    def require_support(self, features: AgentFeatures) -> None: ...


def require_capabilities(sections: Sequence[ConfigureSection], features: AgentFeatures) -> None:
    """Puerta de capacidad previa al envío: la primera sección cuyo propio
    `required_flag` esté en `False` en `features` lanza `UnimplementedError`
    nombrándola (o su propio error, si es un `CapabilityGate`), antes de
    construir un solo `ConfigureRequest`. `create()`/`take()` la llaman
    dentro de `_apply_configure_sections`, que ya termina el sandbox ante
    cualquier fallo (salvo `keep_on_failure`).
    """
    for entry in sections:
        if isinstance(entry, CapabilityGate):
            entry.require_support(features)
            continue
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


def _sections_by_result(
    response: configure_pb2.ConfigureResponse, sections: Sequence[ConfigureSection]
) -> list[tuple[ConfigureSection, configure_pb2.SectionResult]]:
    """Cada `SectionResult` de `response` junto a su sección; un resultado
    para una sección que `sections` no contiene (no debería ocurrir: el
    agente sólo responde por lo que `ConfigureRequest` llevaba) se ignora en
    vez de fallar de forma opaca."""
    by_name = {entry.section: entry for entry in sections}
    paired: list[tuple[ConfigureSection, configure_pb2.SectionResult]] = []
    for result in response.results:
        name = _WIRE_SECTION_NAMES.get(result.section)
        entry = by_name.get(name) if name is not None else None
        if entry is not None:
            paired.append((entry, result))
    return paired


def check_configure_response(
    response: configure_pb2.ConfigureResponse, sections: Sequence[ConfigureSection]
) -> tuple[ConfigureSection, ...]:
    """Traduce cada `SectionResult` de `response` a la excepción de su
    propia sección (`ConfigureSection.check_result`) y devuelve las que
    quedaron en `SECTION_CODE_PENDING`: `create()` no vuelve hasta que
    `wait_settled`/su versión asíncrona las vea asentarse.
    """
    pending: list[ConfigureSection] = []
    for entry, result in _sections_by_result(response, sections):
        entry.check_result(result.code, result.error_class)
        if result.code == configure_pb2.SECTION_CODE_PENDING:
            pending.append(entry)
    return tuple(pending)


def settle_timeout_s(pending: Sequence[ConfigureSection]) -> float:
    """El mayor `settle_timeout_s` de las secciones pendientes: todas se
    sondean juntas en la misma `ConfigureStatus`."""
    return max((entry.settle_timeout_s for entry in pending), default=0.0)


def still_pending(
    status: configure_pb2.ConfigureStatusResponse,
    pending: Sequence[ConfigureSection],
    *,
    final: bool,
) -> tuple[ConfigureSection, ...]:
    """Las secciones de `pending` que `status` todavía no da por asentadas;
    cada una lanza su propia excepción si falló, o si `final` (se agotó
    `settle_timeout_s`) y sigue sin asentarse."""
    return tuple(entry for entry in pending if not entry.check_status(status, final=final))
