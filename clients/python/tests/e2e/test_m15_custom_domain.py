"""`m15-custom-domain` contra AWS real (D3 + etapa de aceptación serializada,
ver `MILESTONES.md` M15 y `design.md` del cambio): despliega
`infra/custom-domain.yaml`, registra una ruta con `traffic_token`, comprueba
HTTP/1.1 de verdad a través de la distribución (DOM-2), el upgrade de
WebSocket (DOM-3), que una petición sin el token da 403, que una ruta ya
`unregister()`-ada da 404, y que `destroy()` espera de verdad a que
CloudFront deshabilite y borre la distribución (en vez de agotar el timeout
genérico de `OptionalStacks` y lanzar `StackException(code="in_progress")`
— el hallazgo de revisión de PR #74 que motivó
`CUSTOM_DOMAIN_WAIT_TIMEOUT_SECONDS`).

Se salta por completo salvo que el entorno de aceptación traiga, además de
`RAYITO_E2E=1`/`RAYITO_TEMPLATE` (`conftest.py`), un dominio propio, un
certificado ACM en `us-east-1` que lo cubra y una etiqueta de ejecución —
D3: sólo el mantenedor puede aportar los dos primeros; la etiqueta la
decide quien lance la corrida. Ningún otro test de este módulo corre sin
ellos: D3 bloquea *ejecutar* este fichero, no escribirlo (hallazgo de
revisión de PR #74).

Coste: una distribución CloudFront, su CloudFront Function y su
KeyValueStore durante la vida del test (deploy + destroy, con destroy
esperando a que CloudFront la deshabilite y borre de verdad) más un
sandbox breve y un puñado de `PutKey`/`DeleteKey` — cifra exacta pendiente
de confirmar en el propio informe de aceptación (ver
`dominio-propio.md`, "Coste y activación")."""

from __future__ import annotations

import base64
import hashlib
import http.client
import os
import secrets
import socket
import ssl
import time
from collections.abc import Iterator

import pytest

from rayito import CustomDomain
from rayito.sandbox_sync.main import Sandbox

from .conftest import E2ESettings

pytestmark = pytest.mark.e2e

#: D3: el dominio propio y el certificado ACM sólo puede aportarlos el
#: mantenedor; sin ellos no hay distribución que desplegar.
PUBLIC_DOMAIN_VAR = "RAYITO_CUSTOM_DOMAIN"
CERTIFICATE_ARN_VAR = "RAYITO_CUSTOM_DOMAIN_CERTIFICATE_ARN"
#: Etiqueta de la corrida de aceptación (regla de limpieza de AWS: tras
#: aceptar, sólo se borran las VMs y versiones de imagen de esta corrida).
#: No tiene un valor por defecto: una corrida de aceptación real siempre la
#: trae, y así cualquier recurso que este test cree queda etiquetado para
#: poder identificarlo si `destroy()`/`unregister()` no llegan a correr.
ACCEPTANCE_RUN_TAG_VAR = "RAYITO_ACCEPTANCE_RUN_TAG"
ACCEPTANCE_RUN_TAG_KEY = "rayito:acceptance-run"

ROUTE_PORT = 8000
#: Suficiente para que el test entero corra sin que la ruta caduque sola
#: (T25); muy por debajo de `MAX_TEST_SANDBOX_TIMEOUT_SECONDS`.
ROUTE_TTL_SECONDS = 1800
HTTPS_PORT = 443
HTTP_REQUEST_TIMEOUT_SECONDS = 20.0
WEBSOCKET_HANDSHAKE_TIMEOUT_SECONDS = 20.0
#: GUID fijo de RFC 6455 §1.3, para calcular `Sec-WebSocket-Accept`.
WEBSOCKET_ACCEPT_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
HTTP_SERVER_BOOT_GRACE_SECONDS = 2.0
TRAFFIC_TOKEN_HEADER = "e2b-traffic-access-token"


