"""Dominio puro de `rayito setup`: los vectores compartidos del plan y de
`SHA256SUMS`, y que cada constante y mensaje coincida con
`testdata/account-setup/contract.json` (el mismo fichero que lee TypeScript)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, get_args

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
    default_artifact_bucket_name,
    parse_checksums,
    release_asset_url,
    release_tag,
    verify_checksum,
    with_setup_hint,
)
from rayito._account_setup._service import AccountSetup
from rayito._agent._template import DEFAULT_AGENT_TEMPLATE_BASE
from rayito._images import BUILD_ROLE_OUTPUT_KEY, BUILDING_VERSION_STATES
from rayito._images_publish_domain import ALL_OS_CAPABILITIES

from .fake_account_setup import (
    bucket_reads,
    fill_placeholders,
    from_vector_input,
    load_cases,
    load_contract,
)

CONTRACT = load_contract()
CONSTANTS = CONTRACT["constants"]
MESSAGES = CONTRACT["messages"]
SHA_A = "a" * 64
SHA_B = "b" * 64


def request_from_vector(vector: dict[str, Any]) -> SetupRequest:
    return SetupRequest(
        region=vector["region"],
        bucket=vector["bucket"],
        environment_bucket=vector["environmentBucket"],
        artifact=None if vector["artifact"] is None else Path(vector["artifact"]),
        sdk_version=SDK_VERSION,
        caps_image=not vector.get("noCapsImage", False),
    )


def plan_from_vector(vector: dict[str, Any]) -> tuple[SetupPlan | None, list[str], str | None]:
    """El plan de `AccountSetup.plan` sobre los dobles del vector, las
    lecturas del bucket y el código del error si lo hubo."""
    account = from_vector_input(vector, SDK_VERSION)
    request = request_from_vector(
        fill_placeholders(
            vector, account=vector["account"], region=vector["region"], sdk_version=SDK_VERSION
        )
    )
    try:
        plan = AccountSetup(account.gateways).plan(request)
    except AccountSetupException as exc:
        return None, bucket_reads(account.calls), exc.code
    return plan, bucket_reads(account.calls), None


PLAN_CASES = load_cases("plan-vectors.json")


@pytest.mark.parametrize("case", PLAN_CASES, ids=lambda case: case["name"])
def test_plan_vectors(case: dict[str, Any]) -> None:
    expected = fill_placeholders(
        case["expected"],
        account=case["input"]["account"],
        region=case["input"]["region"],
        sdk_version=SDK_VERSION,
    )
    plan, reads, error = plan_from_vector(case["input"])
    assert reads == expected["bucketReads"]
    if "error" in expected:
        assert error == expected["error"]
        return
    assert plan is not None
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
    assert CONSTANTS["iamTemplatePath"] == domain.IAM_TEMPLATE_PATH
    assert CONSTANTS["artifactStackName"] == domain.ARTIFACT_STACK_NAME
    assert CONSTANTS["artifactComponentName"] == domain.ARTIFACT_COMPONENT_NAME
    assert CONSTANTS["artifactTemplatePath"] == domain.ARTIFACT_TEMPLATE_PATH
    assert CONSTANTS["bucketNameParameter"] == domain.BUCKET_NAME_PARAMETER
    assert CONSTANTS["bucketNameOutputKey"] == domain.BUCKET_NAME_OUTPUT_KEY
    assert CONSTANTS["defaultArtifactBucketTemplate"] == domain.DEFAULT_ARTIFACT_BUCKET_TEMPLATE
    assert CONSTANTS["artifactExpirationDays"] == domain.ARTIFACT_EXPIRATION_DAYS
    assert sorted(CONSTANTS["explicitBucketSources"]) == sorted(domain.EXPLICIT_BUCKET_SOURCES)
    assert CONSTANTS["buildRoleOutputKey"] == BUILD_ROLE_OUTPUT_KEY
    assert CONSTANTS["baseImageName"] == domain.BASE_IMAGE_NAME
    assert CONSTANTS["baseImageVariant"] == domain.BASE_IMAGE_VARIANT
    assert CONSTANTS["capsImageName"] == domain.CAPS_IMAGE_NAME == DEFAULT_AGENT_TEMPLATE_BASE
    assert CONSTANTS["capsImageOsCapabilities"] == domain.CAPS_IMAGE_OS_CAPABILITIES
    assert domain.CAPS_IMAGE_OS_CAPABILITIES == ALL_OS_CAPABILITIES
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
    assert {k: sorted(v) for k, v in domain.ALLOWED_ACTIONS.items()} == {
        k: sorted(v) for k, v in CONTRACT["resourceActions"].items()
    }
    assert sorted(get_args(domain.BucketSource)) == sorted(CONTRACT["bucketSources"])
    assert sorted(get_args(domain.BucketState)) == sorted(CONTRACT["bucketProbeStates"])
    assert list(get_args(domain.ResourceKind)) == CONTRACT["resources"]
    assert sorted(get_args(domain.SetupAction)) == sorted(CONTRACT["actions"])


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
    assert AccountSetupException("bucket_taken", bucket="b", stack="s").exit_code == 2


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
        SetupStep("bucket", "b", "update")
    with pytest.raises(ValueError, match="baseImage"):
        SetupStep("baseImage", "rayito-base", "update")


def test_a_plan_needs_the_four_steps_in_order() -> None:
    steps = (
        SetupStep("iamStack", "rayito-m0-iam", "reuse"),
        SetupStep("bucket", "b", "reuse"),
        SetupStep("baseImage", "rayito-base", "reuse"),
        SetupStep("capsImage", "rayito-base-caps", "reuse"),
    )
    with pytest.raises(ValueError, match="orden"):
        SetupPlan("us-east-1", "1", steps, (), None, "arn", None)
    with pytest.raises(ValueError, match="orden"):
        SetupPlan("us-east-1", "1", (steps[1], steps[0], steps[2]), (), None, "arn", None)


def test_only_the_caps_step_can_be_skipped() -> None:
    assert SetupStep("capsImage", "rayito-base-caps", "skip").action == "skip"
    with pytest.raises(ValueError, match="baseImage"):
        SetupStep("baseImage", "rayito-base", "skip")


def test_a_skipped_caps_step_still_counts_as_ready() -> None:
    steps = (
        SetupStep("bucket", "b", "reuse"),
        SetupStep("iamStack", "rayito-m0-iam", "reuse"),
        SetupStep("baseImage", "rayito-base", "reuse"),
        SetupStep("capsImage", "rayito-base-caps", "skip"),
    )
    plan = SetupPlan("us-east-1", "1", steps, (), None, "arn", None)
    assert plan.ready
    assert not plan.needs_confirmation
    assert domain.cost_lines(steps) == ()


def test_publishing_only_the_caps_image_also_prices_the_zip() -> None:
    steps = (
        SetupStep("bucket", "b", "reuse"),
        SetupStep("iamStack", "rayito-m0-iam", "reuse"),
        SetupStep("baseImage", "rayito-base", "reuse"),
        SetupStep("capsImage", "rayito-base-caps", "publish", source="release"),
    )
    assert domain.cost_lines(steps) == (
        MESSAGES["cost"]["capsImage"],
        MESSAGES["cost"]["artifact"],
    )


def test_the_default_bucket_name_carries_the_account_and_the_region() -> None:
    assert default_artifact_bucket_name("123456789012", "us-east-1") == (
        "rayito-artifacts-123456789012-us-east-1"
    )


def test_a_bucket_without_a_reported_region_is_accepted() -> None:
    candidate = domain.BucketCandidate("amzn-s3-demo-bucket", "argument")
    step = domain.decide_bucket(candidate, BucketProbe("found", None), "us-east-1")
    assert step is not None
    assert step.action == "reuse"


def test_a_forbidden_bucket_of_the_stack_is_unavailable() -> None:
    candidate = domain.BucketCandidate("amzn-s3-demo-bucket", "iamStack")
    with pytest.raises(AccountSetupException) as caught:
        domain.decide_bucket(candidate, BucketProbe("forbidden", None), "us-east-1")
    assert caught.value.code == "bucket_unavailable"
    assert "el de la pila de IAM" in str(caught.value)


def test_a_forbidden_cdk_bucket_and_a_missing_candidate_decide_nothing() -> None:
    cdk = domain.BucketCandidate("amzn-s3-demo-bucket", "cdkBootstrap")
    assert domain.decide_bucket(cdk, BucketProbe("forbidden", None), "us-east-1") is None
    assert domain.decide_bucket(cdk, BucketProbe("missing", None), "us-east-1") is None


def test_the_default_target_is_probed_only_when_no_stack_manages_it() -> None:
    target = domain.BucketCandidate("rayito-artifacts-1-us-east-1", "rayitoDefault")
    absent = domain.StackObservation(domain.ARTIFACT_STACK_NAME, None, {}, {})
    present = domain.StackObservation(domain.ARTIFACT_STACK_NAME, "CREATE_COMPLETE", {}, {})
    assert domain.needs_default_probe(target, absent)
    assert not domain.needs_default_probe(target, present)
    explicit = domain.BucketCandidate("amzn-s3-demo-bucket", "argument")
    assert not domain.needs_default_probe(explicit, absent)


def test_creating_the_default_bucket_needs_its_probe() -> None:
    target = domain.BucketCandidate("rayito-artifacts-1-us-east-1", "rayitoDefault")
    absent = domain.StackObservation(domain.ARTIFACT_STACK_NAME, None, {}, {})
    with pytest.raises(ValueError, match="sonda"):
        domain.plan_bucket_creation(target, absent, None, "us-east-1")


def test_a_usable_bucket_stack_without_its_output_is_incomplete() -> None:
    target = domain.BucketCandidate("rayito-artifacts-1-us-east-1", "rayitoDefault")
    stack = domain.StackObservation(domain.ARTIFACT_STACK_NAME, "CREATE_COMPLETE", {}, {})
    with pytest.raises(AccountSetupException) as caught:
        domain.plan_bucket_creation(target, stack, None, "us-east-1")
    assert caught.value.code == "stack_incomplete"
    assert "BucketName" in str(caught.value)
    assert domain.ARTIFACT_TEMPLATE_PATH in str(caught.value)
