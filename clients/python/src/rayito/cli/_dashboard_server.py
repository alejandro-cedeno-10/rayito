"""La cáscara local de `rayito dashboard`: un servidor HTTP de la biblioteca
estándar en `127.0.0.1` que sirve la SPA del wheel y la API `/api/v1` de
`DashboardApi`. No añade dependencias ni servidores de Rayito: es un proceso
tuyo, mientras la orden corre.

Defensas (el panel usa tus credenciales de AWS, así que cualquier web que
abras en el navegador es un atacante posible):

- **`Host` y `Origin`**: la misma lista (`ProxyAccess`, `_proxy.build_access`)
  que `rayito sandbox proxy`. `Host` ajeno es `421` (DNS rebinding); un
  `Origin` presente y ajeno, incluido `null`, es `403`.
- **Token de arranque de un solo uso**: la URL impresa lleva `?token=…`; el
  servidor lo cambia por una cookie de sesión `HttpOnly`, `SameSite=Strict`,
  `Path=/`, y redirige a la URL limpia con `Referrer-Policy: no-referrer`. Un
  segundo uso, o uno pasado el TTL, se rechaza. Sin la cookie, nada responde.
- **`Sec-Fetch-Site`**: en `/api/v1`, si el navegador manda la cabecera y no
  es `same-origin` ni `none`, la petición es `403`. Cubre lo que `Origin` no
  ve: un `<img>` o una navegación de otra app local (incluida la web que sirve
  un sandbox por `rayito sandbox proxy` en `127.0.0.1:<otro puerto>`) es
  `same-site`, comparte el tarro de cookies y no manda `Origin`.
- **CSRF**: toda petición que escribe o gasta dinero (`POST` y el `GET` de
  coste facturado, `DashboardApi.requires_csrf`) exige la cabecera
  `X-Rayito-CSRF` con el token de la sesión, que sólo entrega `GET
  /api/v1/session` a quien ya tiene la cookie; un `POST` además exige
  `Content-Type: application/json` (una web ajena no lo manda sin preflight).
- **Sólo lectura por defecto**: las acciones las gobierna `DashboardApi`.
- **Cabeceras**: CSP sin `unsafe-inline`, `nosniff`, `no-store`, `DENY`.
- **Nada se registra**: ni peticiones, ni cabeceras, ni la URL con el token.
"""

from __future__ import annotations

import hmac
import http.cookies
import http.server
import json
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import parse_qs, unquote, urlsplit

from rayito._dashboard._api._contract import (
    API_PREFIX,
    HTTP_OK,
    ApiBackend,
    ApiRequest,
    ApiResponse,
)
from rayito._dashboard._api._headers import LOGO_HEADERS, content_security_policy, security_headers
from rayito._dashboard._api._router import ApiError, error_response
from rayito._dashboard._api._static import INDEX_PATH, LOGO_PATH, load_static_files
from rayito._dashboard._theme import Appearance
from rayito._limits import (
    DASHBOARD_REQUEST_MAX_BODY_BYTES,
    DASHBOARD_REQUEST_TIMEOUT_SECONDS,
    DASHBOARD_SESSION_TOKEN_BYTES,
    DASHBOARD_START_TOKEN_TTL_SECONDS,
)
from rayito.cli._proxy import DASHBOARD_COOKIE_PREFIX, ProxyAccess, build_access

LOOPBACK: Final = "127.0.0.1"
CSRF_HEADER: Final = "X-Rayito-CSRF"
FETCH_SITE_HEADER: Final = "sec-fetch-site"
#: Las cabeceras que anuncian un cuerpo de petición (RFC 9112 §6).
BODY_HEADER_NAMES: Final = ("Content-Length", "Transfer-Encoding")
#: Los únicos valores de `Sec-Fetch-Site` que admite la API: la propia SPA, o
#: una navegación escrita por el usuario (sin origen que atacar).
ADMITTED_FETCH_SITES: Final = frozenset({"same-origin", "none"})
SESSION_PATH: Final = f"{API_PREFIX}/session"
START_TOKEN_PARAM: Final = "token"
JSON_TYPE: Final = "application/json"
HTTP_SEE_OTHER: Final = 303
HTTP_NOT_FOUND: Final = 404
CONTENT_SECURITY_POLICY: Final = content_security_policy()
SECURITY_HEADERS: Final[dict[str, str]] = security_headers()
ALLOWED_METHODS: Final = ("GET", "POST")
#: Cada cuánto mira `serve_forever` si le piden parar: cuánto tarda Ctrl+C.
SHUTDOWN_POLL_SECONDS: Final = 0.1


