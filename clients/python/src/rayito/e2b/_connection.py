"""Opciones de conexión de E2B 2.51 (`ApiParams`, `ConnectionConfig`) sobre
Rayito. Puro salvo `connection_overrides`, que sólo construye objetos
(el plano compartido y unos `TransportSettings`) sin llamar a AWS.

- `headers` son metadatos gRPC extra en cada RPC, detrás de las cabeceras
  reservadas; los mensajes de error nombran la clave, nunca el valor.
- `proxy` es una URL `http://host:puerto` (grpc-core sólo habla HTTP
  CONNECT) para el canal y para botocore; nunca se loguea ni se repite en
  un mensaje.
- `retries` son reintentos de botocore (intentos totales = `retries + 1`).
- `api_key`, `domain`, `debug`, `api_url`, `sandbox_url`,
  `validate_api_key` y `api_headers` avisan y se ignoran.
"""

from __future__ import annotations

import dataclasses
import logging
import re
import warnings
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, ClassVar, Final, TypedDict
from urllib.parse import urlsplit

from rayito._aws import ClientSettings, shared_control_plane
from rayito._transport import TransportSettings
from rayito.e2b.exceptions import RayitoCompatWarning
from rayito.exceptions import InvalidArgumentException


class ConnectionParams(TypedDict, total=False):
    """Los `ApiParams` salvo `request_timeout`, para los métodos de E2B que
    lo llevan como parámetro posicional."""

    retries: int | None
    headers: Mapping[str, str] | None
    api_headers: Mapping[str, str] | None
    api_key: str | None
    validate_api_key: bool | None
    domain: str | None
    api_url: str | None
    debug: bool | None
    proxy: str | None
    sandbox_url: str | None


class ApiParams(ConnectionParams, total=False):
    """Los once `ApiParams` de E2B 2.51, aceptados como keywords."""

    request_timeout: float | None


API_PARAM_NAMES: Final[frozenset[str]] = ApiParams.__optional_keys__ | ApiParams.__required_keys__

IGNORED_API_PARAMS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "api_key": "Rayito usa las credenciales de AWS de la sesión",
        "domain": "el endpoint lo asigna Lambda MicroVMs por sandbox",
        "debug": "no existe un modo debug local",
        "api_url": "no hay API de E2B: el plano de control es Lambda MicroVMs",
        "sandbox_url": "el endpoint lo asigna Lambda MicroVMs por sandbox",
        "validate_api_key": "no hay api_key que validar",
        "api_headers": "no hay API de E2B donde enviarlas; usa headers= para el sandbox",
    }
)

RESERVED_METADATA_KEYS: Final = frozenset(
    {
        "x-aws-proxy-auth",
        "x-aws-proxy-port",
        "x-aws-proxy-force-h2",
        "x-access-token",
        "rayito-compress",
        "user-agent",
        "content-type",
        "te",
        "host",
    }
)
RESERVED_PREFIXES: Final = ("x-aws-proxy-", "grpc-", ":")
RESERVED_SUFFIX: Final = "-bin"
TOKEN_PATTERN: Final = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
PRINTABLE_ASCII_PATTERN: Final = re.compile(r"[\x20-\x7e]*")
INTEGRATION_PATTERN: Final = re.compile(r"[\x21-\x7e]+(?: [\x21-\x7e]+)*")
DEFAULT_CONFIG_REQUEST_TIMEOUT_SECONDS: Final = 60.0
PROXY_URL_MESSAGE: Final = "proxy debe ser una URL http://host:puerto"
CONTROL_PLANE_CONFLICT_MESSAGE: Final = (
    "retries/proxy/set_integration no se combinan con control_plane="
)

MetadataPairs = tuple[tuple[str, str], ...]


