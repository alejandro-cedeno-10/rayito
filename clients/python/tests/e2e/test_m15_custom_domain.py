"""`m15-custom-domain` contra AWS real (etapa de aceptación serializada, ver
`MILESTONES.md` M15 y `design.md` del cambio): despliega
`infra/custom-domain.yaml`, registra rutas con `traffic_token` y comprueba,
a través de la distribución, HTTP/1.1 de verdad (DOM-2), la latencia de
propagación del KeyValueStore al edge (DOM-5, sólo se imprime), que
`refresh()` no corta una ruta viva (DOM-7, parcial), el upgrade de
WebSocket (DOM-3), el auto-resume de un sandbox pausado al llegarle
tráfico por el dominio (DOM-8), que una ruta sin el token da 403, que una
ruta `unregister()`-ada o con el TTL vencido da 404, y que `destroy()`
espera de verdad a que CloudFront deshabilite y borre la distribución.

**Sólo variables de entorno, nada del entorno de prueba en el código.**
Además de `RAYITO_E2E=1`/`RAYITO_TEMPLATE` (`conftest.py`):

- `RAYITO_E2E_DOMAIN`: un dominio cuyo comodín cubre el certificado (si
  el certificado es `*.sbx.example.com`, `RAYITO_E2E_DOMAIN=sbx.example.com`).
- `RAYITO_E2E_CERT_ARN`: ese certificado ACM, en `us-east-1`.
- `RAYITO_ACCEPTANCE_RUN_TAG` (opcional): etiqueta de la corrida; por
  defecto, el identificador aleatorio de la corrida.
- `RAYITO_E2E_TAGS` (opcional, `conftest.e2e_tags`): etiquetas `K=V` separadas
  por comas para la pila, para organizaciones que exigen etiquetas en los
  recursos creados. La etiqueta de la corrida gana si coincide una clave.

Sin las dos primeras el módulo entero se salta. Elige un dominio SIN un
registro DNS comodín que apunte a otra distribución CloudFront: CloudFront
rechaza entonces añadir un nombre más específico ("incorrectly configured
DNS record", `AWS_API_NOTES.md` §29).

**Aislado por corrida y sin tocar DNS.** Cada corrida elige un
identificador aleatorio: la pila se llama `rayito-cd-e2e-<id>` y la
distribución no lleva el comodín `*.<dominio>` sino sólo los hostnames
exactos de sus rutas (`8000-e2e-<id>-<uso>.<dominio>`,
`alternate_domain_names=`), así que nunca choca con otra distribución que ya
tenga ese comodín u otro alias (CloudFront rechaza un alias repetido y, ante
un solape, gana el más específico). Ningún registro DNS hace falta: el test
abre TCP contra el `*.cloudfront.net` de la distribución y manda el hostname
propio como SNI y como `Host` (lo mismo que `curl --connect-to`), validando
el certificado contra ese hostname. Un usuario real, en cambio, apunta un
`CNAME`/alias de su DNS al `DistributionDomainName`.

**Determinista frente a la propagación.** El KeyValueStore llega a cada
servidor del edge por separado y cada petición del test abre una conexión
nueva, que puede caer en otro servidor: un 403 visto una vez no dice que
todos tengan ya la ruta. Por eso la visibilidad exige
`STABLE_OBSERVATIONS` respuestas seguidas, cada respuesta esperada (200,
403, 404) se sondea con su plazo en vez de pedirse una sola vez, el servidor
del sandbox se espera hasta que escucha (no un tiempo fijo) y el eco de
WebSocket acepta varias conexiones, así que un apretón de manos que cae en un
servidor aún sin la ruta se reintenta.

**Requisito: `cloudfront:CreateDistribution`.** Ante cualquier error de
permisos al crear el recurso `Distribution` (`DescribeStackEvents`), el
módulo se salta nombrando ese requisito en vez de fallar.

**Limpieza.** El teardown desregistra cada ruta (idempotente), borra la pila
(distribución, Function y KeyValueStore; las rutas viven en el KVS y se van
con él) aunque el `deploy()` haya fallado a medias, y comprueba que
`status()` ya no la encuentra.

Coste: una distribución CloudFront, su Function y su KVS durante ~20-30 min
(sin tráfico real: céntimos), tres sandboxes breves y un puñado de
`PutKey`/`DeleteKey`; muy por debajo de $1."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import http.client
import os
import secrets
import socket
import ssl
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError

from rayito import CustomDomain, IdlePolicy
from rayito._aws import LambdaMicrovmsControlPlane
from rayito._custom_domain._service import (
    DISTRIBUTION_DOMAIN_NAME_OUTPUT_KEY,
    CustomDomainRoute,
)
from rayito.exceptions import CustomDomainException, SandboxNotFoundException, StackException
from rayito.sandbox_sync.main import Sandbox

from .conftest import BootTimings, E2ESettings, create_test_sandbox, e2e_tags

DOMAIN_VAR = "RAYITO_E2E_DOMAIN"
CERT_ARN_VAR = "RAYITO_E2E_CERT_ARN"

# `skipif` de módulo, no `pytest.skip()` en una fixture: así un entorno sin
# las variables se salta antes de montar ninguna fixture (ni el pre-flight de
# `conftest.py`, que ya llama a AWS).
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not (os.environ.get(DOMAIN_VAR) and os.environ.get(CERT_ARN_VAR)),
        reason=(
            f"este e2e necesita {DOMAIN_VAR} (un dominio cuyo comodín cubre el certificado) "
            f"y {CERT_ARN_VAR} (ese certificado ACM, en us-east-1)"
        ),
    ),
]
#: Opcional: sin ella, la etiqueta es el identificador aleatorio de la corrida.
ACCEPTANCE_RUN_TAG_VAR = "RAYITO_ACCEPTANCE_RUN_TAG"
ACCEPTANCE_RUN_TAG_KEY = "rayito:acceptance-run"

#: 4 bytes = 8 caracteres hexadecimales: improbable que dos corridas
#: coincidan, y la pila (`STACK_NAME_PREFIX` + 8) queda muy por debajo de
#: `MAX_STACK_NAME_LENGTH` (36).
RUN_ID_BYTES = 4
STACK_NAME_PREFIX = "rayito-cd-e2e-"
ROUTE_ALIAS_PREFIX = "e2e"
#: Una ruta (y un nombre alternativo) por test: cada uno registra la suya.
ROUTE_PURPOSES = ("http", "ws", "resume", "ttl")

ROUTE_PORT = 8000
#: Suficiente para que el test entero corra sin que la ruta caduque sola
#: (T25); muy por debajo de `MAX_TEST_SANDBOX_TIMEOUT_SECONDS`.
ROUTE_TTL_SECONDS = 1800
#: TTL corto del test de caducidad: deja ver la ruta (`STABLE_OBSERVATIONS`
#: respuestas seguidas más la propagación) y servir antes de que venza.
SHORT_ROUTE_TTL_SECONDS = 90
#: Cuánto antes de `SHORT_ROUTE_TTL_SECONDS` desde `_register` puede verse el
#: 404 por caducidad y aún ser una caducidad de verdad: `register()` guarda
#: `expires_at = int(clock()) + ttl` (trunca hasta 1 s) y la Function lo
#: compara con el reloj del edge. Criterio fijado antes de medir (no es una
#: medida de AWS): 5 s cubren el truncado más un desfase de reloj entre la
#: máquina de la prueba y el edge; un 404 más temprano es de un servidor sin
#: la ruta, no de su TTL.
EDGE_CLOCK_SKEW_TOLERANCE_SECONDS = 5.0
HTTPS_PORT = 443
HTTP_REQUEST_TIMEOUT_SECONDS = 20.0
WEBSOCKET_HANDSHAKE_TIMEOUT_SECONDS = 20.0
#: Tope de espera a que una escritura/borrado del KVS llegue al edge (DOM-5;
#: AWS documenta "segundos", AWS_API_NOTES.md §29), y entre sondeos.
PROPAGATION_TIMEOUT_SECONDS = 120.0
PROPAGATION_POLL_SECONDS = 1.0
#: Tope para que un sandbox pausado con auto-resume vuelva a servir (DOM-8).
AUTO_RESUME_TIMEOUT_SECONDS = 120.0
#: El mínimo que acepta `idlePolicy` (60) daría pausas espontáneas a mitad
#: de test; 300 s deja que sólo el `pause()` explícito suspenda.
RESUME_IDLE = IdlePolicy(max_idle_seconds=300, auto_resume=True)
#: GUID fijo de RFC 6455 §1.3, para calcular `Sec-WebSocket-Accept`.
WEBSOCKET_ACCEPT_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
#: El eco de WebSocket compartido con TypeScript y dónde se sube en el sandbox.
WEBSOCKET_ECHO_SERVER_SOURCE = (
    Path(__file__).resolve().parents[4] / "testdata" / "custom-domain" / "websocket_echo_server.py"
)
WEBSOCKET_ECHO_SERVER_PATH = "/home/user/websocket_echo_server.py"
#: Cuánto esperar a que el servidor del sandbox escuche en `ROUTE_PORT`, y
#: cada cuánto mirarlo (dentro del sandbox, con `/dev/tcp` de bash).
SERVER_LISTEN_TIMEOUT_SECONDS = 30
SERVER_LISTEN_POLL_SECONDS = 0.2
#: Respuestas seguidas que cuentan como "la ruta ya está en el edge".
STABLE_OBSERVATIONS = 5
TRAFFIC_TOKEN_HEADER = "e2b-traffic-access-token"
HTTP_OK = 200
HTTP_FORBIDDEN = 403
HTTP_NOT_FOUND = 404


#: El recurso de `infra/custom-domain.yaml` que crea la distribución, y lo
#: que deja CloudFormation en `ResourceStatusReason` cuando la pila no puede
#: llamar a `cloudfront:CreateDistribution`.
DISTRIBUTION_LOGICAL_ID = "Distribution"
CREATE_FAILED = "CREATE_FAILED"
ACCESS_DENIED_MARKERS = ("AccessDenied", "not authorized")
DISTRIBUTION_DENIED_REASON = "distribution permission error (needs cloudfront:CreateDistribution)"


def distribution_denied(region: str | None, stack_name: str) -> bool:
    """Si la pila falló por un error de permisos al crear la distribución
    (`DescribeStackEvents`, `AWS_API_NOTES.md` §21)."""
    cloudformation = boto3.session.Session(region_name=region).client("cloudformation")
    try:
        events = cloudformation.describe_stack_events(StackName=stack_name)["StackEvents"]
    except ClientError:
        return False
    return any(
        event.get("LogicalResourceId") == DISTRIBUTION_LOGICAL_ID
        and event.get("ResourceStatus") == CREATE_FAILED
        and any(marker in event.get("ResourceStatusReason", "") for marker in ACCESS_DENIED_MARKERS)
        for event in events
    )


def report(label: str, value: object) -> None:
    print(f"\n[custom-domain e2e] {label}: {value}", flush=True)


@dataclass(frozen=True)
class DomainUnderTest:
    """La distribución de esta corrida: la `CustomDomain`, el
    `*.cloudfront.net` al que se conecta el test (en lugar de resolver DNS)
    y el alias de ruta de cada test."""

    domain: CustomDomain
    connect_host: str
    aliases: dict[str, str]

    def host(self, purpose: str) -> str:
        return self.domain.host_for(self.aliases[purpose], ROUTE_PORT)


def _tls_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    # `create_default_context()` por sí solo todavía permite negociar
    # TLSv1/TLSv1.1 en algunas combinaciones de OpenSSL (CodeQL
    # py/insecure-protocol); CloudFront exige TLS 1.2+ de todos modos.
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def _tls_connect(host: str, connect_host: str, timeout: float) -> ssl.SSLSocket:
    """`curl --connect-to <host>:443:<connect_host>:443`: TCP contra
    `connect_host` (la distribución), `host` como SNI y el certificado
    validado contra `host` (el del dominio propio). Sin DNS para `host`."""
    raw_socket = socket.create_connection((connect_host, HTTPS_PORT), timeout=timeout)
    return _tls_context().wrap_socket(raw_socket, server_hostname=host)


class _ConnectToHTTPSConnection(http.client.HTTPSConnection):
    """`HTTPSConnection` a `host` (que también va como `Host`) sobre un
    socket abierto con `_tls_connect`."""

    def __init__(self, host: str, *, connect_host: str) -> None:
        super().__init__(host, HTTPS_PORT, timeout=HTTP_REQUEST_TIMEOUT_SECONDS)
        self._connect_host = connect_host

    def connect(self) -> None:
        self.sock = _tls_connect(self.host, self._connect_host, HTTP_REQUEST_TIMEOUT_SECONDS)


def _https_status(target: DomainUnderTest, host: str, headers: dict[str, str]) -> int:
    connection = _ConnectToHTTPSConnection(host, connect_host=target.connect_host)
    try:
        connection.request("GET", "/", headers=headers)
        return connection.getresponse().status
    finally:
        connection.close()


def _wait_for_status(
    target: DomainUnderTest,
    host: str,
    headers: dict[str, str],
    expected: int,
    timeout: float,
    *,
    consecutive: int = 1,
) -> float:
    """Sondea hasta que `host` responde `expected` `consecutive` veces
    seguidas; devuelve los segundos que tardó. Un 5xx, otro estado o un
    error de red mientras tanto (el origen aún despertando, DOM-8, o un
    servidor del edge al que aún no llegó el KVS) cuenta como "todavía no" y
    reinicia la cuenta."""
    started = time.monotonic()
    last: object = None
    seen = 0
    while time.monotonic() - started < timeout:
        try:
            last = _https_status(target, host, headers)
        except OSError as exc:
            last = repr(exc)
        seen = seen + 1 if last == expected else 0
        if seen >= consecutive:
            return time.monotonic() - started
        time.sleep(PROPAGATION_POLL_SECONDS)
    raise AssertionError(f"{host} no respondió {expected} en {timeout:.0f} s (último: {last!r})")


def _wait_until_route_visible(target: DomainUnderTest, host: str) -> float:
    """DOM-5 (escritura): una ruta con `traffic_token` que ya llegó al edge
    responde 403 a una petición sin el token, sin contactar el origen (antes
    de llegar, 404); así se mide sin consumir el origen. Hacen falta
    `STABLE_OBSERVATIONS` 403 seguidos: cada conexión puede caer en otro
    servidor del edge."""
    return _wait_for_status(
        target,
        host,
        {},
        HTTP_FORBIDDEN,
        PROPAGATION_TIMEOUT_SECONDS,
        consecutive=STABLE_OBSERVATIONS,
    )


def _wait_for_ok(target: DomainUnderTest, host: str, headers: dict[str, str]) -> float:
    return _wait_for_status(target, host, headers, HTTP_OK, PROPAGATION_TIMEOUT_SECONDS)


@pytest.fixture(scope="module")
def custom_domain(e2e_settings: E2ESettings) -> Iterator[DomainUnderTest]:
    """Una distribución real, desplegada una sola vez para todo el módulo
    (crear/destruir una distribución CloudFront es demasiado lento para
    hacerlo por test). El teardown es también la comprobación de que
    `destroy()` espera lo que de verdad tarda CloudFront
    (`CUSTOM_DOMAIN_WAIT_TIMEOUT_SECONDS`)."""
    public_domain, certificate_arn = os.environ[DOMAIN_VAR], os.environ[CERT_ARN_VAR]
    run_id = secrets.token_hex(RUN_ID_BYTES)
    run_tag = os.environ.get(ACCEPTANCE_RUN_TAG_VAR) or run_id
    stack_name = f"{STACK_NAME_PREFIX}{run_id}"
    domain = CustomDomain(
        public_domain=public_domain,
        stack_name=stack_name,
        region=e2e_settings.region,
    )
    aliases = {purpose: f"{ROUTE_ALIAS_PREFIX}-{run_id}-{purpose}" for purpose in ROUTE_PURPOSES}
    try:
        started = time.monotonic()
        hosts = [domain.host_for(alias, ROUTE_PORT) for alias in aliases.values()]
        try:
            status = domain.deploy(
                certificate_arn=certificate_arn,
                alternate_domain_names=hosts,
                tags={**e2e_tags(), ACCEPTANCE_RUN_TAG_KEY: run_tag},
            )
        except StackException:
            if distribution_denied(e2e_settings.region, stack_name):
                pytest.skip(DISTRIBUTION_DENIED_REASON)
            raise
        report("deploy() (s)", f"{time.monotonic() - started:.0f}")
        yield DomainUnderTest(
            domain=domain,
            connect_host=status.outputs[DISTRIBUTION_DOMAIN_NAME_OUTPUT_KEY],
            aliases=aliases,
        )
    finally:
        for alias in aliases.values():
            # Sin `KvsArn` (el deploy falló antes) no hay nada que borrar.
            with contextlib.suppress(CustomDomainException):
                domain.unregister(alias, ROUTE_PORT)
        started = time.monotonic()
        domain.destroy()
        report("destroy() (s)", f"{time.monotonic() - started:.0f}")
        assert domain.status() is None, "la pila de la corrida sigue existiendo tras destroy()"


def _register(
    target: DomainUnderTest, purpose: str, sandbox: Sandbox, ttl_seconds: int = ROUTE_TTL_SECONDS
) -> tuple[CustomDomainRoute, dict[str, str]]:
    """Registra la ruta de `purpose` hacia `sandbox` con un token nuevo;
    devuelve la ruta y las cabeceras que la autorizan."""
    traffic_token = secrets.token_urlsafe(32)
    route = target.domain.register(
        target.aliases[purpose],
        ROUTE_PORT,
        endpoint=sandbox.endpoint,
        jwe=_jwe(sandbox),
        traffic_token=traffic_token,
        ttl_seconds=ttl_seconds,
    )
    return route, {TRAFFIC_TOKEN_HEADER: traffic_token}


def _jwe(sandbox: Sandbox) -> str:
    return sandbox.get_host(ROUTE_PORT).headers["x-aws-proxy-auth"]


def _wait_until_listening(sandbox: Sandbox) -> None:
    """Espera, dentro del sandbox, a que algo escuche en `ROUTE_PORT`
    (`/dev/tcp` de bash, el shell de `commands.run`): un servidor que tarda
    en arrancar no se confunde con una ruta rota."""
    attempts = int(SERVER_LISTEN_TIMEOUT_SECONDS / SERVER_LISTEN_POLL_SECONDS)
    sandbox.commands.run(
        f"for _ in $(seq {attempts}); do "
        f"(exec 3<>/dev/tcp/127.0.0.1/{ROUTE_PORT}) 2>/dev/null && exit 0; "
        f"sleep {SERVER_LISTEN_POLL_SECONDS}; done; exit 1",
        timeout=SERVER_LISTEN_TIMEOUT_SECONDS * 2,
    )


def _start_in_background(sandbox: Sandbox, command: str) -> None:
    """`command` en segundo plano y sin timeout (`None`: el de 60 s por
    defecto de `commands.run` mataría el servidor a mitad de test), y espera a
    que escuche en `ROUTE_PORT`."""
    sandbox.commands.run(command, background=True, timeout=None)
    _wait_until_listening(sandbox)


def _start_http_server(sandbox: Sandbox) -> None:
    _start_in_background(sandbox, f"python3 -m http.server {ROUTE_PORT}")


def test_custom_domain_routes_a_registered_sandbox(
    custom_domain: DomainUnderTest, sandbox: Sandbox
) -> None:
    _start_http_server(sandbox)
    route, authorized = _register(custom_domain, "http", sandbox)
    visible = _wait_until_route_visible(custom_domain, route.host)
    report("DOM-5 register() -> visible en el edge (s)", f"{visible:.1f}")

    # DOM-2: HTTP/1.1 real a través de `cf.updateRequestOrigin`, con el token.
    _wait_for_ok(custom_domain, route.host, authorized)
    # SEC-T25: sin el token, 403 — nunca una ruta pública por omisión.
    _wait_for_status(custom_domain, route.host, {}, HTTP_FORBIDDEN, PROPAGATION_TIMEOUT_SECONDS)

    # DOM-7 (parcial): `refresh()` reescribe `j:`/`m:` sin cortar la ruta.
    refreshed = custom_domain.domain.refresh(
        route, jwe=_jwe(sandbox), ttl_seconds=ROUTE_TTL_SECONDS
    )
    assert refreshed.host == route.host
    _wait_for_ok(custom_domain, route.host, authorized)

    custom_domain.domain.unregister(route.alias, ROUTE_PORT)
    # Tras `unregister()`, 404 — indistinguible de una ruta que nunca existió.
    gone = _wait_for_status(
        custom_domain, route.host, authorized, HTTP_NOT_FOUND, PROPAGATION_TIMEOUT_SECONDS
    )
    report("DOM-5 unregister() -> 404 en el edge (s)", f"{gone:.1f}")


def test_a_route_past_its_ttl_answers_404(custom_domain: DomainUnderTest, sandbox: Sandbox) -> None:
    """T25 en el runtime real: la Function trata una ruta con `m.x` vencido
    como inexistente, aunque nadie la haya borrado del KVS. Los
    `STABLE_OBSERVATIONS` 404 seguidos tienen que llegar al menos
    `SHORT_ROUTE_TTL_SECONDS` (menos `EDGE_CLOCK_SKEW_TOLERANCE_SECONDS`)
    después de registrar la ruta: si no, es un servidor del edge sin la ruta,
    no su caducidad."""
    _start_http_server(sandbox)
    registered_at = time.monotonic()
    route, authorized = _register(
        custom_domain, "ttl", sandbox, ttl_seconds=SHORT_ROUTE_TTL_SECONDS
    )
    _wait_until_route_visible(custom_domain, route.host)
    _wait_for_status(custom_domain, route.host, authorized, HTTP_OK, SHORT_ROUTE_TTL_SECONDS)
    expired = _wait_for_status(
        custom_domain,
        route.host,
        authorized,
        HTTP_NOT_FOUND,
        SHORT_ROUTE_TTL_SECONDS + PROPAGATION_TIMEOUT_SECONDS,
        consecutive=STABLE_OBSERVATIONS,
    )
    since_registered = time.monotonic() - registered_at
    report("TTL vencido -> 404 (s desde visible)", f"{expired:.1f}")
    report("TTL vencido -> 404 (s desde register)", f"{since_registered:.1f}")
    assert since_registered >= SHORT_ROUTE_TTL_SECONDS - EDGE_CLOCK_SKEW_TOLERANCE_SECONDS, (
        f"{STABLE_OBSERVATIONS} 404 seguidos a los {since_registered:.1f} s de registrar la ruta, "
        f"antes de su TTL de {SHORT_ROUTE_TTL_SECONDS} s: no es una caducidad"
    )


def _recv_until(tls_socket: ssl.SSLSocket, done: Callable[[bytes], bool]) -> bytes:
    """Lee del socket hasta que `done(lo_leído)` o hasta que el otro extremo
    cierre: un `recv` puede traer sólo parte de las cabeceras o de la trama."""
    data = b""
    while not done(data):
        chunk = tls_socket.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


def _frame_complete(data: bytes) -> bool:
    return len(data) >= 2 and len(data) >= 2 + (data[1] & 0x7F)


def _websocket_handshake_and_echo(
    target: DomainUnderTest, host: str, headers: dict[str, str], message: bytes
) -> bytes:
    """Apertura de WebSocket mínima (RFC 6455) con un único frame de texto de
    ida y vuelta, sin más dependencia que la librería estándar: suficiente
    para DOM-3 (¿corre la Function en el upgrade? ¿el proxy de AWS acepta
    la cabecera de token en la petición de upgrade?), no un cliente de
    WebSocket de propósito general. Un upgrade rechazado es
    `WebSocketRejected` (se puede reintentar: el servidor del edge quizá aún
    no tenía la ruta)."""
    tls_socket = _tls_connect(host, target.connect_host, WEBSOCKET_HANDSHAKE_TIMEOUT_SECONDS)
    try:
        websocket_key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        extra_headers = "".join(f"{name}: {value}\r\n" for name, value in headers.items())
        request = (
            "GET / HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {websocket_key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"{extra_headers}\r\n"
        )
        tls_socket.sendall(request.encode("ascii"))
        response = _recv_until(tls_socket, lambda data: b"\r\n\r\n" in data)
        head, _, rest = response.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0].decode("ascii", "replace")
        if " 101 " not in status_line:
            raise WebSocketRejected(status_line)
        expected_accept = base64.b64encode(
            hashlib.sha1((websocket_key + WEBSOCKET_ACCEPT_GUID).encode("ascii")).digest()
        ).decode("ascii")
        if f"Sec-WebSocket-Accept: {expected_accept}".lower() not in head.lower().decode(
            "ascii", "replace"
        ):
            raise AssertionError("Sec-WebSocket-Accept no coincide con la clave enviada")
        tls_socket.sendall(_masked_text_frame(message))
        reply = rest + _recv_until(tls_socket, lambda data: _frame_complete(rest + data))
        payload_length = reply[1] & 0x7F
        return reply[2 : 2 + payload_length]
    finally:
        tls_socket.close()


class WebSocketRejected(AssertionError):
    """El upgrade no devolvió 101: la línea de estado que llegó."""


def _masked_text_frame(message: bytes) -> bytes:
    """Un único frame de texto cliente->servidor, enmascarado como exige RFC
    6455 §5.1 para toda trama que sale de un cliente, de menos de 126 bytes:
    la longitud cabe en el segundo byte sin extensión."""
    mask = secrets.token_bytes(4)
    masked_payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(message))
    return bytes([0x81, 0x80 | len(message)]) + mask + masked_payload


def _echo_with_retries(
    target: DomainUnderTest, host: str, headers: dict[str, str], message: bytes
) -> bytes:
    """`_websocket_handshake_and_echo` hasta que un servidor del edge con la
    ruta acepta el upgrade, dentro de `PROPAGATION_TIMEOUT_SECONDS`."""
    deadline = time.monotonic() + PROPAGATION_TIMEOUT_SECONDS
    while True:
        try:
            return _websocket_handshake_and_echo(target, host, headers, message)
        except (WebSocketRejected, OSError):
            if time.monotonic() >= deadline:
                raise
        time.sleep(PROPAGATION_POLL_SECONDS)


@pytest.fixture
def websocket_echo_sandbox(sandbox: Sandbox) -> Iterator[Sandbox]:
    """Un sandbox con el eco de WebSocket mínimo de
    `testdata/custom-domain/websocket_echo_server.py` (sólo `socket`, sin
    instalar nada) escuchando en `ROUTE_PORT`: lo justo para comprobar DOM-3
    de punta a punta, no un servidor de WebSocket real."""
    sandbox.files.write(
        WEBSOCKET_ECHO_SERVER_PATH, WEBSOCKET_ECHO_SERVER_SOURCE.read_text(encoding="utf-8")
    )
    _start_in_background(sandbox, f"python3 {WEBSOCKET_ECHO_SERVER_PATH} {ROUTE_PORT}")
    yield sandbox


def test_custom_domain_passes_a_websocket_upgrade(
    custom_domain: DomainUnderTest, websocket_echo_sandbox: Sandbox
) -> None:
    route, authorized = _register(custom_domain, "ws", websocket_echo_sandbox)
    _wait_until_route_visible(custom_domain, route.host)
    # DOM-3: ¿corre la Function en la petición de upgrade, y acepta el proxy
    # de AWS la cabecera de token ahí también?
    echoed = _echo_with_retries(custom_domain, route.host, authorized, b"ping")
    assert echoed == b"ping"


@pytest.fixture
def auto_resume_sandbox(
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
    boot_timings: BootTimings,
) -> Iterator[Sandbox]:
    created = create_test_sandbox(
        e2e_settings, control_plane, template_arn, boot_timings, idle=RESUME_IDLE
    )
    try:
        yield created
    finally:
        with contextlib.suppress(SandboxNotFoundException):
            created.kill()


def test_traffic_through_the_domain_resumes_a_paused_sandbox(
    custom_domain: DomainUnderTest, auto_resume_sandbox: Sandbox
) -> None:
    """DOM-8: con `auto_resume=True`, una petición por el dominio propio
    despierta un sandbox pausado (el proxy de AWS hace el resume; la
    Function no sabe nada del estado del VM)."""
    _start_http_server(auto_resume_sandbox)
    route, authorized = _register(custom_domain, "resume", auto_resume_sandbox)
    _wait_until_route_visible(custom_domain, route.host)
    _wait_for_ok(custom_domain, route.host, authorized)
    assert auto_resume_sandbox.pause()
    resumed = _wait_for_status(
        custom_domain, route.host, authorized, HTTP_OK, AUTO_RESUME_TIMEOUT_SECONDS
    )
    report("DOM-8 pausado -> 200 por el dominio (s)", f"{resumed:.1f}")


def test_stack_status_reports_the_deployed_distribution(custom_domain: DomainUnderTest) -> None:
    status = custom_domain.domain.status()
    assert status is not None
    assert status.state in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}
    assert "KvsArn" in status.outputs
    assert status.outputs[DISTRIBUTION_DOMAIN_NAME_OUTPUT_KEY] == custom_domain.connect_host
