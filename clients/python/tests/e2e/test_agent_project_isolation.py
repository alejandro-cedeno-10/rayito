"""Lo que un agente desplegado no puede hacer, en AWS real, un caso por
runtime (`agent-runtimes-byo`): no leer la credencial del modelo, no
encender trazas hacia fuera, hacer llamadas a herramientas y emitir eventos
mientras el egress directo sigue cerrado, y desaparecer al parar.

Variables y coste: cabecera de `agent_project_support.py`. Un MicroVM de 2 GB
de unos minutos por runtime (≈ $0,01 en total) y ≈ 16 llamadas a Haiku
(≈ $0,1). Los despliegues se paran en `finally`.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from typing import Final

import pytest

from rayito import AgentDeployment, AgentFailed, Done, Sandbox, ToolCall
from rayito._aws import LambdaMicrovmsControlPlane
from rayito._limits import TERMINAL_STATES
from rayito.exceptions import SandboxNotFoundException

from .agent_project_support import (
    ACCESS_TOKEN_VAR,
    BEDROCK_SECRET_VAR,
    BUCKET_VAR,
    CASES,
    PROMPT,
    SANDBOX_WORKDIR,
    TOOL_CASES,
    RuntimeCase,
    build_image,
    env_dump_prompt,
    image_name,
    model_credential,
    new_access_token,
    project_for,
    sandbox_exposes_key,
    telemetry_env_names,
    unsupported,
)
from .conftest import E2ESettings

pytestmark = pytest.mark.e2e

TERMINATE_VISIBLE_SECONDS: Final = 30
TERMINATE_POLL_SECONDS: Final = 0.5


@pytest.fixture(scope="module")
def images(e2e_settings: E2ESettings, control_plane: LambdaMicrovmsControlPlane) -> dict[str, str]:
    if not os.environ.get(BEDROCK_SECRET_VAR):
        pytest.skip(f"exporta {BEDROCK_SECRET_VAR} para el agente en AWS")
    region = e2e_settings.region or control_plane.region
    names: dict[str, str] = {}
    for kind in ("py", "cli"):
        explicit = image_name(kind)
        if explicit is None:
            pytest.skip("faltan las imágenes: exporta el prefijo y el bucket, o los nombres")
        names[kind] = build_image(kind, region=region) if os.environ.get(BUCKET_VAR) else explicit
    return names


def sandbox_is_terminated(control_plane: LambdaMicrovmsControlPlane, sandbox_id: str) -> bool:
    """`get-microvm` es eventualmente consistente justo después de
    `terminate-microvm`: se sondea con plazo, y un 404 también es terminal."""
    deadline = time.monotonic() + TERMINATE_VISIBLE_SECONDS
    while time.monotonic() < deadline:
        try:
            if control_plane.get_microvm(sandbox_id).state in TERMINAL_STATES:
                return True
        except SandboxNotFoundException:
            return True
        time.sleep(TERMINATE_POLL_SECONDS)
    return False


@pytest.fixture(scope="module")
def region(e2e_settings: E2ESettings, control_plane: LambdaMicrovmsControlPlane) -> str:
    return e2e_settings.region or control_plane.region


@pytest.fixture(autouse=True)
def shared_access_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """El token con el que `deploy()` crea el sandbox y con el que el test se
    reconecta a él."""
    monkeypatch.setenv(ACCESS_TOKEN_VAR, new_access_token())


@pytest.fixture
def deployments() -> Iterator[list[AgentDeployment]]:
    created: list[AgentDeployment] = []
    yield created
    for deployment in created:
        deployment.stop()


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
def test_agent_cannot_read_the_credential(
    case: RuntimeCase,
    images: dict[str, str],
    region: str,
    control_plane: LambdaMicrovmsControlPlane,
    deployments: list[AgentDeployment],
) -> None:
    reason = unsupported(case, images[case.image])
    if reason is not None:
        pytest.skip(f"{case.id}: {reason}")
    deployment = project_for(case, images[case.image]).deploy(region=region)
    deployments.append(deployment)
    sandbox = Sandbox.connect(deployment.sandbox_id, region=region)
    key = model_credential(region)
    env_path = f"{SANDBOX_WORKDIR}/{case.id}-env.txt"
    uses_tools = case.id in TOOL_CASES
    prompt = env_dump_prompt(env_path) if uses_tools else PROMPT

    with deployment.stream(prompt) as stream:
        events = list(stream)
    assert isinstance(events[-1], Done)
    assert not [event for event in events if isinstance(event, AgentFailed)]
    if uses_tools:
        assert any(e.status == "completed" for e in events if isinstance(e, ToolCall))
        env_dump = sandbox.files.read(env_path)
        assert env_dump.strip()
        assert not telemetry_env_names(env_dump)
        in_agent_env = key in env_dump
        assert not in_agent_env
    assert sandbox_exposes_key(sandbox, key) == []

    deployment.stop()
    deployments.remove(deployment)
    assert sandbox_is_terminated(control_plane, deployment.sandbox_id)
