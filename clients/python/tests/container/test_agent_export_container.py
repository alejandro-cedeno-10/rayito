"""El contenedor que exporta `AgentProject.export()` (`make agent-export-test`),
construido con el Docker del host y probado de verdad: `linux/arm64`, el
usuario sin privilegios, `GET /ping` y `POST /invocations` (JSON, SSE y el
token del contenedor) del contrato `rayito.agent-service/1`, sin credenciales
en el entorno de la imagen.

Corre en el host, no en el `runner` de `make local-e2e` (que no tiene Docker):
por eso lleva su propio marker, `docker_export`, y se salta si no hay `docker`.
La sonda corre dentro del contenedor (`docker exec`), así que no depende de
cómo reenvía puertos el Docker del host. El agente es la app HTTP de ejemplo de
`dev/local/agent/apps`: no llama a ningún modelo.
"""

from __future__ import annotations

import json
import secrets as stdlib_secrets
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest

from rayito import AgentProject
from rayito._agent._domain import AgentModel, AgentSpec
from rayito._agent._gateways import bedrock_gateway
from rayito._limits import AGENT_SERVICE_EXPORT_PORT

pytestmark = pytest.mark.docker_export

custom = pytest.importorskip("rayito._agent._custom")
CustomRuntime = custom.CustomRuntime
HttpApp = custom.HttpApp

APPS_DIR: Final = Path(__file__).resolve().parents[4] / "dev" / "local" / "agent" / "apps"
APP_COMMAND: Final = ["python3", "/opt/agents/app/http_agent.py", "9100"]
APP_PORT: Final = 9100
MODEL_ID: Final = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
BUILD_TIMEOUT_SECONDS: Final = 900
EXEC_TIMEOUT_SECONDS: Final = 60
READY_TIMEOUT_SECONDS: Final = 90
POLL_SECONDS: Final = 1.0
PROBE: Final = f"""
import json, sys, urllib.error, urllib.request
base = "http://127.0.0.1:{AGENT_SERVICE_EXPORT_PORT}"
token = sys.argv[1]

def call(path, body=None, auth=True, accept="application/json"):
    headers = {{"Accept": accept, "Content-Type": "application/json"}}
    if auth:
        headers["Authorization"] = "Bearer " + token
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(base + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()

print(json.dumps({{
    "ping": call("/ping", auth=False),
    "invoke": call("/invocations", {{"prompt": "hola"}}),
    "no_token": call("/invocations", {{"prompt": "hola"}}, auth=False),
    "bad_body": call("/invocations", {{"nope": 1}}),
    "stream": call("/invocations", {{"prompt": "stream"}}, accept="text/event-stream"),
}}))
"""


def run(*args: str, timeout: int = EXEC_TIMEOUT_SECONDS) -> str:
    return subprocess.run(
        args, check=True, capture_output=True, text=True, timeout=timeout
    ).stdout.strip()


@pytest.fixture(scope="module")
def docker() -> str:
    path = shutil.which("docker")
    if path is None:
        pytest.skip("sin docker en el host")
    return path


def exported_project() -> AgentProject:
    spec = AgentSpec(
        model=AgentModel(provider="bedrock", id=MODEL_ID, gateway="bedrock", region="us-east-1")
    )
    return AgentProject(
        name="export-probe",
        runtime=CustomRuntime(app=HttpApp(command=APP_COMMAND, port=APP_PORT)),
        spec=spec,
        gateways={"bedrock": bedrock_gateway("bedrock-key", region="us-east-1", models=[MODEL_ID])},
        source=str(APPS_DIR),
    )


@pytest.fixture(scope="module")
def container(docker: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, str]]:
    context = tmp_path_factory.mktemp("export") / "context"
    exported_project().export(target="docker", out_dir=context)
    tag = f"rayito-export-probe:{stdlib_secrets.token_hex(4)}"
    token = stdlib_secrets.token_urlsafe(16)
    run(
        docker,
        "build",
        "--platform",
        "linux/arm64",
        "-t",
        tag,
        str(context),
        timeout=BUILD_TIMEOUT_SECONDS,
    )
    name = f"rayito-export-probe-{stdlib_secrets.token_hex(4)}"
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
            out = run(docker, "exec", name, "python3", "-c", PROBE, token)
            return {key: (int(v[0]), str(v[1])) for key, v in json.loads(out).items()}
        except subprocess.CalledProcessError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(POLL_SECONDS)


def test_the_exported_container_serves_the_contract(
    docker: str, container: tuple[str, str]
) -> None:
    name, token = container
    results = probe(docker, name, token)
    status, body = results["ping"]
    assert status == 200 and json.loads(body)["status"] == "Healthy"
    status, body = results["invoke"]
    assert status == 200
    payload = json.loads(body)
    assert (payload["text"], payload["failed"]) == ("eco:hola", None)
    assert payload["session_id"]
    assert results["no_token"][0] == 401
    assert results["bad_body"][0] == 400
    status, body = results["stream"]
    assert status == 200
    assert "event: text_delta" in body
    assert body.rstrip().splitlines()[-2] == "event: done"


def test_the_exported_image_is_arm64_unprivileged_and_credential_free(
    docker: str, container: tuple[str, str]
) -> None:
    name, _ = container
    info = json.loads(run(docker, "inspect", name))[0]
    assert info["Config"]["User"] == "user"
    env = "\n".join(info["Config"]["Env"])
    for forbidden in ("AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID", "AWS_BEARER_TOKEN_BEDROCK"):
        assert forbidden not in env
    image = json.loads(run(docker, "image", "inspect", info["Image"]))[0]
    assert image["Architecture"] == "arm64"
    assert info["Config"]["ExposedPorts"] == {f"{AGENT_SERVICE_EXPORT_PORT}/tcp": {}}