def _required_custom_domain_env() -> tuple[str, str, str]:
    public_domain = os.environ.get(PUBLIC_DOMAIN_VAR)
    certificate_arn = os.environ.get(CERTIFICATE_ARN_VAR)
    run_tag = os.environ.get(ACCEPTANCE_RUN_TAG_VAR)
    if not public_domain or not certificate_arn or not run_tag:
        pytest.skip(
            f"este e2e necesita {PUBLIC_DOMAIN_VAR}, {CERTIFICATE_ARN_VAR} y "
            f"{ACCEPTANCE_RUN_TAG_VAR} (D3: dominio y certificado ACM del mantenedor; "
            "sólo disponibles en la etapa de aceptación)"
        )
    return public_domain, certificate_arn, run_tag


@pytest.fixture(scope="module")
def custom_domain(e2e_settings: E2ESettings) -> Iterator[CustomDomain]:
    """Una distribución real, desplegada una sola vez para todo el módulo
    (crear/destruir una distribución CloudFront es demasiado lento y caro
    para hacerlo por test): `destroy()` en el teardown es la comprobación
    de que espera lo que de verdad tarda CloudFront, con
    `CUSTOM_DOMAIN_WAIT_TIMEOUT_SECONDS`."""
    public_domain, certificate_arn, run_tag = _required_custom_domain_env()
    domain = CustomDomain(public_domain=public_domain, region=e2e_settings.region)
    domain.deploy(certificate_arn=certificate_arn, tags={ACCEPTANCE_RUN_TAG_KEY: run_tag})
    try:
        yield domain
    finally:
        domain.destroy()


def _https_status(host: str, path: str, headers: dict[str, str]) -> int:
    connection = http.client.HTTPSConnection(host, HTTPS_PORT, timeout=HTTP_REQUEST_TIMEOUT_SECONDS)
    try:
        connection.request("GET", path, headers=headers)
        return connection.getresponse().status
    finally:
        connection.close()


def _websocket_handshake_and_echo(
    host: str, path: str, headers: dict[str, str], message: bytes
) -> bytes:
    """Apertura de WebSocket mínima (RFC 6455) con un único frame de texto de
    ida y vuelta, sin más dependencia que la librería estándar: suficiente
    para DOM-3 (¿corre la Function en el upgrade? ¿el proxy de AWS acepta
    la cabecera de token en la petición de upgrade?), no un cliente de
    WebSocket de propósito general."""
    raw_socket = socket.create_connection(
        (host, HTTPS_PORT), timeout=WEBSOCKET_HANDSHAKE_TIMEOUT_SECONDS
    )
    tls_socket = ssl.create_default_context().wrap_socket(raw_socket, server_hostname=host)
    try:
        websocket_key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        extra_headers = "".join(f"{name}: {value}\r\n" for name, value in headers.items())
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {websocket_key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"{extra_headers}\r\n"
        )
        tls_socket.sendall(request.encode("ascii"))
        response = tls_socket.recv(4096)
        status_line = response.split(b"\r\n", 1)[0].decode("ascii", "replace")
        if " 101 " not in status_line:
            raise AssertionError(f"el upgrade de WebSocket no se aceptó: {status_line!r}")
        expected_accept = base64.b64encode(
            hashlib.sha1((websocket_key + WEBSOCKET_ACCEPT_GUID).encode("ascii")).digest()
        ).decode("ascii")
        if f"Sec-WebSocket-Accept: {expected_accept}".lower() not in response.lower().decode(
            "ascii", "replace"
        ):
            raise AssertionError("Sec-WebSocket-Accept no coincide con la clave enviada")
        # Un único frame de texto cliente->servidor (enmascarado, como exige
        # RFC 6455 §5.1 para toda trama que sale de un cliente) de menos de
        # 126 bytes, así que la longitud cabe en el segundo byte sin
        # extensión.
        mask = secrets.token_bytes(4)
        masked_payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(message))
        frame = bytes([0x81, 0x80 | len(message)]) + mask + masked_payload
        tls_socket.sendall(frame)
        reply = tls_socket.recv(4096)
        payload_length = reply[1] & 0x7F
        return reply[2 : 2 + payload_length]
    finally:
        tls_socket.close()