def validate_extra_headers(headers: Mapping[str, str] | None) -> MetadataPairs:
    """Los pares `(clave en minúsculas, valor)` que viajan detrás de las
    cabeceras reservadas; una clave reservada, un prefijo reservado, `-bin`,
    una clave que no es token RFC 9110 o un valor fuera de ASCII imprimible
    es `InvalidArgumentException`."""
    if headers is None:
        return ()
    if not isinstance(headers, Mapping):
        raise InvalidArgumentException("headers debe ser un dict de str a str")
    pairs: list[tuple[str, str]] = []
    for raw_key, value in headers.items():
        if not isinstance(raw_key, str) or not isinstance(value, str):
            raise InvalidArgumentException("headers debe ser un dict de str a str")
        key = raw_key.lower()
        if (
            key in RESERVED_METADATA_KEYS
            or key.startswith(RESERVED_PREFIXES)
            or key.endswith(RESERVED_SUFFIX)
            or TOKEN_PATTERN.fullmatch(key) is None
        ):
            raise InvalidArgumentException(f"headers: la clave '{key}' está reservada")
        if PRINTABLE_ASCII_PATTERN.fullmatch(value) is None:
            raise InvalidArgumentException(f"headers: el valor de '{key}' no es ASCII imprimible")
        pairs.append((key, value))
    return tuple(pairs)


def validate_proxy_url(proxy: str | None) -> str | None:
    """`http://[usuario:clave@]host:puerto`; la URL nunca aparece en el error."""
    if proxy is None:
        return None
    if not isinstance(proxy, str):
        raise InvalidArgumentException(PROXY_URL_MESSAGE)
    try:
        parts = urlsplit(proxy)
        port = parts.port
    except ValueError:
        raise InvalidArgumentException(PROXY_URL_MESSAGE) from None
    if parts.scheme != "http" or not parts.hostname or port is None:
        raise InvalidArgumentException(PROXY_URL_MESSAGE)
    return proxy


def resolve_retries(retries: int | None) -> int | None:
    if retries is None:
        return None
    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
        raise InvalidArgumentException("retries debe ser un entero >= 0")
    return retries


def validate_request_timeout(request_timeout: float | None) -> float | None:
    if request_timeout is None:
        return None
    if isinstance(request_timeout, bool) or not isinstance(request_timeout, int | float):
        raise InvalidArgumentException("request_timeout debe ser un número de segundos")
    return request_timeout


def validate_integration(integration: str | None) -> str | None:
    if integration is None:
        return None
    if (
        not isinstance(integration, str)
        or INTEGRATION_PATTERN.fullmatch(integration) is None
        or " /" in integration
        or "/ " in integration
    ):
        raise InvalidArgumentException(
            "set_integration espera un nombre ASCII imprimible no vacío (p. ej. 'acme/1.0')"
        )
    return integration


@dataclass(frozen=True)
class ConnectionSettings:
    """Los `ApiParams` que Rayito aplica, ya validados. `headers` y `proxy`
    no salen en el `repr`."""

    request_timeout: float | None = None
    retries: int | None = None
    headers: MetadataPairs = field(default=(), repr=False)
    proxy: str | None = field(default=None, repr=False)

    @property
    def touches_transport(self) -> bool:
        return bool(self.headers) or self.proxy is not None


def is_given(value: Any) -> bool:
    return value is not None and value is not False


def ignored_param_warnings(params: Mapping[str, Any]) -> tuple[str, ...]:
    """Un aviso por `ApiParam` ignorado presente, con su nombre y nunca su valor."""
    return tuple(
        f"{name} ignorado: {reason}"
        for name, reason in IGNORED_API_PARAMS.items()
        if is_given(params.get(name))
    )


def reject_unknown_params(params: Mapping[str, Any], *, call: str) -> None:
    for name in params:
        if name not in API_PARAM_NAMES:
            raise TypeError(f"{call}() got an unexpected keyword argument '{name}'")


def split_api_params(
    params: Mapping[str, Any], *, call: str
) -> tuple[ConnectionSettings, tuple[str, ...]]:
    """Separa los `ApiParams` que Rayito aplica (validados) de los ignorados
    (un aviso cada uno). Una clave fuera de los once es el `TypeError` que
    daría Python."""
    reject_unknown_params(params, call=call)
    settings = ConnectionSettings(
        request_timeout=validate_request_timeout(params.get("request_timeout")),
        retries=resolve_retries(params.get("retries")),
        headers=validate_extra_headers(params.get("headers")),
        proxy=validate_proxy_url(params.get("proxy")),
    )
    return settings, ignored_param_warnings(params)


def merge_bound_params(bound: Mapping[str, Any], call: Mapping[str, Any]) -> dict[str, Any]:
    """La regla de E2B: gana el valor de la llamada y un `None` cae al del
    cliente; `headers` de la llamada sustituyen a los del cliente."""
    merged = dict(call)
    for key, value in bound.items():
        if merged.get(key) is None:
            merged[key] = value
    return merged


