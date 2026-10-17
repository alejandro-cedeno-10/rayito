"""Los quince agentes de ejemplo (`testdata/agent-samples/`) desplegados en AWS
real sin tocar su código (`agent-zero-config`), cada uno dos veces: sin opciones
y con las propias (4096 MiB, 2 sesiones por VM, cola de 2, de 1 a 2 VMs con 1
caliente). El modelo es Claude Haiku 4.5 por la pasarela de secretos.

Sin opciones comprueba: la detección elige lo que dice el manifiesto, `/ping`
sano, una invocación (con herramienta si el agente la tiene), la ruta propia de
las apps HTTP por la URL del despliegue, que el agente no lee la clave del
modelo, y la limpieza. Con opciones: la concurrencia y la cola (cinco llamadas
a la vez reparten entre dos VMs y el host contesta 429 al sobrante), suspender
y reanudar (`idle`), reconectar con `AgentFleet.connect`, el reinicio de la app
que se cae y la limpieza. Las sesiones dedicadas se prueban en un ejemplo.

Guest y host se prueban una vez, aquí (`parity-checklist.md`): el espejo de
TypeScript (`agent-zero-config.e2e.test.ts`) despliega los ejemplos de
TypeScript y uno de Python. Variables, coste y tope: cabecera de
`agent_zero_config_support.py`. Cada despliegue se para en `finally`; las
imágenes las borra quien lanza la ejecución, por inventario antes y después.
"""

from __future__ import annotations

import contextlib
import json
import shlex
import time
import urllib.request
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Final

import pytest

from rayito import (
    AgentDeployOptions,
    AgentFleet,
    AgentProject,
    Sandbox,
    deploy_agent,
)
from rayito._agent._scaling_domain import decode_handle
from rayito.exceptions import AgentException, SandboxException
from tests.container.zero_config_support import CHILDREN

from .agent_project_support import model_credential, new_access_token, sandbox_exposes_key
from .agent_zero_config_support import (
    BUCKET_VAR,
    GATEWAY_NAME,
    MANIFEST,
    PARALLEL_CALLS,
    POLL_SECONDS,
    RECOVERY_TIMEOUT_SECONDS,
    SECRET_VAR,
    SUSPEND_WAIT_SECONDS,
    TERMINATION_WAIT_SECONDS,
    Ledger,
    project_name,
    region,
    require,
    sample_ids,
    selected_samples,
    staged_sample,
)

pytestmark = pytest.mark.e2e

PROMPT: Final = MANIFEST["prompt"]
EXPECTED_ANSWER: Final = MANIFEST["expectedAnswer"]
LONG_PROMPT: Final = "Write a story of about 300 words about a lighthouse keeper."
SANDBOX_PORT: Final = 8088
SAMPLES: Final = {sample["id"]: sample for sample in selected_samples()}
DEDICATED_SAMPLE: Final = "python-strands"
PING_SCRIPT: Final = (
    "import urllib.request; print(urllib.request.urlopen("
    f"'http://127.0.0.1:{SANDBOX_PORT}/ping', timeout=5).read().decode())"
)


@pytest.fixture(scope="session")
def ledger() -> Ledger:
    return Ledger.from_environment()


@pytest.fixture
def fleets(ledger: Ledger) -> Iterator[list[AgentFleet]]:
    """Todo lo que un test despliega se para al terminar, pase lo que pase, y
    se anota el gasto de sus VMs."""
    created: list[AgentFleet] = []
    started = time.monotonic()
    yield created
    seconds = time.monotonic() - started
    for fleet in created:
        members = max(1, len(fleet.sandbox_ids))
        ledger.add("vm", seconds=seconds * members, memory_mib=fleet_memory(fleet))
        with contextlib.suppress(SandboxException):
            fleet.stop()


def fleet_memory(fleet: AgentFleet) -> int:
    project = getattr(fleet, "project", None)
    return int(getattr(project, "memory_mib", 2048))


def deploy(directory: Path, ledger: Ledger, fleets: list[AgentFleet]) -> tuple[AgentFleet, str]:
    ledger.guard()
    ledger.add("build")
    token = new_access_token()
    fleet = deploy_agent(
        directory,
        options=AgentDeployOptions(
            bucket=require(BUCKET_VAR),
            secret_names={GATEWAY_NAME: require(SECRET_VAR)},
            access_token=token,
        ),
        region=region(),
    )
    fleets.append(fleet)
    return fleet, token


def check_answer(sample: dict[str, Any], text: str) -> None:
    assert text.strip()
    if sample["toolUse"]:
        assert EXPECTED_ANSWER in text


def vm_of(handle: str | None) -> str:
    route = decode_handle(handle or "")
    assert route is not None, handle
    return route.sandbox_id


def own_route(fleet: AgentFleet, path: str, body: Any = None) -> tuple[int, str]:
    deployment = fleet.deployments[0]
    headers = {**deployment.headers, "Content-Type": "application/json"}
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        deployment.url.rstrip("/") + path,
        data=data,
        headers=headers,
        method="POST" if data else "GET",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return int(response.status), response.read().decode()