@pytest.fixture
def websocket_echo_sandbox(sandbox: Sandbox) -> Iterator[Sandbox]:
    """Un sandbox con un eco de WebSocket mínimo (sólo `socket`, sin
    instalar nada) escuchando en `ROUTE_PORT`: lo justo para comprobar
    DOM-3 de punta a punta, no un servidor de WebSocket real."""
    echo_server_script = f"""
import hashlib, base64, socket
GUID = "{WEBSOCKET_ACCEPT_GUID}"
srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("0.0.0.0", {ROUTE_PORT}))
srv.listen(1)
conn, _ = srv.accept()
data = b""
while b"\\r\\n\\r\\n" not in data:
    data += conn.recv(4096)
key = None
for line in data.decode("latin1").split("\\r\\n"):
    if line.lower().startswith("sec-websocket-key:"):
        key = line.split(":", 1)[1].strip()
accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
conn.sendall((
    "HTTP/1.1 101 Switching Protocols\\r\\n"
    "Upgrade: websocket\\r\\n"
    "Connection: Upgrade\\r\\n"
    f"Sec-WebSocket-Accept: {{accept}}\\r\\n\\r\\n"
).encode())
frame = conn.recv(4096)
length = frame[1] & 0x7F
mask = frame[2:6]
payload = bytes(b ^ mask[i % 4] for i, b in enumerate(frame[6:6 + length]))
conn.sendall(bytes([0x81, len(payload)]) + payload)
conn.close()
"""
    sandbox.commands.run(f"python3 -c {echo_server_script!r}", background=True)
    time.sleep(HTTP_SERVER_BOOT_GRACE_SECONDS)
    yield sandbox


def test_custom_domain_routes_a_registered_sandbox_and_destroy_waits_for_real(
    custom_domain: CustomDomain, sandbox: Sandbox
) -> None:
    sandbox.commands.run(f"python3 -m http.server {ROUTE_PORT}", background=True)
    time.sleep(HTTP_SERVER_BOOT_GRACE_SECONDS)
    jwe = sandbox.get_host(ROUTE_PORT).headers["x-aws-proxy-auth"]
    traffic_token = secrets.token_urlsafe(32)
    route = custom_domain.register(
        sandbox.sandbox_id,
        ROUTE_PORT,
        endpoint=sandbox.endpoint,
        jwe=jwe,
        traffic_token=traffic_token,
        ttl_seconds=ROUTE_TTL_SECONDS,
    )

    # DOM-2: HTTP/1.1 real a través de `cf.updateRequestOrigin`, con el
    # token correcto.
    assert _https_status(route.host, "/", {TRAFFIC_TOKEN_HEADER: traffic_token}) == 200

    # SEC-T25: sin el token, 403 — nunca una ruta pública por omisión.
    assert _https_status(route.host, "/", {}) == 403

    custom_domain.unregister(sandbox.sandbox_id, ROUTE_PORT)

    # Tras `unregister()`, 404 — indistinguible de una ruta que nunca
    # existió.
    assert _https_status(route.host, "/", {TRAFFIC_TOKEN_HEADER: traffic_token}) == 404


def test_custom_domain_passes_a_websocket_upgrade(
    custom_domain: CustomDomain, websocket_echo_sandbox: Sandbox
) -> None:
    traffic_token = secrets.token_urlsafe(32)
    jwe = websocket_echo_sandbox.get_host(ROUTE_PORT).headers["x-aws-proxy-auth"]
    route = custom_domain.register(
        websocket_echo_sandbox.sandbox_id,
        ROUTE_PORT,
        endpoint=websocket_echo_sandbox.endpoint,
        jwe=jwe,
        traffic_token=traffic_token,
        ttl_seconds=ROUTE_TTL_SECONDS,
    )
    try:
        # DOM-3: ¿corre la Function en la petición de upgrade, y acepta el
        # proxy de AWS la cabecera de token ahí también?
        echoed = _websocket_handshake_and_echo(
            route.host, "/", {TRAFFIC_TOKEN_HEADER: traffic_token}, b"ping"
        )
        assert echoed == b"ping"
    finally:
        # Idempotente (`CustomDomain.unregister`'s docstring): borrar una
        # ruta que el propio test ya hubiera dejado a medias no es un error.
        custom_domain.unregister(websocket_echo_sandbox.sandbox_id, ROUTE_PORT)


def test_stack_status_reports_the_deployed_distribution(custom_domain: CustomDomain) -> None:
    status = custom_domain.status()
    assert status is not None
    assert status.state in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}
    assert "KvsArn" in status.outputs
