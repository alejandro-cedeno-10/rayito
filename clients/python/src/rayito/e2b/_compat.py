"""Helpers puros del shim E2B: la tabla de kwargs de `create()`, los avisos
por kwarg ignorado, la conversión de modelos y `unimplemented()`. Sin I/O,
para que la tabla sea testeable sin AWS ni `rayd`."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from rayito._code_base import DEFAULT_LANGUAGE, normalize_language
from rayito._limits import SUSPENDED_STATES, TERMINAL_STATES
from rayito._models import PtySize as NativePtySize
from rayito._models import SandboxInfo as NativeSandboxInfo
from rayito._models import SandboxListItem
from rayito._models import SandboxMetrics as NativeSandboxMetrics
from rayito._sandbox_base import LoggingOption, PortLike
from rayito.e2b._models import (
    PtySize,
    SandboxInfo,
    SandboxMetrics,
    SandboxQuery,
    SandboxState,
)
from rayito.e2b.exceptions import NotFoundException, UnimplementedError
from rayito.exceptions import InvalidArgumentException

E2B_DEFAULT_TIMEOUT_SECONDS: Final = 300
SHIM_DEFAULT_INGRESS: Final[tuple[str, ...]] = ("ALL_INGRESS",)
INTERNET_EGRESS: Final[tuple[str, ...]] = ("INTERNET_EGRESS",)

E2B_STATE_FILTERS: Final[dict[SandboxState, tuple[str, ...]]] = {
    SandboxState.RUNNING: ("PENDING", "RUNNING"),
    SandboxState.PAUSED: ("SUSPENDING", "SUSPENDED"),
}
METADATA_QUERY_STATES: Final[tuple[str, ...]] = ("RUNNING",)

IGNORED_KWARG_REASONS: Final[dict[str, str]] = {
    "api_key": "Rayito usa las credenciales de AWS de la sesión",
    "domain": "el endpoint lo asigna Lambda MicroVMs por sandbox",
    "debug": "no existe un modo debug local",
    "proxy": "el SDK habla con el proxy de AWS directamente",
}
SECURE_FALSE_WARNING: Final = "secure=False ignorado: Rayito exige x-access-token en todo RPC"
NO_EGRESS_REASON: Final = (
    "un MicroVM lanzado sin egressNetworkConnectors hereda el conector de egress de la "
    "versión de imagen (INTERNET_EGRESS en rayito-base) y sigue saliendo a internet "
    "(AWS_API_NOTES.md Q44, medido 2026-09-16); el allowlist de egress llega con el track "
    "de endurecimiento"
)


def unimplemented(feature: str, reason: str) -> UnimplementedError:
    return UnimplementedError(feature, reason)


@dataclass(frozen=True)
class CreateMapping:
    """Resultado de `map_create_kwargs`: los kwargs del `create()` nativo y
    los avisos que el wrapper emite (uno por kwarg ignorado)."""

    native_kwargs: dict[str, Any]
    warnings: tuple[str, ...]


def ignored_kwarg_warnings(
    *, api_key: str | None, domain: str | None, debug: bool, proxy: str | None, secure: bool
) -> tuple[str, ...]:
    given = {"api_key": api_key is not None, "domain": domain is not None, "debug": debug}
    given["proxy"] = proxy is not None
    messages = [
        f"{name} ignorado: {IGNORED_KWARG_REASONS[name]}"
        for name, present in given.items()
        if present
    ]
    if not secure:
        messages.append(SECURE_FALSE_WARNING)
    return tuple(messages)


def map_create_kwargs(
    *,
    template: str | None = None,
    timeout: int | None = None,
    metadata: Mapping[str, str] | None = None,
    envs: Mapping[str, str] | None = None,
    api_key: str | None = None,
    domain: str | None = None,
    debug: bool = False,
    request_timeout: float | None = None,
    proxy: str | None = None,
    secure: bool = True,
    allow_internet_access: bool = True,
    region: str | None = None,
    session: Any | None = None,
    template_version: str | None = None,
    execution_role_arn: str | None = None,
    allowed_ports: Sequence[PortLike] | None = None,
    ingress: Sequence[str] | None = None,
    logging: LoggingOption = "disabled",
    access_token: str | None = None,
    ready_timeout: float | None = None,
    reconnect_timeout: float | None = None,
    keep_on_failure: bool = False,
    control_plane: Any | None = None,
    transport: Any | None = None,
) -> CreateMapping:
    """La tabla D4 del diseño: `timeout` de E2B (300 s por defecto) es la vida
    del sandbox sin política de idle; `allow_internet_access=True` es el
    conector `INTERNET_EGRESS` y `False` es `UnimplementedError` (Q44: sin
    conector el MicroVM sigue saliendo a internet); `ingress` es `ALL_INGRESS`
    salvo que se pase; `api_key`, `domain`, `debug`, `proxy` y `secure=False`
    sólo avisan."""
    if not allow_internet_access:
        raise unimplemented("allow_internet_access=False", NO_EGRESS_REASON)
    native: dict[str, Any] = {
        "template": template,
        "timeout": E2B_DEFAULT_TIMEOUT_SECONDS if timeout is None else timeout,
        "idle": None,
        "envs": envs,
        "metadata": metadata,
        "ingress": list(SHIM_DEFAULT_INGRESS) if ingress is None else list(ingress),
        "egress": list(INTERNET_EGRESS),
        "logging": logging,
        "keep_on_failure": keep_on_failure,
    }
    optional = {
        "request_timeout": request_timeout,
        "region": region,
        "session": session,
        "template_version": template_version,
        "execution_role_arn": execution_role_arn,
        "allowed_ports": allowed_ports,
        "access_token": access_token,
        "ready_timeout": ready_timeout,
        "reconnect_timeout": reconnect_timeout,
        "control_plane": control_plane,
        "transport": transport,
    }
    native.update({name: value for name, value in optional.items() if value is not None})
    warnings = ignored_kwarg_warnings(
        api_key=api_key, domain=domain, debug=debug, proxy=proxy, secure=secure
    )
    return CreateMapping(native_kwargs=native, warnings=warnings)


def sandbox_state_from_aws(state: str, *, sandbox_id: str) -> SandboxState:
    """`PENDING|RUNNING` → `RUNNING`, `SUSPENDING|SUSPENDED` → `PAUSED`; un
    sandbox terminado es `NotFoundException`, como el 404 de E2B."""
    if state in TERMINAL_STATES:
        raise NotFoundException(f"el sandbox {sandbox_id} está {state}")
    if state in SUSPENDED_STATES:
        return SandboxState.PAUSED
    return SandboxState.RUNNING


def info_from_native(info: NativeSandboxInfo | SandboxListItem) -> SandboxInfo:
    end_at = info.expires_at if isinstance(info, NativeSandboxInfo) else None
    return SandboxInfo(
        sandbox_id=info.sandbox_id,
        template_id=info.template,
        name=info.template_name,
        metadata=None if info.metadata is None else dict(info.metadata),
        started_at=info.started_at,
        end_at=end_at,
        state=sandbox_state_from_aws(info.state, sandbox_id=info.sandbox_id),
        raw_state=info.state,
    )


def metrics_from_native(metrics: NativeSandboxMetrics) -> SandboxMetrics:
    return SandboxMetrics(
        timestamp=metrics.timestamp,
        cpu_used_pct=metrics.cpu_used_pct,
        cpu_count=metrics.cpu_count,
        mem_used=metrics.mem_used_bytes,
        mem_total=metrics.mem_total_bytes,
        disk_used=metrics.disk_used_bytes,
        disk_total=metrics.disk_total_bytes,
    )


def pty_size_to_native(size: PtySize | NativePtySize) -> NativePtySize:
    if isinstance(size, NativePtySize):
        return size
    if not isinstance(size, PtySize):
        raise InvalidArgumentException(
            f"size debe ser rayito.e2b.PtySize(rows, cols), recibido {type(size).__name__}"
        )
    return NativePtySize(cols=size.cols, rows=size.rows)


def states_for(
    state: Sequence[SandboxState] | None, query: SandboxQuery | None
) -> tuple[str, ...] | None:
    """Estados nativos de `list()`: sin filtro, el nativo por defecto (todo
    menos terminal); con `query.metadata`, sólo `RUNNING` (los metadatos
    viven en el agente y leerlos despertaría un sandbox pausado)."""
    wanted = tuple(state) if state is not None else None
    if query is not None and query.metadata is not None:
        if wanted is None or set(wanted) == {SandboxState.RUNNING}:
            return METADATA_QUERY_STATES
        raise unimplemented(
            "list(state=PAUSED, query.metadata)",
            "los metadatos viven en el agente; leerlos despertaría el sandbox",
        )
    if wanted is None:
        return None
    resolved: list[str] = []
    for entry in wanted:
        resolved.extend(E2B_STATE_FILTERS[SandboxState(entry)])
    return tuple(resolved)


AVAILABLE_KERNELS_REASON: Final = (
    "kernels disponibles: python y bash (variante rayito-base-poly); "
    "javascript es un nombre reservado sin kernel instalado en ninguna imagen; R y Java no"
)


def normalized_language_or_unimplemented(language: str | None, feature: str) -> str | None:
    """El nombre canónico que el core entiende (`bash`, `javascript`; `js` es
    alias); `None` y `python` (sin distinguir mayúsculas) son el kernel por
    defecto y viajan como `None`, así una celda Python no lleva `language`.
    Cualquier otro kernel de E2B (`r`, `java`...) es `UnimplementedError`
    antes de tocar el agente; que la imagen tenga instalado el kernel lo
    decide el agente (`bash` sólo en `rayito-base-poly`; `javascript` es un
    nombre reservado que hoy ninguna imagen trae)."""
    if language is None:
        return None
    try:
        canonical = normalize_language(language)
    except InvalidArgumentException as exc:
        raise unimplemented(f"{feature}(language={language!r})", AVAILABLE_KERNELS_REASON) from exc
    return None if canonical == DEFAULT_LANGUAGE else canonical


def create_only_kwargs_given(**kwargs: Any) -> tuple[str, ...]:
    """Nombres de los kwargs de creación que un `Sandbox(sandbox_id=...)`
    (conexión, estilo E2B v1) no puede aplicar."""
    return tuple(name for name, value in kwargs.items() if value is not None)
