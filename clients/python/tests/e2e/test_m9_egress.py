"""M9 `m9-egress-policy` contra AWS real (design D19, ADR-012): la política
de egress en el guest sobre una imagen M9 `rayito-base-caps`
(`RAYITO_TEMPLATE_CAPS`) y la compuerta fail-closed sobre la imagen por
defecto (`RAYITO_TEMPLATE`).

- `test_guest_network_facts` registra QE1 (resolver, `ip route get`, IPv6,
  alcance de las interfaces, reglas 100/150) y sólo exige que la sonda
  corra; las direcciones reales nunca se imprimen.
- Modo rutas: `allow_internet_access=False`, `deny_out` por IP con
  `allow_out` ganando, el ciclo de `update_network` en menos de 1 s y la
  política que sobrevive a `pause()`/`resume()`.
- Modo proxy: la allowlist por nombre de host, el guard del proxy local
  (IMDS y hooks nunca alcanzables) y el encadenado a un SOCKS5 del operador
  grabado por el propio test.
- `test_root_traffic_is_never_filtered` exporta con `files.download_url`
  bajo deny-all (`RAYITO_E2E_TRANSFER_BUCKET`/`RAYITO_E2E_TRANSFER_PREFIX`).
- `test_https_ports_measurement` es QE2 (`openssl` en el host del test) y el
  shim E2B (`rayito.e2b`) se prueba en sus variantes sync y async.

`RAYITO_EXECUTION_ROLE_ARN` activa `logging="cloudwatch"` y la comprobación
de que el log de `rayd` no contiene el usuario ni la dirección del proxy.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib
import ipaddress
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import boto3
import pytest

from rayito import (
    ALL_TRAFFIC,
    AsyncSandbox,
    EgressEnforcement,
    IdlePolicy,
    NetworkOptions,
    S3Staging,
    Sandbox,
    UnimplementedError,
)
from rayito._aws import ControlPlane, LambdaMicrovmsControlPlane
from rayito._models import SandboxInfo
from rayito.cli._logs import LogsNotFound, default_log_group, find_streams, iter_events
from rayito.exceptions import CommandExitException, SandboxNotFoundException

from .conftest import E2ESettings

pytestmark = pytest.mark.e2e

CAPS_TEMPLATE_VAR = "RAYITO_TEMPLATE_CAPS"
TRANSFER_BUCKET_VAR = "RAYITO_E2E_TRANSFER_BUCKET"
TRANSFER_PREFIX_VAR = "RAYITO_E2E_TRANSFER_PREFIX"
CAPS_IMAGE = "rayito-base-caps"
SANDBOX_TIMEOUT_SECONDS = 900
PAUSE_IDLE = IdlePolicy(max_idle_seconds=300, auto_resume=True)
COMMAND_TIMEOUT_SECONDS = 60
REFUSAL_BUDGET_SECONDS = 6.0
POLICY_SWITCH_BUDGET_SECONDS = 1.0
TERMINATE_BUDGET_SECONDS = 60.0
POLL_SECONDS = 1.0
HTTP_TIMEOUT_SECONDS = 20
HTTP_READY_TIMEOUT_SECONDS = 30.0
LOCAL_SERVER_PORT = 8000
HTTPS_PORT = 8443
SOCKS_PORT = 1080
SOCKS_USERNAME = "rayito-e2e"
SOCKS_PASSWORD_ENV = "RAYITO_E2E_SOCKS_PASSWORD"
DOWNLOAD_BYTES = 1024 * 1024
ALLOWED_HOST = "aws.amazon.com"
DENIED_HOST = "example.com"
IMDS_URL = "http://169.254.169.254/latest/meta-data/"
HOOKS_URL = "http://127.0.0.1:9000/"
QE2_BODY = b"rayito-qe2"
QE2_DIR = "/home/user/qe2"
SOCKS_SCRIPT = "/home/user/socks_recorder.py"
SOCKS_LOG = "/home/user/socks_recorder.log"
IPV4_LITERAL = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")
UNMASKED = frozenset({"1.1.1.1", "127.0.0.1", "169.254.169.254", "0.0.0.0"})
NO_PROXY_ENV_UNSET = "env -u NO_PROXY -u no_proxy"
CURL_STATUS = "curl -sS -o /dev/null -w '%{http_code}' -m 15"
SOCKS_METHODS = re.compile(r"methods=([0-9a-f]+)")
LOGS_WAIT_SECONDS = 90.0
LOGS_POLL_SECONDS = 5.0

TCP_PROBE = (
    "python3 -c 'import socket, sys; "
    "socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=5).close()' {host} {port}"
)
URLLIB_PROBE = (
    'python3 -c "import urllib.request; '
    "print(urllib.request.urlopen('https://{host}', timeout=5).status)\""
)
DNS_PROBE = "python3 -c \"import socket; socket.getaddrinfo('{host}', 443)\""
# Adenda de ADR-012 (QE1, fila Q66): en caps los resolvedores de la plataforma
# escuchan dentro del guest, así que bajo deny-all el nombre puede resolverse.
# Lo que se exige es que ninguna dirección resuelta sea alcanzable: sale con 0
# e imprime `unresolved` o `blocked`, y con 1 (`connected`) si alguna conecta.
RESOLVE_CONNECT_PROBE = """\
import socket, sys
try:
    infos = socket.getaddrinfo(sys.argv[1], 443, socket.AF_INET, socket.SOCK_STREAM)
