"""Las funciones opcionales contra Floci (`make local-e2e`): `OptionalStacks`
(CloudFormation), el índice de metadatos (DynamoDB), `SecretStore` y
`secrets=`/`gateways=` (Secrets Manager + `ConfigureSandbox` en el guest) y
`Template.build` (subida del zip a S3 + `create-microvm-image`).

Floci aplica las plantillas sin crear nada facturable y converge al
instante; lo que mide coste, cuotas o permisos IAM reales sigue en los e2e
contra AWS (`tests/e2e`)."""

from __future__ import annotations

import contextlib
import secrets as stdlib_secrets
from typing import Final

import boto3
import pytest

from rayito import (
    DynamoDbIndex,
    OptionalStacks,
    Sandbox,
    SecretCache,
    SecretGateway,
    SecretRef,
    SecretStore,
    Template,
)
from rayito.exceptions import SandboxNotFoundException

from .conftest import LOCAL_BUILD_ROLE_ARN, LOCAL_IMAGE_NAME, LocalSettings, create_local_sandbox
from .guest import LocalGuestControlPlane

pytestmark = pytest.mark.local

DEPLOYED: Final = "CREATE_COMPLETE"
UPDATED: Final = "UPDATE_COMPLETE"
#: Lo mínimo que cada componente necesita para desplegarse; los ARN son de
#: Floci (cuenta 000000000000), que no los resuelve.
COMPONENT_PARAMETERS: Final[dict[str, dict[str, str]]] = {
    "metadata-index": {},
    "secrets-access": {},
    "otlp-export": {},
    "s3-mounts": {"BucketName": "rayito-local-mounts"},
    "sizes-guard": {
        "ImageArns": f"arn:aws:lambda:us-east-1:000000000000:microvm-image:{LOCAL_IMAGE_NAME}"
    },
    "templates": {
        "ArtifactBucketArn": "arn:aws:s3:::rayito-local-artifacts",
        "BuildRoleArn": LOCAL_BUILD_ROLE_ARN,
    },
}
#: Los que despliegan un zip de Lambda al bucket de artefactos.
ARTIFACT_COMPONENTS: Final = frozenset({"events-webhooks"})


def run_suffix() -> str:
    return stdlib_secrets.token_hex(4)


@pytest.mark.parametrize("component", sorted(COMPONENT_PARAMETERS))
def test_optional_stack_deploy_status_destroy(
    component: str, aws_session: boto3.session.Session
) -> None:
    stacks = OptionalStacks(session=aws_session)
    name = f"rayito-local-{component}-{run_suffix()}"
    deployed = stacks.deploy(component, stack_name=name, parameters=COMPONENT_PARAMETERS[component])
    try:
        assert deployed.state == DEPLOYED
        status = stacks.status(component, stack_name=name)
        assert status is not None
        assert status.outputs == deployed.outputs
    finally:
        stacks.destroy(component, stack_name=name)
    assert stacks.status(component, stack_name=name) is None


def test_redeploy_keeps_the_parameters_it_was_not_given(aws_session: boto3.session.Session) -> None:
    """`UsePreviousValue` en `UpdateStack` (optional-stacks-redeploy-keeps-
    parameters): redesplegar sin repetir `TableName` no reemplaza la tabla."""
    stacks = OptionalStacks(session=aws_session)
    name = f"rayito-local-index-{run_suffix()}"
    table = f"rayito-local-{run_suffix()}"
    stacks.deploy("metadata-index", stack_name=name, parameters={"TableName": table})
    try:
        redeployed = stacks.deploy(
            "metadata-index", stack_name=name, parameters={"DeletionProtection": "false"}
        )
        assert redeployed.state in {DEPLOYED, UPDATED}
        assert redeployed.parameters["TableName"] == table
    finally:
        stacks.destroy("metadata-index", stack_name=name)


def test_metadata_index_lists_by_metadata(
    local_settings: LocalSettings,
    control_plane: LocalGuestControlPlane,
    template_arn: str,
    aws_session: boto3.session.Session,
) -> None:
    stacks = OptionalStacks(session=aws_session)
    name = f"rayito-local-index-{run_suffix()}"
    table = f"rayito-local-{run_suffix()}"
    stacks.deploy("metadata-index", stack_name=name, parameters={"TableName": table})
    try:
        index = DynamoDbIndex(table, session=aws_session)
        run = run_suffix()
        sandbox = create_local_sandbox(
            local_settings,
            control_plane,
            template_arn,
            metadata={"suite": "local", "run": run},
            index=index,
        )
        try:
            found = list(
                Sandbox.list(metadata={"run": run}, index=index, control_plane=control_plane)
            )
            assert [item.sandbox_id for item in found] == [sandbox.sandbox_id]
            assert found[0].metadata == {"suite": "local", "run": run}
        finally:
            with contextlib.suppress(SandboxNotFoundException):
                sandbox.kill()
    finally:
        stacks.destroy("metadata-index", stack_name=name)


