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
  `Upgrade` (WebSocket, donde se deja intacta) y a partir de ahí hace de
  tubería en los dos sentidos hasta que un lado cierra.
- **Sólo la primera petición de cada conexión se reescribe.** Como todas
  las respuestas fuerzan `Connection: close` (salvo `Upgrade`), un cliente
  bien portado no manda una segunda petición por la misma conexión; si lo
  hace igualmente (pipelining), esos bytes se reenvían tal cual por la
  tubería, sin volver a pasar por `parse_http_head`/`rewrite_head` — AWS la
  rechaza de todos modos por no traer `x-aws-proxy-auth` propio (esa
  cabecera ya se puso una vez, al principio de la conexión, no por
  petición).
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
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from rayito._aws import ControlPlane, PortSpec
from rayito._limits import ENDPOINT_TLS_PORT, HOOKS_PORT, PORT_MAX, PORT_MIN, TERMINAL_STATES
from rayito._transport import TokenRefresher, TokenStore
from rayito.exceptions import InvalidArgumentException, SandboxStateException

HEADER_TERMINATOR: bytes = b"\r\n\r\n"
BAD_REQUEST_RESPONSE: bytes = (
    b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
)
# RFC 9110 §5.6.2 `tchar`: los nombres de cabecera del cliente que no
# cumplan esto se rechazan en vez de reenviarse tal cual.
_TOKEN_CHARS = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
PROXY_HEADER_PREFIX = "x-aws-proxy-"
AUTH_HEADER_NAME = "X-aws-proxy-auth"
PORT_HEADER_NAME = "X-aws-proxy-port"
HOST_HEADER_NAME = "Host"
CONNECTION_HEADER_NAME = "Connection"
UPGRADE_HEADER_NAME = "Upgrade"
LOOPBACK_ADDRESSES = frozenset({"127.0.0.1", "::1", "localhost"})
DEFAULT_BIND = "127.0.0.1"
PIPE_CHUNK_BYTES = 64 * 1024

BIND_NEEDS_ALLOW_REMOTE_MESSAGE = (
    "--bind fuera de loopback necesita --allow-remote: "
    "cualquiera que llegue a ese puerto usa el sandbox mientras el proxy esté vivo"
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


def local_url(bind: str, port: int) -> str:
    host = f"[{bind}]" if ":" in bind else bind
    return f"http://{host}:{port}"


def bind_listener_socket(bind: str, local_port: int) -> socket.socket:
    """Reserva el socket local ANTES de tocar AWS (`GetMicrovm`,
    `CreateMicrovmAuthToken`): un `--local-port` ocupado o un `--bind`
    inválido falla aquí, limpio, sin haber gastado ninguna llamada.
    `asyncio.start_server(sock=...)` hace el resto (listen + no bloqueante)."""
    family = socket.AF_INET6 if ":" in bind else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((bind, local_port))
    except OSError:
        sock.close()
        raise
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
        tokens = {
            token.strip().lower()
            for token in (self.header(CONNECTION_HEADER_NAME) or "").split(",")
        }
        return "upgrade" in tokens or self.header(UPGRADE_HEADER_NAME) is not None


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


async def handle_connection(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    *,
    endpoint: str,
    port: int,
    jwe_provider: JweProvider,
    connector: Connector,
) -> None:
    """Una conexión de cliente: reescribe la cabecera de la primera petición
    y hace de tubería en los dos sentidos. Nunca registra nada de lo que
    pasa por ella."""
    try:
        raw_head = await client_reader.readuntil(HEADER_TERMINATOR)
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError, OSError):
        await _close(client_writer)
        return
    try:
        head = parse_http_head(raw_head)
    except MalformedHttpHeadError:
        with contextlib.suppress(ConnectionError, OSError):
            client_writer.write(BAD_REQUEST_RESPONSE)
            await client_writer.drain()
        await _close(client_writer)
        return
    jwe = jwe_provider()
    if jwe is None:
        await _close(client_writer)
        return
    upstream_head = rewrite_head(head, endpoint=endpoint, jwe=jwe, port=port)
    try:
        upstream_reader, upstream_writer = await connector()
    except OSError:
        await _close(client_writer)
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


async def serve_proxy(
    spec: ProxySpec,
    jwe_provider: JweProvider,
    connector: Connector,
    *,
    listener: socket.socket,
    ready: Callable[[asyncio.Server], None] | None = None,
) -> None:
    """Corre el listener (ya reservado con `bind_listener_socket`, antes de
    cualquier llamada a AWS) hasta que lo cancelen (Ctrl-C en la CLI, un
    `stop_event` de `run_proxy`, o el test cierra el server)."""

    async def on_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await handle_connection(
            reader,
            writer,
            endpoint=spec.endpoint,
            port=spec.port,
            jwe_provider=jwe_provider,
            connector=connector,
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
    esto en un hilo aparte porque SIGINT no cruza hilos): cancela la tarea en
    cuanto se marca, en vez de reimplementar el bucle de servir + parar."""
    if stop_event is None:
        await serve_proxy(spec, jwe_provider, connector, listener=listener, ready=ready)
        return
    task = asyncio.ensure_future(
        serve_proxy(spec, jwe_provider, connector, listener=listener, ready=ready)
    )
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, stop_event.wait)
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
    listener = bind_listener_socket(bind, local_port)
    try:
        endpoint = resolve_endpoint(control_plane, sandbox_id)
        refresher = build_refresher(control_plane, sandbox_id, port)
    except Exception:
        listener.close()
        raise
    refresher.start()
    spec = ProxySpec(
        sandbox_id=sandbox_id, port=port, endpoint=endpoint, bind=bind, local_port=local_port
    )
    connector = connector_factory(endpoint)

    def announce(_server: asyncio.Server) -> None:
        on_ready(f"{local_url(bind, local_port)} → {sandbox_id}:{port}")

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
