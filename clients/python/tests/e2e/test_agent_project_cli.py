"""`rayito agent run|deploy|invoke|status|stop` contra AWS real, un caso por
runtime (`agent-runtimes-byo`): la CLI de Python como la usaría cualquiera
(`sys.executable -m rayito.cli`, en un subproceso) sobre un `rayito-agent.toml`
escrito para la imagen ya construida del caso.

Cada runtime hace un `run --stream --json` (los eventos, una línea por evento,
terminan en `done` con el uso de tokens) y un ciclo `deploy` / `status` /
`invoke` / `stop`, con el access token en un `--token-file`. `deploy` va con
`--no-build`: reutiliza la imagen compartida del caso, ya construida con
todos sus runtimes, en vez de construir encima una versión con uno solo (que
sería la más reciente para las demás pruebas). `stop` va por el
nombre de la flota, así que también borra su receta de Parameter Store
(`/rayito/agents/<nombre>/recipe`), y la prueba comprueba que no queda. Las
cabeceras de acceso del despliegue no salen por ninguna salida. El espejo en TypeScript está en
`clients/typescript/tests/e2e/agent-project-cli.e2e.test.ts`.

Variables y coste: cabecera de `agent_project_support.py`. Un MicroVM de 2 GB
de unos minutos por runtime y por orden (≈ $0,03 en total) y ≈ 16 llamadas a
Haiku (≈ $0,15). El despliegue se para en `finally`.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest

from rayito._agent._fleet_recipe import recipe_parameter_name
from rayito._agent._fleet_recipe_aws import default_recipe_store
from rayito._aws import LambdaMicrovmsControlPlane

from .agent_project_support import (
    BEDROCK_SECRET_VAR,
    BUCKET_VAR,
    CASES,
    PROMPT,
    RuntimeCase,
    app_fixture,
    build_image,
    image_name,
    project_file_text,
    unsupported,
)
from .conftest import E2ESettings

pytestmark = pytest.mark.e2e

CLI: Final = (sys.executable, "-m", "rayito.cli")
COMMAND_BUDGET_SECONDS: Final = 1500
PROJECT_FILE_NAME: Final = "rayito-agent.toml"
TOKEN_FILE_NAME: Final = "agent.token"


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


def rayito(*args: str) -> subprocess.CompletedProcess[str]:
    """La CLI en un subproceso; el access token del despliegue va en un
    `--token-file`, nunca por argv ni por el entorno."""
    return subprocess.run(
        [*CLI, *args],
        capture_output=True,
        text=True,
        timeout=COMMAND_BUDGET_SECONDS,
        check=False,
    )


def json_of(done: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert done.returncode == 0, done.stderr[-400:]
    document = json.loads(done.stdout)
    assert isinstance(document, dict)
    return document


@pytest.fixture
def project_dir(case: RuntimeCase, images: dict[str, str], tmp_path: Path) -> Path:
    """El proyecto del caso: el código de su tipo de imagen y el
    `rayito-agent.toml`. Va en un subdirectorio para que el fichero del
    token, junto a él, nunca entre en el código que `deploy` empaqueta."""
    reason = unsupported(case, images[case.image])
    if reason is not None:
        pytest.skip(f"{case.id}: {reason}")
    directory = tmp_path / "project"
    shutil.copytree(app_fixture(case.image), directory)
    (directory / PROJECT_FILE_NAME).write_text(project_file_text(case, images[case.image]))
    return directory


@pytest.fixture
def deployed() -> Iterator[list[str]]:
    """Las flotas que un test despliega; se paran por su nombre al terminar,
    pase lo que pase, y eso borra también su receta."""
    names: list[str] = []
    yield names
    for name in names:
        with contextlib.suppress(Exception):
            rayito("agent", "stop", name)


def recipe_left(name: str, region: str) -> bool:
    """Si la receta de la flota `name` sigue en Parameter Store (la misma
    lectura que hace `AgentFleet.connect`)."""
    parameter = recipe_parameter_name(name)
    assert parameter is not None
    return default_recipe_store(region, None).load(parameter) is not None


@pytest.fixture(params=CASES, ids=lambda case: case.id)
def case(request: pytest.FixtureRequest) -> RuntimeCase:
    return request.param  # type: ignore[no-any-return]


def test_cli_run_streams_events(case: RuntimeCase, project_dir: Path) -> None:
    done = rayito("--json", "agent", "run", "--stream", "--file", str(project_dir), PROMPT)
    assert done.returncode == 0, done.stderr[-400:]
    events = [json.loads(line) for line in done.stdout.splitlines() if line.strip()]
    kinds = [event["type"] for event in events]
    assert kinds[-1] == "done"
    assert "agent_failed" not in kinds
    assert {"text", "text_delta"} & set(kinds)
    assert events[-1]["sessionId"]
    if case.calls_the_model:
        usage = events[-1]["usage"]
        assert usage["input"] + usage["output"] > 0


def test_cli_deploy_status_invoke_stop(
    case: RuntimeCase,
    project_dir: Path,
    deployed: list[str],
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
) -> None:
    token_file = project_dir.parent / TOKEN_FILE_NAME
    token = ("--token-file", str(token_file))
    deploy = json_of(
        rayito("--json", "agent", "deploy", "--file", str(project_dir), "--no-build", *token)
    )
    name = str(deploy["name"])
    deployed.append(name)
    sandbox_id = str(deploy["sandboxId"])
    assert str(deploy["url"]).startswith("https://")
    status = json_of(rayito("--json", "agent", "status", sandbox_id, *token))
    assert status["ready"] is True
    reply = json_of(rayito("--json", "agent", "invoke", sandbox_id, PROMPT, *token))
    assert str(reply["text"]).strip()
    for output in (json.dumps(deploy), json.dumps(status), json.dumps(reply)):
        assert "authorization" not in output.lower()
    stopped = json_of(rayito("--json", "agent", "stop", name))
    assert stopped == {"name": name, "stopped": len(deploy["sandboxIds"])}
    deployed.remove(name)
    assert not recipe_left(name, e2e_settings.region or control_plane.region)