def test_secret_store_and_secret_injection(
    local_settings: LocalSettings,
    control_plane: LocalGuestControlPlane,
    template_arn: str,
    aws_session: boto3.session.Session,
) -> None:
    store = SecretStore(session=aws_session)
    name = f"local-{run_suffix()}"
    value = f"sentinel-{stdlib_secrets.token_hex(8)}"
    info = store.create(name, value)
    try:
        assert info.version == 1
        # Floci 2.1.0 ignora el `ClientRequestToken` de `CreateSecret` (le pone
        # un UUID), así que la versión que `update` deriva de los tokens no es
        # la de AWS: aquí solo se comprueba el valor nuevo
        # (docs/research/2026-10-local-testing.md, diferencias de Floci).
        store.update(name, f"{value}-2")
        cache = SecretCache(store=store)
        sandbox = create_local_sandbox(
            local_settings,
            control_plane,
            template_arn,
            secrets={"RAYITO_LOCAL_SECRET": name},
            secret_cache=cache,
        )
        try:
            printed = sandbox.commands.run("printenv RAYITO_LOCAL_SECRET").stdout.strip()
            assert printed == f"{value}-2"
            assert value not in repr(sandbox._launch_options)
        finally:
            with contextlib.suppress(SandboxNotFoundException):
                sandbox.kill()
    finally:
        assert store.destroy(name) is True


def test_secret_gateway_route_is_applied_in_the_guest(
    local_settings: LocalSettings,
    control_plane: LocalGuestControlPlane,
    template_arn: str,
    aws_session: boto3.session.Session,
) -> None:
    """La sección `gateways=` de `ConfigureSandbox`: el SDK resuelve la
    cabecera contra Secrets Manager (Floci) y `rayd` abre el listener de
    loopback. El reenvío al upstream HTTPS no se cubre en local."""
    store = SecretStore(session=aws_session)
    name = f"local-gw-{run_suffix()}"
    store.create(name, "Bearer local")
    try:
        gateway = SecretGateway(
            upstream="https://api.example.com",
            headers={"Authorization": SecretRef(name)},
            allow=[("GET", "/v1/*")],
        )
        sandbox = create_local_sandbox(
            local_settings,
            control_plane,
            template_arn,
            gateways={"api": gateway},
            secret_cache=SecretCache(store=store),
        )
        try:
            status = sandbox.gateways["api"]
            assert status.last_error_class is None
            assert status.port > 0
            connect = f"import socket; socket.create_connection(('127.0.0.1', {status.port}))"
            probe = sandbox.commands.run(f'python3 -c "{connect}"')
            assert probe.exit_code == 0
        finally:
            with contextlib.suppress(SandboxNotFoundException):
                sandbox.kill()
    finally:
        store.destroy(name)


def test_template_build_uploads_the_artifact_and_creates_the_image(
    template_arn: str, artifact_bucket: str, aws_session: boto3.session.Session
) -> None:
    template = (
        Template()
        .from_base_image(LOCAL_IMAGE_NAME)
        .pip_install(["httpx"])
        .set_envs({"RAYITO_LOCAL_TEMPLATE": "1"})
    )
    name = f"rayito-local-tpl-{run_suffix()}"
    # El primer build va con `force=True`: Floci 2.1.0 responde
    # `ResourceNotFoundException` a `ListMicrovmImageVersions` de una imagen
    # que aún no existe, y la búsqueda de reuso empieza por ahí
    # (docs/research/2026-10-local-testing.md, diferencias de Floci).
    first = Template.build(template, name, bucket=artifact_bucket, force=True, session=aws_session)
    assert first.template_id.endswith(f":microvm-image:{name}")
    assert Template.exists(name, session=aws_session) is True
    reused = Template.build(template, name, bucket=artifact_bucket, session=aws_session)
    assert reused.template_id == first.template_id
    uploaded = aws_session.client("s3").list_objects_v2(Bucket=artifact_bucket)
    assert uploaded.get("KeyCount", 0) >= 1
