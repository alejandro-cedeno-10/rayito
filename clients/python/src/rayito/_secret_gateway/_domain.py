"""Dominio puro de `gateways=` (ADR-023, m15-secrets-gateway): qué es un
`SecretGateway` válido y cómo se ve su estado una vez `rayd` lo aplica.
Nada de AWS, grpc ni reloj: `_section.py` (adaptador) resuelve cada
cabecera contra `SecretCache` y llama a `ConfigureSandbox`; este módulo
sólo valida la forma de lo que el llamante escribió, antes de tocar nada.

Los límites (`MAX_ROUTES_PER_GATEWAY`, ...) mirroran uno a uno los de
`rayd_core::secret_gateway::route` (mismo valor, mismo porqué); viven aquí
en vez de `limits.json` porque, como los de `_secrets.py`
(`DEFAULT_TTL_SECONDS`/`MAX_TTL_SECONDS`), son internos a esta única
función y no cruzan a TypeScript como un parámetro negociado con el guest.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from rayito.exceptions import InvalidArgumentException

if TYPE_CHECKING:
    from rayito._secrets import SecretRef

# -- rayd_core::secret_gateway::route --------------------------------------
MAX_ROUTE_NAME_LEN: Final = 64
MAX_ROUTES_PER_GATEWAY: Final = 8
MAX_HEADERS_PER_ROUTE: Final = 16
MAX_ALLOW_RULES_PER_ROUTE: Final = 32
# 10 peticiones/segundo: un tope conservador para un agente que llama a una
# única API externa de forma interactiva (ver "Coste y activación" de
# `SecretGateway`, más abajo).
DEFAULT_RATE_PER_MINUTE: Final = 600
MIN_RATE_PER_MINUTE: Final = 1
MAX_RATE_PER_MINUTE: Final = 6_000
WILDCARD_SUFFIX: Final = "/*"
LOOPBACK_HOST: Final = "127.0.0.1"

_ROUTE_NAME_PATTERN: Final = re.compile(rf"[a-z0-9-]{{1,{MAX_ROUTE_NAME_LEN}}}")
# E2B-style HTTP methods: ninguna pasarela admite un comodín de método, cada
# una lista los que de verdad necesita.
_VALID_METHODS: Final = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
# RFC 9110 §5.6.2 `token`: los mismos bytes que acepta
# `rayd_core::secret_gateway::route::is_header_token_byte`.
_HEADER_TOKEN_PATTERN: Final = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
# Bytes que una ruta de `allow` puede llevar en crudo: `unreserved`, `:`,
# `@` y los `sub-delims` de RFC 3986 salvo `;` (Tomcat, Jetty, Spring y
# varios proxies quitan un parámetro `;...` antes de enrutar), más `/` y el
# `%` de un byte codificado. Mismo conjunto que
# `rayd_core::secret_gateway::route::is_allowed_raw_path_byte`.
_ALLOWED_RAW_PATH_CHARS: Final = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~:@!$&'()*+,=/%"
)
# Bytes que un segmento no puede llevar una vez decodificado: `/` y `\`
# cambian cómo parte la ruta un upstream que decodifica, `%` es una segunda
# capa de codificación y `;` vuelve a ser un parámetro de ruta
# (`rayd_core::secret_gateway::route::FORBIDDEN_DECODED_BYTES`).
_FORBIDDEN_DECODED_BYTES: Final = frozenset(b"/\\%;")
_FIRST_PRINTABLE_ASCII: Final = 0x20
_ASCII_DELETE: Final = 0x7F
_HEX_DIGITS: Final = frozenset("0123456789abcdefABCDEF")
# Cabeceras hop-by-hop (RFC 9110 §7.6.1) y de framing/host: una ruta nunca
# puede inyectarlas, o podría desincronizar la petición reenviada o apuntarla
# a un host distinto del de `upstream` (mismo conjunto que
# `rayd_core::secret_gateway::route::FORBIDDEN_INJECTED_HEADERS`).
_FORBIDDEN_INJECTED_HEADERS: Final = frozenset(
    {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "trailer",
        "upgrade",
    }
)


@dataclass(frozen=True)
class SecretGateway:
    """Una entrada de `gateways={"nombre": SecretGateway(...)}` en
    `Sandbox.create()`: un único `upstream` fijo al que `rayd` reenvía,
    inyectando en cada petición permitida el valor de cada cabecera de
    `headers` (resuelto de Secrets Manager por su nombre, nunca aquí) y
    rechazando lo que `allow` no cubre o exceda `rate_per_minute`.

    Coste y activación
    -------------------
    Activa: `gateways={"nombre": SecretGateway(...)}` en `Sandbox.create()`
        (o `SandboxPool.take(gateways=)`); sin él, `rayd` no abre ningún listener de
        loopback y el SDK no hace ninguna llamada a `ConfigureSandbox` ni a
        Secrets Manager.
    Recursos y llamadas AWS: `secretsmanager:GetSecretValue` una vez por
        cabecera y TTL de la `SecretCache` que ya usa `secrets=` (un acierto
        no llama a AWS), y una por cabecera en cada `sbx.gateways.refresh()`;
        ningún recurso nuevo (reutiliza `infra/secrets-access.yaml`).
    Coste aproximado: el de `SecretCache` (`$0,05` por 10 000 llamadas,
        us-east-1, 2026-09-30) más el del secreto en sí si no existía ya.
    IAM: `secretsmanager:GetSecretValue` en las credenciales del llamante,
        igual que `secrets=` (no el execution role: la pasarela corre en
        `rayd`, no necesita rol de ejecución).
    Cómo apagarla: no pases `gateways=` (por defecto `None`).
    Ejemplo:
        sbx = Sandbox.create(gateways={
            "anthropic": SecretGateway(
                upstream="https://api.anthropic.com",
                headers={"x-api-key": "anthropic"},
                allow=[("POST", "/v1/messages")],
                rate_per_minute=600,
            )
        })
        url = sbx.gateways["anthropic"].url
        sbx.commands.run("python agent.py", envs={"ANTHROPIC_BASE_URL": url})
    """

    upstream: str
    headers: Mapping[str, str | SecretRef]
    allow: Sequence[tuple[str, str]]
    rate_per_minute: int = 0

    def __post_init__(self) -> None:
        validate_gateway(self)


def validate_gateway(gateway: SecretGateway) -> None:
    """Sólo la forma: ningún valor de `headers` se resuelve ni se toca AWS
    aquí (`_section.GatewaySection.fill` lo hace, justo antes de cada
    `Configure`)."""
    if not isinstance(gateway.upstream, str) or not gateway.upstream.startswith("https://"):
        raise InvalidArgumentException(
            "SecretGateway.upstream debe ser 'https://host', sin ruta ni query"
        )
    rest = gateway.upstream.removeprefix("https://")
    if not rest or "/" in rest or "?" in rest or "#" in rest or "@" in rest:
        raise InvalidArgumentException(
            "SecretGateway.upstream no admite ruta, query, userinfo ni fragmento: "
            "sólo esquema y host"
        )
    if not isinstance(gateway.headers, Mapping) or not (
        1 <= len(gateway.headers) <= MAX_HEADERS_PER_ROUTE
    ):
        raise InvalidArgumentException(
            f"SecretGateway.headers debe tener entre 1 y {MAX_HEADERS_PER_ROUTE} entradas"
        )
    _validate_header_names(gateway.headers)
    if not isinstance(gateway.allow, Sequence):
        raise InvalidArgumentException("SecretGateway.allow debe ser una lista de (método, ruta)")
    if not gateway.allow:
        raise InvalidArgumentException(
            "SecretGateway.allow no puede estar vacío: ninguna petición se reenviaría nunca"
        )
    if len(gateway.allow) > MAX_ALLOW_RULES_PER_ROUTE:
        raise InvalidArgumentException(
            f"SecretGateway.allow admite como mucho {MAX_ALLOW_RULES_PER_ROUTE} reglas"
        )
    for rule in gateway.allow:
        _validate_allow_rule(rule)
    if gateway.rate_per_minute != 0 and not (
        MIN_RATE_PER_MINUTE <= gateway.rate_per_minute <= MAX_RATE_PER_MINUTE
    ):
        raise InvalidArgumentException(
            "SecretGateway.rate_per_minute debe ser 0 (usa el valor por defecto) o estar en "
            f"{MIN_RATE_PER_MINUTE}..{MAX_RATE_PER_MINUTE}"
        )


def _validate_header_names(headers: Mapping[str, str | SecretRef]) -> None:
    """Un nombre inválido fallaría en cada petición con un 502 en vez de aquí
    (`rayd` construye `http::HeaderName` justo antes de reenviar); uno
    prohibido (`host`, hop-by-hop, ...) dejaría que una ruta desincronice la
    petición o la apunte a otro host. Lo mismo lower-case que ya usa
    `stripped_header_names` en el lado de `rayd`: dos nombres que sólo
    difieren en mayúsculas ('X-Api-Key' y 'x-api-key') se cuentan como el
    mismo, así que nunca inyectan el valor dos veces."""
    seen: set[str] = set()
    for name in headers:
        lower = name.lower() if isinstance(name, str) else ""
        if not lower or not _HEADER_TOKEN_PATTERN.fullmatch(lower):
            raise InvalidArgumentException(f"nombre de cabecera inválido en headers: {name!r}")
        if lower in _FORBIDDEN_INJECTED_HEADERS:
            raise InvalidArgumentException(
                f"headers no puede inyectar la cabecera reservada {lower!r}"
            )
        if lower in seen:
            raise InvalidArgumentException(
                f"headers tiene dos nombres que sólo difieren en mayúsculas: {lower!r}"
            )
        seen.add(lower)


def _validate_allow_rule(rule: tuple[str, str]) -> None:
    if not isinstance(rule, tuple) or len(rule) != 2:
        raise InvalidArgumentException("cada regla de allow debe ser (método, ruta)")
    method, path = rule
    if not isinstance(method, str) or method not in _VALID_METHODS:
        raise InvalidArgumentException(
            f"método no soportado en allow: {method!r} (uno de {sorted(_VALID_METHODS)})"
        )
    if not isinstance(path, str) or not path.startswith("/"):
        raise InvalidArgumentException("una ruta de allow debe ser absoluta (empezar por '/')")
    if not is_safe_request_path(path):
        raise InvalidArgumentException(
            "una ruta de allow sólo admite caracteres RFC 3986 sin ';', ni segmentos "
            "'.'/'..' o vacíos, ni '/', '\\', '%' o ';' codificados: rayd rechazaría "
            "cada petición que la cumpliera"
        )


def is_safe_request_path(path: str) -> bool:
    """La misma regla que `rayd_core::secret_gateway::route::path_is_safe`
    (lista de permitidos, `testdata/secret-gateway/request-paths.json`):
    absoluta, cada byte crudo en `_ALLOWED_RAW_PATH_CHARS`, ningún segmento
    vacío salvo el último y cada segmento, decodificado una sola vez, UTF-8
    válido, sin bytes de control ni `_FORBIDDEN_DECODED_BYTES` y distinto de
    `.` y `..`. `rayd` reenvía la ruta tal cual llega, así que una regla que
    no la cumpla nunca casaría con una petición."""
    if not path.startswith("/") or not all(char in _ALLOWED_RAW_PATH_CHARS for char in path):
        return False
    segments = path[1:].split("/")
    last = len(segments) - 1
    for index, segment in enumerate(segments):
        if segment == "":
            if index != last:
                return False
            continue
        decoded = _decode_segment_once(segment)
        if decoded is None or decoded in (".", ".."):
            return False
    return True


def _decode_segment_once(segment: str) -> str | None:
    decoded = bytearray()
    index = 0
    while index < len(segment):
        char = segment[index]
        if char != "%":
            decoded.extend(char.encode("ascii"))
            index += 1
            continue
        escape = segment[index + 1 : index + 3]
        if len(escape) != 2 or not all(digit in _HEX_DIGITS for digit in escape):
            return None
        value = int(escape, 16)
        if (
            value < _FIRST_PRINTABLE_ASCII
            or value == _ASCII_DELETE
            or value in _FORBIDDEN_DECODED_BYTES
        ):
            return None
        decoded.append(value)
        index += 3
    try:
        return decoded.decode("utf-8")
    except UnicodeDecodeError:
        return None


def validate_route_name(name: str) -> str:
    """1-64 `[a-z0-9-]`, como `rayd_core::secret_gateway::route`; es la
    clave pública de `sbx.gateways[name]`, así que nunca es secreta."""
    if not isinstance(name, str) or not _ROUTE_NAME_PATTERN.fullmatch(name):
        raise InvalidArgumentException(
            f"nombre de gateway inválido: 1-{MAX_ROUTE_NAME_LEN} caracteres [a-z0-9-]"
        )
    return name


def validate_gateways(gateways: Mapping[str, SecretGateway]) -> Mapping[str, SecretGateway]:
    """La forma completa de `gateways=`: nombres válidos, sin más de
    `MAX_ROUTES_PER_GATEWAY` y cada valor un `SecretGateway` (que ya se
    validó solo, en `__post_init__`)."""
    if not isinstance(gateways, Mapping) or not gateways:
        raise InvalidArgumentException("gateways debe ser un dict no vacío {nombre: SecretGateway}")
    if len(gateways) > MAX_ROUTES_PER_GATEWAY:
        raise InvalidArgumentException(f"como mucho {MAX_ROUTES_PER_GATEWAY} gateways por sandbox")
    for name, gateway in gateways.items():
        validate_route_name(name)
        if not isinstance(gateway, SecretGateway):
            raise InvalidArgumentException("cada valor de gateways debe ser un SecretGateway")
    return gateways


@dataclass(frozen=True)
class GatewayStatus:
    """Espejo de `SecretGatewayRouteStatus`: el estado de una ruta tras el
    `Configure` (o `ConfigureStatus`) más reciente."""

    port: int
    last_error_class: str | None = None

    @property
    def url(self) -> str:
        """`"http://127.0.0.1:<port>"`: no es secreto (nada más que uid 1000
        puede alcanzar el puerto dentro del sandbox)."""
        return f"http://{LOOPBACK_HOST}:{self.port}"
