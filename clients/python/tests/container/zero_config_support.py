"""Lo común a `test_agent_zero_config_container.py`: el manifiesto de los
agentes de ejemplo, el Docker del host y el Bedrock falso que contesta en
nombre del modelo.

Variables de entorno (todas opcionales):

- `RAYITO_ZC_SAMPLES`: ids del manifiesto separados por comas; sin ella corren
  los quince.
- `RAYITO_ZC_ENDPOINT_HOST`: el nombre con el que el contenedor alcanza el
  Bedrock falso del host (`host.docker.internal` por defecto; con Lima,
  `host.lima.internal`).
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final, NamedTuple

from rayito._agent._project_domain import AgentProject, ServeOptions

REPO_ROOT: Final = Path(__file__).resolve().parents[4]
SAMPLES_DIR: Final = REPO_ROOT / "testdata" / "agent-samples"
PROVIDERS_DIR: Final = REPO_ROOT / "dev" / "local" / "providers"
MANIFEST: Final[dict[str, Any]] = json.loads((SAMPLES_DIR / "manifest.json").read_text("utf-8"))
H2C_FRONT: Final = Path(__file__).with_name("h2c_front.mjs")
SAMPLES_VAR: Final = "RAYITO_ZC_SAMPLES"
ENDPOINT_HOST_VAR: Final = "RAYITO_ZC_ENDPOINT_HOST"
DEFAULT_ENDPOINT_HOST: Final = "host.docker.internal"
REGION: Final = "us-east-1"
BUILD_TIMEOUT_SECONDS: Final = 1800
EXEC_TIMEOUT_SECONDS: Final = 120
READY_TIMEOUT_SECONDS: Final = 120
POLL_SECONDS: Final = 1.0
SERVICE_PORT: Final = 8080
SERVICE_TOKEN: Final = "zero-config-test-token"
PLACEHOLDER_KEY: Final = "zero-config-placeholder"
#: Las opciones con las que se exporta cada ejemplo: una sesión por VM y una
#: plaza de cola, para probar la cola y el 429 con tres peticiones a la vez.
QUEUE_MAX: Final = 1
QUEUE_TIMEOUT_SECONDS: Final = 30
SLOW_SECONDS: Final = 3

PROBE: Final = r"""
import json, sys, urllib.error, urllib.request
args = json.load(sys.stdin)
headers = dict(args.get("headers") or {})
body = args.get("body")
data = None if body is None else json.dumps(body).encode()
request = urllib.request.Request(
    "http://127.0.0.1:%d" % args["port"] + args["path"],
    data=data, headers=headers, method=args.get("method", "GET"),
)
try:
    with urllib.request.urlopen(request, timeout=args.get("timeout", 60)) as response:
        print(json.dumps([response.status, response.read().decode()]))
except urllib.error.HTTPError as error:
    print(json.dumps([error.code, error.read().decode()]))
"""

CHILDREN: Final = r"""
import os
def cmdline(pid):
    try:
        return open("/proc/%s/cmdline" % pid, "rb").read()
    except OSError:
        return b""
host = [p for p in os.listdir("/proc") if p.isdigit() and b"agent_host" in cmdline(p)]
kids = []
for pid in os.listdir("/proc"):
    if not pid.isdigit():
        continue
    try:
        fields = open("/proc/%s/stat" % pid).read().rsplit(")", 1)[1].split()
    except OSError:
        continue
    if fields[1] in host:
        kids.append(pid)
