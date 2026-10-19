"""Dominio puro de `rayito setup`: los vectores compartidos del plan y de
`SHA256SUMS`, y que cada constante y mensaje coincida con
`testdata/account-setup/contract.json` (el mismo fichero que lee TypeScript)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rayito import __version__ as SDK_VERSION
from rayito._account_setup import _domain as domain
from rayito._account_setup._domain import (
    AccountSetupException,
    BucketProbe,
    DownloadedRelease,
    SetupPlan,
    SetupRequest,
    SetupResult,
    SetupStep,
    checksum_for,
    choose_bucket_candidate,
    parse_checksums,
    plan_setup,
    release_asset_url,
    release_tag,
    verify_checksum,
    with_setup_hint,
)
from rayito._images import BUILD_ROLE_OUTPUT_KEY, BUILDING_VERSION_STATES

from .fake_account_setup import (
    fill_placeholders,
    image_from_vector,
    load_cases,
    load_contract,
    stack_from_vector,
)

CONTRACT = load_contract()
CONSTANTS = CONTRACT["constants"]
MESSAGES = CONTRACT["messages"]
SHA_A = "a" * 64
SHA_B = "b" * 64


def plan_from_vector(vector: dict[str, Any]) -> SetupPlan:
    region, account = vector["region"], vector["account"]
    data = fill_placeholders(vector, account=account, region=region, sdk_version=SDK_VERSION)
    request = SetupRequest(
        region=region,
        bucket=data["bucket"],
        environment_bucket=data["environmentBucket"],
        artifact=None if data["artifact"] is None else Path(data["artifact"]),
        sdk_version=SDK_VERSION,
    )
    stack = stack_from_vector(data["stack"], domain.IAM_STACK_NAME)
    candidate = choose_bucket_candidate(request, stack, account)
    buckets = data["buckets"]
    probe = BucketProbe(candidate.name in buckets, buckets.get(candidate.name))
    image = image_from_vector(data["image"], domain.BASE_IMAGE_NAME, "arn:image")
    return plan_setup(request, account, stack, candidate, probe, image)


PLAN_CASES = load_cases("plan-vectors.json")


@pytest.mark.parametrize("case", PLAN_CASES, ids=lambda case: case["name"])
def test_plan_vectors(case: dict[str, Any]) -> None:
    expected = fill_placeholders(
        case["expected"],
        account=case["input"]["account"],
        region=case["input"]["region"],
        sdk_version=SDK_VERSION,
    )
    if "error" in expected:
        with pytest.raises(AccountSetupException) as caught:
            plan_from_vector(case["input"])
        assert caught.value.code == expected["error"]
        return
    plan = plan_from_vector(case["input"])
    assert [step.as_json() for step in plan.steps] == expected["steps"]
    assert plan.ready == expected["ready"]
    assert plan.needs_confirmation == expected["needsConfirmation"]
    assert list(plan.cost) == expected["cost"]


@pytest.mark.parametrize("case", load_cases("checksums.json"), ids=lambda case: case["name"])
def test_checksum_vectors(case: dict[str, Any]) -> None:
    assert checksum_for(case["text"], case["asset"]) == case["expected"]


def test_parse_checksums_keeps_every_well_formed_entry() -> None:
    text = f"{SHA_A}  one\n{SHA_B} *two\nnot a line\n"
    assert parse_checksums(text) == {"one": SHA_A, "two": SHA_B}


def release(sha256: str, checksums: str) -> DownloadedRelease:
    return DownloadedRelease(checksums, Path("rayito-image.zip"), sha256)


def test_verify_checksum_accepts_the_listed_hash() -> None:
    verify_checksum(release(SHA_A, f"{SHA_A}  rayito-image.zip\n"), "rayd-v1.2.3")


def test_verify_checksum_rejects_a_different_hash() -> None:
    with pytest.raises(AccountSetupException) as caught:
        verify_checksum(release(SHA_B, f"{SHA_A}  rayito-image.zip\n"), "rayd-v1.2.3")
    assert caught.value.code == "checksum_mismatch"
    assert "rayd-v1.2.3" in str(caught.value)


def test_verify_checksum_rejects_a_missing_entry() -> None:
    with pytest.raises(AccountSetupException) as caught:
        verify_checksum(release(SHA_A, f"{SHA_A}  other\n"), "rayd-v1.2.3")
    assert caught.value.code == "checksum_missing"


def test_constants_match_the_contract() -> None:
    assert CONSTANTS["iamStackName"] == domain.IAM_STACK_NAME
    assert CONSTANTS["iamComponentName"] == domain.IAM_COMPONENT_NAME
    assert list(domain.IAM_CAPABILITIES) == CONSTANTS["iamCapabilities"]
    assert CONSTANTS["artifactBucketParameter"] == domain.ARTIFACT_BUCKET_PARAMETER
    assert CONSTANTS["buildRoleOutputKey"] == BUILD_ROLE_OUTPUT_KEY
    assert CONSTANTS["baseImageName"] == domain.BASE_IMAGE_NAME
    assert CONSTANTS["baseImageVariant"] == domain.BASE_IMAGE_VARIANT
    assert CONSTANTS["cdkBootstrapQualifier"] == domain.CDK_BOOTSTRAP_QUALIFIER
    assert CONSTANTS["bucketEnvVar"] == domain.BUCKET_ENV_VAR
    assert CONSTANTS["releaseTagPrefix"] == domain.RELEASE_TAG_PREFIX
    assert CONSTANTS["releaseDownloadUrlTemplate"] == domain.RELEASE_DOWNLOAD_URL_TEMPLATE
    assert CONSTANTS["imageAssetName"] == domain.IMAGE_ASSET_NAME
    assert CONSTANTS["checksumsAssetName"] == domain.CHECKSUMS_ASSET_NAME
    assert CONSTANTS["releaseDownloadTimeoutSeconds"] == domain.RELEASE_DOWNLOAD_TIMEOUT_SECONDS
    assert sorted(domain.USABLE_STACK_STATES) == sorted(CONSTANTS["usableStackStates"])
    assert CONSTANTS["inProgressStackSuffix"] == domain.IN_PROGRESS_STACK_SUFFIX
    assert CONSTANTS["deletingStackState"] == domain.DELETING_STACK_STATE
    assert sorted(BUILDING_VERSION_STATES) == sorted(CONSTANTS["buildingVersionStates"])


def test_resources_actions_and_sources_match_the_contract() -> None:
    assert list(domain.RESOURCE_ORDER) == CONTRACT["resources"]
    allowed = {action for actions in domain.ALLOWED_ACTIONS.values() for action in actions}
    assert sorted(allowed) == sorted(CONTRACT["actions"])
    assert sorted(domain.BUCKET_SOURCE_LABELS) == sorted(CONTRACT["bucketSources"])


def test_messages_match_the_contract() -> None:
    assert dict(domain.ERROR_MESSAGES) == MESSAGES["errors"]
    assert dict(domain.PROGRESS_MESSAGES) == MESSAGES["progress"]
    assert dict(domain.DOWNLOAD_REASONS) == MESSAGES["downloadReasons"]
    assert dict(domain.COST_TEXTS) == MESSAGES["cost"]
    assert dict(domain.BUCKET_SOURCE_LABELS) == MESSAGES["bucketSources"]
    assert MESSAGES["accountSetupHint"] == domain.ACCOUNT_SETUP_HINT
    assert MESSAGES["hintFormat"] == domain.HINT_TEMPLATE


def test_error_exit_codes_match_the_contract() -> None:
    assert dict(domain.ERROR_EXIT_CODES) == CONTRACT["errorExitCodes"]
    assert set(domain.ERROR_EXIT_CODES) == set(domain.ERROR_MESSAGES)


def test_an_error_formats_its_message_and_exposes_the_exit_code() -> None:
    error = AccountSetupException("stack_blocked", name="rayito-m0-iam", status="ROLLBACK_COMPLETE")
    assert str(error) == MESSAGES["errors"]["stack_blocked"].format(
        name="rayito-m0-iam", status="ROLLBACK_COMPLETE"
    )
    assert error.code == "stack_blocked"
    assert error.exit_code == 1
    assert AccountSetupException("no_bucket").exit_code == 2


def test_release_tag_and_asset_url() -> None:
    tag = release_tag("1.2.3")
    assert tag == "rayd-v1.2.3"
    assert release_asset_url(tag, "SHA256SUMS") == (
        "https://github.com/alejandro-cedeno-10/rayito/releases/download/rayd-v1.2.3/SHA256SUMS"
    )


def test_with_setup_hint_appends_the_hint_in_parentheses() -> None:
    assert with_setup_hint("falta") == f"falta ({MESSAGES['accountSetupHint']})"


def test_step_and_result_json_keys_follow_the_contract_order() -> None:
    step = SetupStep("baseImage", "rayito-base", "reuse", image_version="1.0")
    assert list(step.as_json()) == CONTRACT["json"]["stepKeys"]
    result = SetupResult("bucket", "arn", "1.0", None)
    assert list(result.as_json()) == CONTRACT["json"]["resultKeys"]


def test_a_step_rejects_an_action_its_resource_does_not_admit() -> None:
    with pytest.raises(ValueError, match="bucket"):
        SetupStep("bucket", "b", "create")
    with pytest.raises(ValueError, match="baseImage"):
        SetupStep("baseImage", "rayito-base", "update")


def test_a_plan_needs_the_three_steps_in_order() -> None:
    steps = (
        SetupStep("iamStack", "rayito-m0-iam", "reuse"),
        SetupStep("bucket", "b", "reuse"),
        SetupStep("baseImage", "rayito-base", "reuse"),
    )
    with pytest.raises(ValueError, match="orden"):
        SetupPlan("us-east-1", "1", steps, (), None, "arn", None)


def test_a_bucket_without_a_reported_region_is_accepted() -> None:
    candidate = domain.BucketCandidate("amzn-s3-demo-bucket", "argument")
    step = domain.decide_bucket(candidate, BucketProbe(True, None), "us-east-1")
    assert step.action == "reuse"


def test_a_missing_bucket_from_the_stack_is_unavailable_not_missing() -> None:
    candidate = domain.BucketCandidate("amzn-s3-demo-bucket", "iamStack")
    with pytest.raises(AccountSetupException) as caught:
        domain.decide_bucket(candidate, BucketProbe(False, None), "us-east-1")
    assert caught.value.code == "bucket_unavailable"
    assert "el de la pila de IAM" in str(caught.value)
