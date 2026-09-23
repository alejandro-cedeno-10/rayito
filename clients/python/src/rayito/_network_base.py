"""Núcleo puro de la política de egress (ADR-012) compartido por `Sandbox` y
`AsyncSandbox`: resolución de `network=`/`allow_internet_access`, las
comprobaciones tempranas de forma, la conversión a proto y la compuerta
fail-closed de `create()`. `rayd` sigue siendo la autoridad: estas
comprobaciones sólo evitan lanzar un MicroVM con una política que él
rechazaría. Nunca registra las listas, la dirección del proxy ni las
credenciales.
"""

from __future__ import annotations

import functools
import ipaddress
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from logging import getLogger
from typing import Final

import grpc

from rayito._limits import (
    EGRESS_HOSTNAME_MAX_CHARS,
    EGRESS_MAX_ENTRIES_PER_LIST,
    EGRESS_MAX_HOSTNAME_ENTRIES,
    EGRESS_PROXY_CREDENTIAL_MAX_BYTES,
)
from rayito._models import (
    ALL_TRAFFIC,
    EgressEnforcement,
    EgressProxy,
    NetworkOptions,
    NetworkPolicy,
    NetworkSelector,
    NetworkSelectorContext,
    NetworkState,
    SandboxHealth,
)
from rayito._transport import rpc_status, translate_rpc_error
from rayito.exceptions import InvalidArgumentException, UnimplementedError
from rayito.v1 import network_pb2

logger = getLogger("rayito.network")

NetworkInput = NetworkPolicy | NetworkOptions | Mapping[str, object] | None

ALLOW_INTERNET_ACCESS_FEATURE: Final = "allow_internet_access=False"
NETWORK_FEATURE: Final = "network"
UPDATE_NETWORK_FEATURE: Final = "update_network"
GET_NETWORK_FEATURE: Final = "get_network"
NETWORK_KEYS: Final = frozenset({"allow_out", "deny_out", "egress_proxy"})
EGRESS_PROXY_KEYS: Final = frozenset({"address", "username", "password"})
WILDCARD_PREFIX: Final = "*."

EGRESS_UNAVAILABLE_REASON: Final = (
    "la imagen no aplica política de egress en el guest (Health.egress_enforcement=NONE): usa "
    "una imagen M9 de rayito-base-caps (additionalOsCapabilities ALL) o, a nivel de "
    "plataforma, rayito.Sandbox.create(egress=[<ConnectorArn de "
    "infra/egress-connector.yaml>]); el sandbox se ha terminado"
)
CAPS_REASON: Final = (
    "la imagen no tiene CAP_NET_ADMIN: la política de egress exige una imagen M9 de "
    "rayito-base-caps (additionalOsCapabilities ALL)"
)
OLD_AGENT_REASON: Final = (
    "el rayd de esta imagen es anterior a NetworkService: usa una imagen M9 de rayito-base-caps"
)
ALLOW_ONLY_NOTICE: Final = (
    "allow_out sin deny_out no restringe nada: sin deny_out todo el egress está permitido "
    "(semántica de E2B); añade deny_out=[ALL_TRAFFIC] para una allowlist"
)

ENFORCEMENT_FROM_PROTO: Final[Mapping[int, EgressEnforcement]] = {
    network_pb2.EGRESS_ENFORCEMENT_UNSPECIFIED: EgressEnforcement.UNSPECIFIED,
    network_pb2.EGRESS_ENFORCEMENT_NONE: EgressEnforcement.NONE,
    network_pb2.EGRESS_ENFORCEMENT_GUEST_ROUTES: EgressEnforcement.GUEST_ROUTES,
    network_pb2.EGRESS_ENFORCEMENT_GUEST_ROUTES_AND_PROXY: (
        EgressEnforcement.GUEST_ROUTES_AND_PROXY
    ),
}
UNENFORCED: Final = frozenset({EgressEnforcement.UNSPECIFIED, EgressEnforcement.NONE})