print(" ".join(kids))
"""


def selected_samples() -> list[dict[str, Any]]:
    wanted = {name for name in os.environ.get(SAMPLES_VAR, "").split(",") if name}
    return [s for s in MANIFEST["samples"] if not wanted or s["id"] in wanted]


def sample_ids() -> list[str]:
    return [sample["id"] for sample in selected_samples()]


def docker_binary() -> str:
    path = shutil.which("docker")
    if path is None:
        raise RuntimeError("sin docker en el host")
    return path


STDERR_TAIL_CHARS: Final = 3000


def run(*args: str, timeout: int = EXEC_TIMEOUT_SECONDS, stdin: str | None = None) -> str:
    """Corre un comando; si falla, el error lleva el final de su salida de
    error (el de un `docker build` es lo único que explica el fallo)."""
    done = subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, input=stdin, check=False
    )
    if done.returncode != 0:
        raise RuntimeError(
            f"{args[1] if len(args) > 1 else args[0]} falló ({done.returncode}):\n"
            f"{done.stderr[-STDERR_TAIL_CHARS:]}\n{done.stdout[-STDERR_TAIL_CHARS:]}"
        )
    return done.stdout.strip()


def detected_project(sample: dict[str, Any]) -> AgentProject:
    """El proyecto que sale de detectar el ejemplo, con la cola del host
    activada para las pruebas de concurrencia."""
    project, _ = AgentProject.detect(SAMPLES_DIR / sample["dir"], region=REGION)
    import dataclasses

    serve = ServeOptions(
        max_concurrency=1, queue_max=QUEUE_MAX, queue_timeout_seconds=QUEUE_TIMEOUT_SECONDS
    )
    return dataclasses.replace(project, serve=serve)


def _load_fake_upstream() -> Any:
    sys.path.insert(0, str(PROVIDERS_DIR))
    try:
        import fake_upstream  # type: ignore[import-not-found]
    finally:
        sys.path.remove(str(PROVIDERS_DIR))
    return fake_upstream


class FakeBedrock(NamedTuple):
    """Los puertos del Bedrock falso: HTTP/1.1 y, delante, HTTP/2 en claro."""

    http1: int
    h2c: int


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("0.0.0.0", 0))
        return int(probe.getsockname()[1])


@contextmanager
def _h2c_front(upstream_port: int) -> Iterator[int]:
    port = _free_port()
    front = subprocess.Popen(
        ["node", str(H2C_FRONT), str(port), str(upstream_port)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert front.stdout is not None
        assert front.stdout.readline().strip() == "ready"
        yield port
    finally:
        front.terminate()
        front.wait(timeout=10)


@contextmanager
def fake_bedrock() -> Iterator[FakeBedrock]:
    """El Bedrock falso del entorno local en puertos libres del host, para que
    los contenedores lo alcancen por `RAYITO_ZC_ENDPOINT_HOST`."""
    fake = _load_fake_upstream()
    server = ThreadingHTTPServer(("0.0.0.0", 0), fake.UpstreamHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    http1 = int(server.server_address[1])
    try:
        with _h2c_front(http1) as h2c:
            yield FakeBedrock(http1=http1, h2c=h2c)
    finally:
        server.shutdown()
        server.server_close()


def endpoint_url(port: int) -> str:
    return f"http://{os.environ.get(ENDPOINT_HOST_VAR, DEFAULT_ENDPOINT_HOST)}:{port}"


OPENCODE_CONFIG_ENV: Final = "OPENCODE_CONFIG_CONTENT"
OPENCODE_BEDROCK_PROVIDER: Final = "amazon-bedrock"


def uses_http2(sample: dict[str, Any]) -> bool:
    """El SDK de AWS para JavaScript habla HTTP/2 en claro con un endpoint
    `http://`; lo hace la app propia de un ejemplo TypeScript. Los clientes
    que crea el host (`protocol` de framework) usan HTTP/1.1."""
    return bool(sample["language"] == "typescript" and sample["expected"]["protocol"] == "json")


def model_endpoint_envs(sample: dict[str, Any], bedrock: FakeBedrock) -> list[str]:
    """Los `-e` que dirigen al modelo falso. Los clientes de AWS leen
    `AWS_ENDPOINT_URL_BEDROCK_RUNTIME`; OpenCode no, y su `endpoint` se
    sobreescribe con la configuración en línea que admite."""
    url = endpoint_url(bedrock.h2c if uses_http2(sample) else bedrock.http1)
    envs = ["-e", f"AWS_ENDPOINT_URL_BEDROCK_RUNTIME={url}"]
    if sample["expected"]["framework"] == "opencode":
        override = {"provider": {OPENCODE_BEDROCK_PROVIDER: {"options": {"endpoint": url}}}}
        envs += ["-e", f"{OPENCODE_CONFIG_ENV}={json.dumps(override)}"]
    return envs


class Container:
    """Un contenedor del agente exportado y las sondas que corren dentro de él."""

    def __init__(self, docker: str, name: str) -> None:
        self.docker = docker
        self.name = name

    def call(
        self,
        path: str,
        body: Any = None,
        *,
        method: str | None = None,
        accept: str = "application/json",
        auth: bool = True,
        timeout: int = 60,
    ) -> tuple[int, str]:
        headers = {"Accept": accept, "Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {SERVICE_TOKEN}"
        args = {
            "port": SERVICE_PORT,
            "path": path,
            "body": body,
            "headers": headers,
            "method": method or ("GET" if body is None else "POST"),
            "timeout": timeout,
        }
        out = run(
            self.docker, "exec", "-i", self.name, "python3", "-c", PROBE,
            timeout=timeout + 30, stdin=json.dumps(args),
        )  # fmt: skip
        status, text = json.loads(out)
        return int(status), str(text)

    def children_of_host(self) -> list[int]:
        out = run(self.docker, "exec", self.name, "python3", "-c", CHILDREN)
        return [int(pid) for pid in out.split()]

    def kill(self, pid: int) -> None:
        run(
            self.docker, "exec", self.name, "python3", "-c",
            f"import os, signal; os.kill({pid}, signal.SIGKILL)",
        )  # fmt: skip

    def logs(self) -> str:
        return subprocess.run(
            [self.docker, "logs", self.name], capture_output=True, text=True, check=False
        ).stdout
