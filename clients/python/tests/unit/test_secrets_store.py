"""`SecretStore`: el contrato de parámetros de `AWS_API_NOTES.md` §19
(comprobado con `Stubber` contra el modelo de botocore), la codificación de
versiones y metadatos, el reintento de `create` tras un borrado y que ningún
error repite el nombre ni el valor."""

from __future__ import annotations

import re
import warnings
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from botocore.stub import Stubber

from rayito import SecretStore
from rayito._secrets import (
    DESCRIPTION_MAX_CHARS,
    IAM_ACTIONS,
    METADATA_PREFIX,
    SecretRef,
    current_version,
    decode_metadata,
    encode_metadata,
    resolve_secret_id,
    version_from_id,
    version_token,
)
from rayito.exceptions import (
    InvalidArgumentException,
    NotFoundException,
    RateLimitException,
    SandboxException,
    SecretException,
    SecretNotFoundException,
)

from .fake_secrets import (
    SENTINEL_NAME,
    SENTINEL_VALUE,
    FakeSecretsManager,
    SpySession,
    arn_for,
    client_error,
    stubbed_client,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
CREATED = datetime(2026, 9, 30, tzinfo=UTC)


def store_with(api: Any, **kwargs: Any) -> SecretStore:
    return SecretStore(session=cast(Any, SpySession(api=api)), **kwargs)


# ------------------------------------------------------------------ puro


def test_version_token_is_42_chars_within_the_client_request_token_range() -> None:
    token = version_token(7)
    assert token == "rayito-secret-version-00000000000000000007"
    assert len(token) == 42
    assert 32 <= len(token) <= 64
    assert version_from_id(token) == 7
    assert version_from_id("f3a1c2d4-uuid-from-console") == 0
    with pytest.raises(InvalidArgumentException):
        version_token(0)


def test_current_version_reads_awscurrent_from_version_ids_to_stages() -> None:
    stages = {
        version_token(1): ["AWSPREVIOUS"],
        version_token(2): ["AWSCURRENT"],
    }
    assert current_version(stages) == 2
    assert current_version({}) == 0
    assert current_version(None) == 0


def test_metadata_round_trips_through_a_compact_prefixed_description() -> None:
    encoded = encode_metadata({"b": "2", "a": "1"})
    assert encoded == METADATA_PREFIX + '{"a":"1","b":"2"}'
    assert decode_metadata(encoded) == {"a": "1", "b": "2"}
    assert decode_metadata("una descripción escrita a mano") == {}
    assert decode_metadata(None) == {}


def test_metadata_over_2048_chars_is_rejected_before_calling_aws() -> None:
    api = FakeSecretsManager()
    store = store_with(api)
    with pytest.raises(InvalidArgumentException, match=str(DESCRIPTION_MAX_CHARS)):
        store.create("big", "v", metadata={"k": "x" * DESCRIPTION_MAX_CHARS})
    assert api.calls == {}


def test_names_resolve_under_the_prefix_and_arns_pass_through() -> None:
    assert resolve_secret_id("openai", "rayito/") == "rayito/openai"
    assert resolve_secret_id("openai", "") == "openai"
    arn = arn_for("rayito/openai")
    assert resolve_secret_id(arn, "rayito/") == arn
    with pytest.raises(InvalidArgumentException) as excinfo:
        resolve_secret_id(f"{SENTINEL_NAME} with spaces", "rayito/")
    assert SENTINEL_NAME not in str(excinfo.value)


def test_secret_ref_rejects_both_version_selectors() -> None:
    with pytest.raises(InvalidArgumentException):
        SecretRef("a", version_id="x", version_stage="AWSCURRENT")
    assert SecretRef("a").selector == ("VersionStage", "AWSCURRENT")
    assert SecretRef("a", version_id="v").selector == ("VersionId", "v")


# ------------------------------------------------------------------ Stubber


def test_construction_never_builds_a_client() -> None:
    spy = SpySession()
    SecretStore(session=cast(Any, spy))
    assert spy.built == []


def test_create_sends_exactly_the_documented_parameters() -> None:
    client = stubbed_client()
    store = SecretStore(session=cast(Any, SpySession(api=client)), kms_key_id="alias/rayito")
    with Stubber(client) as stub:
        stub.add_response(
            "create_secret",
            {
                "ARN": arn_for("rayito/openai"),
                "Name": "rayito/openai",
                "VersionId": version_token(1),
            },
            expected_params={
                "Name": "rayito/openai",
                "SecretString": SENTINEL_VALUE,
                "Description": METADATA_PREFIX + '{"team":"ml"}',
                "KmsKeyId": "alias/rayito",
                "ClientRequestToken": version_token(1),
            },
        )
        info = store.create("openai", SENTINEL_VALUE, metadata={"team": "ml"})
        stub.assert_no_pending_responses()
    assert info.secret_id == arn_for("rayito/openai")
    assert info.name == "openai"
    assert info.version == 1
    assert info.metadata == {"team": "ml"}


def test_update_describes_then_puts_version_n_plus_one_and_replaces_metadata() -> None:
    client = stubbed_client()
    store = SecretStore(session=cast(Any, SpySession(api=client)))
    with Stubber(client) as stub, warnings.catch_warnings():
        warnings.simplefilter("ignore")
        stub.add_response(
            "describe_secret",
            {
                "ARN": arn_for("rayito/openai"),
                "Name": "rayito/openai",
                "Description": METADATA_PREFIX + "{}",
                "CreatedDate": CREATED,
                "VersionIdsToStages": {
                    version_token(1): ["AWSPREVIOUS"],
                    version_token(2): ["AWSCURRENT"],
                },
            },
            expected_params={"SecretId": "rayito/openai"},
        )
        stub.add_response(
            "put_secret_value",
            {"ARN": arn_for("rayito/openai"), "VersionId": version_token(3)},
            expected_params={
                "SecretId": "rayito/openai",
                "SecretString": "nuevo",
                "ClientRequestToken": version_token(3),
            },
        )
        stub.add_response(
            "update_secret",
            {"ARN": arn_for("rayito/openai")},
            expected_params={
                "SecretId": "rayito/openai",
                "Description": METADATA_PREFIX + '{"team":"ops"}',
            },
        )
        info = store.update("openai", "nuevo", metadata={"team": "ops"})
        stub.assert_no_pending_responses()
    assert info.version == 3
    assert info.metadata == {"team": "ops"}
    assert info.created_at == CREATED


def test_get_info_list_and_destroy_parameters() -> None:
    client = stubbed_client()
    store = SecretStore(session=cast(Any, SpySession(api=client)))
    described = {
        "ARN": arn_for("rayito/a"),
        "Name": "rayito/a",
        "Description": METADATA_PREFIX + '{"k":"v"}',
        "CreatedDate": CREATED,
        "LastChangedDate": CREATED,
        "VersionIdsToStages": {version_token(4): ["AWSCURRENT"]},
    }
    with Stubber(client) as stub:
        stub.add_response("describe_secret", described, expected_params={"SecretId": "rayito/a"})
        stub.add_response(
            "list_secrets",
            {
                "SecretList": [
                    {
                        "ARN": arn_for("rayito/a"),
                        "Name": "rayito/a",
                        "CreatedDate": CREATED,
                        "SecretVersionsToStages": {version_token(2): ["AWSCURRENT"]},
                    }
                ],
                "NextToken": "tok-2",
            },
            expected_params={
                "IncludePlannedDeletion": False,
                "Filters": [{"Key": "name", "Values": ["rayito/"]}],
                "MaxResults": 10,
                "NextToken": "tok-1",
            },
        )
        stub.add_response(
            "delete_secret",
            {"ARN": arn_for("rayito/a"), "Name": "rayito/a", "DeletionDate": CREATED},
            expected_params={"SecretId": "rayito/a", "ForceDeleteWithoutRecovery": True},
        )
        stub.add_client_error(
            "delete_secret",
            service_error_code="ResourceNotFoundException",
            expected_params={"SecretId": "rayito/gone", "ForceDeleteWithoutRecovery": True},
        )
        info = store.get_info("a")
        page = store.list(limit=10, next_token="tok-1")
        assert store.destroy("a") is True
        assert store.destroy("gone") is False
        stub.assert_no_pending_responses()
    assert (info.name, info.version, info.metadata) == ("a", 4, {"k": "v"})
    assert [(item.name, item.version) for item in page.items] == [("a", 2)]
    assert page.next_token == "tok-2"


def test_empty_prefix_lists_without_a_name_filter() -> None:
    client = stubbed_client()
    store = SecretStore(session=cast(Any, SpySession(api=client)), prefix="")
    with Stubber(client) as stub:
        stub.add_response(
            "list_secrets", {"SecretList": []}, expected_params={"IncludePlannedDeletion": False}
        )
        assert store.list().items == []
        stub.assert_no_pending_responses()


def test_get_secret_value_parameters_with_a_version_selector() -> None:
    client = stubbed_client()
    store = SecretStore(session=cast(Any, SpySession(api=client)))
    with Stubber(client) as stub:
        stub.add_response(
            "get_secret_value",
            {"ARN": arn_for("rayito/a"), "SecretString": "v", "VersionId": version_token(5)},
            expected_params={"SecretId": "rayito/a", "VersionId": version_token(5)},
        )
        stub.add_response(
            "get_secret_value",
            {"ARN": arn_for("rayito/a"), "SecretString": "v"},
            expected_params={"SecretId": "rayito/a", "VersionStage": "AWSPREVIOUS"},
        )
        assert store.read_value(SecretRef("a", version_id=version_token(5))) == "v"
        assert store.read_value(SecretRef("a", version_stage="AWSPREVIOUS")) == "v"


# ------------------------------------------------------------------ semántica


def test_create_retries_while_the_name_is_scheduled_for_deletion() -> None:
    api = FakeSecretsManager()
    failures = [
        client_error(
            "InvalidRequestException",
            "You can't create this secret because a secret with this name is already "
            "scheduled for deletion.",
            "CreateSecret",
        )
    ] * 2
    real_create = api.create_secret

    def create_secret(**params: Any) -> dict[str, Any]:
        if failures:
            api._record("CreateSecret", params)
            raise failures.pop()
        return real_create(**params)

    api.create_secret = create_secret  # type: ignore[method-assign]
    store = store_with(api)
    sleeps: list[float] = []
    store._sleep = sleeps.append
    info = store.create("a", "v")
    assert info.version == 1
    assert sleeps == [0.5, 1.0]
    tokens = {params["ClientRequestToken"] for op, params in api.requests if op == "CreateSecret"}
    assert tokens == {version_token(1)}


def test_create_gives_up_after_the_30_second_budget() -> None:
    api = FakeSecretsManager()

    def always_deleting(**params: Any) -> dict[str, Any]:
        raise client_error("InvalidRequestException", "scheduled for deletion", "CreateSecret")

    api.create_secret = always_deleting  # type: ignore[method-assign]
    store = store_with(api)
    now = [0.0]
    store._clock = lambda: now[0]
    store._sleep = lambda seconds: now.__setitem__(0, now[0] + seconds)
    with pytest.raises(SecretException) as excinfo:
        store.create(SENTINEL_NAME, SENTINEL_VALUE)
    assert now[0] <= 30.0
    assert SENTINEL_NAME not in str(excinfo.value)
    assert SENTINEL_VALUE not in str(excinfo.value)


def test_create_on_an_existing_name_and_other_invalid_requests_do_not_retry() -> None:
    api = FakeSecretsManager()
    store = store_with(api)
    store.create(SENTINEL_NAME, "v")
    with pytest.raises(SecretException, match="ya existe") as excinfo:
        store.create(SENTINEL_NAME, "v")
    assert SENTINEL_NAME not in str(excinfo.value)
    assert excinfo.value.aws_code == "ResourceExistsException"


def test_not_found_is_both_secret_and_native_not_found_without_the_name() -> None:
    store = store_with(FakeSecretsManager())
    with pytest.raises(SecretNotFoundException) as excinfo:
        store.get_info(SENTINEL_NAME)
    assert isinstance(excinfo.value, NotFoundException)
    assert isinstance(excinfo.value, SecretException)
    assert isinstance(excinfo.value, SandboxException)
    assert SENTINEL_NAME not in str(excinfo.value)
    cause = excinfo.value.__cause__
    assert cause is not None and SENTINEL_NAME not in str(cause)
    assert store.exists(SENTINEL_NAME) is False


def test_a_secret_scheduled_for_deletion_is_not_found() -> None:
    client = stubbed_client()
    store = SecretStore(session=cast(Any, SpySession(api=client)))
    with Stubber(client) as stub:
        stub.add_response(
            "describe_secret",
            {"ARN": arn_for("rayito/a"), "Name": "rayito/a", "DeletedDate": CREATED},
        )
        with pytest.raises(SecretNotFoundException):
            store.get_info("a")


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("ThrottlingException", RateLimitException),
        ("AccessDeniedException", SecretException),
        ("LimitExceededException", SecretException),
        ("InternalServiceError", SecretException),
    ],
)
def test_aws_errors_map_by_code_never_by_message(code: str, expected: type[Exception]) -> None:
    api = FakeSecretsManager()

    def failing(**params: Any) -> dict[str, Any]:
        raise client_error(code, f"message naming {SENTINEL_NAME}", "DescribeSecret")

    api.describe_secret = failing  # type: ignore[method-assign]
    with pytest.raises(expected) as excinfo:
        store_with(api).get_info(SENTINEL_NAME)
    assert SENTINEL_NAME not in str(excinfo.value)
    assert SENTINEL_NAME not in str(excinfo.value.__cause__)