def connection_overrides(
    settings: ConnectionSettings,
    *,
    transport: TransportSettings | None,
    control_plane: Any | None,
    session: Any | None,
    region: str | None,
    integration: str | None,
) -> tuple[TransportSettings | None, Any | None]:
    """El `transport` con los metadatos extra y el proxy del canal, y el
    plano con reintentos, proxy e integración (uno compartido por
    `(session, region, settings)`). Sin nada que aplicar devuelve lo dado."""
    resolved_transport = transport
    if settings.touches_transport:
        resolved_transport = dataclasses.replace(
            transport or TransportSettings(),
            extra_metadata=settings.headers,
            http_proxy=settings.proxy,
        )
    if settings.retries is None and settings.proxy is None and integration is None:
        return resolved_transport, control_plane
    if control_plane is not None:
        raise InvalidArgumentException(CONTROL_PLANE_CONFLICT_MESSAGE)
    plane = shared_control_plane(
        session,
        region=region,
        settings=ClientSettings(
            retries=settings.retries, proxy=settings.proxy, integration=integration
        ),
    )
    return resolved_transport, plane


class ConnectionConfig:
    """`e2b.ConnectionConfig`: la configuración efectiva de un sandbox o una
    construida a mano con las mismas reglas que `create`.

    `set_integration` fija un nombre de integración de proceso que los
    planos construidos después añaden al User-Agent de botocore; no
    reconstruye los que ya existen (llámalo una vez al arrancar).
    """

    _integration: ClassVar[str | None] = None

    def __init__(
        self,
        *,
        request_timeout: float | None = None,
        retries: int | None = None,
        headers: Mapping[str, str] | None = None,
        proxy: str | None = None,
        logger: logging.Logger | None = None,
        region: str | None = None,
        **ignored: Any,
    ) -> None:
        reject_unknown_params(ignored, call="ConnectionConfig")
        for message in ignored_param_warnings(ignored):
            warnings.warn(message, RayitoCompatWarning, stacklevel=2)
        self._request_timeout = validate_request_timeout(request_timeout)
        self._retries = resolve_retries(retries)
        self._headers = MappingProxyType(dict(validate_extra_headers(headers)))
        self._proxy = validate_proxy_url(proxy)
        self._logger = logger
        self._region = region
        self._integration_snapshot = type(self)._integration

    @classmethod
    def set_integration(cls, integration: str | None) -> None:
        ConnectionConfig._integration = validate_integration(integration)

    @classmethod
    def current_integration(cls) -> str | None:
        return ConnectionConfig._integration

    @property
    def request_timeout(self) -> float:
        if self._request_timeout is None:
            return DEFAULT_CONFIG_REQUEST_TIMEOUT_SECONDS
        return float(self._request_timeout)

    @property
    def retries(self) -> int | None:
        return self._retries

    @property
    def headers(self) -> Mapping[str, str]:
        return self._headers

    @property
    def proxy(self) -> str | None:
        return self._proxy

    @property
    def logger(self) -> logging.Logger | None:
        return self._logger

    @property
    def region(self) -> str | None:
        return self._region

    @property
    def integration(self) -> str | None:
        return self._integration_snapshot

    def get_request_timeout(self, request_timeout: float | None = None) -> float:
        if request_timeout is not None:
            return float(request_timeout)
        return self.request_timeout

    def __repr__(self) -> str:
        return (
            f"ConnectionConfig(region={self._region!r}, request_timeout={self.request_timeout!r}, "
            f"retries={self._retries!r}, headers={sorted(self._headers)!r}, "
            f"integration={self._integration_snapshot!r})"
        )


def snapshot_config(
    settings: ConnectionSettings,
    *,
    region: str | None,
    logger: logging.Logger | None,
    integration: str | None,
) -> ConnectionConfig:
    """El `connection_config` de un sandbox: lo dado en create/connect más
    la región nativa y la integración vigente al crearlo."""
    config = ConnectionConfig(
        request_timeout=settings.request_timeout,
        retries=settings.retries,
        headers=dict(settings.headers),
        proxy=settings.proxy,
        logger=logger,
        region=region,
    )
    config._integration_snapshot = integration
    return config
