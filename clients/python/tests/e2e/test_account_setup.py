"""`rayito setup` contra AWS real (ADR-030, openspec `account-setup`).

Variables (todas genéricas, nada de la cuenta va en el repositorio):

- `RAYITO_E2E=1`: sin ella todo se salta (marker `e2e`).
- `RAYITO_E2E_SETUP_REGION`: una región con Lambda MicroVMs donde `rayito-m0-iam`
  y `rayito-base` **no** existan. Sin ella se saltan los tests que crean.
  Si alguno de los dos existe, esos tests se saltan también: nunca se toca lo
  que ya estaba.
- `RAYITO_E2E_SETUP_BUCKET` (opcional): bucket de artefactos en esa región
  (`--bucket`); sin él, el del bootstrap de CDK.
- `RAYITO_E2E_SETUP_ARTIFACT` (opcional): zip de la imagen ya construido
  (`--artifact`); sin él se descarga la release `rayd-v<versión>`, que no
  existe todavía durante la aceptación de una release.
- `RAYITO_TEMPLATE` y `AWS_REGION`: los del resto de los e2e; sólo para el test
  de la región por defecto, que no escribe nada.

La CLI corre como la usaría cualquiera, `sys.executable -m rayito.cli` en un
subproceso. El teardown borra sólo lo que esta corrida creó: la imagen (con su
grupo de logs, `image_cleanup.BuiltImages`), la pila y la clave del artefacto
únicamente si `artifactUploaded` fue verdadero.

Cada test que crea construye `rayito-base` (≈ 10 min y ≈ $0,04 por semana de
almacenamiento de la versión, borrada al terminar).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import boto3
import pytest
from botocore.exceptions import ClientError

from rayito._account_setup._domain import BASE_IMAGE_NAME, IAM_STACK_NAME
from rayito._images import BUILDING_VERSION_STATES, DEFAULT_BUILD_TIMEOUT_SECONDS, account_image_arn
from rayito._stacks._cloudformation import CloudFormationProvisioner
from rayito._stacks._service import DEFAULT_WAIT_TIMEOUT_SECONDS

from .conftest import E2ESettings
from .image_cleanup import BuiltImages

pytestmark = pytest.mark.e2e

REGION_VAR = "RAYITO_E2E_SETUP_REGION"
BUCKET_VAR = "RAYITO_E2E_SETUP_BUCKET"
ARTIFACT_VAR = "RAYITO_E2E_SETUP_ARTIFACT"
CLI = (sys.executable, "-m", "rayito.cli")
#: Un `setup` completo: pila (segundos), descarga y build de la imagen (≈ 10 min)
#: más la cola de builds de la cuenta.
SETUP_BUDGET_SECONDS = DEFAULT_BUILD_TIMEOUT_SECONDS + DEFAULT_WAIT_TIMEOUT_SECONDS
DRY_RUN_BUDGET_SECONDS = 120.0
BUILD_POLL_SECONDS = 15.0
STACK_NOT_FOUND_MARKER = "does not exist"


def setup_region() -> str:
    region = os.environ.get(REGION_VAR)
    if not region:
        pytest.skip(f"este e2e necesita {REGION_VAR}=<región sin rayito-m0-iam ni rayito-base>")
    return region


def cli_arguments(region: str, *arguments: str) -> list[str]:
    extra = []
    if bucket := os.environ.get(BUCKET_VAR):
        extra += ["--bucket", bucket]
    if artifact := os.environ.get(ARTIFACT_VAR):
        extra += ["--artifact", artifact]
    return [*CLI, "--region", region, "--json", "setup", *arguments, *extra]


def run_setup(region: str, *arguments: str, budget: float) -> tuple[int, dict[str, Any], str]:
    completed = subprocess.run(
        cli_arguments(region, *arguments),
        capture_output=True,
        text=True,
        timeout=budget,
        check=False,
    )
    return completed.returncode, json.loads(completed.stdout), completed.stderr


class Account:
    """Lecturas de sólo lectura de la región de prueba y su limpieza."""

    def __init__(self, region: str) -> None:
        session = boto3.session.Session(region_name=region)
        self.region = region
        self.microvms = session.client("lambda-microvms")
        self.cloudformation = session.client("cloudformation")
        self.s3 = session.client("s3")
        self.logs = session.client("logs")
        self.account_id = str(session.client("sts").get_caller_identity()["Account"])
        self.image_arn = account_image_arn(region, self.account_id, BASE_IMAGE_NAME)

    def stack_exists(self) -> bool:
        try:
            self.cloudformation.describe_stacks(StackName=IAM_STACK_NAME)
        except ClientError as exc:
            if STACK_NOT_FOUND_MARKER in str(exc):
                return False
            raise
        return True

    def versions(self) -> list[dict[str, Any]]:
        try:
            paginator = self.microvms.get_paginator("list_microvm_image_versions")
            return [
                item
                for page in paginator.paginate(imageIdentifier=self.image_arn)
                for item in page["items"]
            ]
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                return []
            raise

    def image_exists(self) -> bool:
        try:
            self.microvms.get_microvm_image(imageIdentifier=self.image_arn)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                return False
            raise
        return True

    def building(self) -> bool:
        return any(item["state"] in BUILDING_VERSION_STATES for item in self.versions())

    def artifact_location(self) -> tuple[str, str] | None:
        """`(bucket, clave)` del zip de la versión más reciente, si hay una."""
        versions = self.versions()
        if not versions:
            return None
        uri = urlparse(
            str(max(versions, key=lambda item: item["createdAt"])["codeArtifact"]["uri"])
        )
        return uri.netloc, uri.path.lstrip("/")


@dataclass
class Created:
    """Lo que esta corrida creó, para borrar sólo eso."""

    account: Account
    artifact_uploaded: bool = False
    artifact: tuple[str, str] | None = None
    notes: list[str] = field(default_factory=list)

    def remember_artifact(self) -> None:
        self.artifact = self.account.artifact_location()

    def teardown(self) -> list[str]:
        account = self.account
        images = BuiltImages(
            account.microvms,
            resolve_arn=lambda name: account_image_arn(account.region, account.account_id, name),
            logs=account.logs,
        )
        images.track(BASE_IMAGE_NAME)
        failures = images.delete_all()
        provisioner = CloudFormationProvisioner(region=account.region)
        provisioner.delete(IAM_STACK_NAME)
        provisioner.wait(IAM_STACK_NAME, "deleted", DEFAULT_WAIT_TIMEOUT_SECONDS)
        if self.artifact_uploaded and self.artifact is not None:
            bucket, key = self.artifact
            account.s3.delete_object(Bucket=bucket, Key=key)
        return failures


@pytest.fixture
def clean_account() -> Iterator[Created]:
    """Una región donde no existe ni la pila ni la imagen; al terminar borra
    sólo lo que el test creó."""
    account = Account(setup_region())
    if account.stack_exists() or account.image_exists():
        pytest.skip(f"{IAM_STACK_NAME} o {BASE_IMAGE_NAME} ya existen: no se toca lo que había")
    created = Created(account)
    yield created
    failures = created.teardown()
    assert not failures, f"no se pudo borrar, hazlo a mano: {failures}"


def actions(document: dict[str, Any]) -> list[str]:
    return [step["action"] for step in document["steps"]]


def test_dry_run_plans_the_creation_and_writes_nothing() -> None:
    account = Account(setup_region())
    if account.stack_exists() or account.image_exists():
        pytest.skip(f"{IAM_STACK_NAME} o {BASE_IMAGE_NAME} ya existen: no se toca lo que había")
    code, document, stderr = run_setup(account.region, "--dry-run", budget=DRY_RUN_BUDGET_SECONDS)
    assert code == 0, stderr
    assert actions(document) == ["reuse", "create", "publish"]
    assert document["dryRun"] is True
    assert document["ready"] is False
    assert document["doctor"] is None
    assert not account.stack_exists()
    assert not account.image_exists()


def test_setup_prepares_the_account_and_a_second_run_changes_nothing(
    clean_account: Created,
) -> None:
    account = clean_account.account
    code, document, stderr = run_setup(account.region, "--yes", budget=SETUP_BUDGET_SECONDS)
    clean_account.remember_artifact()
    assert code == 0, stderr
    assert document["ready"] is True
    assert document["error"] is None
    result = document["result"]
    clean_account.artifact_uploaded = result["artifactUploaded"] is True
    assert result["imageArn"] == account.image_arn
    versions_after_first_run = len(account.versions())
    assert versions_after_first_run == 1

    code, document, stderr = run_setup(account.region, budget=SETUP_BUDGET_SECONDS)
    assert code == 0, stderr
    assert actions(document) == ["reuse", "reuse", "reuse"]
    assert document["ready"] is True
    assert document["result"]["artifactUploaded"] is None
    assert len(account.versions()) == versions_after_first_run


def test_an_interrupted_setup_resumes_without_a_second_build(clean_account: Created) -> None:
    account = clean_account.account
    process = subprocess.Popen(
        cli_arguments(account.region, "--yes"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + SETUP_BUDGET_SECONDS
    try:
        while not account.building():
            if process.poll() is not None:
                pytest.fail("setup terminó antes de que hubiera un build en curso")
            if time.monotonic() > deadline:
                pytest.fail("no apareció ningún build en curso a tiempo")
            time.sleep(BUILD_POLL_SECONDS)
    finally:
        process.kill()
        process.wait()

    code, document, stderr = run_setup(account.region, "--yes", budget=SETUP_BUDGET_SECONDS)
    assert code == 0, stderr
    assert document["ready"] is True
    assert len(account.versions()) == 1


def test_a_prepared_default_region_is_three_reuse_and_no_write(e2e_settings: E2ESettings) -> None:
    if not e2e_settings.region:
        pytest.skip("este e2e necesita AWS_REGION: la región de la cuenta ya preparada")
    account = Account(e2e_settings.region)
    versions_before = len(account.versions())
    code, document, stderr = run_setup(
        e2e_settings.region, "--dry-run", budget=DRY_RUN_BUDGET_SECONDS
    )
    assert code == 0, stderr
    assert actions(document) == ["reuse", "reuse", "reuse"]
    assert document["ready"] is True
    assert len(account.versions()) == versions_before