def test_update_more_often_than_every_600_s_warns_once_without_the_name() -> None:
    api = FakeSecretsManager()
    store = store_with(api, region="eu-west-9")
    now = [1000.0]
    store._clock = lambda: now[0]
    store.create(SENTINEL_NAME, "v1")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        store.update(SENTINEL_NAME, "v2")
        now[0] += 10
        store.update(SENTINEL_NAME, "v3")
        now[0] += 10
        store.update(SENTINEL_NAME, "v4")
    frequency = [w for w in caught if "600 s" in str(w.message)]
    assert len(frequency) == 1
    assert frequency[0].category is UserWarning
    assert SENTINEL_NAME not in str(frequency[0].message)
    assert store.get_info(SENTINEL_NAME).version == 4


def test_values_are_validated_before_calling_aws() -> None:
    api = FakeSecretsManager()
    store = store_with(api)
    with pytest.raises(InvalidArgumentException):
        store.create("a", "")
    with pytest.raises(InvalidArgumentException):
        store.create("a", "x" * 65_537)
    with pytest.raises(InvalidArgumentException):
        store.create(arn_for("rayito/a"), "v")
    with pytest.raises(InvalidArgumentException):
        store.list(limit=0)
    assert api.calls == {}


def test_repr_never_shows_a_value() -> None:
    store = store_with(FakeSecretsManager(), region="us-east-1")
    store.create("a", SENTINEL_VALUE)
    assert SENTINEL_VALUE not in repr(store)


