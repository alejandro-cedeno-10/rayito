"""`Sandbox.create(volumes=...)` y el `volume_mounts` del shim de E2B contra
AWS real (`RAYITO_E2E=1`, `m15-efs-volumes`): monta un volumen de lectura y
escritura y otro de sólo lectura sobre una pila `efs-volumes` ya desplegada,
escribe y lee como el usuario del sandbox (uid 1000), comprueba que el de
sólo lectura da `EROFS` y que el shim monta por el mismo camino.

Necesita, además de las variables habituales de `conftest.py`
(`RAYITO_EXECUTION_ROLE_ARN` con la política `CallerPolicyArn` de la pila):
- `RAYITO_E2E_EFS_STACK`: el nombre de una pila `efs-volumes` desplegada
  (`EfsVolumes.deploy`).
- `RAYITO_E2E_EFS_TEMPLATE`: una imagen con `amazon-efs-utils` (ARN o
  nombre; `rayito image publish --with-efs --os-capabilities ALL`).

Nunca imprime un id de la pila, del sistema de ficheros ni de la red: sólo
estados y códigos. Crea dos access points efímeros y los borra al final.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import Iterator
from dataclasses import replace

import pytest

from rayito import EfsVolume, EfsVolumes, Sandbox, VolumeStore
from rayito.e2b import E2B
from rayito.exceptions import CommandExitException, SandboxNotFoundException

from .conftest import E2ESettings

pytestmark = pytest.mark.e2e

STACK_VAR = "RAYITO_E2E_EFS_STACK"
TEMPLATE_VAR = "RAYITO_E2E_EFS_TEMPLATE"
CONNECTOR_OUTPUT = "ConnectorArn"
READ_ONLY_ERROR = "Read-only file system"
SANDBOX_TIMEOUT_SECONDS = 600

requires_stack = pytest.mark.skipif(
    not (os.environ.get(STACK_VAR) and os.environ.get(TEMPLATE_VAR)),
    reason=f"necesita {STACK_VAR} y {TEMPLATE_VAR}",
)


def report(label: str, value: object) -> None:
    print(f"[efs-volumes mount e2e] {label}: {value}")


@pytest.fixture(scope="module")
def efs(e2e_settings: E2ESettings) -> EfsVolumes:
    if not e2e_settings.execution_role_arn:
        pytest.skip("necesita RAYITO_EXECUTION_ROLE_ARN con la política de la pila")
    return EfsVolumes(stack_name=os.environ[STACK_VAR], region=e2e_settings.region)


@pytest.fixture(scope="module")
def connector_arn(efs: EfsVolumes) -> str:
    status = efs.status()
    if status is None:
        pytest.fail(f"la pila de {STACK_VAR} no existe")
    return status.outputs[CONNECTOR_OUTPUT]


@pytest.fixture(scope="module")
def store(efs: EfsVolumes) -> VolumeStore:
    return efs.volume_store()


@pytest.fixture(scope="module")
def volumes(store: VolumeStore) -> Iterator[tuple[EfsVolume, EfsVolume]]:
    suffix = uuid.uuid4().hex[:8]
    names = (f"e2e-rw-{suffix}", f"e2e-ro-{suffix}")
    created = [store.create(name) for name in names]
    try:
        yield created[0], replace(created[1], read_only=True)
    finally:
        for name in names:
            with contextlib.suppress(Exception):
                store.destroy(name)


def launch(e2e_settings: E2ESettings, connector_arn: str, **volumes: EfsVolume) -> Sandbox:
    return Sandbox.create(
        os.environ[TEMPLATE_VAR],
        timeout=SANDBOX_TIMEOUT_SECONDS,
        execution_role_arn=e2e_settings.execution_role_arn,
        egress=[connector_arn],
        region=e2e_settings.region,
        volumes={f"/mnt/{name}": volume for name, volume in volumes.items()},
    )


@requires_stack
def test_create_mounts_read_write_and_read_only_volumes(
    e2e_settings: E2ESettings, connector_arn: str, volumes: tuple[EfsVolume, EfsVolume]
) -> None:
    read_write, read_only = volumes
    sandbox = launch(e2e_settings, connector_arn, rw=read_write, ro=read_only)
    try:
        states = {path: status.state for path, status in sandbox.volumes.items()}
        report("states", states)
        assert states == {"/mnt/rw": "mounted", "/mnt/ro": "mounted"}
        written = sandbox.commands.run("echo hola > /mnt/rw/e2e.txt && cat /mnt/rw/e2e.txt")
        assert written.stdout.strip() == "hola"
        with pytest.raises(CommandExitException) as refused:
            sandbox.commands.run("touch /mnt/ro/e2e.txt")
        report("read-only exit", refused.value.exit_code)
        assert READ_ONLY_ERROR in refused.value.stderr
    finally:
        with contextlib.suppress(SandboxNotFoundException):
            sandbox.kill()


@requires_stack
def test_the_e2b_shim_volume_mounts_through_the_connector(
    e2e_settings: E2ESettings, connector_arn: str, store: VolumeStore
) -> None:
    """`client.Volume.create` (CRUD over the bound store) and
    `client.Sandbox.create(volume_mounts=...)` with both a `Volume` and its
    name: the shim launches only through `volume_connector_arn`."""
    client = E2B(region=e2e_settings.region, volume_store=store, volume_connector_arn=connector_arn)
    volume = client.Volume.create(f"e2e-shim-{uuid.uuid4().hex[:8]}")
    try:
        sandbox = client.Sandbox.create(
            os.environ[TEMPLATE_VAR],
            timeout=SANDBOX_TIMEOUT_SECONDS,
            execution_role_arn=e2e_settings.execution_role_arn,
            volume_mounts={"/mnt/shared": volume, "/mnt/again": volume.volume_id},
        )
        try:
            written = sandbox.commands.run(
                "echo shim > /mnt/shared/shim.txt && cat /mnt/again/shim.txt"
            )
            assert written.stdout.strip() == "shim"
        finally:
            with contextlib.suppress(SandboxNotFoundException):
                sandbox.kill()
    finally:
        assert client.Volume.destroy(volume.volume_id) in (True, False)
