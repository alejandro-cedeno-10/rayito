"""Guardrails de e2e (MILESTONES.md, M1 "para siempre").

- Nada corre sin `RAYITO_E2E=1` y `RAYITO_TEMPLATE` (ARN o nombre de la imagen);
  aplica a los markers `e2e` y `bench` (nightly, `make test-bench`).
- Todo sandbox de test nace con `maximumDurationInSeconds=900` y sin `idlePolicy`
  (M5 pasa su propio `timeout`, siempre <= 1800, y su `IdlePolicy` explícita).
- La fixture `sandbox` llama a `terminate-microvm` en teardown (idempotente) e
  imprime `run-microvm -> Health agent_ready y kernel_ready` en segundos (el
  `kernel_ready_s` de M4), que `boot_timings` conserva por `sandbox_id`.
- Un pre-flight falla si hay más de 10 MicroVMs vivos de la imagen de test y un
  sweeper de fin de sesión termina sólo lo que la sesión creó: el `control_plane`
  de sesión apunta cada id que devuelve `run_microvm` y se terminan esos, si
  siguen vivos, a <= 10/s (token bucket). Los demás MicroVMs vivos de la imagen
  (los previos y los que nazcan por otro camino) no se tocan y se informan por
  número (`session_sweep.py`). Lo que nace sin pasar por ese `control_plane` (el
  corpus E2B y la CLI en subprocesos) lo acota el `maximumDurationInSeconds=900`.

Variables opcionales: `AWS_REGION`/`AWS_PROFILE` (boto3), `RAYITO_EXECUTION_ROLE_ARN`
(activa `logging="cloudwatch"`; sin rol no hay logs de runtime), `RAYITO_TEMPLATE_POLY`
(la variante `rayito-base-poly` de la fixture `poly_sandbox`).
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest

from rayito import IdlePolicy, Sandbox, TelemetryExport
from rayito._aws import LambdaMicrovmsControlPlane, LaunchRequest
from rayito._models import SandboxInfo
from rayito._sandbox_base import LoggingOption
from rayito.exceptions import SandboxNotFoundException

from .session_sweep import plan_session_sweep

E2E_FLAG_VAR = "RAYITO_E2E"
TEMPLATE_VAR = "RAYITO_TEMPLATE"
TEMPLATE_VERSION_VAR = "RAYITO_TEMPLATE_VERSION"
EXECUTION_ROLE_VAR = "RAYITO_EXECUTION_ROLE_ARN"
POLY_TEMPLATE_VAR = "RAYITO_TEMPLATE_POLY"
#: Etiquetas `K=V` separadas por comas que todo e2e que crea pilas o recursos
#: etiquetables manda (para organizaciones que exigen etiquetas en los recursos
#: creados). Opcional; nunca van valores en el código.
TAGS_VAR = "RAYITO_E2E_TAGS"
TEST_SANDBOX_TIMEOUT_SECONDS = 900
MAX_TEST_SANDBOX_TIMEOUT_SECONDS = 1800
MAX_LIVE_TEST_SANDBOXES = 10


@dataclass(frozen=True)
class E2ESettings:
    template: str
    region: str | None
    execution_role_arn: str | None
    template_version: str | None = None

    @property
    def logging(self) -> LoggingOption:
        return "cloudwatch" if self.execution_role_arn else "disabled"


def e2e_tags() -> dict[str, str]:
    """Las etiquetas de `RAYITO_E2E_TAGS`: pares `K=V` separados por comas, sin
    entradas vacías y con clave y valor sin espacios sobrantes. Un par sin `=`
    falla nombrando la variable, nunca el valor."""
    tags: dict[str, str] = {}
    for pair in os.environ.get(TAGS_VAR, "").split(","):
        if not pair.strip():
            continue
        key, separator, value = pair.partition("=")
        if not separator or not key.strip():
            pytest.fail(f"{TAGS_VAR} tiene un par que no es K=V")
        tags[key.strip()] = value.strip()
    return tags


def e2e_enabled() -> bool:
    return os.environ.get(E2E_FLAG_VAR) == "1"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if e2e_enabled():
        return
    skip = pytest.mark.skip(reason=f"e2e desactivado: exporta {E2E_FLAG_VAR}=1 y {TEMPLATE_VAR}")
    for item in items:
        if "e2e" in item.keywords or "bench" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def e2e_settings() -> E2ESettings:
    template = os.environ.get(TEMPLATE_VAR)
    if not e2e_enabled() or not template:
        pytest.fail(f"los tests e2e requieren {E2E_FLAG_VAR}=1 y {TEMPLATE_VAR}=<arn|nombre>")
    return E2ESettings(
        template=template,
        region=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"),
        execution_role_arn=os.environ.get(EXECUTION_ROLE_VAR) or None,
        template_version=os.environ.get(TEMPLATE_VERSION_VAR) or None,
    )


class RecordingControlPlane(LambdaMicrovmsControlPlane):
    """El plano de control de la sesión: apunta el id de cada MicroVM que
    `run_microvm` devuelve, que es lo único que el sweeper de fin de sesión
    termina (`session_sweep.py`)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.created_ids: list[str] = []

    def run_microvm(self, request: LaunchRequest) -> SandboxInfo:
        info = super().run_microvm(request)
        self.created_ids.append(info.sandbox_id)
        return info