# ------------------------------------------------------------------ §19


def test_every_operation_used_by_both_sdks_is_documented_in_section_19() -> None:
    notes = (REPO_ROOT / "AWS_API_NOTES.md").read_text(encoding="utf-8")
    match = re.search(r"## 19\. Secrets Manager.*?(?=\n## \d+\.|\Z)", notes, re.S)
    assert match, "AWS_API_NOTES.md: falta el §19 de Secrets Manager"
    section = match.group(0)
    python = (REPO_ROOT / "clients/python/src/rayito/_secrets.py").read_text(encoding="utf-8")
    snake = set(IAM_ACTIONS)
    snake |= set(re.findall(r'_call\(\s*"(\w+)"', python))
    snake |= set(re.findall(r"api\(\)\.(\w+)\(", python))
    for name in snake:
        pascal = "".join(part.capitalize() for part in name.split("_"))
        assert f"`{pascal}`" in section or pascal in section, f"§19 no documenta {pascal}"
        assert f"`{name}`" in section, f"§19 no nombra el método boto3 {name}"
    typescript = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (REPO_ROOT / "clients/typescript/src/secrets").glob("*.ts")
    )
    commands = set(re.findall(r"\b(\w+)Command\b", typescript)) - {""}
    assert commands, "src/secrets/** no usa ningún comando de Secrets Manager"
    for command in commands:
        assert f"{command}Command" in section, f"§19 no documenta {command}Command"
    for parameter in ("ForceDeleteWithoutRecovery", "IncludePlannedDeletion", "ClientRequestToken"):
        assert parameter in section
