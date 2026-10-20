"""El contexto `agentcore` que exporta `AgentProject.export()` (ADR-031),
construido con el Docker del host y el argumento de build `AGENT_BASE_IMAGE`
(como lo hace CodeBuild) y probado de verdad: `GET /ping`, y el id de sesión de
la cabecera de AgentCore (`X-Amzn-Bedrock-AgentCore-Runtime-Session-Id`) que el
host traduce al del driver y devuelve tal cual, también en la segunda llamada de
la misma sesión. Sin credenciales en el entorno de la imagen.

Corre en el host (marker `docker_export`) y se salta si no hay `docker`. La
sonda corre dentro del contenedor (`docker exec`). Una imagen que no se puede
descargar (sin red) también lo salta.
"""

from __future__ import annotations

import json
import secrets as stdlib_secrets
import subprocess
import time
from collections.abc import Iterator
from typing import Final

import pytest

from rayito._agent._export import BASE_IMAGE_REFERENCE
from rayito._agentcore._domain import base_image_reference
from rayito._limits import (
    AGENT_EXPORT_BASE_IMAGE_DIGEST,
    AGENT_SERVICE_EXPORT_PORT,
    AGENT_SERVICE_SESSION_HEADER,
)

from .test_agent_export_container import (
    BUILD_TIMEOUT_SECONDS,
    POLL_SECONDS,
    READY_TIMEOUT_SECONDS,
    docker,
    exported_project,
    run,
)

pytestmark = pytest.mark.docker_export

__all__ = ["docker"]

SESSION: Final = "rayito-" + "c" * 32
PROBE: Final = f"""
import json, sys, urllib.request
base = "http://127.0.0.1:{AGENT_SERVICE_EXPORT_PORT}"
token = sys.argv[1]
session = sys.argv[2]

def call(path, body=None, headers=None):
    all_headers = {{"Content-Type": "application/json", "Authorization": "Bearer " + token}}
    all_headers.update(headers or {{}})
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(base + path, data=data, headers=all_headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.status, response.read().decode()

header = {{"{AGENT_SERVICE_SESSION_HEADER}": session}}
print(json.dumps({{
    "ping": call("/ping"),
    "first": call("/invocations", {{"prompt": "hola"}}, header),
    "second": call("/invocations", {{"prompt": "otra vez"}}, header),
}}))
"""


@pytest.fixture(scope="module")
def container(docker: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, str]]:
    context = tmp_path_factory.mktemp("agentcore-export") / "context"
    exported_project().export(target="agentcore", out_dir=context)
    tag = f"rayito-agentcore-probe:{stdlib_secrets.token_hex(4)}"
    token = stdlib_secrets.token_urlsafe(16)
    base = base_image_reference(AGENT_EXPORT_BASE_IMAGE_DIGEST)
    try:
        run(
            docker,
            "build",
            "--platform",
            "linux/arm64",
            "--build-arg",
            f"AGENT_BASE_IMAGE={base}",
            "-t",
            tag,
            str(context),
            timeout=BUILD_TIMEOUT_SECONDS,
        )
    except subprocess.CalledProcessError:
        pytest.skip("no se pudo construir la imagen (¿sin red hacia ECR Public?)")
    name = f"rayito-agentcore-probe-{stdlib_secrets.token_hex(4)}"
    run(
        docker,
        "run",
        "-d",
        "--rm",
        "--name",
        name,
        "--platform",
        "linux/arm64",
        "-e",
        f"RAYITO_AGENT_SERVICE_TOKEN={token}",
        tag,
    )
    try:
        yield name, token
    finally:
        subprocess.run([docker, "rm", "-f", name], capture_output=True, check=False)
        subprocess.run([docker, "rmi", "-f", tag], capture_output=True, check=False)


def probe(docker: str, name: str, token: str) -> dict[str, tuple[int, str]]:
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while True:
        try:
            out = run(docker, "exec", name, "python3", "-c", PROBE, token, SESSION)
            return {key: (int(v[0]), str(v[1])) for key, v in json.loads(out).items()}
        except subprocess.CalledProcessError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(POLL_SECONDS)


def test_the_agentcore_image_answers_with_the_session_of_the_header(
    docker: str, container: tuple[str, str]
) -> None:
    name, token = container
    results = probe(docker, name, token)
    assert json.loads(results["ping"][1])["status"] == "Healthy"
    first = json.loads(results["first"][1])
    second = json.loads(results["second"][1])
    assert (first["text"], first["failed"]) == ("eco:hola", None)
    assert first["session_id"] == second["session_id"] == SESSION


def test_the_dockerfile_default_base_is_the_pinned_reference() -> None:
    assert BASE_IMAGE_REFERENCE.endswith(AGENT_EXPORT_BASE_IMAGE_DIGEST)
