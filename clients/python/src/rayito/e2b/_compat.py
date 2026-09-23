"""Helpers puros del shim E2B 2.51: la tabla de kwargs de `create()`, el
ciclo de vida, la red, los filtros de `list()`, la conversión de modelos y
la resolución de opciones de conexión de las variantes de clase. Sin I/O,
para que las tablas sean testeables sin AWS ni `rayd`."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, cast

import grpc

from rayito._code_base import DEFAULT_LANGUAGE, normalize_language
from rayito._limits import (
    LIFECYCLE_CAP_MARGIN_SECONDS,
    MAX_DURATION_SECONDS,
    SUSPENDED_STATES,
    TERMINAL_STATES,
)
from rayito._metrics_base import HISTORY_UNIMPLEMENTED_REASON, is_history_unavailable
from rayito._models import ALL_TRAFFIC, IdlePolicy, NetworkOptions, NetworkState, SandboxListItem
from rayito._models import PtySize as NativePtySize
from rayito._models import SandboxInfo as NativeSandboxInfo
from rayito._models import SandboxMetrics as NativeSandboxMetrics
from rayito._sandbox_base import ACCESS_TOKEN_ENV_VAR, LoggingOption, PortLike
from rayito.e2b._connection import (
    API_PARAM_NAMES,
    ConnectionConfig,
    ConnectionSettings,
    connection_overrides,
    ignored_param_warnings,
    merge_bound_params,
    reject_unknown_params,
    split_api_params,
)
from rayito.e2b._models import (
    PtySize,
    SandboxInfo,
    SandboxMetrics,
    SandboxQuery,
    SandboxState,
)
from rayito.e2b._unimplemented import unimplemented
from rayito.e2b.exceptions import NotFoundException, UnimplementedError
from rayito.exceptions import InvalidArgumentException, SandboxException

E2B_DEFAULT_TIMEOUT_SECONDS: Final = 300
E2B_DEFAULT_MAX_LIFETIME_SECONDS: Final = 3600
E2B_PAUSE_IDLE_SECONDS: Final = 300
SHIM_DEFAULT_INGRESS: Final[tuple[str, ...]] = ("ALL_INGRESS",)
INTERNET_EGRESS: Final = "INTERNET_EGRESS"
INTERNET_EGRESS_CONNECTORS: Final[tuple[str, ...]] = (INTERNET_EGRESS,)

E2B_STATE_FILTERS: Final[dict[SandboxState, tuple[str, ...]]] = {
    SandboxState.RUNNING: ("PENDING", "RUNNING"),
    SandboxState.PAUSED: ("SUSPENDING", "SUSPENDED"),
}
METADATA_QUERY_STATES: Final[tuple[str, ...]] = ("RUNNING",)

SECURE_FALSE_WARNING: Final = "secure=False ignorado: Rayito exige x-access-token en todo RPC"
AVAILABLE_KERNELS_REASON: Final = (
    "kernels disponibles: python en toda imagen; bash, javascript y typescript en la "
    "variante rayito-base-poly; R y Java no (SPEC.md §4)"
)
POLY_KERNELS_REASON: Final = (
    "este kernel sólo existe en la variante rayito-base-poly (publícala con make "
    "image-publish-poly y úsala como template)"
)
LIFECYCLE_IMAGE_REASON: Final = (
    "la imagen no impone el timeout del servidor: publica una imagen M9 (ADR-011)"
)
METRICS_HISTORY_IMAGE_REASON: Final = HISTORY_UNIMPLEMENTED_REASON
CLASS_METRICS_REASON: Final = (
    "rayd exige el access token del sandbox (x-access-token): pásalo con access_token= o "
    "define RAYITO_ACCESS_TOKEN"
)
LIST_METADATA_STATE_REASON: Final = (
    "los metadatos viven en el agente; leerlos despertaría el sandbox"
)
HTTPS_PORTS_SUPPORTED: Final[bool] = False
HTTPS_PORTS_REASON: Final = (
    "el proxy de Lambda MicroVMs no reenvía TLS extremo a extremo a un puerto del guest "
    "(medido, fila 67 QE2 de AWS_API_NOTES.md §16); get_host(puerto) sirve HTTP en claro"
)
ON_RESUME_VALUES: Final = ("restore", "reboot")
LIFECYCLE_KEYS: Final = frozenset({"on_timeout", "auto_resume"})
ON_TIMEOUT_KEYS: Final = frozenset({"action", "keep_memory"})
TIMEOUT_ACTIONS: Final = ("kill", "pause")
NETWORK_POLICY_KEYS: Final = ("allow_out", "deny_out", "egress_proxy")
NETWORK_UPDATE_KEYS: Final = frozenset({*NETWORK_POLICY_KEYS, "rules", "allow_internet_access"})
MIN_PORT: Final = 1
MAX_PORT: Final = 65535
USER_SLOT_MESSAGES: Final = {
    "watch_dir": (
        "watch_dir: on_event es keyword-only en el contrato 2.x (el segundo posicional es user)"
    ),
    "pty.create": "pty.create: on_data es keyword-only en el contrato 2.x",
}


@dataclass(frozen=True)
class ShimLifecycle:
    """`lifecycle` de E2B ya validado: la acción al vencer y si una petición
    posterior reanuda un sandbox pausado."""

    on_timeout: str
    auto_resume: bool


@dataclass(frozen=True)
class CreateMapping:
    """Resultado de `map_create_kwargs`: los kwargs del `create()` nativo y
    los avisos que el wrapper emite (uno por kwarg ignorado)."""

    native_kwargs: dict[str, Any]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class NativeCall:
    """Kwargs de una llamada nativa con el `transport` y el `control_plane`
    resueltos desde los `ApiParams`, los avisos de los ignorados y los
    ajustes aplicados (para `connection_config`)."""

    kwargs: dict[str, Any]
    warnings: tuple[str, ...]
    settings: ConnectionSettings
    integration: str | None


@dataclass(frozen=True)
class ListMapping:
    """Los filtros nativos de `Sandbox.paginate` para un `list()` de E2B."""

    states: tuple[str, ...] | None
    metadata: dict[str, str] | None
    started_after: datetime | None
    template: str | None


def action_of(on_timeout: Any) -> tuple[str, Any]:
    if on_timeout is None:
        return "kill", None
    if isinstance(on_timeout, str):
        if on_timeout not in TIMEOUT_ACTIONS:
            raise InvalidArgumentException(
                f"on_timeout debe ser 'pause' o 'kill' (recibido {on_timeout!r})"
            )
        return on_timeout, None
    if not isinstance(on_timeout, Mapping):
        raise InvalidArgumentException("on_timeout debe ser 'pause', 'kill' o un dict con action")
    for key in on_timeout:
        if key not in ON_TIMEOUT_KEYS:
            raise InvalidArgumentException(f"on_timeout: clave desconocida {key!r}")
    action = on_timeout.get("action")
    if action not in TIMEOUT_ACTIONS:
        raise InvalidArgumentException(
            f"on_timeout[\"action\"] debe ser 'pause' o 'kill' (recibido {action!r})"
        )
    return str(action), on_timeout.get("keep_memory")


def map_lifecycle(
    lifecycle: Mapping[str, Any] | None, *, auto_pause: bool | None = None
) -> ShimLifecycle:
    """El `lifecycle` de E2B validado como E2B: una acción desconocida, una
    clave desconocida, `keep_memory` con `kill` o `auto_resume=True` sin
    pausa es `InvalidArgumentException`; `keep_memory: False` es
    `UnimplementedError` (`suspend-microvm` siempre guarda la memoria).
    `auto_pause=True` (de `beta_create`) es `pause` sin auto-resume."""
    if lifecycle is None:
        return ShimLifecycle("pause" if auto_pause else "kill", False)
    if auto_pause is not None:
        raise InvalidArgumentException("auto_pause y lifecycle no se combinan: usa lifecycle")
    if not isinstance(lifecycle, Mapping):
        raise InvalidArgumentException("lifecycle debe ser un dict con on_timeout y auto_resume")
    for key in lifecycle:
        if key not in LIFECYCLE_KEYS:
            raise InvalidArgumentException(f"lifecycle: clave desconocida {key!r}")
    action, keep_memory = action_of(lifecycle.get("on_timeout"))
    if keep_memory is not None and action == "kill":
        raise InvalidArgumentException("on_timeout: keep_memory sólo aplica a action='pause'")
    if keep_memory is False:
        raise unimplemented("lifecycle.on_timeout.keep_memory=False")
    if keep_memory is not None and not isinstance(keep_memory, bool):
        raise InvalidArgumentException("on_timeout: keep_memory debe ser un bool")
    auto_resume = lifecycle.get("auto_resume")
    if auto_resume is not None and not isinstance(auto_resume, bool):
        raise InvalidArgumentException("auto_resume debe ser un bool")
    if auto_resume and action != "pause":
        raise InvalidArgumentException("auto_resume sólo puede ser True con on_timeout='pause'")
    return ShimLifecycle(action, bool(auto_resume))


def default_max_lifetime(timeout: int) -> int:
    """`max(3600, min(timeout + 60, 28800))`: la vida de plataforma por
    defecto de un sandbox del shim."""
    return max(
        E2B_DEFAULT_MAX_LIFETIME_SECONDS,
        min(timeout + LIFECYCLE_CAP_MARGIN_SECONDS, MAX_DURATION_SECONDS),
    )


def validate_https_ports(value: Any) -> None:
    if not isinstance(value, list | tuple):
        raise InvalidArgumentException("network: https_ports debe ser una lista de puertos")
    for port in value:
        if isinstance(port, bool) or not isinstance(port, int) or not MIN_PORT <= port <= MAX_PORT:
            raise InvalidArgumentException("network: https_ports admite enteros de 1 a 65535")
    if value and not HTTPS_PORTS_SUPPORTED:
        raise unimplemented("network.https_ports", HTTPS_PORTS_REASON)


def map_network_key(key: str, value: Any, options: dict[str, Any]) -> None:
    if key in NETWORK_POLICY_KEYS:
        options[key] = value
    elif key == "rules":
        raise unimplemented("network.rules")
    elif key == "mask_request_host":
        raise unimplemented("network.mask_request_host")
    elif key == "allow_public_traffic":
        if value is True:
            raise unimplemented("network.allow_public_traffic=True")
        if value not in (False, None):
            raise InvalidArgumentException("network: allow_public_traffic debe ser un bool")
    elif key == "https_ports":
        validate_https_ports(value)
    else:
        raise TypeError(f"network: clave desconocida '{key}'")


def map_network(network: Mapping[str, Any] | None) -> NetworkOptions | None:
    """`network` de E2B como `network=` nativo: `allow_out`, `deny_out` y
    `egress_proxy` pasan (los selectores invocables reciben el contexto de
    E2B con `ctx.all_traffic`); `rules`, `mask_request_host` y
    `allow_public_traffic=True` son `UnimplementedError`; `allow_public_traffic=
    False` es el comportamiento permanente; `https_ports` sigue la medición
    QE2; cualquier otra clave es `TypeError`. `None` si no pide política."""
    if network is None:
        return None
    if not isinstance(network, Mapping):
        raise InvalidArgumentException("network debe ser un dict")
    options: dict[str, Any] = {}
    for key, value in network.items():
        map_network_key(key, value, options)
    if not options:
        return None
    return cast("NetworkOptions", options)


def map_network_update(
    network: Mapping[str, Any] | None,
) -> tuple[NetworkOptions | None, bool | None]:
    """El `update_network(network)` de E2B: las claves de la política más
    `allow_internet_access`; devuelve la política y el flag."""
    if network is None:
        return None, None
    if not isinstance(network, Mapping):
        raise InvalidArgumentException("network debe ser un dict")
    policy = {key: value for key, value in network.items() if key != "allow_internet_access"}
    for key in policy:
        if key not in NETWORK_UPDATE_KEYS:
            raise TypeError(f"network: clave desconocida '{key}'")
    allow = network.get("allow_internet_access")
    if allow is not None and not isinstance(allow, bool):
        raise InvalidArgumentException("allow_internet_access debe ser un bool")
    return map_network(policy), allow


def reject_resource_kwargs(*, mcp: Any, iam: Any, volume_mounts: Any) -> None:
    if mcp is not None:
        raise unimplemented("mcp")
    if iam is not None:
        raise unimplemented("iam")
    if volume_mounts is not None:
        raise unimplemented("volume_mounts")


def map_create_kwargs(
    template: str | None = None,
    timeout: int | None = None,
    metadata: Mapping[str, str] | None = None,
    envs: Mapping[str, str] | None = None,
    secure: bool | None = None,
    allow_internet_access: bool | None = None,
    mcp: Any | None = None,
    network: Mapping[str, Any] | None = None,
    iam: Any | None = None,
    lifecycle: Mapping[str, Any] | None = None,
    volume_mounts: Any | None = None,
    logger: logging.Logger | None = None,
    *,
    max_lifetime: int | None = None,
    auto_pause: bool | None = None,
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
    """La tabla D5 en el orden posicional de E2B 2.x: `mcp`, `iam` y
    `volume_mounts` son `UnimplementedError` antes de mapear nada; `timeout`
    (300 s por defecto) es el plazo lógico que impone `rayd`, con
    `max_lifetime` (por defecto `max(3600, min(timeout + 60, 28800))`) como
    `maximumDurationInSeconds`; `lifecycle` es `on_timeout` más la política
    de idle de la pausa; `allow_internet_access` y `network` son la política
    de egress en el guest sobre el conector `INTERNET_EGRESS`; `ingress` es
    `ALL_INGRESS` salvo que se pase; `secure=False` sólo avisa."""
    reject_resource_kwargs(mcp=mcp, iam=iam, volume_mounts=volume_mounts)
    shim_lifecycle = map_lifecycle(lifecycle, auto_pause=auto_pause)
    native_network = map_network(network)
    resolved_timeout = E2B_DEFAULT_TIMEOUT_SECONDS if timeout is None else timeout
    idle = (
        IdlePolicy(max_idle_seconds=E2B_PAUSE_IDLE_SECONDS, auto_resume=shim_lifecycle.auto_resume)
        if shim_lifecycle.on_timeout == "pause"
        else None
    )
    native: dict[str, Any] = {
        "template": template,
        "timeout": resolved_timeout,
        "on_timeout": shim_lifecycle.on_timeout,
        "idle": idle,
        "envs": envs,
        "metadata": metadata,
        "ingress": list(SHIM_DEFAULT_INGRESS) if ingress is None else list(ingress),
        "egress": list(INTERNET_EGRESS_CONNECTORS),
        "allow_internet_access": allow_internet_access is not False,
        "logging": logging,
        "keep_on_failure": keep_on_failure,
    }
    native["max_lifetime"] = (
        default_max_lifetime(resolved_timeout) if max_lifetime is None else max_lifetime
    )
    optional = {
        "network": native_network,
        "logger": logger,
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
    warnings = (SECURE_FALSE_WARNING,) if secure is False else ()
    return CreateMapping(native_kwargs=native, warnings=warnings)


def native_call_kwargs(
    bound: Mapping[str, Any],
    native_kwargs: Mapping[str, Any],
    api_params: Mapping[str, Any],
    *,
    call: str,
    with_transport: bool = True,
    with_request_timeout: bool = True,
) -> NativeCall:
    """Los kwargs nativos de una llamada del shim: valida los `ApiParams` de
    la llamada (una clave desconocida es `TypeError`), mezcla debajo los del
    cliente `E2B` (`bound`), avisa sólo de los ignorados de esta llamada (los
    del cliente avisaron al construirlo) y resuelve `transport` y
    `control_plane`."""
    reject_unknown_params(api_params, call=call)
    bound_api = {key: value for key, value in bound.items() if key in API_PARAM_NAMES}
    bound_native = {key: value for key, value in bound.items() if key not in API_PARAM_NAMES}
    native = merge_bound_params(bound_native, native_kwargs)
    settings, _ = split_api_params(merge_bound_params(bound_api, api_params), call=call)
    integration = ConnectionConfig.current_integration()
    transport, plane = connection_overrides(
        settings,
        transport=native.pop("transport", None),
        control_plane=native.pop("control_plane", None),
        session=native.get("session"),
        region=native.get("region"),
        integration=integration,
    )
    if with_transport and transport is not None:
        native["transport"] = transport
    if plane is not None:
        native["control_plane"] = plane
    if with_request_timeout and settings.request_timeout is not None:
        native["request_timeout"] = settings.request_timeout
    return NativeCall(
        kwargs=native,
        warnings=ignored_param_warnings(api_params),
        settings=settings,
        integration=integration,
    )


def lifecycle_unimplemented() -> UnimplementedError:
    """El error del shim para una imagen anterior a M9: el nativo ya terminó
    el VM recién lanzado."""
    return unimplemented("lifecycle", LIFECYCLE_IMAGE_REASON)


def validate_on_resume(on_resume: str) -> None:
    if on_resume == "reboot":
        raise unimplemented("connect(on_resume='reboot')")
    if on_resume not in ON_RESUME_VALUES:
        raise InvalidArgumentException(
            f"on_resume debe ser 'restore' o 'reboot' (recibido {on_resume!r})"
        )


def validate_keep_memory(keep_memory: bool | None) -> None:
    if keep_memory is False:
        raise unimplemented("pause(keep_memory=False)")


def reject_callable_in_user_slot(user: Any, operation: str) -> None:
    """Un invocable donde 2.x espera `user` es una llamada al estilo 1.x
    (`watch_dir(path, on_event)`): falla en alto en vez de tomarlo por usuario."""
    if callable(user):
        raise InvalidArgumentException(USER_SLOT_MESSAGES[operation])


def native_stdin(stdin: bool | None) -> bool:
    return bool(stdin)


def native_background(background: bool | None) -> bool:
    return bool(background)


def sandbox_state_from_aws(state: str, *, sandbox_id: str) -> SandboxState:
    """`PENDING|RUNNING` → `RUNNING`, `SUSPENDING|SUSPENDED` → `PAUSED`; un
    sandbox terminado es `NotFoundException`, como el 404 de E2B."""
    if state in TERMINAL_STATES:
        raise NotFoundException(f"el sandbox {sandbox_id} está {state}")
    if state in SUSPENDED_STATES:
        return SandboxState.PAUSED
    return SandboxState.RUNNING


def has_internet_connector(egress: Sequence[str]) -> bool:
    return any(connector.rsplit(":", 1)[-1] == INTERNET_EGRESS for connector in egress)


def network_dict(network: NetworkState) -> dict[str, list[str]]:
    return {"allow_out": list(network.allow_out), "deny_out": list(network.deny_out)}


def internet_access_from(
    network: NetworkState | None, *, network_read: bool, egress: Sequence[str]
) -> bool | None:
    """`False` sólo con una política leída que lo deniega todo; `True` con
    una leída que no, o sin política en el guest y con `INTERNET_EGRESS`;
    `None` si no se leyó."""
    if not network_read:
        return None
    if network is not None:
        return not (ALL_TRAFFIC in network.deny_out and not network.allow_out)
    return True if has_internet_connector(egress) else None


def lifecycle_dict(info: NativeSandboxInfo) -> dict[str, Any] | None:
    lifecycle = info.lifecycle
    if lifecycle is None or not lifecycle.managed:
        return None
    return {"on_timeout": lifecycle.on_timeout or "kill", "auto_resume": lifecycle.auto_resume}


def info_from_native(
    info: NativeSandboxInfo | SandboxListItem,
    *,
    network: NetworkState | None = None,
    network_read: bool = False,
) -> SandboxInfo:
    """El `SandboxInfo` 2.x de E2B (D10). `network` es el `NetworkState` que
    el shim leyó (`network_read`); sin él `network` y
    `allow_internet_access` son `None`."""
    state = sandbox_state_from_aws(info.state, sandbox_id=info.sandbox_id)
    metadata = None if info.metadata is None else dict(info.metadata)
    if isinstance(info, SandboxListItem):
        return SandboxInfo(
            sandbox_id=info.sandbox_id,
            sandbox_domain=None,
            template_id=info.template,
            name=info.template_name,
            metadata=metadata,
            started_at=info.started_at,
            end_at=None,
            state=state,
            cpu_count=None,
            memory_mb=None,
            envd_version=None,
            raw_state=info.state,
        )
    return SandboxInfo(
        sandbox_id=info.sandbox_id,
        sandbox_domain=info.endpoint,
        template_id=info.template,
        name=info.template_name,
        metadata=metadata,
        started_at=info.started_at,
        end_at=info.expires_at,
        state=state,
        cpu_count=info.cpu_count,
        memory_mb=info.memory_mb,
        envd_version=info.agent_version,
        allow_internet_access=internet_access_from(
            network, network_read=network_read, egress=info.egress
        ),
        network=None if network is None else network_dict(network),
        lifecycle=lifecycle_dict(info),
        raw_state=info.state,
    )


def list_item_to_info(item: SandboxListItem) -> SandboxInfo:
    return info_from_native(item)


def metrics_from_native(metrics: NativeSandboxMetrics) -> SandboxMetrics:
    return SandboxMetrics(
        timestamp=metrics.timestamp,
        cpu_used_pct=metrics.cpu_used_pct,
        cpu_count=metrics.cpu_count,
        mem_used=metrics.mem_used_bytes,
        mem_total=metrics.mem_total_bytes,
        disk_used=metrics.disk_used_bytes,
        disk_total=metrics.disk_total_bytes,
        mem_cache=metrics.mem_cache_bytes,
    )


def history_falls_back_to_snapshot(exc: UnimplementedError, *, ranged: bool) -> bool:
    """La decisión de `get_metrics()` cuando el historial falla, compartida
    por los shims sync y async (que hacen la IO): `True` si es una imagen
    anterior a M9 sin rango (cae en la instantánea); con rango es
    `UnimplementedError` encadenado a `exc`; `False` para cualquier otro
    fallo, que el llamador relanza tal cual."""
    if not is_history_unavailable(exc):
        return False
    if ranged:
        raise unimplemented("get_metrics(start=, end=)", METRICS_HISTORY_IMAGE_REASON) from exc
    return True


def needs_metrics_snapshot(samples: Sequence[NativeSandboxMetrics], *, ranged: bool) -> bool:
    """Sin rango y sin muestras, `get_metrics()` devuelve la instantánea de
    `Metrics` (un punto real, no una aproximación); con rango, la lista
    vacía es la respuesta."""
    return not samples and not ranged


def class_metrics_unimplemented(exc: UnimplementedError) -> UnimplementedError | None:
    if is_history_unavailable(exc):
        return unimplemented("Sandbox.get_metrics(sandbox_id)", METRICS_HISTORY_IMAGE_REASON)
    return None


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
        raise unimplemented("list(state=PAUSED, query.metadata)", LIST_METADATA_STATE_REASON)
    if wanted is None:
        return None
    resolved: list[str] = []
    for entry in wanted:
        resolved.extend(E2B_STATE_FILTERS[SandboxState(entry)])
    return tuple(resolved)


def list_mapping(
    query: SandboxQuery | None,
    state: Sequence[SandboxState] | None,
    template: str | None,
) -> ListMapping:
    """Los filtros de `list()`: `state` y `query.state` distintos, o
    `template` y `query.template` distintos, son `InvalidArgumentException`;
    los estados pasan por `states_for`."""
    query_state = None if query is None else query.state
    if state is not None and query_state is not None and set(state) != set(query_state):
        raise InvalidArgumentException("state y query.state difieren: usa sólo query.state")
    query_template = None if query is None else query.template
    if template is not None and query_template is not None and template != query_template:
        raise InvalidArgumentException(
            "template y query.template difieren: usa sólo query.template"
        )
    return ListMapping(
        states=states_for(state if state is not None else query_state, query),
        metadata=None if query is None else query.metadata,
        started_after=None if query is None else query.started_after,
        template=template if template is not None else query_template,
    )


def normalized_language_or_unimplemented(language: str | None, feature: str) -> str | None:
    """El nombre canónico que el core entiende (`bash`, `javascript`,
    `typescript`; `js` y `ts` son alias); `None` y `python` (sin distinguir
    mayúsculas) son el kernel por defecto y viajan como `None`, así una celda
    Python no lleva `language`. Cualquier otro kernel de E2B (`r`, `java`...)
    es `UnimplementedError` antes de tocar el agente. Que la imagen tenga el
    kernel lo decide el agente: bash, javascript y typescript sólo existen en
    `rayito-base-poly`, y en las demás imágenes el `UNIMPLEMENTED` del agente
    lo traduce `unimplemented_language`."""
    if language is None:
        return None
    try:
        canonical = normalize_language(language)
    except InvalidArgumentException as exc:
        raise unimplemented(f"{feature}(language={language!r})", AVAILABLE_KERNELS_REASON) from exc
    return None if canonical == DEFAULT_LANGUAGE else canonical


def unimplemented_language(
    error: SandboxException, feature: str, language: str | None
) -> UnimplementedError | None:
    """El `UnimplementedError` del shim para un agente que respondió
    `UNIMPLEMENTED` a un kernel que la imagen no trae (`grpc_code`
    `UNIMPLEMENTED`), o `None` para cualquier otro error, que el shim
    propaga sin tocar (p. ej. el `INVALID_ARGUMENT` de un agente anterior a
    M9 que no conoce `typescript`)."""
    if error.grpc_code is not grpc.StatusCode.UNIMPLEMENTED:
        return None
    return unimplemented(f"{feature}(language={language!r})", POLY_KERNELS_REASON)


def create_only_kwargs_given(**kwargs: Any) -> tuple[str, ...]:
    """Nombres de los kwargs de creación que un `Sandbox(sandbox_id=...)`
    (conexión, estilo E2B v1) no puede aplicar."""
    return tuple(name for name, value in kwargs.items() if value is not None)


def resolve_metrics_token(access_token: str | None, environ: Mapping[str, str]) -> str:
    """El token de `Sandbox.get_metrics(sandbox_id)`: `access_token` o
    `RAYITO_ACCESS_TOKEN`; sin ninguno es `UnimplementedError` antes de
    llamar a AWS."""
    token = access_token or environ.get(ACCESS_TOKEN_ENV_VAR)
    if not token:
        raise unimplemented("Sandbox.get_metrics(sandbox_id)", CLASS_METRICS_REASON)
    return token