class SessionGuard:
    """El token de arranque (un solo uso, con TTL), la sesión y el token
    CSRF de una ejecución. Todo se compara en tiempo constante."""

    def __init__(
        self,
        port: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        token_bytes: int = DASHBOARD_SESSION_TOKEN_BYTES,
        start_ttl_seconds: float = DASHBOARD_START_TOKEN_TTL_SECONDS,
    ) -> None:
        self._clock = clock
        self._token_bytes = token_bytes
        self._start_ttl = start_ttl_seconds
        self._issued_at = clock()
        self._start_token: str | None = secrets.token_urlsafe(token_bytes)
        self._session: str | None = None
        self.csrf_token = secrets.token_urlsafe(token_bytes)
        self.cookie_name = f"{DASHBOARD_COOKIE_PREFIX}{port}"

    @property
    def start_token(self) -> str:
        """El token que se imprime una sola vez en la URL de arranque."""
        if self._start_token is None:
            raise RuntimeError("el token de arranque ya se canjeó")
        return self._start_token

    def redeem(self, presented: str | None) -> str | None:
        """Cambia el token de arranque por el valor de la cookie de sesión;
        `None` si no coincide, ya se usó o caducó. Se quema siempre que se
        presenta uno válido, y sólo entonces."""
        expected = self._start_token
        if presented is None or expected is None:
            return None
        if not hmac.compare_digest(presented.encode(), expected.encode()):
            return None
        self._start_token = None
        if self._clock() - self._issued_at > self._start_ttl:
            return None
        self._session = secrets.token_urlsafe(self._token_bytes)
        return self._session

    def has_session(self, cookie_header: str | None) -> bool:
        if self._session is None or not cookie_header:
            return False
        jar: http.cookies.SimpleCookie = http.cookies.SimpleCookie()
        try:
            jar.load(cookie_header)
        except http.cookies.CookieError:
            return False
        morsel = jar.get(self.cookie_name)
        if morsel is None:
            return False
        return hmac.compare_digest(morsel.value.encode(), self._session.encode())

    def csrf_matches(self, presented: str | None) -> bool:
        return presented is not None and hmac.compare_digest(
            presented.encode(), self.csrf_token.encode()
        )

    def session_cookie(self, value: str) -> str:
        return f"{self.cookie_name}={value}; HttpOnly; SameSite=Strict; Path=/"


@dataclass(frozen=True)
class Reply:
    status: int
    body: bytes
    content_type: str
    headers: Mapping[str, str]


def json_reply(response: ApiResponse, headers: Mapping[str, str] | None = None) -> Reply:
    return Reply(
        response.status,
        json.dumps(response.body, ensure_ascii=False).encode(),
        JSON_TYPE,
        headers or {},
    )


def text_reply(status: int, text: str, headers: Mapping[str, str] | None = None) -> Reply:
    return Reply(status, text.encode(), "text/plain; charset=utf-8", headers or {})


class LocalApp:
    """Lo que el servidor decide por petición, sin sockets: `respond`. Los
    tests de seguridad la llaman directamente y la cáscara HTTP sólo la
    envuelve."""

    def __init__(
        self,
        api: ApiBackend,
        access: ProxyAccess,
        guard: SessionGuard,
        static: Mapping[str, tuple[bytes, str]],
    ) -> None:
        self._api = api
        self._access = access
        self._guard = guard
        self._static = static

    def respond(
        self, method: str, target: str, headers: Mapping[str, str], body: bytes | None
    ) -> Reply:
        """`headers` con las claves en minúsculas; `body` ya leído (o `None`
        si no hay) y acotado por la cáscara."""
        rejection = self._reject_origin(headers)
        if rejection is not None:
            return rejection
        url = urlsplit(target)
        query = parse_qs(url.query)
        if method not in ALLOWED_METHODS:
            return self.error_reply("method_not_allowed")
        if method == "GET" and url.path == INDEX_PATH and START_TOKEN_PARAM in query:
            return self._redeem(query[START_TOKEN_PARAM][0])
        if not self._guard.has_session(headers.get("cookie")):
            return self.error_reply("unauthorized")
        if url.path.startswith(f"{API_PREFIX}/"):
            rejection = self._reject_api_request(
                ApiRequest(method, unquote(url.path), query, body or b""), headers
            )
            if rejection is not None:
                return rejection
        return self._route(method, url.path, query, body or b"")

    def _reject_origin(self, headers: Mapping[str, str]) -> Reply | None:
        if not self._access.admits_host(headers.get("host")):
            return self.error_reply("misdirected_request")
        if not self._access.admits_origin(headers.get("origin")):
            return self.error_reply("forbidden")
        return None

    def _reject_api_request(self, request: ApiRequest, headers: Mapping[str, str]) -> Reply | None:
        fetch_site = headers.get(FETCH_SITE_HEADER)
        if fetch_site is not None and fetch_site.lower() not in ADMITTED_FETCH_SITES:
            return self.error_reply("forbidden")
        if request.method == "POST":
            content_type = headers.get("content-type", "").split(";")[0].strip().lower()
            if content_type != JSON_TYPE:
                return self.error_reply("unsupported_media_type")
        if self._api.requires_csrf(request) and not self._guard.csrf_matches(
            headers.get(CSRF_HEADER.lower())
        ):
            return self.error_reply("csrf_failed")
        return None

    def _redeem(self, token: str) -> Reply:
        session = self._guard.redeem(token)
        if session is None:
            return self.error_reply("unauthorized")
        return Reply(
            HTTP_SEE_OTHER,
            b"",
            "text/plain; charset=utf-8",
            {"Location": INDEX_PATH, "Set-Cookie": self._guard.session_cookie(session)},
        )

    def _route(self, method: str, path: str, query: dict[str, list[str]], body: bytes) -> Reply:
        if path == SESSION_PATH and method == "GET":
            return json_reply(ApiResponse(HTTP_OK, {"csrfToken": self._guard.csrf_token}))
        if path.startswith(f"{API_PREFIX}/"):
            return json_reply(self._api.handle(ApiRequest(method, unquote(path), query, body)))
        static = self._static.get(path) if method == "GET" else None
        if static is None:
            return self.error_reply("not_found")
        headers = LOGO_HEADERS if path == LOGO_PATH else {}
        return Reply(HTTP_OK, static[0], static[1], headers)

    def error_reply(self, code: str) -> Reply:
        return json_reply(error_response(ApiError(code)))


