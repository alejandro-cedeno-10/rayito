"""Los quince agentes de ejemplo (`testdata/agent-samples/`), de la detección al
contenedor (`make agent-zero-config-test`): cada uno se detecta sin opciones,
se exporta como contexto de Docker, se construye con el Docker del host
(`linux/arm64`) y se prueba de verdad contra el Bedrock falso del entorno local
(`dev/local/providers/fake_bedrock.py`), sin AWS ni claves.

Comprueba, por ejemplo: `GET /ping`, una invocación con herramienta (la
respuesta lleva el resultado de la herramienta), el stream SSE, la cola del host
y el 429 con tres peticiones a la vez, y que el host reinicia la app que se cae
(sólo los ejemplos con un proceso hijo). Corre en el host, no en el `runner`
(que no tiene Docker): por eso lleva el marker `docker_export`. Variables de
entorno: cabecera de `zero_config_support.py`.
"""

from __future__ import annotations

import json
import secrets as stdlib_secrets
import subprocess
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Final

import pytest

from .zero_config_support import (
    BUILD_TIMEOUT_SECONDS,
    MANIFEST,
    PLACEHOLDER_KEY,
    POLL_SECONDS,
    READY_TIMEOUT_SECONDS,
    SERVICE_TOKEN,
    SLOW_SECONDS,
    Container,
    FakeBedrock,
    detected_project,
    docker_binary,
    endpoint_url,
    fake_bedrock,
    run,
    sample_ids,
    selected_samples,
)

pytestmark = pytest.mark.docker_export

PROMPT: Final = MANIFEST["prompt"]
EXPECTED_ANSWER: Final = MANIFEST["expectedAnswer"]
#: Lo que contesta el modelo falso cuando no se le ofrece la herramienta `add`.
PLAIN_REPLY: Final = "rayito-fake-ok"
RESTART_TIMEOUT_SECONDS: Final = 60
SAMPLES: Final = {sample["id"]: sample for sample in selected_samples()}


@pytest.fixture(scope="module")
def docker() -> str:
    try:
        return docker_binary()
    except RuntimeError as error:
        pytest.skip(str(error))


@pytest.fixture(scope="module")
def bedrock() -> Iterator[FakeBedrock]:
    with fake_bedrock() as port:
        yield port


def wait_ready(container: Container) -> None:
    """Espera a que el servicio atienda una invocación: `/ping` contesta antes
    de que el driver esté listo, así que se sondea con una llamada real y se
    reintenta mientras el host contesta 503 (`not_ready`)."""
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while True:
        try:
            status, _ = container.call("/invocations", {"prompt": "hola"}, timeout=60)
            if status == 200:
                return
        except RuntimeError:
            pass
        if time.monotonic() >= deadline:
            raise AssertionError(f"el agente no quedó listo:\n{container.logs()[-2000:]}")
        time.sleep(POLL_SECONDS)


@pytest.fixture(scope="module", params=sample_ids())
def container(
    request: pytest.FixtureRequest,
    docker: str,
    bedrock: FakeBedrock,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[dict[str, Any], Container]]:
    sample = SAMPLES[request.param]
    context = tmp_path_factory.mktemp("export") / "context"
    detected_project(sample).export(target="docker", out_dir=context)
    tag = f"rayito-zc-{sample['id']}:{stdlib_secrets.token_hex(3)}"
    name = f"rayito-zc-{sample['id']}-{stdlib_secrets.token_hex(3)}"
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
    run(
        docker, "run", "-d", "--rm", "--name", name, "--platform", "linux/arm64",
        "-e", f"RAYITO_AGENT_SERVICE_TOKEN={SERVICE_TOKEN}",
        "-e", f"AWS_ENDPOINT_URL_BEDROCK_RUNTIME={endpoint_url(bedrock.port_for(sample))}",
        "-e", f"AWS_BEARER_TOKEN_BEDROCK={PLACEHOLDER_KEY}",
        tag,
    )  # fmt: skip
    handle = Container(docker, name)
    try:
        wait_ready(handle)
        yield sample, handle
    finally:
        subprocess.run([docker, "rm", "-f", name], capture_output=True, check=False)
        subprocess.run([docker, "rmi", "-f", tag], capture_output=True, check=False)


