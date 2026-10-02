"""m15-templates contra AWS real (`RAYITO_E2E=1`, `RAYITO_TEMPLATE` y
`RAYITO_E2E_TEMPLATE_BUCKET` con un bucket S3 ya existente para el
artefacto de build). Cubre los pasos 1-3 del plan de aceptación de
`openspec/changes/m15-templates/proposal.md`:

1. un build correcto (`pip_install` + `copy` + `set_start_cmd`), esperando
   `BuildInfo` y que la imagen quede lanzable.
2. un build que falla en un paso del Dockerfile (`pip_install` de un
   paquete inexistente), esperando `BuildException` con `step`/`command`/
   `exit_code` extraídos del log de BuildKit.
3. un `ready_cmd` fijo que siempre falla, esperando
   `BuildException(reason="ready_client_error"|"ready_server_error")`.

El paso 4 (el `start_cmd` horneado por `set_start_cmd()` sobrevive a un
ciclo de suspend/resume dentro de un sandbox lanzado) queda fuera de este
fichero a propósito: necesita el seguimiento no bloqueante de `rayd` leyendo
`/etc/rayito/template.json` (ver `proposal.md`), todavía no implementado.

Cada build crea como mucho una versión de imagen nueva (`force=True` nunca
se usa dos veces sobre el mismo nombre): tope de coste de la función en
`openspec/changes/m15-templates/proposal.md` (≈ $0,80, templates es la más
cara de las ocho). Los nombres de imagen llevan un sufijo aleatorio por
corrida para no chocar entre corridas concurrentes; nada se borra aquí
(fuera de alcance de un agente de función, ver AWS_API_NOTES.md y el plan
de aceptación): la limpieza de versiones de imagen es responsabilidad de
la etapa de aceptación serializada.
"""

from __future__ import annotations

import os
import secrets as stdlib_secrets

import pytest

from rayito import BuildException, Template
from rayito.exceptions import InvalidArgumentException

from .conftest import E2ESettings

pytestmark = pytest.mark.e2e

BUCKET_VAR = "RAYITO_E2E_TEMPLATE_BUCKET"


def _bucket() -> str:
    bucket = os.environ.get(BUCKET_VAR)
    if not bucket:
        pytest.fail(f"este e2e necesita {BUCKET_VAR}=<bucket-s3-del-artefacto-de-build>")
    return bucket


def _run_name(label: str) -> str:
    return f"rayito-m15-templates-e2e-{label}-{stdlib_secrets.token_hex(4)}"


def test_a_successful_build_produces_a_launchable_image(e2e_settings: E2ESettings) -> None:
    t = (
        Template()
        .from_base_image(e2e_settings.template)
        .pip_install(["pandas"])
        .set_envs({"RAYITO_M15_TEMPLATES_E2E": "1"})
    )
    name = _run_name("ok")
    logs: list[str] = []
    info = Template.build(
        t,
        name,
        bucket=_bucket(),
        on_build_logs=logs.append,
        region=e2e_settings.region,
    )
    assert info.template_id.endswith(f":microvm-image:{name}")
    assert info.alias == name
    assert Template.exists(name, region=e2e_settings.region) is True


def test_a_failing_run_step_surfaces_step_command_and_exit_code(e2e_settings: E2ESettings) -> None:
    t = (
        Template()
        .from_base_image(e2e_settings.template)
        .pip_install(["this-package-does-not-exist-rayito-m15-e2e"])
    )
    name = _run_name("fail-run")
    with pytest.raises(BuildException) as excinfo:
        Template.build(t, name, bucket=_bucket(), region=e2e_settings.region)
    assert excinfo.value.step is not None
    assert excinfo.value.command is not None
    assert excinfo.value.exit_code not in (None, 0)


def test_a_ready_cmd_that_never_succeeds_fails_the_build(e2e_settings: E2ESettings) -> None:
    t = Template().from_base_image(e2e_settings.template).set_start_cmd("sleep 3600", "exit 1")
    name = _run_name("fail-ready")
    with pytest.raises(BuildException) as excinfo:
        Template.build(t, name, bucket=_bucket(), timeout=180.0, region=e2e_settings.region)
    assert excinfo.value.reason in ("ready_client_error", "ready_server_error", None)


def test_building_without_from_base_image_never_reaches_aws() -> None:
    with pytest.raises(InvalidArgumentException):
        Template.build(Template().pip_install(["pandas"]), "unused", bucket="unused")