except OSError:
    print("unresolved")
    sys.exit(0)
for info in infos:
    try:
        socket.create_connection(info[4][:2], timeout=5).close()
    except OSError:
        continue
    print("connected")
    sys.exit(1)
print("blocked")
"""
SWITCH_PROBE = """\
import socket, sys, time
host, want_open = sys.argv[1], sys.argv[2] == "open"
start = time.monotonic()
while True:
    try:
        socket.create_connection((host, 443), timeout=0.5).close()
        is_open = True
    except OSError:
        is_open = False
    elapsed = time.monotonic() - start
    if is_open == want_open:
        print(f"{elapsed:.3f}")
        sys.exit(0)
    if elapsed > 5.0:
        print(f"timeout {elapsed:.3f}")
        sys.exit(1)
    time.sleep(0.05)
"""
HTTPS_SERVER = f"""\
import http.server, ssl
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain("{QE2_DIR}/cert.pem", "{QE2_DIR}/key.pem")
context.set_alpn_protocols(["http/1.1"])
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = {QE2_BODY!r}
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
server = http.server.HTTPServer(("0.0.0.0", {HTTPS_PORT}), Handler)
server.socket = context.wrap_socket(server.socket, server_side=True)
server.serve_forever()
"""
SOCKS_RECORDER = f"""\
import os, socket, sys
address, port, log_path = sys.argv[1], int(sys.argv[2]), sys.argv[3]
expected_password = os.environ["{SOCKS_PASSWORD_ENV}"].encode()
def read_exact(conn, size):
    data = b""
    while len(data) < size:
        chunk = conn.recv(size - len(data))
        if not chunk:
            raise EOFError
        data += chunk
    return data
def record(line):
    with open(log_path, "a") as log:
        log.write(line + "\\n")
def serve(conn):
    version, count = read_exact(conn, 2)
    methods = read_exact(conn, count)
    record("methods=" + methods.hex())
    conn.sendall(b"\\x05\\x02")
    read_exact(conn, 1)
    username = read_exact(conn, read_exact(conn, 1)[0]).decode()
    password = read_exact(conn, read_exact(conn, 1)[0])
    record(f"username={{username}} password_ok={{password == expected_password}}")
    conn.sendall(b"\\x01\\x00")
    _, command, _, atyp = read_exact(conn, 4)
    if atyp == 3:
        host = read_exact(conn, read_exact(conn, 1)[0]).decode()
    elif atyp == 1:
        host = socket.inet_ntop(socket.AF_INET, read_exact(conn, 4))
    else:
        host = socket.inet_ntop(socket.AF_INET6, read_exact(conn, 16))
    target_port = int.from_bytes(read_exact(conn, 2), "big")
    record(f"command={{command:02x}} atyp={{atyp:02x}} host={{host}} port={{target_port}}")
    conn.sendall(b"\\x05\\x05\\x00\\x01" + bytes(6))
listener = socket.create_server((address, port))
while True:
    conn, _ = listener.accept()
    try:
        serve(conn)
    except (EOFError, OSError, ValueError):
        record("aborted")
    finally:
        conn.close()
