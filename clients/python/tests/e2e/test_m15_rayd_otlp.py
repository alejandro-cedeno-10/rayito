"""m15-rayd-otlp contra AWS real (`RAYITO_E2E=1`, cap $0.35, MILESTONES.md
§8). Exige que `RAYITO_TEMPLATE` apunte a una imagen `rayito-base-caps` (o
derivada) con el `rayd` del tag `rayd-v0.6.0` o posterior y
`RAYITO_EXECUTION_ROLE_ARN` con la política `RayitoOtlpExport`
(`infra/otlp-export.yaml`, `rayito stack deploy otlp-export`) adjunta.

Cubre, de los Q `OT*` reservados por la investigación
(`docs/research/2026-10-e2b-out-of-scope.md` §6.8, Q-números reales
asignados por la etapa de aceptación, ≥ Q95):

- **OT1**: el `PutMetricData` firmado llega y `Health.features.telemetry_export`
  se activa.
- **OT5**: la exportación sobrevive un ciclo `/suspend`/`/resume`, con un
  vaciado dentro del presupuesto de `/suspend`.
- Una imagen cuyo nombre no permite saber la variante (un ARN) y sin
  execution role: `rayd` rechaza la sección (`role_not_permitted`), el SDK
  termina el sandbox y lanza `SandboxException`. Sobre un ARN no hay
  chequeo de caps antes de lanzar (ver la guía): con rol, `rayd` exporta.

No toca AWS si `RAYITO_E2E` no está a `1` (ver `conftest.py`): este fichero
se recolecta siempre, pero sus casos se saltan por defecto.
"""

from __future__ import annotations

import contextlib
import time

import pytest

from rayito import OtlpAuth, Sandbox, TelemetryExport
from rayito._aws import LambdaMicrovmsControlPlane
from rayito.exceptions import SandboxException, SandboxNotFoundException

from .conftest import BootTimings, E2ESettings, create_test_sandbox

pytestmark = pytest.mark.e2e

#: OT5 mide una pausa corta; más que suficiente para que un vaciado de
#: `/suspend` (acotado a 2 s en el agente) y un primer export tras
#: `/resume` tengan tiempo de completarse sin acercarse al cap de coste.
SUSPEND_PAUSE_SECONDS = 70
#: Cuánto esperar, tras crear el sandbox, a que el primer lote (muestreado
#: cada 5 s por el anillo de `MetricsHistory`) llegue a exportarse.
FIRST_EXPORT_POLL_SECONDS = 20
#: El mínimo que acepta `TelemetryExport` (15..=300 s): el primer lote llega
#: dentro de `FIRST_EXPORT_POLL_SECONDS`.
E2E_EXPORT_INTERVAL_SECONDS = 15
FIRST_EXPORT_POLL_INTERVAL_SECONDS = 2


def _wait_for_first_export(sbx: Sandbox) -> None:
    deadline = time.monotonic() + FIRST_EXPORT_POLL_SECONDS
    while time.monotonic() < deadline:
        status = sbx.get_telemetry_status()
        if status.exported > 0:
            return
        time.sleep(FIRST_EXPORT_POLL_INTERVAL_SECONDS)
    pytest.fail("OT1: ningún lote se exportó dentro del plazo esperado")


def test_execution_role_auth_exports_and_survives_a_suspend_resume_cycle(
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
    boot_timings: BootTimings,
) -> None:
    """OT1 + OT5: `rayd` exporta con `OtlpAuth.execution_role()` y sigue
    exportando después de un `/suspend`/`/resume`."""
    if not e2e_settings.execution_role_arn:
        pytest.skip(f"exporta {e2e_settings.execution_role_arn!r}: hace falta un execution role")
    sbx = create_test_sandbox(
        e2e_settings,
        control_plane,
        template_arn,
        boot_timings,
        telemetry=TelemetryExport(interval_s=E2E_EXPORT_INTERVAL_SECONDS),
    )
    try:
        health = sbx.get_health()
        assert health.lifecycle is not None, "se espera un rayd >= 0.3.0"
        _wait_for_first_export(sbx)
        before = sbx.get_telemetry_status()

        sbx.pause()
        time.sleep(SUSPEND_PAUSE_SECONDS)
        sbx.connect()

        _wait_for_first_export(sbx)
        after = sbx.get_telemetry_status()
        assert after.exported > before.exported, "OT5: no hubo exportación nueva tras /resume"
        assert after.dropped == before.dropped, "no se esperaban lotes descartados en este camino"
    finally:
        with contextlib.suppress(SandboxNotFoundException):
            sbx.kill()


def test_execution_role_auth_without_a_role_terminates_and_raises(
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
) -> None:
    """Confirma contra AWS real que `OtlpAuth.execution_role()` sin execution
    role se rechaza después de `/run` (sobre un ARN el SDK no puede decidir
    antes): `rayd` responde `role_not_permitted` y el sandbox no queda
    facturando."""
    with pytest.raises(SandboxException, match="role_not_permitted"):
        Sandbox.create(
            template_arn,
            telemetry=TelemetryExport(auth=OtlpAuth.execution_role()),
            control_plane=control_plane,
        )
    # Si `create()` hubiera dejado algo vivo, el `sandbox_sweeper` de
    # `conftest.py` lo detecta y falla el pre-flight de la siguiente sesión.
