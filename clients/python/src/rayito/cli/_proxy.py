"""`rayito sandbox proxy <id> --port N`: un proxy TCP local, de un solo
puerto, hacia el endpoint de un sandbox. Sin coste de AWS más allá de
`GetMicrovm` y `CreateMicrovmAuthToken` (gratuitos): no hay ningún recurso
nuevo, no hay servidor propio de Rayito y el JWE se acuña con el mismo
`TokenRefresher`/`TokenStore` de `rayito._transport` (renovado a los 45 min,
nunca por conexión).

Contrato (research custom-domain opción D, AWS_API_NOTES.md §7):

- Siempre `PortSpec.single(N)` (nunca `allPorts`); rechaza `N=9000` (puerto
  de los lifecycle hooks, ADR-006) y cualquier valor fuera de 1..65535.
- El listener habla HTTP/1.1: por conexión lee sólo la cabecera de la
  petición (tope 64 KiB, el límite por defecto de `asyncio.StreamReader`),
  la valida como HTTP/1.1 estricto (sin CR/LF sueltos, sin `obs-fold`,
  nombres de cabecera sólo con los `tchar` de RFC 9110; si no lo es,
  responde `400` y cierra sin reenviar nada — ver "Contrabando de
  cabeceras" más abajo), quita cualquier `x-aws-proxy-*` que traiga el
  cliente, fija `Host: <endpoint>`, añade `X-aws-proxy-auth: <JWE vigente>`
  y `X-aws-proxy-port: N`, fuerza `Connection: close` salvo en una petición
  de upgrade (`Upgrade` + el token `upgrade` en `Connection`, RFC 9110
  §7.8; ahí se deja intacta) y a partir de ahí hace de tubería en los dos
  sentidos hasta que un lado cierra. El paso de WebSocket está
  implementado, no medido contra AWS (sólo hay un test contra un upstream
  falso en loopback).
- Tiempos acotados: la cabecera del cliente tiene que llegar en
  `HEAD_READ_TIMEOUT_SECONDS` y la conexión TLS al endpoint abrirse en
  `UPSTREAM_CONNECT_TIMEOUT_SECONDS`. Sin JWE vigente (el refresher lleva
  fallando más allá del TTL) o si la conexión al upstream falla o vence,
  responde `502` + `Connection: close` y escribe en stderr una línea sin
  JWE, cabeceras ni ruta.
- **Sólo la primera petición de cada conexión se reescribe.** Como todas
  las peticiones que no son upgrade llegan al upstream con
  `Connection: close` (el `Connection` del cliente se descarta), el
  servidor del guest cierra tras la primera respuesta. Si un cliente manda
  igualmente más peticiones por la misma conexión (pipelining), esos bytes
  se reenvían tal cual por la tubería, sin volver a pasar por
  `parse_http_head`/`rewrite_head`. Si el proxy de AWS valida
  `X-aws-proxy-auth` por petición o por conexión en un keep-alive HTTP/1.1
  NO está medido (AWS_API_NOTES.md §16, Q-M12-1): no se cuenta con que AWS
  las rechace.
- **Contrabando de cabeceras (`request smuggling`)**: `parse_http_head`
  rechaza (`MalformedHttpHeadError`, el llamante responde `400` y cierra)
  cualquier cabecera con un CR o LF suelto fuera de un `\\r\\n`, una
  continuación `obs-fold` (RFC 7230 la quitó), un nombre con espacios o
  fuera de los `tchar` de RFC 9110 §5.6.2, o una línea sin `:`. Antes de
  esto, una cabecera con un `\\n` suelto en su valor podía colar una línea
  `x-aws-proxy-port` falsa delante de la real (el nombre exterior no
  empezaba por `x-aws-proxy-`, así que el filtro por nombre no la veía).
- TLS al endpoint es `ssl.create_default_context()` con SNI = `endpoint`
  (`default_connector`); los tests inyectan otra fábrica de conectores
  contra un upstream en loopback sin TLS.
- Nunca registra el JWE, las cabeceras, los cuerpos ni las rutas de las
  peticiones que pasan por el proxy: ni aquí ni en `rayito.transport` hay una
  sola llamada a `logging` con esos valores.
- Un sandbox `SUSPENDED` con `auto_resume` se despierta con la primera
  petición que llega al proxy (factura cómputo, como cualquier reanudación).
- No hace falta el access token del sandbox: sólo el JWE del proxy.
- **`Host` y `Origin` (anti DNS rebinding y anti CSRF).** El proxy añade el
  JWE del operador a todo lo que reenvía y el guest sólo ve `Host:
  <endpoint>`, así que no puede distinguir una petición del operador de
  una que haga una web cualquiera abierta en su navegador. Por eso, antes
  de reenviar nada, `handle_connection` exige un `Host` de la lista de
  `ProxyAccess` (`127.0.0.1`, `localhost` y `[::1]` con el puerto local, la
  dirección de `--bind` si no es comodín, `<id>.localhost` con un `--bind`
  de loopback y cada `--allowed-host`); si falta o no está, responde `421`
  y cierra. Un `Origin` presente tiene que ser `http://` más una de esas
  autoridades, o un `--allow-origin`; si no (incluido `Origin: null`),
  `403`. Así se cortan el DNS rebinding, el POST entre sitios y el
  WebSocket entre orígenes. `--bind 0.0.0.0`/`::` necesita `--allowed-host`
  porque ningún cliente escribe `Host: 0.0.0.0`.
- **Tope de conexiones.** Como mucho `--max-connections` (por defecto
  `DEFAULT_MAX_CONNECTIONS`, el cap no ajustable de 8 conexiones de una
  MicroVM de 1 vCPU, AWS_API_NOTES.md §7) conexiones reenviadas a la vez;
  la siguiente recibe `503` sin abrir el upstream, para que una web o un
  cliente remoto no agote las conexiones del proxy de AWS que también usa
  el SDK.
- **Cookies.** Las cookies no se aíslan por puerto (RFC 6265) y `same-site`
  ignora el puerto: lo que el guest sirva en `http://127.0.0.1:<puerto>`
  comparte tarro y "sitio" con cualquier otra app local, y el proxy le
  reenvía tal cual las cookies que mande el navegador. Por eso el anuncio
  también ofrece `http://<id>.localhost:<puerto>`, con tarro propio.
- `--port` y `--local-port` se validan (1..65535, `--port` nunca 9000)
  **antes** de tocar AWS; el socket local se reserva (`bind`) también antes
  de `GetMicrovm`/`CreateMicrovmAuthToken`, así que un puerto ocupado o
  inválido falla limpio sin gastar ninguna llamada.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import socket
import ssl
import sys
import threading
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass

from rayito._authority import http_authority, is_ipv6_literal, is_wildcard
from rayito._aws import ControlPlane, PortSpec
from rayito._limits import (
    ENDPOINT_TLS_PORT,
    HOOKS_PORT,
    MAX_CONCURRENT_CONNECTIONS_1_VCPU,
    PORT_MAX,
    PORT_MIN,
    TERMINAL_STATES,
)
from rayito._transport import TokenRefresher, TokenStore
from rayito.exceptions import InvalidArgumentException, SandboxStateException

HEADER_TERMINATOR: bytes = b"\r\n\r\n"
BAD_REQUEST_RESPONSE: bytes = (
    b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
)
BAD_GATEWAY_RESPONSE: bytes = (
    b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
)
#: `Host` ausente o fuera de `ProxyAccess` (RFC 9110 §15.5.20).
MISDIRECTED_REQUEST_RESPONSE: bytes = (
    b"HTTP/1.1 421 Misdirected Request\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
)
#: `Origin` presente y fuera de `ProxyAccess`.
FORBIDDEN_RESPONSE: bytes = (
    b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
)
#: Todas las conexiones de `--max-connections` ocupadas.
SERVICE_UNAVAILABLE_RESPONSE: bytes = (
    b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
)
# Un cliente que abre la conexión y no termina la cabecera, o un handshake
# TLS que se queda colgado, no retienen una tarea para siempre.
HEAD_READ_TIMEOUT_SECONDS = 30.0
UPSTREAM_CONNECT_TIMEOUT_SECONDS = 30.0
# Cada cuánto `_run_until_stopped` mira el `stop_event` del e2e.
STOP_POLL_SECONDS = 0.1
# RFC 9110 §5.6.2 `tchar`: los nombres de cabecera del cliente que no
# cumplan esto se rechazan en vez de reenviarse tal cual.
_TOKEN_CHARS = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
PROXY_HEADER_PREFIX = "x-aws-proxy-"
AUTH_HEADER_NAME = "X-aws-proxy-auth"
PORT_HEADER_NAME = "X-aws-proxy-port"
HOST_HEADER_NAME = "Host"
CONNECTION_HEADER_NAME = "Connection"
UPGRADE_HEADER_NAME = "Upgrade"
ORIGIN_HEADER_NAME = "Origin"
LOOPBACK_ADDRESSES = frozenset({"127.0.0.1", "::1", "localhost"})
#: Los nombres de loopback que el proxy acepta siempre en `Host`, con el
#: puerto local.
LOOPBACK_AUTHORITY_HOSTS = ("127.0.0.1", "localhost", "::1")
DEFAULT_BIND = "127.0.0.1"
PIPE_CHUNK_BYTES = 64 * 1024
#: Un `Host` sin puerto sólo equivale a `<nombre>:<puerto>` cuando el puerto
#: es el de HTTP (RFC 9110 §4.2.1).
HTTP_DEFAULT_PORT = 80
HTTP_SCHEME = "http"
ORIGIN_SCHEMES = ("http://", "https://")
#: Navegadores resuelven `*.localhost` a loopback (RFC 6761 §6.3) y le dan un
#: tarro de cookies propio, distinto del de `127.0.0.1`/`localhost`.
LOCALHOST_SUFFIX = ".localhost"
#: Una etiqueta DNS (RFC 1035 §2.3.4): `<id>.localhost` sólo se ofrece si el
#: id del sandbox cabe en una.
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
#: Lo que nunca aparece en un `host[:puerto]`: esquema, ruta, userinfo,
#: espacios.
_NOT_IN_AUTHORITY = re.compile(r"[/@\s?#]")
#: El cap no ajustable de conexiones concurrentes de una MicroVM de 1 vCPU
#: (AWS_API_NOTES.md §7, tabla de límites): el mínimo que cualquier sandbox
#: aguanta.
DEFAULT_MAX_CONNECTIONS = MAX_CONCURRENT_CONNECTIONS_1_VCPU
ALLOW_REMOTE_OPTION = "--allow-remote"
ALLOWED_HOST_OPTION = "--allowed-host"
ALLOW_ORIGIN_OPTION = "--allow-origin"
MAX_CONNECTIONS_OPTION = "--max-connections"

BIND_NEEDS_ALLOW_REMOTE_MESSAGE = (
    f"--bind fuera de loopback necesita {ALLOW_REMOTE_OPTION}: "
    "cualquiera que llegue a ese puerto usa el sandbox mientras el proxy esté vivo"
)
WILDCARD_NEEDS_ALLOWED_HOST_MESSAGE = (
    f"--bind 0.0.0.0/:: necesita {ALLOWED_HOST_OPTION} <nombre>: ningún cliente escribe "
    "Host: 0.0.0.0, así que el proxy no sabría qué Host aceptar"
)

Connector = Callable[[], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]
ConnectorFactory = Callable[[str], Connector]
JweProvider = Callable[[], str | None]


def validate_port_range(port: int, *, option: str) -> int:
    """1..65535, para `--port` y `--local-port`. Se llama antes de tocar AWS."""
    if not PORT_MIN <= port <= PORT_MAX:
        raise InvalidArgumentException(f"{option} fuera de rango ({PORT_MIN}-{PORT_MAX}): {port}")
    return port


def validate_proxy_port(port: int) -> int:
    """`N` de `--port`: nunca el de los hooks, siempre en rango. Se llama
    antes de tocar AWS (ni `GetMicrovm` ni `CreateMicrovmAuthToken`)."""
    if port == HOOKS_PORT:
        raise InvalidArgumentException(
            f"el puerto {HOOKS_PORT} es el de los lifecycle hooks (ADR-006): "
            "no se puede exponer con el proxy"
        )
    return validate_port_range(port, option="--port")


def validate_local_port(port: int) -> int:
    """`N` de `--local-port`: sólo el rango; no hereda la prohibición del
    9000 (es un puerto local del operador, no del guest). Se llama antes de
    tocar AWS y antes de reservar el socket."""
    return validate_port_range(port, option="--local-port")


def is_loopback_bind(bind: str) -> bool:
    return bind in LOOPBACK_ADDRESSES


def validate_max_connections(value: int) -> int:
    """`--max-connections`: al menos 1. Se llama antes de tocar AWS."""
    if value < 1:
        raise InvalidArgumentException(
            f"{MAX_CONNECTIONS_OPTION} tiene que ser al menos 1: {value}"
        )
    return value


@dataclass(frozen=True)
class ProxyAccess:
    """Los `Host` y `Origin` que acepta el listener, ya normalizados (en
    minúsculas, sin `/` final). Ver "`Host` y `Origin`" en el docstring del
    módulo."""

    hosts: frozenset[str]
    origins: frozenset[str]
    sandbox_host: str | None = None

    def admits_host(self, host: str | None) -> bool:
        return host is not None and host.strip().lower() in self.hosts

    def admits_origin(self, origin: str | None) -> bool:
        """Sin `Origin` (curl, un navegador en una petición del mismo
        origen que no lo manda) pasa; `null` u otro origen, no."""
        return origin is None or origin.strip().lower().rstrip("/") in self.origins


def _bracketed(host: str) -> str:
    return f"[{host}]" if is_ipv6_literal(host) else host


def _name_authorities(name: str, local_port: int) -> set[str]:
    """`nombre:puerto` y, sólo si el puerto es el 80, también `nombre` a
    secas (un navegador omite el puerto por defecto)."""
    authorities = {http_authority(name, local_port).lower()}
    if local_port == HTTP_DEFAULT_PORT:
        authorities.add(_bracketed(name).lower())
    return authorities


def _has_explicit_port(value: str) -> bool:
    if value.startswith("["):
        return "]:" in value
    return not is_ipv6_literal(value) and ":" in value


def _allowed_host_authorities(value: str, local_port: int) -> set[str]:
    """Un `--allowed-host`: con puerto se compara tal cual; sin puerto
    admite el nombre a secas (un proxy inverso delante, en el 80/443) y con
    el puerto local."""
    host = value.strip().lower()
    if not host or _NOT_IN_AUTHORITY.search(host):
        raise InvalidArgumentException(
            f"{ALLOWED_HOST_OPTION} espera host o host:puerto, sin esquema ni ruta"
        )
    if not _has_explicit_port(host):
        return {_bracketed(host), http_authority(host, local_port).lower()}
    _, _, port = host.rpartition(":")
    if not port.isdigit():
        raise InvalidArgumentException(f"{ALLOWED_HOST_OPTION}: puerto inválido")
    return {host}


def _allowed_origin(value: str) -> str:
    origin = value.strip().lower().rstrip("/")
    scheme = next((prefix for prefix in ORIGIN_SCHEMES if origin.startswith(prefix)), None)
    authority = "" if scheme is None else origin[len(scheme) :]
    if not authority or _NOT_IN_AUTHORITY.search(authority):
        raise InvalidArgumentException(
            f"{ALLOW_ORIGIN_OPTION} espera un origen http(s)://host[:puerto], sin ruta"
        )
    return origin


def sandbox_localhost_name(sandbox_id: str) -> str | None:
    """`<id>.localhost`, o `None` si el id no cabe en una etiqueta DNS."""
    label = sandbox_id.lower()
    return f"{label}{LOCALHOST_SUFFIX}" if _DNS_LABEL.match(label) else None


def build_access(
    *,
    bind: str,
    local_port: int,
    sandbox_id: str,
    allowed_hosts: Iterable[str] = (),
    allowed_origins: Iterable[str] = (),
) -> ProxyAccess:
    """La lista de `Host`/`Origin` del listener: los nombres de loopback y
    `--bind` (si no es comodín) con el puerto local, `<id>.localhost` con un
    `--bind` de loopback, cada `--allowed-host` y cada `--allow-origin`.
    Un valor mal formado es `InvalidArgumentException`, antes de tocar
    AWS."""
    names = list(LOOPBACK_AUTHORITY_HOSTS)
    if not is_wildcard(bind):
        names.append(bind)
    sandbox_host = sandbox_localhost_name(sandbox_id) if is_loopback_bind(bind) else None
    if sandbox_host is not None:
        names.append(sandbox_host)
    hosts: set[str] = set()
    for name in names:
        hosts |= _name_authorities(name, local_port)
    for value in allowed_hosts:
        hosts |= _allowed_host_authorities(value, local_port)
    origins = {f"{HTTP_SCHEME}://{host}" for host in hosts}
    origins |= {_allowed_origin(value) for value in allowed_origins}
    return ProxyAccess(
        hosts=frozenset(hosts), origins=frozenset(origins), sandbox_host=sandbox_host
    )


def local_url(bind: str, port: int) -> str:
    host = f"[{bind}]" if ":" in bind else bind
    return f"http://{host}:{port}"


def announcement(spec: ProxySpec) -> str:
    """Lo que la CLI imprime al arrancar: la URL local y, con un `--bind`
    de loopback, la de `<id>.localhost`, que no comparte cookies con otras
    apps locales."""
    line = f"{local_url(spec.bind, spec.local_port)} → {spec.sandbox_id}:{spec.port}"
    if spec.access.sandbox_host is None:
        return line
    isolated = f"{HTTP_SCHEME}://{spec.access.sandbox_host}:{spec.local_port}"
    return f"{line}\n  con cookies aisladas de otras apps locales: {isolated}"


def bind_listener_socket(bind: str, local_port: int) -> socket.socket:
    """Reserva el socket local ANTES de tocar AWS (`GetMicrovm`,
    `CreateMicrovmAuthToken`): un `--local-port` ocupado o un `--bind`
    inválido falla aquí, limpio (`InvalidArgumentException` con el puerto,
    que la CLI muestra sin traceback), sin haber gastado ninguna llamada.
    `asyncio.start_server(sock=...)` hace el resto (no bloqueante)."""
    family = socket.AF_INET6 if ":" in bind else socket.AF_INET
    try:
        sock = socket.socket(family, socket.SOCK_STREAM)
    except OSError as exc:
        raise InvalidArgumentException(f"--bind {bind}: {exc.strerror or exc}") from exc
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((bind, local_port))
        # `listen()` aquí y no en `start_server`: con `SO_REUSEADDR` dos
        # `bind()` al mismo puerto conviven en Linux hasta el primer
        # `listen()`, así que el conflicto con otro proceso que ya escucha
        # sale ahora, antes de tocar AWS.
        sock.listen()
    except OSError as exc:
        sock.close()
        raise InvalidArgumentException(
            f"no se puede escuchar en {bind} con --local-port {local_port}: {exc.strerror or exc}"
        ) from exc
    return sock


@dataclass(frozen=True)
class HttpHead:
    """Línea de petición + cabeceras de un HTTP/1.1 ya separadas."""

    request_line: str
    headers: tuple[tuple[str, str], ...]

    def header(self, name: str) -> str | None:
        target = name.lower()
        for key, value in self.headers:
            if key.lower() == target:
                return value
        return None

    def is_upgrade(self) -> bool:
        """RFC 9110 §7.8: hace falta `Upgrade` Y el token `upgrade` en
        `Connection`; un `Upgrade` suelto (p. ej. con `Connection:
        keep-alive`) no es un upgrade y se trata como cualquier petición."""
        tokens = {
            token.strip().lower()
            for token in (self.header(CONNECTION_HEADER_NAME) or "").split(",")
        }
        return "upgrade" in tokens and self.header(UPGRADE_HEADER_NAME) is not None


class MalformedHttpHeadError(ValueError):
    """La cabecera de la petición del cliente no es HTTP/1.1 estricto:
    quien llama responde 400 y cierra en vez de reenviar algo ambiguo. Nunca
    lleva el contenido de la petición en el mensaje (podría acabar en un
    log)."""


def parse_http_head(raw: bytes) -> HttpHead:
    """`raw` incluye el `\\r\\n\\r\\n` final. HTTP/1.1 estricto: cada línea
    debe terminar en `\\r\\n` (nada de CR o LF sueltos, que es como un
    cliente podría colar una cabecera falsa dentro del valor de otra —
    `request smuggling`), sin continuación `obs-fold`, con nombre de
    cabecera hecho sólo de `tchar` (RFC 9110 §5.6.2, sin espacios) y con
    `:`; cualquier otra cosa es `MalformedHttpHeadError`."""
    if not raw.endswith(HEADER_TERMINATOR):
        raise MalformedHttpHeadError("cabecera sin terminador CRLF CRLF")
    text = raw[: -len(HEADER_TERMINATOR)].decode("latin-1")
    lines = text.split("\r\n")
    for line in lines:
        if "\r" in line or "\n" in line:
            raise MalformedHttpHeadError("CR o LF suelto fuera de un CRLF")
    if not lines[0]:
        raise MalformedHttpHeadError("línea de petición vacía")
    request_line = lines[0]
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if not line:
            continue
        if line[0] in (" ", "\t"):
            raise MalformedHttpHeadError("continuación obs-fold no soportada")
        name, sep, value = line.partition(":")
        if not sep:
            raise MalformedHttpHeadError("línea de cabecera sin ':'")
        if not _TOKEN_CHARS.match(name):
            raise MalformedHttpHeadError("nombre de cabecera fuera de los tchar de RFC 9110")
        headers.append((name, value.strip()))
    return HttpHead(request_line=request_line, headers=tuple(headers))


def rewrite_head(head: HttpHead, *, endpoint: str, jwe: str, port: int) -> bytes:
    """Cabecera reescrita hacia `<endpoint>:443` (AWS_API_NOTES.md §7): quita
    cualquier `x-aws-proxy-*` y `Host` que traiga el cliente, añade `Host`,
    `X-aws-proxy-auth` y `X-aws-proxy-port`, y fuerza `Connection: close`
    salvo en una petición `Upgrade`, donde `Connection`/`Upgrade` se dejan
    intactas para que el handshake de WebSocket llegue igual."""
    upgrade = head.is_upgrade()
    kept = [
        (name, value)
        for name, value in head.headers
        if not name.lower().startswith(PROXY_HEADER_PREFIX)
        and name.lower() != HOST_HEADER_NAME.lower()
        and (upgrade or name.lower() != CONNECTION_HEADER_NAME.lower())
    ]
    kept.append((HOST_HEADER_NAME, endpoint))
    kept.append((AUTH_HEADER_NAME, jwe))
    kept.append((PORT_HEADER_NAME, str(port)))
    if not upgrade:
        kept.append((CONNECTION_HEADER_NAME, "close"))
    lines = [head.request_line, *(f"{name}: {value}" for name, value in kept), "", ""]
    return "\r\n".join(lines).encode("latin-1")


def default_connector(endpoint: str) -> Connector:
    """TLS al endpoint del sandbox, SNI = `endpoint`."""
    context = ssl.create_default_context()

    async def connect() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.open_connection(
            endpoint, ENDPOINT_TLS_PORT, ssl=context, server_hostname=endpoint
        )

    return connect


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await reader.read(PIPE_CHUNK_BYTES)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        with contextlib.suppress(Exception):
            if writer.can_write_eof():
                writer.write_eof()


async def _close(writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(Exception):
        writer.close()
        await writer.wait_closed()


def _report(reason: str) -> None:
    """Una línea en stderr para que el operador sepa por qué una petición
    recibió `502`. `reason` es un texto fijo: nunca lleva el JWE, las
    cabeceras, el cuerpo ni la ruta de la petición."""
    print(f"rayito: proxy: {reason}", file=sys.stderr, flush=True)


async def _reply_and_close(writer: asyncio.StreamWriter, response: bytes) -> None:
    with contextlib.suppress(ConnectionError, OSError):
        writer.write(response)
        await writer.drain()
    await _close(writer)


async def handle_connection(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    *,
    endpoint: str,
    port: int,
    jwe_provider: JweProvider,
    connector: Connector,
    access: ProxyAccess,
    slots: asyncio.Semaphore,
    head_timeout: float = HEAD_READ_TIMEOUT_SECONDS,
    connect_timeout: float = UPSTREAM_CONNECT_TIMEOUT_SECONDS,
) -> None:
    """Una conexión de cliente: comprueba `Host` (`421`) y `Origin` (`403`)
    contra `access`, reserva una de las `slots` (`503` si no queda), y sólo
    entonces reescribe la cabecera de la primera petición y hace de tubería
    en los dos sentidos. Nunca registra nada de lo que pasa por ella; sin
    JWE o sin upstream responde `502` y deja en stderr sólo el motivo."""
    try:
        raw_head = await asyncio.wait_for(client_reader.readuntil(HEADER_TERMINATOR), head_timeout)
    except (
        TimeoutError,
        asyncio.IncompleteReadError,
        asyncio.LimitOverrunError,
        ConnectionError,
        OSError,
    ):
        await _close(client_writer)
        return
    try:
        head = parse_http_head(raw_head)
    except MalformedHttpHeadError:
        await _reply_and_close(client_writer, BAD_REQUEST_RESPONSE)
        return
    rejection = _access_rejection(head, access)
    if rejection is not None:
        await _reply_and_close(client_writer, rejection)
        return
    if slots.locked():
        _report(f"{MAX_CONNECTIONS_OPTION} alcanzado: 503")
        await _reply_and_close(client_writer, SERVICE_UNAVAILABLE_RESPONSE)
        return
    async with slots:
        await _forward(
            head,
            client_reader,
            client_writer,
            endpoint=endpoint,
            port=port,
            jwe_provider=jwe_provider,
            connector=connector,
            connect_timeout=connect_timeout,
        )


def _access_rejection(head: HttpHead, access: ProxyAccess) -> bytes | None:
    """La respuesta de rechazo, o `None` si `Host` y `Origin` están en
    `access`. La línea de stderr nunca lleva el valor recibido (lo controla
    quien conecta y podría traer secuencias de escape)."""
    if not access.admits_host(head.header(HOST_HEADER_NAME)):
        _report(f"Host ausente o no permitido (añádelo con {ALLOWED_HOST_OPTION}): 421")
        return MISDIRECTED_REQUEST_RESPONSE
    if not access.admits_origin(head.header(ORIGIN_HEADER_NAME)):
        _report(f"Origin no permitido (añádelo con {ALLOW_ORIGIN_OPTION}): 403")
        return FORBIDDEN_RESPONSE
    return None


async def _forward(
    head: HttpHead,
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    *,
    endpoint: str,
    port: int,
    jwe_provider: JweProvider,
    connector: Connector,
    connect_timeout: float,
) -> None:
    jwe = jwe_provider()
    if jwe is None:
        _report("sin JWE vigente (falla la renovación con CreateMicrovmAuthToken): 502")
        await _reply_and_close(client_writer, BAD_GATEWAY_RESPONSE)
        return
    upstream_head = rewrite_head(head, endpoint=endpoint, jwe=jwe, port=port)
    try:
        upstream_reader, upstream_writer = await asyncio.wait_for(connector(), connect_timeout)
    except TimeoutError:
        _report("la conexión al upstream (endpoint del sandbox) no se abrió a tiempo: 502")
        await _reply_and_close(client_writer, BAD_GATEWAY_RESPONSE)
        return
    except OSError:
        _report("no se pudo conectar al upstream (endpoint del sandbox): 502")
        await _reply_and_close(client_writer, BAD_GATEWAY_RESPONSE)
        return
    try:
        upstream_writer.write(upstream_head)
        await upstream_writer.drain()
        await asyncio.gather(
            _pump(client_reader, upstream_writer),
            _pump(upstream_reader, client_writer),
        )
    finally:
        await _close(upstream_writer)
        await _close(client_writer)


@dataclass(frozen=True)
class ProxySpec:
    """Configuración resuelta de un `rayito sandbox proxy` en marcha."""

    sandbox_id: str
    port: int
    endpoint: str
    bind: str
    local_port: int
    access: ProxyAccess
    max_connections: int = DEFAULT_MAX_CONNECTIONS


async def serve_proxy(
    spec: ProxySpec,
    jwe_provider: JweProvider,
    connector: Connector,
    *,
    listener: socket.socket,
    ready: Callable[[asyncio.Server], None] | None = None,
    head_timeout: float = HEAD_READ_TIMEOUT_SECONDS,
    connect_timeout: float = UPSTREAM_CONNECT_TIMEOUT_SECONDS,
) -> None:
    """Corre el listener (ya reservado con `bind_listener_socket`, antes de
    cualquier llamada a AWS) hasta que lo cancelen (Ctrl-C en la CLI, un
    `stop_event` de `run_proxy`, o el test cierra el server)."""
    slots = asyncio.Semaphore(spec.max_connections)

    async def on_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await handle_connection(
            reader,
            writer,
            endpoint=spec.endpoint,
            port=spec.port,
            jwe_provider=jwe_provider,
            connector=connector,
            access=spec.access,
            slots=slots,
            head_timeout=head_timeout,
            connect_timeout=connect_timeout,
        )

    server = await asyncio.start_server(on_connection, sock=listener)
    if ready is not None:
        ready(server)
    async with server:
        await server.serve_forever()


def resolve_endpoint(control_plane: ControlPlane, sandbox_id: str) -> str:
    """`get-microvm`; sale con error si el sandbox ya está terminado."""
    info = control_plane.get_microvm(sandbox_id)
    if info.state in TERMINAL_STATES:
        raise SandboxStateException(
            f"el sandbox {sandbox_id} está {info.state.lower()}: no se puede exponer con el proxy"
        )
    return info.endpoint


def build_refresher(control_plane: ControlPlane, sandbox_id: str, port: int) -> TokenRefresher:
    """El mismo `TokenRefresher` que usa el SDK para el canal gRPC, acuñando
    siempre `PortSpec.single(port)` (nunca `allPorts`, ADR-006)."""
    refresher = TokenRefresher(
        TokenStore(), lambda ports: control_plane.create_auth_token(sandbox_id, ports)
    )
    refresher.mint((PortSpec.single(port),))
    return refresher


async def _run_until_stopped(
    spec: ProxySpec,
    jwe_provider: JweProvider,
    connector: Connector,
    listener: socket.socket,
    ready: Callable[[asyncio.Server], None],
    stop_event: threading.Event | None,
) -> None:
    """Sin `stop_event` (la CLI real): equivale a `await serve_proxy(...)`,
    que sólo vuelve por Ctrl-C. Con un `threading.Event` (el e2e, que lanza
    esto en un hilo aparte porque SIGINT no cruza hilos): espera a lo que
    llegue antes, que el servidor acabe (y entonces relanza su excepción, p.
    ej. si `start_server` falló) o que se marque el evento (y entonces lo
    cancela). Sondea el evento en vez de bloquear un hilo del executor en
    `stop_event.wait()`, que dejaría `asyncio.run` colgado al salir."""
    if stop_event is None:
        await serve_proxy(spec, jwe_provider, connector, listener=listener, ready=ready)
        return
    task = asyncio.ensure_future(
        serve_proxy(spec, jwe_provider, connector, listener=listener, ready=ready)
    )
    while not task.done() and not stop_event.is_set():
        await asyncio.wait({task}, timeout=STOP_POLL_SECONDS)
    if task.done():
        task.result()
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def run_proxy(
    control_plane: ControlPlane,
    *,
    sandbox_id: str,
    port: int,
    local_port: int,
    bind: str,
    on_ready: Callable[[str], None],
    allowed_hosts: Sequence[str] = (),
    allowed_origins: Sequence[str] = (),
    max_connections: int = DEFAULT_MAX_CONNECTIONS,
    connector_factory: ConnectorFactory = default_connector,
    stop_event: threading.Event | None = None,
) -> None:
    """Punto de entrada síncrono de la CLI (y del e2e, vía `stop_event`, para
    que los dos corran exactamente el mismo camino): valida `--port` y
    `--local-port` y reserva el socket local ANTES de tocar AWS, resuelve el
    endpoint (`GetMicrovm`), acuña el primer JWE, arranca el refresher (hilo
    daemon, renueva a los 45 min) y sirve hasta Ctrl-C — o hasta que
    `stop_event` (un `threading.Event`) se marque desde otro hilo — parando
    el refresher al salir.

    `stop_event`, si se pasa, es un `threading.Event` (no `asyncio.Event`:
    `run_proxy` es la función síncrona que un hilo aparte llama con
    `target=`; ver `tests/e2e/test_sandbox_proxy_e2e.py`)."""
    validate_proxy_port(port)
    validate_local_port(local_port)
    validate_max_connections(max_connections)
    listener = bind_listener_socket(bind, local_port)
    try:
        access = build_access(
            bind=bind,
            local_port=local_port,
            sandbox_id=sandbox_id,
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
        )
        endpoint = resolve_endpoint(control_plane, sandbox_id)
        refresher = build_refresher(control_plane, sandbox_id, port)
    except Exception:
        listener.close()
        raise
    refresher.start()
    spec = ProxySpec(
        sandbox_id=sandbox_id,
        port=port,
        endpoint=endpoint,
        bind=bind,
        local_port=local_port,
        access=access,
        max_connections=max_connections,
    )
    connector = connector_factory(endpoint)

    def announce(_server: asyncio.Server) -> None:
        on_ready(announcement(spec))

    try:
        asyncio.run(
            _run_until_stopped(
                spec,
                lambda: refresher.store.jwe_for(port),
                connector,
                listener,
                announce,
                stop_event,
            )
        )
    except KeyboardInterrupt:
        pass
    finally:
        refresher.stop()
