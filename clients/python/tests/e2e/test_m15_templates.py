"""m15-templates contra AWS real (`RAYITO_E2E=1`, `RAYITO_TEMPLATE` y
`RAYITO_E2E_TEMPLATE_BUCKET` con un bucket S3 ya existente para el
artefacto de build). Cubre los pasos 1-3 del plan de aceptación de
`openspec/changes/archive/2026-10-03-m15-templates/proposal.md`:

1. un build correcto (`pip_install` + `copy` + `set_start_cmd`), esperando
   `BuildInfo` y que la imagen quede lanzable.
2. un build que falla en un paso del Dockerfile (`pip_install` de un
   paquete inexistente), esperando `BuildException` con `step`/`command`/
   `exit_code` extraídos del log de BuildKit.
3. un `ready_cmd` fijo que siempre falla, esperando
   `BuildException(reason="ready_client_error"|"ready_server_error")`.

El paso 4 (el `start_cmd` horneado por `set_start_cmd()` sobrevive a un
ciclo de suspend/resume dentro de un sandbox lanzado) lo cubre la etapa de
aceptación a mano (necesita una `rayito-base` publicada con el `rayd` de
esta versión, que lee `/etc/rayito/template.json`).

Cada build crea como mucho una versión de imagen nueva (`force=True` nunca
se usa dos veces sobre el mismo nombre): tope de coste de la función en
`openspec/changes/archive/2026-10-03-m15-templates/proposal.md` (≈ $0,80, templates es la más
cara de las ocho). Los nombres de imagen llevan un sufijo aleatorio por
corrida para no chocar entre corridas concurrentes. El fixture
`built_images` borra al terminar cada test (pase o falle) la imagen que
construyó, con sus versiones y su grupo de logs `/rayito/<nombre>`
(`image_cleanup.py`); lo que no pueda borrar hace fallar el test con el
nombre, para borrarlo a mano. Necesita `lambda:DeleteMicrovmImage` y
`logs:DeleteLogGroup` sobre `/rayito/*` en la identidad de la aceptación.
"""

from __future__ import annotations

import os
import secrets as stdlib_secrets
import time
from collections.abc import Iterator

import pytest

from rayito import BuildException, Sandbox, Template
from rayito._aws import LambdaMicrovmsControlPlane
from rayito._templates._dsl import NODE_PACKAGES, NPM_MISSING_EXIT_CODE
from rayito.exceptions import InvalidArgumentException, SandboxNotFoundException

from .conftest import E2ESettings
from .image_cleanup import BuiltImages

pytestmark = pytest.mark.e2e

BUCKET_VAR = "RAYITO_E2E_TEMPLATE_BUCKET"
#: Build (~115 s, Q85) + el plazo por defecto de un `ready_cmd` crudo (60 s,
#: `DEFAULT_READY_TIMEOUT_SECONDS`) tras el cual `rayd` responde 500, con
#: holgura para la cola de builds de la cuenta.
READY_FAIL_BUDGET_SECONDS = 600.0


def _bucket() -> str:
    bucket = os.environ.get(BUCKET_VAR)
    if not bucket:
        pytest.skip(f"este e2e necesita {BUCKET_VAR}=<bucket-s3-del-artefacto-de-build>")
    return bucket


def _run_name(label: str) -> str:
    return f"rayito-m15-templates-e2e-{label}-{stdlib_secrets.token_hex(4)}"


@pytest.fixture
def built_images(control_plane: LambdaMicrovmsControlPlane) -> Iterator[BuiltImages]:
    """Las imágenes que construye el test y sus grupos de logs; se borran al
    terminar, también si el test falla, y lo que no se pudo borrar hace
    fallar el teardown."""
    session = control_plane.session
    assert session is not None, "control_plane se construye con from_session()"
    microvms = session.client("lambda-microvms", region_name=control_plane.region)
    logs = session.client("logs", region_name=control_plane.region)
    images = BuiltImages(microvms, resolve_arn=control_plane.resolve_template_arn, logs=logs)
    yield images
    failures = images.delete_all()
    if failures:
        pytest.fail("imágenes de test sin borrar (bórralas a mano): " + "; ".join(failures))


def test_a_successful_build_produces_a_launchable_image(
    e2e_settings: E2ESettings, built_images: BuiltImages
) -> None:
    t = (
        Template()
        .from_base_image(e2e_settings.template)
        .pip_install(["pandas"])
        .set_envs({"RAYITO_M15_TEMPLATES_E2E": "1"})
    )
    name = built_images.track(_run_name("ok"))
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