@pytest.mark.parametrize("sample_id", sample_ids())
def test_zero_config(
    sample_id: str, tmp_path: Path, ledger: Ledger, fleets: list[AgentFleet]
) -> None:
    sample = SAMPLES[sample_id]
    directory = staged_sample(sample, tmp_path)
    _, report = AgentProject.detect(directory, region=region())
    expected = sample["expected"]
    assert report is not None
    assert (report.framework, report.kind, report.protocol) == (
        expected["framework"],
        expected["kind"],
        expected["protocol"],
    )
    fleet, token = deploy(directory, ledger, fleets)

    assert fleet.status().healthy == 1
    reply = fleet.invoke(PROMPT)
    check_answer(sample, reply.text)
    if sample.get("app") and expected["protocol"] == "json":
        status, body = own_route(fleet, sample["app"]["health"])
        assert status == 200, body
    sandbox = Sandbox.connect(fleet.sandbox_ids[0], access_token=token, region=region())
    credential = model_credential(region())
    assert sandbox_exposes_key(sandbox, credential) == []
    fleet.stop()
    fleets.remove(fleet)
    assert_no_vms_left(project_name(sample, ""))


def assert_no_vms_left(template: str) -> None:
    """Terminar un VM tarda unos segundos en dejar de listarse: se espera a que
    la imagen se quede sin VMs."""
    deadline = time.monotonic() + TERMINATION_WAIT_SECONDS
    while True:
        left = [item.sandbox_id for item in Sandbox.list(template=template, region=region())]
        if not left:
            return
        assert time.monotonic() < deadline, left
        time.sleep(POLL_SECONDS)


def sandbox_children(sandbox: Any) -> list[int]:
    result = sandbox.commands.run(f"python3 -c {shlex.quote(CHILDREN)}", timeout=60)
    return [int(pid) for pid in result.stdout.split()]


def wait_for_ping(sandbox: Any) -> None:
    deadline = time.monotonic() + RECOVERY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            result = sandbox.commands.run(f"python3 -c {shlex.quote(PING_SCRIPT)}", timeout=20)
            if '"Healthy"' in result.stdout:
                return
        except SandboxException:
            pass
        time.sleep(POLL_SECONDS)
    raise AssertionError("el host no recuperó /ping tras matar la app")


@pytest.mark.parametrize("sample_id", sample_ids())
def test_custom_options(
    sample_id: str, tmp_path: Path, ledger: Ledger, fleets: list[AgentFleet]
) -> None:
    sample = SAMPLES[sample_id]
    fleet, token = deploy(staged_sample(sample, tmp_path, variant="c"), ledger, fleets)
    status = fleet.status()
    assert (status.min_vms, status.max_vms, status.warm_vms) == (1, 2, 1)
    first = fleet.invoke(PROMPT)
    check_answer(sample, first.text)

    with ThreadPoolExecutor(max_workers=PARALLEL_CALLS) as pool:
        replies = list(pool.map(lambda _: fleet.invoke(LONG_PROMPT), range(PARALLEL_CALLS)))
    assert all(reply.text.strip() for reply in replies)
    assert len({vm_of(reply.session_id) for reply in replies}) >= 2

    direct = fleet.deployments[0]
    with ThreadPoolExecutor(max_workers=PARALLEL_CALLS) as pool:
        outcomes = list(
            pool.map(lambda _: busy_or_text(direct, LONG_PROMPT), range(PARALLEL_CALLS))
        )
    assert "busy" in outcomes and "text" in outcomes

    rebuilt = AgentFleet.connect(
        fleet.name, access_token=token, scaling=fleet.scaling, region=region()
    )
    again = rebuilt.invoke(PROMPT, session_id=first.session_id)
    assert vm_of(again.session_id) == vm_of(first.session_id)

    assert_restarts_a_dead_app(fleet, token, sample)
    assert_suspends_and_resumes(fleet, sample)
    fleet.stop()
    fleets.remove(fleet)
    assert_no_vms_left(project_name(sample, "c"))


def busy_or_text(deployment: Any, prompt: str) -> str:
    try:
        return "text" if deployment.invoke(prompt).text.strip() else "empty"
    except AgentException as error:
        if error.reason == "busy":
            return "busy"
        raise


def assert_restarts_a_dead_app(fleet: AgentFleet, token: str, sample: dict[str, Any]) -> None:
    sandbox = Sandbox.connect(fleet.sandbox_ids[0], access_token=token, region=region())
    children = sandbox_children(sandbox)
    if not children:
        return
    for pid in children:
        sandbox.commands.run(f"python3 -c 'import os,signal; os.kill({pid}, signal.SIGKILL)'")
    wait_for_ping(sandbox)
    check_answer(sample, fleet.invoke(PROMPT).text)


def assert_suspends_and_resumes(fleet: AgentFleet, sample: dict[str, Any]) -> None:
    time.sleep(SUSPEND_WAIT_SECONDS)
    states = {
        item.sandbox_id: item.state for item in Sandbox.list(template=fleet.name, region=region())
    }
    assert "SUSPENDED" in states.values(), states
    started = time.monotonic()
    reply = fleet.invoke(PROMPT)
    elapsed = time.monotonic() - started
    print(f"[{sample['id']}] reanudar y primera llamada: {elapsed:.1f} s", flush=True)
    check_answer(sample, reply.text)


def test_dedicated_sessions_get_their_own_vm(
    tmp_path: Path, ledger: Ledger, fleets: list[AgentFleet]
) -> None:
    sample = SAMPLES.get(DEDICATED_SAMPLE)
    if sample is None:
        pytest.skip(f"{DEDICATED_SAMPLE} no está en RAYITO_E2E_ZC_SAMPLES")
    fleet, _ = deploy(staged_sample(sample, tmp_path, variant="d"), ledger, fleets)
    first = fleet.invoke(PROMPT)
    second = fleet.invoke(PROMPT)
    assert vm_of(first.session_id) != vm_of(second.session_id)
    again = fleet.invoke("repite", session_id=first.session_id)
    assert vm_of(again.session_id) == vm_of(first.session_id)
