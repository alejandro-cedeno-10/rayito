"""`AccountSetup` sobre los dobles en memoria: el plan sólo lee, `apply`
escribe en el orden del diseño D6 y nunca de más, y una descarga manipulada
no publica nada."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from rayito import __version__ as SDK_VERSION
from rayito._account_setup._domain import (
    AccountSetupException,
    ImageGateResult,
    SetupPlan,
    SetupRequest,
    SetupResult,
)
from rayito._account_setup._service import AccountSetup

from .fake_account_setup import (
    ARTIFACT_SHA256,
    BUILD_ROLE_ARN,
    IMAGE_VERSION_PUBLISHED,
    FakeAccount,
    from_vector_input,
    load_cases,
)

VECTORS = {case["name"]: case["input"] for case in load_cases("plan-vectors.json")}
FRESH = "cuenta nueva con el bucket del bootstrap de CDK"
PREPARED = "cuenta preparada a mano: todo se reutiliza"
CHANGED_BUCKET = "--bucket distinto del de la pila actualiza ArtifactBucket"
STACK_IN_PROGRESS = "pila en curso: se espera, sin coste nuevo"
BUILD_IN_PROGRESS = "build en curso: se espera en vez de lanzar otro"
ARTIFACT = "--artifact publica ese zip en vez de descargar la release"
UNSUPPORTED = "región sin Lambda MicroVMs"
NO_BUCKET = "sin ningún bucket: se crea uno con el nombre por defecto"
NAMED_MISSING = "--bucket que no existe: se crea con ese nombre"
BUCKET_STACK_IN_PROGRESS = "pila del bucket en curso: se espera, sin crear otra"


def account_for(name: str) -> FakeAccount:
    return from_vector_input(copy.deepcopy(VECTORS[name]), SDK_VERSION)


def request_for(name: str) -> SetupRequest:
    vector = VECTORS[name]
    return SetupRequest(
        region=vector["region"],
        bucket=vector["bucket"],
        environment_bucket=vector["environmentBucket"],
        artifact=None if vector["artifact"] is None else Path(vector["artifact"]),
        sdk_version=SDK_VERSION,
    )


class Planned:
    """Una cuenta con su plan ya calculado y el registro de `apply` aparte."""

    def __init__(self, name: str, tmp_path: Path) -> None:
        self.account = account_for(name)
        self.service = AccountSetup(self.account.gateways)
        self.plan: SetupPlan = self.service.plan(request_for(name))
        self.reads = len(self.account.calls)
        self.progress: list[str] = []
        self.workdir = tmp_path

    def apply(self) -> SetupResult:
        return self.service.apply(self.plan, workdir=self.workdir, on_progress=self.progress.append)

    @property
    def writes(self) -> list[tuple[str, ...]]:
        return self.account.calls[self.reads :]


def test_construction_makes_no_call() -> None:
    account = account_for(FRESH)
    AccountSetup(account.gateways)
    assert account.calls == []


def test_the_plan_of_a_fresh_account_only_reads() -> None:
    account = account_for(FRESH)
    AccountSetup(account.gateways).plan(request_for(FRESH))
    names = [call[0] for call in account.calls]
    assert names == ["account", "describe", "head_bucket", "get_image"]
    assert "list_versions" not in names


def test_an_account_with_a_reusable_bucket_never_reads_the_bucket_stack() -> None:
    for name in (FRESH, PREPARED):
        account = account_for(name)
        AccountSetup(account.gateways).plan(request_for(name))
        assert "describe_artifact_stack" not in account.recorder.names()


def test_the_plan_of_a_prepared_account_lists_the_versions() -> None:
    account = account_for(PREPARED)
    AccountSetup(account.gateways).plan(request_for(PREPARED))
    assert [call[0] for call in account.calls] == [
        "account",
        "describe",
        "head_bucket",
        "get_image",
        "list_versions",
    ]


def test_an_unsupported_region_stops_before_any_call() -> None:
    account = account_for(UNSUPPORTED)
    with pytest.raises(AccountSetupException) as caught:
        AccountSetup(account.gateways).plan(request_for(UNSUPPORTED))
    assert caught.value.code == "unsupported_region"
    assert account.calls == []


def test_apply_on_a_fresh_account_creates_downloads_and_publishes(tmp_path: Path) -> None:
    planned = Planned(FRESH, tmp_path)
    result = planned.apply()
    bucket = planned.plan.bucket
    assert planned.writes == [
        ("create", bucket),
        ("download", "SHA256SUMS"),
        ("download", "rayito-image.zip"),
        ("publish", str(tmp_path / "rayito-image.zip")),
    ]
    assert planned.account.publisher.published_with == (bucket, BUILD_ROLE_ARN)
    assert result == SetupResult(
        bucket=bucket,
        image_arn=planned.plan.image_arn,
        image_version=IMAGE_VERSION_PUBLISHED,
        artifact_uploaded=True,
    )


def test_apply_reports_progress_in_order(tmp_path: Path) -> None:
    planned = Planned(FRESH, tmp_path)
    planned.apply()
    assert planned.progress == [
        "creando la pila de IAM rayito-m0-iam",
        "pila de IAM rayito-m0-iam lista",
        f"sha256 de rayito-image.zip verificado contra SHA256SUMS: {ARTIFACT_SHA256}",
        f"imagen rayito-base {IMAGE_VERSION_PUBLISHED} lanzable",
    ]


def test_an_update_sends_only_the_new_bucket(tmp_path: Path) -> None:
    planned = Planned(CHANGED_BUCKET, tmp_path)
    result = planned.apply()
    assert planned.writes == [("update", "amzn-s3-demo-bucket1")]
    assert result.artifact_uploaded is None
    assert result.image_version == "1.0"


def test_a_stack_in_progress_is_waited_for_not_recreated(tmp_path: Path) -> None:
    planned = Planned(STACK_IN_PROGRESS, tmp_path)
    planned.apply()
    names = [call[0] for call in planned.writes]
    assert names[0] == "wait"
    assert "create" not in names
    assert "update" not in names
    assert planned.account.publisher.published_with[1] == BUILD_ROLE_ARN


def test_a_build_in_progress_is_waited_for_and_never_published(tmp_path: Path) -> None:
    planned = Planned(BUILD_IN_PROGRESS, tmp_path)
    result = planned.apply()
    assert planned.writes == [("gate", "2.0")]
    assert result.image_version == "2.0"
    assert result.artifact_uploaded is None
    assert planned.progress[0] == "esperando el build en curso de rayito-base (versión 2.0)"


def test_a_waited_build_that_is_not_launchable_fails_the_run(tmp_path: Path) -> None:
    planned = Planned(BUILD_IN_PROGRESS, tmp_path)
    planned.account.images.gate = ImageGateResult(launchable=False, version_state="FAILED")
    with pytest.raises(AccountSetupException) as caught:
        planned.apply()
    assert caught.value.code == "image_build_failed"
    assert "2.0" in str(caught.value)
    assert [call[0] for call in planned.writes] == ["gate"]


@pytest.mark.parametrize(
    ("listed", "code"),
    [(None, "checksum_missing"), ("0" * 64, "checksum_mismatch")],
)
def test_a_bad_checksum_publishes_nothing(tmp_path: Path, listed: str | None, code: str) -> None:
    planned = Planned(FRESH, tmp_path)
    planned.account.releases.sha256_listed = listed
    with pytest.raises(AccountSetupException) as caught:
        planned.apply()
    assert caught.value.code == code
    assert "publish" not in [call[0] for call in planned.account.calls]


def test_an_artifact_argument_downloads_nothing(tmp_path: Path) -> None:
    planned = Planned(ARTIFACT, tmp_path)
    planned.apply()
    assert planned.writes == [
        ("create", planned.plan.bucket),
        ("publish", "image/rayito-image.zip"),
    ]


def test_a_prepared_account_writes_nothing(tmp_path: Path) -> None:
    planned = Planned(PREPARED, tmp_path)
    result = planned.apply()
    assert planned.writes == []
    assert result.image_version == "1.0"
    assert result.artifact_uploaded is None


def test_a_reused_stack_is_read_again_only_to_publish(tmp_path: Path) -> None:
    vector = copy.deepcopy(VECTORS[PREPARED])
    vector["image"] = None
    account = from_vector_input(vector, SDK_VERSION)
    service = AccountSetup(account.gateways)
    plan = service.plan(request_for(PREPARED))
    reads = len(account.calls)
    service.apply(plan, workdir=tmp_path, on_progress=lambda _line: None)
    assert account.calls[reads][0] == "describe"
    assert account.publisher.published_with[1] == BUILD_ROLE_ARN


def test_a_created_stack_without_the_build_role_output_is_incomplete(tmp_path: Path) -> None:
    planned = Planned(FRESH, tmp_path)
    planned.account.iam_stack.created_outputs = {}
    with pytest.raises(AccountSetupException) as caught:
        planned.apply()
    assert caught.value.code == "stack_incomplete"
    assert "publish" not in [call[0] for call in planned.account.calls]


def test_apply_on_an_account_without_a_bucket_creates_it_first(tmp_path: Path) -> None:
    planned = Planned(NO_BUCKET, tmp_path)
    result = planned.apply()
    bucket = planned.plan.bucket
    assert planned.plan.steps[0].source == "rayitoDefault"
    assert planned.writes == [
        ("create_artifact_stack", bucket),
        ("create", bucket),
        ("download", "SHA256SUMS"),
        ("download", "rayito-image.zip"),
        ("publish", str(tmp_path / "rayito-image.zip")),
    ]
    assert planned.account.publisher.published_with == (bucket, BUILD_ROLE_ARN)
    assert result.bucket == bucket
    assert planned.progress[:3] == [
        f"creando el bucket {bucket} con la pila rayito-m0-artifacts",
        f"bucket {bucket} listo (pila rayito-m0-artifacts)",
        "creando la pila de IAM rayito-m0-iam",
    ]


def test_an_explicit_missing_bucket_is_created_with_that_name(tmp_path: Path) -> None:
    planned = Planned(NAMED_MISSING, tmp_path)
    planned.apply()
    assert planned.writes[0] == ("create_artifact_stack", "amzn-s3-demo-bucket1")
    assert planned.writes[1] == ("create", "amzn-s3-demo-bucket1")


def test_a_bucket_stack_in_progress_is_waited_for_and_never_created(tmp_path: Path) -> None:
    planned = Planned(BUCKET_STACK_IN_PROGRESS, tmp_path)
    planned.apply()
    names = planned.account.recorder.names()
    assert planned.writes[0] == ("wait_artifact_stack",)
    assert "create_artifact_stack" not in names
    assert planned.progress[0].startswith("esperando a la pila rayito-m0-artifacts del bucket")
    assert planned.account.publisher.published_with[0] == planned.plan.bucket


def test_a_created_bucket_stack_without_the_bucket_output_is_incomplete(tmp_path: Path) -> None:
    planned = Planned(NO_BUCKET, tmp_path)
    planned.account.artifact_stack.created_outputs_key = "BucketArn"
    with pytest.raises(AccountSetupException) as caught:
        planned.apply()
    assert caught.value.code == "stack_incomplete"
    assert "create" not in planned.account.recorder.names()