@dataclass(frozen=True)
class NetworkLaunch:
    """La política de `create()` ya resuelta y validada, y la feature que
    nombra el `UnimplementedError` de la compuerta."""

    policy: NetworkPolicy
    feature: str

    @property
    def enforce(self) -> bool:
        return requires_enforcement(self.policy)

    @property
    def stored_policy(self) -> NetworkPolicy | None:
        """Lo que `LaunchOptions.network` guarda para `reincarnate()`."""
        return None if self.policy == NetworkPolicy() else self.policy


# ------------------------------------------------------------------ resolve


def resolve_selector(
    selector: NetworkSelector | None, ctx: NetworkSelectorContext, *, field: str
) -> tuple[str, ...]:
    """Una lista de cadenas tal cual; un invocable se llama con `ctx` (como
    E2B, en el cliente). Una cadena suelta no es una lista."""
    resolved = selector(ctx) if callable(selector) else selector
    if resolved is None:
        return ()
    if isinstance(resolved, str | bytes) or not isinstance(resolved, Sequence):
        raise InvalidArgumentException(f"{field} debe ser una lista de cadenas")
    for index, entry in enumerate(resolved):
        if not isinstance(entry, str):
            raise InvalidArgumentException(f"{field}[{index}] debe ser str")
    return tuple(resolved)


def resolve_network(network: NetworkInput, *, allow_internet_access: bool) -> NetworkPolicy:
    """`network=` como `NetworkPolicy`, con `allow_internet_access=False`
    añadiendo `ALL_TRAFFIC` a `deny_out` si falta (equivalencia de E2B)."""
    policy = policy_from_input(network)
    if allow_internet_access or ALL_TRAFFIC in policy.deny_out:
        return policy
    return NetworkPolicy(
        allow_out=policy.allow_out,
        deny_out=(*policy.deny_out, ALL_TRAFFIC),
        egress_proxy=policy.egress_proxy,
    )


def policy_from_input(network: NetworkInput) -> NetworkPolicy:
    ctx = NetworkSelectorContext()
    if network is None:
        return NetworkPolicy()
    if isinstance(network, NetworkPolicy):
        return NetworkPolicy(
            allow_out=resolve_selector(network.allow_out, ctx, field="allow_out"),
            deny_out=resolve_selector(network.deny_out, ctx, field="deny_out"),
            egress_proxy=proxy_from_input(network.egress_proxy),
        )
    if not isinstance(network, Mapping):
        raise InvalidArgumentException("network debe ser un NetworkPolicy o un dict")
    reject_unknown_keys(network, NETWORK_KEYS, field="network")
    return NetworkPolicy(
        allow_out=resolve_selector(network.get("allow_out"), ctx, field="allow_out"),  # type: ignore[arg-type]
        deny_out=resolve_selector(network.get("deny_out"), ctx, field="deny_out"),  # type: ignore[arg-type]
        egress_proxy=proxy_from_input(network.get("egress_proxy")),
    )


def proxy_from_input(proxy: object) -> EgressProxy | None:
    if proxy is None:
        return None
    if isinstance(proxy, EgressProxy):
        values: Mapping[str, object] = {
            "address": proxy.address,
            "username": proxy.username,
            "password": proxy.password,
        }
    elif isinstance(proxy, Mapping):
        reject_unknown_keys(proxy, EGRESS_PROXY_KEYS, field="egress_proxy")
        values = proxy
    else:
        raise InvalidArgumentException("egress_proxy debe ser un EgressProxy o un dict")
    address = values.get("address")
    if not isinstance(address, str):
        raise InvalidArgumentException("egress_proxy.address debe ser str (host:puerto)")
    return EgressProxy(
        address=address,
        username=optional_string(values.get("username"), field="egress_proxy.username"),
        password=optional_string(values.get("password"), field="egress_proxy.password"),
    )


def optional_string(value: object, *, field: str) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise InvalidArgumentException(f"{field} debe ser str")


def reject_unknown_keys(keys: Iterable[object], allowed: frozenset[str], *, field: str) -> None:
    for key in keys:
        if key not in allowed:
            raise InvalidArgumentException(
                f"{field}: clave desconocida {key!r} (admite {', '.join(sorted(allowed))})"
            )


def requires_enforcement(policy: NetworkPolicy) -> bool:
    """Espejo de `EgressPolicy::requires_enforcement` de `rayd`: sin
    `deny_out` ni `egress_proxy` la política no restringe nada."""
    return bool(policy.deny_out) or policy.egress_proxy is not None


