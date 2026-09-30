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
  quita cualquier `x-aws-proxy-*` que traiga el cliente, fija `Host:
  <endpoint>`, añade `X-aws-proxy-auth: <JWE vigente>` y
  `X-aws-proxy-port: N`, fuerza `Connection: close` salvo en una petición
  `Upgrade` (WebSocket, donde se deja intacta) y a partir de ahí hace de
  tubería en los dos sentidos hasta que un lado cierra.
- TLS al endpoint es `ssl.create_default_context()` con SNI = `endpoint`
  (`default_connector`); los tests inyectan otra fábrica de conectores
  contra un upstream en loopback sin TLS.
- Nunca registra el JWE, las cabeceras, los cuerpos ni las rutas de las
  peticiones que pasan por el proxy: ni aquí ni en `rayito.transport` hay una
  sola llamada a `logging` con esos valores.
- Un sandbox `SUSPENDED` con `auto_resume` se despierta con la primera
  petición que llega al proxy (factura cómputo, como cualquier reanudación).
- No hace falta el access token del sandbox: sólo el JWE del proxy.
"""

from __future__ import annotations

import asyncio
import contextlib
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from rayito._aws import ControlPlane, PortSpec
from rayito._limits import ENDPOINT_TLS_PORT, HOOKS_PORT, PORT_MAX, PORT_MIN, TERMINAL_STATES
from rayito._transport import TokenRefresher, TokenStore
from rayito.exceptions import InvalidArgumentException, SandboxStateException

HEADER_TERMINATOR: bytes = b"\r\n\r\n"
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


def validate_proxy_port(port: int) -> int:
    """`N` de `--port`: nunca el de los hooks, siempre en rango. Se llama
    antes de tocar AWS (ni `GetMicrovm` ni `CreateMicrovmAuthToken`)."""
    if port == HOOKS_PORT:
        raise InvalidArgumentException(
            f"el puerto {HOOKS_PORT} es el de los lifecycle hooks (ADR-006): "
            "no se puede exponer con el proxy"
        )
    if not PORT_MIN <= port <= PORT_MAX:
        raise InvalidArgumentException(f"puerto fuera de rango ({PORT_MIN}-{PORT_MAX}): {port}")
    return port


def is_loopback_bind(bind: str) -> bool:
    return bind in LOOPBACK_ADDRESSES


def local_url(bind: str, port: int) -> str:
    host = f"[{bind}]" if ":" in bind else bind
    return f"http://{host}:{port}"


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


def parse_http_head(raw: bytes) -> HttpHead:
    """`raw` incluye el `\\r\\n\\r\\n` final. Una línea sin `:` se ignora en
    vez de romper el parseo (una cabecera plegada rara no debe tumbar el
    proxy; el peor caso es que esa línea no viaje)."""
    text = raw.decode("latin-1")
    lines = text.split("\r\n")
    request_line = lines[0]
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if not line or ":" not in line:
            continue
        name, _, value = line.partition(":")
        headers.append((name.strip(), value.strip()))
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
    jwe = jwe_provider()
    if jwe is None:
        await _close(client_writer)
        return
    head = parse_http_head(raw_head)
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
    ready: Callable[[asyncio.Server], None] | None = None,
) -> None:
    """Corre el listener hasta que lo cancelen (Ctrl-C en la CLI, o el test
    cierra el server)."""

    async def on_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await handle_connection(
            reader,
            writer,
            endpoint=spec.endpoint,
            port=spec.port,
            jwe_provider=jwe_provider,
            connector=connector,
        )

    server = await asyncio.start_server(on_connection, spec.bind, spec.local_port)
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


def run_proxy(
    control_plane: ControlPlane,
    *,
    sandbox_id: str,
    port: int,
    local_port: int,
    bind: str,
    on_ready: Callable[[str], None],
    connector_factory: ConnectorFactory = default_connector,
) -> None:
    """Punto de entrada síncrono de la CLI: valida el puerto, resuelve el
    endpoint (`GetMicrovm`), acuña el primer JWE, arranca el refresher (hilo
    daemon, renueva a los 45 min) y sirve hasta Ctrl-C, parando el refresher
    al salir."""
    validate_proxy_port(port)
    endpoint = resolve_endpoint(control_plane, sandbox_id)
    refresher = build_refresher(control_plane, sandbox_id, port)
    refresher.start()
    spec = ProxySpec(
        sandbox_id=sandbox_id, port=port, endpoint=endpoint, bind=bind, local_port=local_port
    )
    connector = connector_factory(endpoint)

    def announce(_server: asyncio.Server) -> None:
        on_ready(f"{local_url(bind, local_port)} → {sandbox_id}:{port}")

    try:
        asyncio.run(
            serve_proxy(spec, lambda: refresher.store.jwe_for(port), connector, ready=announce)
        )
    except KeyboardInterrupt:
        pass
    finally:
        refresher.stop()