def test_a_failing_run_step_surfaces_step_command_and_exit_code(
    e2e_settings: E2ESettings, built_images: BuiltImages
) -> None:
    t = (
        Template()
        .from_base_image(e2e_settings.template)
        .pip_install(["this-package-does-not-exist-rayito-m15-e2e"])
    )
    name = built_images.track(_run_name("fail-run"))
    with pytest.raises(BuildException) as excinfo:
        Template.build(t, name, bucket=_bucket(), region=e2e_settings.region)
    assert excinfo.value.step is not None
    assert excinfo.value.command is not None
    assert excinfo.value.exit_code not in (None, 0)


def test_a_ready_cmd_that_never_succeeds_fails_the_build(
    e2e_settings: E2ESettings, built_images: BuiltImages
) -> None:
    t = Template().from_base_image(e2e_settings.template).set_start_cmd("sleep 3600", "exit 1")
    name = built_images.track(_run_name("fail-ready"))
    with pytest.raises(BuildException) as excinfo:
        Template.build(
            t, name, bucket=_bucket(), timeout=READY_FAIL_BUDGET_SECONDS, region=e2e_settings.region
        )
    assert excinfo.value.reason == "ready_server_error"


def test_building_without_from_base_image_never_reaches_aws() -> None:
    with pytest.raises(InvalidArgumentException):
        Template.build(Template().pip_install(["pandas"]), "unused", bucket="unused")


#: Paquete pequeño de los repos de AL2023 para comprobar `dnf_install`.
DNF_PROBE_PACKAGE = "tree"
NPM_PROBE_PACKAGE = "left-pad@1.3.0"
#: WORKDIR explícito: `npm ls --prefix` no depende del cwd del sandbox.
INSTALL_WORKDIR = "/opt/install-probe"
#: Usuario no root previo a `dnf_install`: comprueba que el paso sube a root.
NON_ROOT_USER = "1000"
#: `delete-microvm-image` rechaza una imagen con MicroVMs en marcha: tras
#: `kill()` se espera a `TERMINATED` antes del teardown (medido 2026-10-14:
#: `ValidationException` "Cannot delete microvm image with running microvms").
TERMINATED_STATE = "TERMINATED"
TERMINATE_WAIT_SECONDS = 180.0
TERMINATE_POLL_SECONDS = 5.0
INSTALL_SANDBOX_TIMEOUT_SECONDS = 300


def _wait_terminated(control_plane: LambdaMicrovmsControlPlane, sandbox_id: str) -> None:
    deadline = time.monotonic() + TERMINATE_WAIT_SECONDS
    while time.monotonic() < deadline:
        try:
            if control_plane.get_microvm(sandbox_id).state == TERMINATED_STATE:
                return
        except SandboxNotFoundException:
            return
        time.sleep(TERMINATE_POLL_SECONDS)
    pytest.fail(f"{sandbox_id} sigue vivo tras {TERMINATE_WAIT_SECONDS} s")


def test_dnf_and_npm_install_shortcuts_are_usable_in_a_sandbox(
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    built_images: BuiltImages,
) -> None:
    t = (
        Template()
        .from_base_image(e2e_settings.template)
        .set_user(NON_ROOT_USER)
        .dnf_install([DNF_PROBE_PACKAGE, *NODE_PACKAGES])
        .set_user("root")
        .run_cmd(f"mkdir -p {INSTALL_WORKDIR}")
        .workdir(INSTALL_WORKDIR)
        .npm_install(NPM_PROBE_PACKAGE)
    )
    name = built_images.track(_run_name("install"))
    Template.build(t, name, bucket=_bucket(), region=e2e_settings.region)
    sbx = Sandbox.create(
        control_plane.resolve_template_arn(name),
        timeout=INSTALL_SANDBOX_TIMEOUT_SECONDS,
        idle=None,
        execution_role_arn=e2e_settings.execution_role_arn,
        logging=e2e_settings.logging,
        control_plane=control_plane,
    )
    try:
        assert sbx.commands.run(f"{DNF_PROBE_PACKAGE} --version").exit_code == 0
        assert sbx.commands.run("node --version").exit_code == 0
        assert sbx.commands.run(f"npm ls --prefix {INSTALL_WORKDIR} left-pad").exit_code == 0
    finally:
        sbx.kill()
        _wait_terminated(control_plane, sbx.sandbox_id)


def test_npm_install_without_node_fails_with_a_clear_step(
    e2e_settings: E2ESettings, built_images: BuiltImages
) -> None:
    t = Template().from_base_image(e2e_settings.template).npm_install(NPM_PROBE_PACKAGE)
    name = built_images.track(_run_name("no-node"))
    with pytest.raises(BuildException) as excinfo:
        Template.build(t, name, bucket=_bucket(), region=e2e_settings.region)
    assert excinfo.value.exit_code == NPM_MISSING_EXIT_CODE