@pytest.fixture(scope="session")
def control_plane(e2e_settings: E2ESettings) -> RecordingControlPlane:
    plane = RecordingControlPlane.from_session(region=e2e_settings.region)
    assert isinstance(plane, RecordingControlPlane)
    return plane


@pytest.fixture(scope="session")
def template_arn(e2e_settings: E2ESettings, control_plane: LambdaMicrovmsControlPlane) -> str:
    return control_plane.resolve_template_arn(e2e_settings.template)


def live_sandbox_ids(control_plane: LambdaMicrovmsControlPlane, template_arn: str) -> list[str]:
    return [item.sandbox_id for item in control_plane.list_microvms(image_arn=template_arn)]


@pytest.fixture(scope="session", autouse=True)
def sandbox_sweeper(control_plane: RecordingControlPlane, template_arn: str) -> Iterator[None]:
    baseline = live_sandbox_ids(control_plane, template_arn)
    if len(baseline) > MAX_LIVE_TEST_SANDBOXES:
        pytest.fail(
            f"pre-flight: hay {len(baseline)} MicroVMs vivos de {template_arn}; "
            "limpia antes de correr"
        )
    yield
    plan = plan_session_sweep(
        baseline, control_plane.created_ids, live_sandbox_ids(control_plane, template_arn)
    )
    for sandbox_id in plan.terminate:
        with contextlib.suppress(SandboxNotFoundException):
            control_plane.terminate_microvm(sandbox_id)
    note = plan.report()
    if note is not None:
        print(f"\n{note}", flush=True)


BootTimings = dict[str, float]


def create_test_sandbox(
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
    boot_timings: BootTimings,
    *,
    timeout: int = TEST_SANDBOX_TIMEOUT_SECONDS,
    idle: IdlePolicy | None = None,
    telemetry: TelemetryExport | None = None,
) -> Sandbox:
    """`create()` con los parámetros de todo sandbox de test; `create()` sólo
    vuelve con `agent_ready` y `kernel_ready`, así que el tiempo medido es
    `run-microvm -> kernel_ready` (incluye la rotación del kernel en `/run`).
    `timeout` nunca supera los 1800 s del guardrail; `idle` sólo lo pasa M5;
    `telemetry`, m15-rayd-otlp."""
    assert timeout <= MAX_TEST_SANDBOX_TIMEOUT_SECONDS, "guardrail: timeout <= 1800 s"
    started = time.perf_counter()
    created = Sandbox.create(
        template_arn,
        template_version=e2e_settings.template_version,
        timeout=timeout,
        idle=idle,
        execution_role_arn=e2e_settings.execution_role_arn,
        ingress=["ALL_INGRESS"],
        logging=e2e_settings.logging,
        control_plane=control_plane,
        telemetry=telemetry,
    )
    elapsed = time.perf_counter() - started
    boot_timings[created.sandbox_id] = elapsed
    print(
        f"\n{created.sandbox_id}: run-microvm -> Health agent_ready y kernel_ready en "
        f"{elapsed:.2f} s (kernel_ready_s)",
        flush=True,
    )
    return created


@pytest.fixture(scope="session")
def boot_timings() -> BootTimings:
    return {}


@pytest.fixture
def sandbox(
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
    boot_timings: BootTimings,
) -> Iterator[Sandbox]:
    created = create_test_sandbox(e2e_settings, control_plane, template_arn, boot_timings)
    try:
        yield created
    finally:
        with contextlib.suppress(SandboxNotFoundException):
            created.kill()


def poly_template() -> str | None:
    return os.environ.get(POLY_TEMPLATE_VAR) or None


@pytest.fixture
def poly_sandbox(
    e2e_settings: E2ESettings, control_plane: LambdaMicrovmsControlPlane
) -> Iterator[Sandbox]:
    """Un sandbox de la variante `rayito-base-poly` (`RAYITO_TEMPLATE_POLY`,
    ARN o nombre), compartido por los e2e de kernels de M7 y M9; se salta si
    la variable falta y se termina en teardown."""
    template = poly_template()
    if not template:
        pytest.skip(f"exporta {POLY_TEMPLATE_VAR}=<arn|nombre> para probar los kernels poly")
    started = time.perf_counter()
    created = Sandbox.create(
        control_plane.resolve_template_arn(template),
        timeout=TEST_SANDBOX_TIMEOUT_SECONDS,
        idle=None,
        execution_role_arn=e2e_settings.execution_role_arn,
        ingress=["ALL_INGRESS"],
        logging=e2e_settings.logging,
        control_plane=control_plane,
    )
    print(f"\n[poly] poly image kernel_ready_s: {time.perf_counter() - started:.2f}", flush=True)
    try:
        yield created
    finally:
        with contextlib.suppress(SandboxNotFoundException):
            created.kill()