class _Handler(http.server.BaseHTTPRequestHandler):
    server: DashboardHttpServer
    protocol_version = "HTTP/1.1"
    timeout = DASHBOARD_REQUEST_TIMEOUT_SECONDS

    def do_GET(self) -> None:
        self._serve_without_body()

    def do_POST(self) -> None:
        length = _content_length(self.headers.get("Content-Length"))
        if length is None:
            self._send(self.server.app.error_reply("invalid_argument"), close=True)
            return
        if length > DASHBOARD_REQUEST_MAX_BODY_BYTES:
            self._send(self.server.app.error_reply("payload_too_large"), close=True)
            return
        self._serve(self.rfile.read(length))

    def do_PUT(self) -> None:
        self._serve_without_body()

    do_DELETE = do_PATCH = do_PUT

    def _serve_without_body(self) -> None:
        """Estos métodos no leen cuerpo: si la petición trae uno, se cierra la
        conexión tras responder para que sus bytes no se lean como la
        petición siguiente (desincronización de HTTP/1.1 con keep-alive)."""
        carries_body = any(name in self.headers for name in BODY_HEADER_NAMES)
        self._serve(None, close=carries_body)

    def _serve(self, body: bytes | None, *, close: bool = False) -> None:
        headers = {name.lower(): value for name, value in self.headers.items()}
        reply = self.server.app.respond(self.command, self.path, headers, body)
        self._send(reply, close=close)

    def _send(self, reply: Reply, *, close: bool) -> None:
        self.send_response_only(reply.status)
        self.send_header("Content-Type", reply.content_type)
        self.send_header("Content-Length", str(len(reply.body)))
        for name, value in {**SECURITY_HEADERS, **reply.headers}.items():
            self.send_header(name, value)
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(reply.body)

    def log_message(self, format: str, *args: Any) -> None:
        """Nada se registra: la línea de petición lleva el token de arranque."""

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        """Ver `log_message`."""

    def log_error(self, format: str, *args: Any) -> None:
        """Ver `log_message`."""


def _content_length(value: str | None) -> int | None:
    if value is None or not value.isdigit():
        return None
    return int(value)


class DashboardHttpServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    app: LocalApp


def start_url(port: int, token: str) -> str:
    return f"http://{LOOPBACK}:{port}{INDEX_PATH}?{START_TOKEN_PARAM}={token}"


class LocalDashboard:
    """Une `DashboardApi` con el servidor: reserva el puerto (0, uno al
    azar), construye la lista de `Host`/`Origin` con ese puerto y sirve."""

    def __init__(
        self, api: ApiBackend, *, port: int = 0, appearance: Appearance | None = None
    ) -> None:
        self._api = api
        self._server = DashboardHttpServer((LOOPBACK, port), _Handler)
        self.port = int(self._server.server_address[1])
        self.guard = SessionGuard(self.port)
        access = build_access(bind=LOOPBACK, local_port=self.port, sandbox_id=None)
        self.app = LocalApp(api, access, self.guard, load_static_files(appearance))
        self._server.app = self.app
        self._serving = False

    @property
    def url(self) -> str:
        """La URL de arranque con el token: la única vez que se muestra."""
        return start_url(self.port, self.guard.start_token)

    def serve_forever(self) -> None:
        self._serving = True
        self._server.serve_forever(poll_interval=SHUTDOWN_POLL_SECONDS)

    def shutdown(self) -> None:
        """Para el bucle (si llegó a arrancar), cierra el socket y la cola de
        acciones. `BaseServer.shutdown` espera a un `serve_forever` que nunca
        empezó, así que sólo se llama si empezó."""
        if self._serving:
            self._server.shutdown()
        self._server.server_close()
        self._api.close()