def pool_network_kwarg(network: NetworkInput) -> NetworkPolicy | None:
    """`network=` tal como lo compara `create(pool=...)`: `None` si no pide
    nada, para que un `{}` explícito no cuente como kwarg de lanzamiento."""
    requested = resolve_network(network, allow_internet_access=True)
    return None if requested == NetworkPolicy() else requested


def plan_network_launch(network: NetworkInput, *, allow_internet_access: bool) -> NetworkLaunch:
    """Paso 1 de la compuerta de `create()`, antes de cualquier llamada a AWS:
    resuelve, valida y decide la feature del error con `egress_feature`."""
    policy = checked_policy(resolve_network(network, allow_internet_access=allow_internet_access))
    return NetworkLaunch(policy=policy, feature=egress_feature(policy, allow_internet_access))


def egress_feature(policy: NetworkPolicy, allow_internet_access: bool) -> str:
    """`allow_internet_access=False` sólo cuando la política resuelta es la que
    produce el flag por sí solo (`deny_out=[ALL_TRAFFIC]`, sin `allow_out` ni
    proxy); cualquier otra es `network`. Misma regla que `egressFeature` de TS."""
    only_the_flag = (
        not allow_internet_access
        and not policy.allow_out
        and policy.egress_proxy is None
        and policy.deny_out == (ALL_TRAFFIC,)
    )
    return ALLOW_INTERNET_ACCESS_FEATURE if only_the_flag else NETWORK_FEATURE


def update_policy(network: NetworkInput, allow_internet_access: bool | None) -> NetworkPolicy:
    """La política completa de `update_network`: `None` o `{}` es sin
    restricciones y sólo un `False` explícito añade `ALL_TRAFFIC`."""
    return checked_policy(
        resolve_network(network, allow_internet_access=allow_internet_access is not False)
    )


def checked_policy(policy: NetworkPolicy) -> NetworkPolicy:
    validate_policy_shape(policy)
    if policy.allow_out and not policy.deny_out:
        log_allow_only_notice()
    return policy


@functools.cache
def log_allow_only_notice() -> None:
    """Una vez por proceso: describe una configuración, no una llamada."""
    logger.info(ALLOW_ONLY_NOTICE)


# --------------------------------------------------------------- validation


def validate_policy_shape(policy: NetworkPolicy) -> NetworkPolicy:
    """Comprobaciones tempranas con los límites de `limits.json`; los mensajes
    nombran la lista y el índice, nunca el texto de la entrada."""
    for field, entries in (("allow_out", policy.allow_out), ("deny_out", policy.deny_out)):
        validate_entries(entries, field=field)
    for index, entry in enumerate(policy.deny_out):
        if is_hostname_entry(entry):
            raise InvalidArgumentException(
                f"deny_out[{index}]: los nombres de host no se admiten en deny_out"
            )
    hostnames = sum(1 for entry in policy.allow_out if is_hostname_entry(entry))
    if hostnames > EGRESS_MAX_HOSTNAME_ENTRIES:
        raise InvalidArgumentException(
            f"allow_out admite como mucho {EGRESS_MAX_HOSTNAME_ENTRIES} nombres de host"
        )
    if policy.egress_proxy is not None:
        validate_proxy_shape(policy.egress_proxy)
    return policy


def validate_entries(entries: Sequence[str], *, field: str) -> None:
    if len(entries) > EGRESS_MAX_ENTRIES_PER_LIST:
        raise InvalidArgumentException(
            f"{field} admite como mucho {EGRESS_MAX_ENTRIES_PER_LIST} entradas"
        )
    for index, entry in enumerate(entries):
        if not entry:
            raise InvalidArgumentException(f"{field}[{index}] está vacía")
        if is_hostname_entry(entry) and len(hostname_body(entry)) > EGRESS_HOSTNAME_MAX_CHARS:
            raise InvalidArgumentException(
                f"{field}[{index}]: un nombre de host admite como mucho "
                f"{EGRESS_HOSTNAME_MAX_CHARS} caracteres"
            )