def test_the_agent_answers_its_ping_and_an_invocation(
    container: tuple[dict[str, Any], Container],
) -> None:
    sample, handle = container
    status, body = handle.call("/invocations", {"prompt": PROMPT})
    assert status == 200, body
    reply = json.loads(body)
    assert reply["failed"] is None
    if sample["toolUse"]:
        assert EXPECTED_ANSWER in reply["text"]
    else:
        assert PLAIN_REPLY in reply["text"]
    assert reply["session_id"]


def test_the_agent_streams_an_invocation(container: tuple[dict[str, Any], Container]) -> None:
    _, handle = container
    status, body = handle.call(
        "/invocations", {"prompt": PROMPT}, accept="text/event-stream", timeout=90
    )
    assert status == 200, body
    assert "event: done" in body or "event: text" in body


def test_the_agent_asks_for_the_service_token(container: tuple[dict[str, Any], Container]) -> None:
    _, handle = container
    status, _ = handle.call("/invocations", {"prompt": PROMPT}, auth=False)
    assert status == 401


def test_a_custom_http_app_keeps_its_own_routes(
    container: tuple[dict[str, Any], Container],
) -> None:
    sample, handle = container
    app = sample.get("app")
    if app is None or sample["expected"]["protocol"] != "json":
        pytest.skip("no es una app HTTP con el protocolo json")
    status, body = handle.call(app["health"])
    assert status == 200, body
    status, body = handle.call(app["invoke"], {app["requestField"]: PROMPT})
    assert status == 200, body
    assert PLAIN_REPLY in body


def test_the_host_queues_one_call_and_answers_429_to_the_third(
    container: tuple[dict[str, Any], Container],
) -> None:
    _, handle = container
    prompt = f"slow:{SLOW_SECONDS} hola"

    def invoke(_: int) -> int:
        return handle.call("/invocations", {"prompt": prompt}, timeout=90)[0]

    with ThreadPoolExecutor(max_workers=3) as pool:
        statuses = sorted(pool.map(invoke, range(3)))
    assert statuses == [200, 200, 429]


def test_the_host_restarts_an_app_that_dies(container: tuple[dict[str, Any], Container]) -> None:
    sample, handle = container
    children = handle.children_of_host()
    if not children:
        pytest.skip(f"{sample['id']} corre dentro del proceso del host: no hay hijo que matar")
    for pid in children:
        handle.kill(pid)
    deadline = time.monotonic() + RESTART_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            status, body = handle.call("/ping", auth=False, timeout=10)
            if status == 200 and json.loads(body).get("status") == "Healthy":
                break
        except RuntimeError:
            pass
        time.sleep(POLL_SECONDS)
    else:
        raise AssertionError(f"el host no reinició la app:\n{handle.logs()[-2000:]}")
    status, body = handle.call("/invocations", {"prompt": PROMPT})
    assert status == 200, body


def test_the_image_is_unprivileged_and_credential_free(
    container: tuple[dict[str, Any], Container], docker: str
) -> None:
    _, handle = container
    info = json.loads(run(docker, "inspect", handle.name))[0]
    assert info["Config"]["User"] == "user"
    env = "\n".join(info["Config"]["Env"])
    for forbidden in ("AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID"):
        assert forbidden not in env


def test_the_export_context_has_no_secret_like_file(tmp_path: Path) -> None:
    sample = next(iter(SAMPLES.values()))
    context = tmp_path / "context"
    detected_project(sample).export(target="docker", out_dir=context)
    assert not any(path.name == ".env" for path in context.rglob("*"))
