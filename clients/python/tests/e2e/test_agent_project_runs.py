"""`AgentProject.run()` y `AgentProject.deploy()` contra AWS real, un caso por
runtime (`agent-runtimes-byo`): deepagents, LangGraph, Strands (SDK y
harness), un comando propio, OpenCode, Hermes y Strands sobre Node, con
Claude Haiku 4.5 por `bedrock_gateway` y el egress cerrado.

Cada runtime hace un `run()` y un `deploy()` con `status()`, `invoke()`,
`stream()` y `stop()`. El guest y el servicio se prueban una vez, aquí: la
CLI y el servicio no se repiten en TypeScript (el espejo de las clases está en
`clients/typescript/tests/e2e/agent-project.e2e.test.ts`).

Variables y coste: cabecera de `agent_project_support.py`. Dos imágenes
desechables por carril (≈ $0,06 por versión y semana) y ≈ 16 MicroVMs de 2 GB
de unos minutos (≈ $0,05) más ≈ 40 llamadas a Haiku (≈ $0,3). Los
despliegues se paran en `finally`; si el proceso muere, los borra el inventario
de quien lanza la ejecución.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Final

import pytest

from rayito import AgentDeployment, AgentFailed, Done, Text, TextDelta
from rayito._aws import LambdaMicrovmsControlPlane

from .agent_project_support import (
    BEDROCK_SECRET_VAR,
    BUCKET_VAR,
    CASES,
    PROMPT,
    RuntimeCase,
    build_image,
    image_name,
    project_for,
    unsupported,
)
from .conftest import E2ESettings

pytestmark = pytest.mark.e2e

READY_BUDGET_SECONDS: Final = 120


@pytest.fixture(scope="module")
def images(e2e_settings: E2ESettings, control_plane: LambdaMicrovmsControlPlane) -> dict[str, str]:
    """Los nombres de las dos imágenes; las construye si hay prefijo y bucket."""
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


def project_of(case: RuntimeCase, images: dict[str, str]):  # type: ignore[no-untyped-def]
    template = images[case.image]
    reason = unsupported(case, template)
    if reason is not None:
        pytest.skip(f"{case.id}: {reason}")
    return project_for(case, template)


@pytest.fixture
def region(e2e_settings: E2ESettings, control_plane: LambdaMicrovmsControlPlane) -> str:
    return e2e_settings.region or control_plane.region


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
def test_run(case: RuntimeCase, images: dict[str, str], region: str) -> None:
    result = project_of(case, images).run(PROMPT, region=region)
    assert result.text.strip()
    if case.calls_the_model:
        assert result.usage.total > 0


@pytest.fixture
def deployments() -> Iterator[list[AgentDeployment]]:
    """Todo lo que un test despliega se para al terminar, pase lo que pase."""
    created: list[AgentDeployment] = []
    yield created
    for deployment in created:
        deployment.stop()


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
def test_deploy_invoke_stream_stop(
    case: RuntimeCase,
    images: dict[str, str],
    region: str,
    deployments: list[AgentDeployment],
) -> None:
    deployment = project_of(case, images).deploy(region=region)
    deployments.append(deployment)
    status = deployment.status()
    assert status.ready
    assert deployment.url.startswith("https://")
    reply = deployment.invoke(PROMPT)
    assert reply.text.strip()
    with deployment.stream(PROMPT) as events:
        collected = list(events)
    assert isinstance(collected[-1], Done)
    assert not any(isinstance(event, AgentFailed) for event in collected)
    assert any(isinstance(event, TextDelta | Text) for event in collected)
    deployment.stop()
    deployments.remove(deployment)