"""


@dataclass(frozen=True)
class ProbeOutcome:
    exit_code: int
    stdout: str
    stderr: str
    seconds: float

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def report(label: str, value: object) -> None:
    print(f"\n[m9-egress] {label}: {value}", flush=True)


def masked(text: str) -> str:
    """Salida de `ip` sin las direcciones del guest (placeholders en la
    fila de `AWS_API_NOTES.md`); deja las sondas conocidas."""
    return IPV4_LITERAL.sub(
        lambda match: match.group(1) if match.group(1) in UNMASKED else "<ip>", text
    )


def run(sandbox: Sandbox, command: str, *, user: str | None = None) -> ProbeOutcome:
    """Un comando que puede fallar: el exit code es el dato, nunca una
    excepción."""
    started = time.perf_counter()
    try:
        result = sandbox.commands.run(command, user=user, timeout=COMMAND_TIMEOUT_SECONDS)
    except CommandExitException as exc:
        return ProbeOutcome(exc.exit_code, exc.stdout, exc.stderr, time.perf_counter() - started)
    return ProbeOutcome(0, result.stdout, result.stderr, time.perf_counter() - started)


def tcp_open(sandbox: Sandbox, host: str, port: int = 443) -> bool:
    return run(sandbox, TCP_PROBE.format(host=host, port=port)).ok


def first_ipv4(host: str) -> str:
    return str(socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)[0][4][0])


def caps_template() -> str:
    template = os.environ.get(CAPS_TEMPLATE_VAR) or None
    if template is None:
        pytest.skip(f"exporta {CAPS_TEMPLATE_VAR}=<arn|nombre> (imagen M9 {CAPS_IMAGE})")
    return template


def transfer_staging() -> S3Staging:
    bucket = os.environ.get(TRANSFER_BUCKET_VAR) or None
    if bucket is None:
        pytest.skip(f"exporta {TRANSFER_BUCKET_VAR} (y {TRANSFER_PREFIX_VAR}) para download_url")
    prefix = os.environ.get(TRANSFER_PREFIX_VAR) or None
    return S3Staging(bucket) if prefix is None else S3Staging(bucket, prefix=prefix)


@contextlib.contextmanager
def launched(
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    template: str,
    **kwargs: Any,
) -> Iterator[Sandbox]:
    """Un sandbox de test (900 s, sin idle salvo que se pase) terminado en
    teardown; imprime el `kernel_ready_s` con la política ya aplicada."""
    kwargs.setdefault("idle", None)
    started = time.perf_counter()
    created = Sandbox.create(
        control_plane.resolve_template_arn(template),
        timeout=SANDBOX_TIMEOUT_SECONDS,
        execution_role_arn=e2e_settings.execution_role_arn,
        ingress=["ALL_INGRESS"],
        logging=e2e_settings.logging,
        control_plane=control_plane,
        **kwargs,
    )
    report(f"{created.sandbox_id} create() with policy (s)", f"{time.perf_counter() - started:.2f}")
    try:
        yield created
    finally:
        with contextlib.suppress(SandboxNotFoundException):
            created.kill()


def local_proxy_port(sandbox: Sandbox) -> int:
    port = sandbox.get_network().local_proxy_port
    assert port is not None, "el proxy local de rayd debería estar corriendo"
    return port


def http_status(url: str, headers: dict[str, str]) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as error:
        return int(error.code), b""


def wait_for_status(url: str, headers: dict[str, str]) -> int:
    deadline = time.monotonic() + HTTP_READY_TIMEOUT_SECONDS
    status = 0
    while time.monotonic() < deadline:
        with contextlib.suppress(urllib.error.URLError, TimeoutError, OSError):
            status, _ = http_status(url, headers)
            if status == 200:
                return status
        time.sleep(POLL_SECONDS)
    return status


def assert_imds_refused_by_proxy(sandbox: Sandbox) -> None:
    port = local_proxy_port(sandbox)
    outcome = run(
        sandbox,
        f"{NO_PROXY_ENV_UNSET} curl -s -o /dev/null -w '%{{http_code}}' -m 10 "
        f"-x http://127.0.0.1:{port} {IMDS_URL}",
    )
    report("proxy -> IMDS http_code", outcome.stdout.strip())
    assert outcome.stdout.strip() == "403"


# ------------------------------------------------------------------- QE1


def resolver_kinds(resolv_conf: str) -> list[str]:
    kinds: list[str] = []
    for line in resolv_conf.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0] != "nameserver":
            continue
        address = ipaddress.ip_address(parts[1].split("%")[0])
        if address.is_loopback:
            kinds.append("loopback")
        elif address.is_link_local:
            kinds.append("link-local")
        elif address.is_private:
            kinds.append("private")
        else:
            kinds.append("other")
    return kinds


def interface_scopes(ip_addr_output: str) -> list[str]:
    """`scope` y longitud de prefijo de cada dirección, nunca la dirección."""
    scopes: list[str] = []
    for line in ip_addr_output.splitlines():
        fields = line.split()
        if "inet" not in fields and "inet6" not in fields:
            continue
        family = "inet6" if "inet6" in fields else "inet"
        prefix = fields[fields.index(family) + 1].split("/")[-1]
        scope = fields[fields.index("scope") + 1] if "scope" in fields else "?"
        scopes.append(f"{fields[1]} {family} /{prefix} scope {scope}")
    return scopes


def test_guest_network_facts(
    e2e_settings: E2ESettings, control_plane: LambdaMicrovmsControlPlane
) -> None:
    with launched(e2e_settings, control_plane, caps_template(), allow_internet_access=False) as sbx:
        resolv = run(sbx, "cat /etc/resolv.conf")
        report("QE1 (a) nameserver kinds", resolver_kinds(resolv.stdout))
        dns = run(sbx, DNS_PROBE.format(host=ALLOWED_HOST))
        report("QE1 (b) getaddrinfo as uid 1000 under deny-all", f"exit={dns.exit_code}")
        for target, uid in (("1.1.1.1", 1000), ("127.0.0.1", 1000), ("1.1.1.1", 0)):
            probe = run(sbx, f"ip route get {target} uid {uid}")
            report(
                f"QE1 (c) ip route get {target} uid {uid}",
                f"exit={probe.exit_code} out={masked(probe.stdout.strip())!r} "
                f"err={masked(probe.stderr.strip())!r}",
            )
        inet6 = run(sbx, "cat /proc/net/if_inet6 | wc -l; ip -6 route show default | wc -l")
        report("QE1 (d) if_inet6 lines / default v6 routes", inet6.stdout.split())
        addresses = run(sbx, "ip -o addr show")
        report("QE1 (e) interface scopes", interface_scopes(addresses.stdout))
        rules = run(sbx, "ip -4 rule show")
        report(
            "QE1 (f) ip rule priorities", [line.split(":")[0] for line in rules.stdout.splitlines()]
        )
        imds_route = run(sbx, "ip route get 169.254.169.254 uid 0")
        report(
            "QE1 (f) ip route get IMDS uid 0",
            f"exit={imds_route.exit_code} out={masked(imds_route.stdout.strip())!r}",
        )
        user_imds = run(sbx, f"curl -s -o /dev/null -w '%{{http_code}}' -m 3 {IMDS_URL}")
        report(
            "QE1 (f) IMDS uid 1000",
            f"http_code={user_imds.stdout.strip()} exit={user_imds.exit_code}",
        )
        assert rules.ok and resolv.ok


# ------------------------------------------------------------- routes mode


def test_internet_off(e2e_settings: E2ESettings, control_plane: LambdaMicrovmsControlPlane) -> None:
    with launched(
        e2e_settings,
        control_plane,
        caps_template(),
        allow_internet_access=False,
        allowed_ports=[LOCAL_SERVER_PORT],
    ) as sbx:
        blocked = run(sbx, URLLIB_PROBE.format(host=ALLOWED_HOST))
        report("urllib https as uid 1000 refused in (s)", f"{blocked.seconds:.2f}")
        assert not blocked.ok
        assert blocked.seconds < REFUSAL_BUDGET_SECONDS
        sbx.files.write("/home/user/resolve_connect_probe.py", RESOLVE_CONNECT_PROBE)
        unreachable = run(sbx, f"python3 /home/user/resolve_connect_probe.py {ALLOWED_HOST}")
        report("resolve + connect as uid 1000 under deny-all", unreachable.stdout.strip())
        assert unreachable.ok, unreachable.stdout
        assert unreachable.stdout.strip() in {"unresolved", "blocked"}
        server = sbx.commands.run(
            f"python3 -m http.server {LOCAL_SERVER_PORT} --bind 127.0.0.1",
            background=True,
            timeout=None,
        )
        try:
            host = sbx.get_host(LOCAL_SERVER_PORT)
            assert wait_for_status(host.url, host.headers) == 200
        finally:
            server.kill()
        assert sbx.get_health().egress_enforcement is EgressEnforcement.GUEST_ROUTES
        assert sbx.get_network().deny_out == (ALL_TRAFFIC,)


def test_root_traffic_is_never_filtered(
    e2e_settings: E2ESettings, control_plane: LambdaMicrovmsControlPlane
) -> None:
    staging = transfer_staging()
    payload = secrets.token_bytes(DOWNLOAD_BYTES)
    with launched(
        e2e_settings,
        control_plane,
        caps_template(),
        allow_internet_access=False,
        transfer=staging,
    ) as sbx:
        sbx.files.write("/home/user/export.bin", payload)
        link = sbx.files.download_url("/home/user/export.bin")
        with urllib.request.urlopen(str(link), timeout=HTTP_TIMEOUT_SECONDS) as response:
            fetched = response.read()
        assert hashlib.sha256(fetched).hexdigest() == hashlib.sha256(payload).hexdigest()
        assert sbx.get_health().egress_enforcement is EgressEnforcement.GUEST_ROUTES


class RecordingControlPlane:
    """El plano real que además recuerda cada `sandbox_id` lanzado: la
    compuerta termina el VM antes de que `create()` devuelva nada."""

    def __init__(self, plane: LambdaMicrovmsControlPlane) -> None:
        self._plane = plane
        self.launched: list[str] = []

    def run_microvm(self, request: Any) -> Any:
        info = self._plane.run_microvm(request)
        self.launched.append(info.sandbox_id)
        return info

    def __getattr__(self, name: str) -> Any:
        return getattr(self._plane, name)


def wait_terminated(plane: LambdaMicrovmsControlPlane, sandbox_id: str) -> float:
    started = time.perf_counter()
    while time.perf_counter() - started < TERMINATE_BUDGET_SECONDS:
        try:
            state = plane.get_microvm(sandbox_id).state
        except SandboxNotFoundException:
            state = "TERMINATED"
        if state == "TERMINATED":
            return time.perf_counter() - started
        time.sleep(POLL_SECONDS)
    pytest.fail(f"{sandbox_id} no llegó a TERMINATED en {TERMINATE_BUDGET_SECONDS:g} s")


@pytest.mark.parametrize("keep_on_failure", [False, True])
def test_default_image_fails_closed(
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
    keep_on_failure: bool,
) -> None:
    recording = RecordingControlPlane(control_plane)
    with pytest.raises(UnimplementedError) as excinfo:
        Sandbox.create(
            template_arn,
            template_version=e2e_settings.template_version,
            timeout=SANDBOX_TIMEOUT_SECONDS,
            idle=None,
            allow_internet_access=False,
            keep_on_failure=keep_on_failure,
            control_plane=cast("ControlPlane", recording),
        )
    assert CAPS_IMAGE in excinfo.value.reason
    assert len(recording.launched) == 1
    elapsed = wait_terminated(control_plane, recording.launched[0])
    report(
        f"default image fail-closed (keep_on_failure={keep_on_failure}) TERMINATED in (s)",
        f"{elapsed:.1f}",
    )


def test_deny_ip_and_allow_wins(
    e2e_settings: E2ESettings, control_plane: LambdaMicrovmsControlPlane
) -> None:
    denied, other = first_ipv4(DENIED_HOST), first_ipv4(ALLOWED_HOST)
    with launched(
        e2e_settings, control_plane, caps_template(), network={"deny_out": [f"{denied}/32"]}
    ) as sbx:
        assert not tcp_open(sbx, denied)
        assert tcp_open(sbx, other)
        sbx.update_network({"deny_out": [f"{denied}/24"], "allow_out": [f"{denied}/32"]})
        assert tcp_open(sbx, denied)
        sbx.update_network({"deny_out": [ALL_TRAFFIC], "allow_out": [f"{denied}/32"]})
        assert tcp_open(sbx, denied)
        assert not tcp_open(sbx, other)


def policy_switch_seconds(sandbox: Sandbox, host: str, *, want_open: bool) -> float:
    sandbox.files.write("/home/user/switch_probe.py", SWITCH_PROBE)
    outcome = run(
        sandbox, f"python3 /home/user/switch_probe.py {host} {'open' if want_open else 'closed'}"
    )
    assert outcome.ok, outcome.stdout
    return float(outcome.stdout.strip())


def test_update_network_cycle(
    e2e_settings: E2ESettings, control_plane: LambdaMicrovmsControlPlane, sandbox: Sandbox
) -> None:
    target = first_ipv4(ALLOWED_HOST)
    with launched(e2e_settings, control_plane, caps_template()) as sbx:
        assert policy_switch_seconds(sbx, target, want_open=True) <= POLICY_SWITCH_BUDGET_SECONDS
        closed = sbx.update_network(allow_internet_access=False)
        assert closed.enforcement is EgressEnforcement.GUEST_ROUTES
        switch_off = policy_switch_seconds(sbx, target, want_open=False)
        reopened = sbx.update_network(None)
        assert reopened.deny_out == ()
        switch_on = policy_switch_seconds(sbx, target, want_open=True)
        report("update_network deny-all took effect in (s)", f"{switch_off:.3f}")
        report("update_network allow-all took effect in (s)", f"{switch_on:.3f}")
        assert switch_off <= POLICY_SWITCH_BUDGET_SECONDS
        assert switch_on <= POLICY_SWITCH_BUDGET_SECONDS
    with pytest.raises(UnimplementedError):
        sandbox.update_network({"deny_out": [ALL_TRAFFIC]})


def test_policy_survives_pause_resume(
    e2e_settings: E2ESettings, control_plane: LambdaMicrovmsControlPlane
) -> None:
    denied, allowed = first_ipv4(DENIED_HOST), first_ipv4(ALLOWED_HOST)
    policy: NetworkOptions = {"deny_out": [ALL_TRAFFIC], "allow_out": [f"{allowed}/32"]}
    with launched(
        e2e_settings, control_plane, caps_template(), network=policy, idle=PAUSE_IDLE
    ) as sbx:
        before = sbx.get_network()
        assert sbx.pause() is True
        sbx.resume()
        assert sbx.get_health().egress_enforcement is EgressEnforcement.GUEST_ROUTES
        assert sbx.get_network() == before
        assert tcp_open(sbx, allowed)
        assert not tcp_open(sbx, denied)


# -------------------------------------------------------------- proxy mode


def curl_status(sandbox: Sandbox, url: str, *extra: str) -> ProbeOutcome:
    return run(sandbox, " ".join((CURL_STATUS, *extra, url)))


def test_hostname_allowlist(
    e2e_settings: E2ESettings, control_plane: LambdaMicrovmsControlPlane
) -> None:
    with launched(
        e2e_settings,
        control_plane,
        caps_template(),
        network={"allow_out": [ALLOWED_HOST], "deny_out": [ALL_TRAFFIC]},
    ) as sbx:
        assert sbx.get_health().egress_enforcement is EgressEnforcement.GUEST_ROUTES_AND_PROXY
        assert curl_status(sbx, f"https://{ALLOWED_HOST}").stdout.strip() == "200"
        refused = curl_status(sbx, f"https://{DENIED_HOST}")
        report("curl to a denied host", masked(refused.stderr.strip()))
        assert not refused.ok and "403" in refused.stderr
        assert not curl_status(sbx, f"https://{ALLOWED_HOST}", "--noproxy '*'").ok
        assert not tcp_open(sbx, "1.1.1.1")
        honoured = run(sbx, URLLIB_PROBE.format(host=ALLOWED_HOST))
        assert honoured.ok and honoured.stdout.strip() == "200"


def test_proxy_guard(e2e_settings: E2ESettings, control_plane: LambdaMicrovmsControlPlane) -> None:
    template = caps_template()
    with launched(
        e2e_settings,
        control_plane,
        template,
        network={"allow_out": [ALL_TRAFFIC, DENIED_HOST], "deny_out": [ALL_TRAFFIC]},
    ) as sbx:
        assert_imds_refused_by_proxy(sbx)
        port = local_proxy_port(sbx)
        hooks = run(
            sbx,
            f"{NO_PROXY_ENV_UNSET} curl -s -o /dev/null -w '%{{http_connect}}' -m 10 -p "
            f"-x http://127.0.0.1:{port} {HOOKS_URL}",
        )
        report("proxy CONNECT to the hooks port", f"exit={hooks.exit_code} {hooks.stdout.strip()}")
        assert not hooks.ok and "403" in hooks.stdout
        socks = run(
            sbx,
            f"{NO_PROXY_ENV_UNSET} curl -s -m 10 --socks5-hostname 127.0.0.1:{port} "
            "http://169.254.169.254/",
        )
        assert not socks.ok
    with launched(e2e_settings, control_plane, template, allow_internet_access=False) as routes:
        assert_imds_refused_by_proxy(routes)


def guest_address(sandbox: Sandbox) -> str:
    """La dirección privada del VM (`scope global`, o `scope link` que no
    sea IMDS); nunca se imprime."""
    for scope in ("global", "link"):
        listing = run(sandbox, f"ip -4 -o addr show scope {scope}")
        for match in re.finditer(r"inet (\d{1,3}(?:\.\d{1,3}){3})/", listing.stdout):
            if match.group(1) != "169.254.169.254":
                return match.group(1)
    pytest.fail("el VM no tiene una dirección IPv4 no loopback")


def read_rayd_log(logs: Any, info: SandboxInfo) -> list[str]:
    group = default_log_group(info.template)
    try:
        streams = find_streams(logs, group, info)
    except LogsNotFound:
        return []
    lines: list[str] = []
    for stream in streams:
        lines.extend(str(event.get("message", "")) for event in iter_events(logs, group, stream))
    return lines


def rayd_log_lines(control_plane: LambdaMicrovmsControlPlane, sandbox: Sandbox) -> list[str]:
    """El stream de CloudWatch del sandbox en cuanto tiene eventos (la
    ingesta tarda); vacío sólo si nunca llegaron en `LOGS_WAIT_SECONDS`."""
    logs = boto3.client("logs", region_name=control_plane.region)
    info = sandbox.get_info()
    deadline = time.monotonic() + LOGS_WAIT_SECONDS
    lines = read_rayd_log(logs, info)
    while not lines and time.monotonic() < deadline:
        time.sleep(LOGS_POLL_SECONDS)
        lines = read_rayd_log(logs, info)
    return lines


def test_egress_proxy_chain(
    e2e_settings: E2ESettings, control_plane: LambdaMicrovmsControlPlane
) -> None:
    password = secrets.token_urlsafe(16)
    with launched(e2e_settings, control_plane, caps_template()) as sbx:
        address = guest_address(sbx)
        sbx.files.write(SOCKS_SCRIPT, SOCKS_RECORDER)
        recorder = sbx.commands.run(
            f"python3 {SOCKS_SCRIPT} {address} {SOCKS_PORT} {SOCKS_LOG}",
            background=True,
            envs={SOCKS_PASSWORD_ENV: password},
            timeout=None,
        )
        try:
            state = sbx.update_network(
                {
                    "allow_out": [ALLOWED_HOST],
                    "deny_out": [ALL_TRAFFIC],
                    "egress_proxy": {
                        "address": f"{address}:{SOCKS_PORT}",
                        "username": SOCKS_USERNAME,
                        "password": password,
                    },
                }
            )
            assert state.egress_proxy_configured
            run(sbx, f"curl -sS -o /dev/null -m 15 https://{ALLOWED_HOST}")
            log = sbx.files.read(SOCKS_LOG)
            offered = SOCKS_METHODS.search(log)
            assert offered is not None and "02" in offered.group(1)
            assert f"username={SOCKS_USERNAME} password_ok=True" in log
            assert f"atyp=03 host={ALLOWED_HOST} port=443" in log
            assert not curl_status(sbx, f"https://{ALLOWED_HOST}", "--noproxy '*'").ok
        finally:
            recorder.kill()
        if e2e_settings.execution_role_arn:
            lines = rayd_log_lines(control_plane, sbx)
            report("rayd log lines checked for proxy data", len(lines))
            assert lines, "sin eventos de rayd en CloudWatch"
            assert not any(SOCKS_USERNAME in line or address in line for line in lines)


# ------------------------------------------------------------- QE2 + shim


def self_signed_certificate(directory: Path) -> tuple[bytes, bytes]:
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.fail("QE2 necesita `openssl` en el host del test para el certificado desechable")
    key, cert = directory / "key.pem", directory / "cert.pem"
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=rayito-qe2",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    return cert.read_bytes(), key.read_bytes()


def https_through_proxy(url: str, headers: dict[str, str]) -> tuple[int, bool]:
    try:
        status, body = http_status(url, headers)
    except (urllib.error.URLError, TimeoutError, OSError, ssl.SSLError):
        return 0, False
    return status, body == QE2_BODY


def e2b_compat() -> Any:
    return importlib.import_module("rayito.e2b._compat")


def test_https_ports_measurement(
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
    tmp_path: Path,
) -> None:
    cert, key = self_signed_certificate(tmp_path)
    with launched(e2e_settings, control_plane, template_arn, allowed_ports=[HTTPS_PORT]) as sbx:
        sbx.files.make_dir(QE2_DIR)
        sbx.files.write(f"{QE2_DIR}/cert.pem", cert)
        sbx.files.write(f"{QE2_DIR}/key.pem", key)
        sbx.files.write(f"{QE2_DIR}/server.py", HTTPS_SERVER)
        server = sbx.commands.run(f"python3 {QE2_DIR}/server.py", background=True, timeout=None)
        try:
            time.sleep(POLL_SECONDS)
            host = sbx.get_host(HTTPS_PORT)
            plain = https_through_proxy(host.url, host.headers)
            forced = https_through_proxy(host.url, {**host.headers, "x-aws-proxy-force-h2": "true"})
        finally:
            server.kill()
    report("QE2 GET / via get_host(8443) (status, body arrived)", plain)
    report("QE2 GET / with x-aws-proxy-force-h2 (status, body arrived)", forced)
    measured = any(status == 200 and body for status, body in (plain, forced))
    report("QE2 https_ports supported", measured)
    supported = getattr(e2b_compat(), "HTTPS_PORTS_SUPPORTED", None)
    if supported is None:
        pytest.skip("HTTPS_PORTS_SUPPORTED aún no existe en rayito.e2b._compat (tarea 7.2)")
    assert supported is measured


def e2b_shim() -> Any:
    shim = importlib.import_module("rayito.e2b")
    if not hasattr(shim, "ALL_TRAFFIC") or not hasattr(e2b_compat(), "map_network"):
        pytest.skip("el shim E2B aún no mapea network= (m9-egress-policy tareas 7.x)")
    return shim


def shim_kwargs(
    e2e_settings: E2ESettings, control_plane: LambdaMicrovmsControlPlane
) -> dict[str, Any]:
    return {
        "timeout": SANDBOX_TIMEOUT_SECONDS,
        "execution_role_arn": e2e_settings.execution_role_arn,
        "logging": e2e_settings.logging,
        "control_plane": control_plane,
    }


def test_e2b_shim_sync(
    e2e_settings: E2ESettings, control_plane: LambdaMicrovmsControlPlane
) -> None:
    shim = e2b_shim()
    template = control_plane.resolve_template_arn(caps_template())
    offline = shim.Sandbox.create(
        template=template, allow_internet_access=False, **shim_kwargs(e2e_settings, control_plane)
    )
    try:
        assert not run(offline.native, URLLIB_PROBE.format(host=ALLOWED_HOST)).ok
    finally:
        offline.kill()
    sbx = shim.Sandbox.create(
        template=template,
        network={"allow_out": [ALLOWED_HOST], "deny_out": lambda ctx: [ctx.all_traffic]},
        **shim_kwargs(e2e_settings, control_plane),
    )
    try:
        native = sbx.native
        assert curl_status(native, f"https://{ALLOWED_HOST}").stdout.strip() == "200"
        assert "403" in curl_status(native, f"https://{DENIED_HOST}").stderr
        assert sbx.update_network({}) is None
        assert tcp_open(native, first_ipv4(DENIED_HOST))
        closed = shim.Sandbox.update_network(
            sbx.sandbox_id,
            {"deny_out": [shim.ALL_TRAFFIC]},
            access_token=native.access_token,
            control_plane=control_plane,
        )
        assert closed is None
        assert not tcp_open(native, first_ipv4(DENIED_HOST))
    finally:
        sbx.kill()


async def async_probe_ok(sandbox: AsyncSandbox, command: str) -> bool:
    try:
        await sandbox.commands.run(command, timeout=COMMAND_TIMEOUT_SECONDS)
    except CommandExitException:
        return False
    return True


def test_e2b_shim_async(
    e2e_settings: E2ESettings, control_plane: LambdaMicrovmsControlPlane
) -> None:
    shim = e2b_shim()
    template = control_plane.resolve_template_arn(caps_template())
    kwargs = shim_kwargs(e2e_settings, control_plane)

    async def scenario() -> None:
        sbx = await shim.AsyncSandbox.create(
            template=template, allow_internet_access=False, **kwargs
        )
        try:
            native = sbx.native
            denied = first_ipv4(DENIED_HOST)
            assert not await async_probe_ok(native, TCP_PROBE.format(host=denied, port=443))
            assert await sbx.update_network({}) is None
            assert await async_probe_ok(native, TCP_PROBE.format(host=denied, port=443))
            assert (
                await shim.AsyncSandbox.update_network(
                    sbx.sandbox_id,
                    {"deny_out": [shim.ALL_TRAFFIC]},
                    access_token=native.access_token,
                    control_plane=control_plane,
                )
                is None
            )
            assert not await async_probe_ok(native, TCP_PROBE.format(host=denied, port=443))
        finally:
            await sbx.kill()

    asyncio.run(scenario())
