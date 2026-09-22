"""Línea de comandos de `python -m rayito.mcp` / `rayito-mcp`: stdio por
defecto, `--http` para streamable HTTP en loopback (design D9). Vive aquí y
no en `__main__.py` para que el paquete pueda reexportar `main` sin que
`python -m` importe `__main__` dos veces.

Nada se escribe en stdout salvo el protocolo: el log va a stderr por el
logger raíz que configura `MCPServer(log_level=)`. La configuración del
sandbox viene sólo del entorno (`McpSettings.from_env`); `--host`/`--port`
describen el transporte, no el sandbox.
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from rayito.mcp._lease import SandboxLease
from rayito.mcp._server import build_server
from rayito.mcp._settings import McpSettings

logger = logging.getLogger("rayito.mcp")

PROGRAM = "rayito-mcp"
DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8000
STREAMABLE_HTTP = "streamable-http"
LOOPBACK_HOSTS = frozenset({"localhost"})
USAGE_ERROR_EXIT_CODE = 2

Runner = Callable[..., None]


@dataclass(frozen=True)
class RunOptions:
    http: bool = False
    host: str = DEFAULT_HTTP_HOST
    port: int = DEFAULT_HTTP_PORT


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description=(
            "Servidor MCP de Rayito: un sandbox por proceso, configurado por "
            "RAYITO_TEMPLATE y el resto de variables RAYITO_MCP_*."
        ),
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="sirve streamable HTTP en http://<host>:<port>/mcp en vez de stdio",
    )
    parser.add_argument(
        "--host", default=None, help=f"interfaz del modo HTTP (por defecto {DEFAULT_HTTP_HOST})"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"puerto del modo HTTP (por defecto {DEFAULT_HTTP_PORT})",
    )
    return parser


def parse_args(argv: Sequence[str]) -> RunOptions:
    """`--host`/`--port` sin `--http` es un error de uso (exit 2); una
    dirección comodín (`0.0.0.0`, `::`) también, porque `--host` es a la vez
    la interfaz y la autoridad que el servidor exige en `Host`/`Origin`: un
    cliente que alcanza un servidor ligado a todas las interfaces escribe la
    dirección que marcó, nunca la comodín, así que el middleware respondería
    421 a cada petición."""
    parser = build_parser()
    namespace = parser.parse_args(list(argv))
    if not namespace.http and (namespace.host is not None or namespace.port is not None):
        parser.error("--host y --port sólo tienen sentido con --http")
    host = DEFAULT_HTTP_HOST if namespace.host is None else str(namespace.host)
    if is_wildcard(host):
        parser.error(
            f"--host {host} liga todas las interfaces y ningún cliente escribe esa "
            "dirección en la cabecera Host: pasa en --host la dirección concreta por "
            "la que los clientes alcanzan el servidor"
        )
    return RunOptions(
        http=bool(namespace.http),
        host=host,
        port=DEFAULT_HTTP_PORT if namespace.port is None else int(namespace.port),
    )


def http_authority(host: str, port: int) -> str:
    """`Host` y `Origin` tal y como los escribe un cliente: un literal IPv6
    va entre corchetes, que es la forma que compara el middleware de `mcp`."""
    try:
        bracketed = ipaddress.ip_address(host).version == 6
    except ValueError:
        bracketed = False
    return f"[{host}]:{port}" if bracketed else f"{host}:{port}"


def transport_security(options: RunOptions) -> TransportSecuritySettings:
    """Siempre explícito: `mcp` sólo auto-activa la protección anti-rebinding
    para tres cadenas de host exactas, así que `--host 127.0.0.2` servía sin
    mirar `Host` ni `Origin` y cualquier web podía alcanzar el sandbox por
    DNS rebinding."""
    authority = http_authority(options.host, options.port)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[authority],
        allowed_origins=[f"http://{authority}"],
    )


def is_wildcard(host: str) -> bool:
    """`0.0.0.0` y `::` ligan todas las interfaces: son una interfaz válida y
    una autoridad imposible."""
    try:
        return ipaddress.ip_address(host).is_unspecified
    except ValueError:
        return False


def is_loopback(host: str) -> bool:
    if host in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def run(
    server: MCPServer[SandboxLease], options: RunOptions, *, runner: Runner | None = None
) -> None:
    """Arranca el servidor con el transporte elegido; `runner` sustituye a
    `server.run` en los tests para comprobar los argumentos exactos."""
    invoke: Runner = server.run if runner is None else runner
    if not options.http:
        invoke()
        return
    if not is_loopback(options.host):
        logger.warning(
            "sin autenticación: cualquier cliente que alcance %s:%s controla el sandbox",
            options.host,
            options.port,
        )
    invoke(
        transport=STREAMABLE_HTTP,
        host=options.host,
        port=options.port,
        transport_security=transport_security(options),
    )


def main(argv: Sequence[str] | None = None) -> int:
    options = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        settings = McpSettings.from_env(os.environ)
    except ValueError as exc:
        sys.stderr.write(f"{PROGRAM}: {exc}\n")
        return USAGE_ERROR_EXIT_CODE
    run(build_server(settings), options)
    return 0