def is_hostname_entry(entry: str) -> bool:
    """Todo lo que no es una IP ni un CIDR se trata como nombre de host;
    `rayd` decide después si es un nombre válido."""
    try:
        ipaddress.ip_network(entry, strict=False)
    except ValueError:
        return True
    return False


def hostname_body(entry: str) -> str:
    name = entry.removesuffix(".")
    return name.removeprefix(WILDCARD_PREFIX)


def validate_proxy_shape(proxy: EgressProxy) -> None:
    if not proxy.address:
        raise InvalidArgumentException("egress_proxy.address no puede estar vacía")
    if proxy.password is not None and proxy.username is None:
        raise InvalidArgumentException("egress_proxy.password exige egress_proxy.username")
    for field, value in (("username", proxy.username), ("password", proxy.password)):
        if value is not None and not 1 <= len(value.encode("utf-8")) <= (
            EGRESS_PROXY_CREDENTIAL_MAX_BYTES
        ):
            raise InvalidArgumentException(
                f"egress_proxy.{field} debe tener entre 1 y "
                f"{EGRESS_PROXY_CREDENTIAL_MAX_BYTES} bytes (RFC 1929)"
            )


# ------------------------------------------------------------------- proto


def policy_to_proto(policy: NetworkPolicy) -> network_pb2.NetworkPolicy:
    message = network_pb2.NetworkPolicy(allow_out=policy.allow_out, deny_out=policy.deny_out)
    proxy = policy.egress_proxy
    if proxy is not None:
        message.egress_proxy.address = proxy.address
        if proxy.username is not None:
            message.egress_proxy.username = proxy.username
        if proxy.password is not None:
            message.egress_proxy.password = proxy.password
    return message


def enforcement_from_proto(value: int) -> EgressEnforcement:
    """Un valor que este SDK no conoce cuenta como `UNSPECIFIED`: la
    compuerta falla cerrada ante lo desconocido."""
    return ENFORCEMENT_FROM_PROTO.get(int(value), EgressEnforcement.UNSPECIFIED)


def state_from_proto(response: network_pb2.NetworkState) -> NetworkState:
    port = int(response.local_proxy_port)
    return NetworkState(
        allow_out=tuple(str(entry) for entry in response.allow_out),
        deny_out=tuple(str(entry) for entry in response.deny_out),
        egress_proxy_configured=bool(response.egress_proxy_configured),
        enforcement=enforcement_from_proto(response.enforcement),
        local_proxy_port=port or None,
    )


# -------------------------------------------------------------------- gate


def readiness_enforcement(health: SandboxHealth | None) -> EgressEnforcement:
    """Lo que publicó el `Health` que satisfizo la readiness; sin él,
    `UNSPECIFIED`, que la compuerta trata como `NONE` (falla cerrada)."""
    return EgressEnforcement.UNSPECIFIED if health is None else health.egress_enforcement


def egress_gate_error(
    sandbox_id: str, enforcement: EgressEnforcement, feature: str
) -> UnimplementedError | None:
    """El error de la compuerta cuando el guest no aplica la política
    (`NONE`, o `UNSPECIFIED` de un agente anterior a M9); `None` si la aplica.
    El caller ya terminó (o termina) el MicroVM."""
    if enforcement not in UNENFORCED:
        return None
    error = UnimplementedError(feature, EGRESS_UNAVAILABLE_REASON)
    error.add_note(f"sandbox terminado: {sandbox_id}")
    return error


def network_rpc_error(exc: grpc.RpcError, *, feature: str = UPDATE_NETWORK_FEATURE) -> Exception:
    """`FAILED_PRECONDITION` (imagen sin `CAP_NET_ADMIN`) y `UNIMPLEMENTED`
    (agente anterior a `NetworkService`) son features ausentes; el resto
    sigue la tabla unaria (`INVALID_ARGUMENT`, `INTERNAL`...)."""
    code = rpc_status(exc)
    if code is grpc.StatusCode.FAILED_PRECONDITION:
        return UnimplementedError(feature, CAPS_REASON)
    if code is grpc.StatusCode.UNIMPLEMENTED:
        return UnimplementedError(feature, OLD_AGENT_REASON)
    return translate_rpc_error(exc)
