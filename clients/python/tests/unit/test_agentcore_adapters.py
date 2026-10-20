"""Los adaptadores de AWS de `--target agentcore` (`_agentcore/_aws.py`) contra
clientes reales de botocore con `Stubber`: cada petición se valida con el modelo
de botocore (los nombres y las formas de `AWS_API_NOTES.md`), los errores se
traducen por código y nada de AWS cruza los puertos."""

from __future__ import annotations

import base64
import hashlib
import io
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import boto3
import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber

from rayito._agentcore._aws import (
    AgentCoreClients,
    AgentCoreInvoker,
    AgentCoreRuntimes,
    CloudFormationAgentStack,
    CloudWatchRuntimeLogs,
    CodeBuildImageBuilder,
    EcrImageRegistry,
    S3ArtifactBucket,
    StsIdentity,
)
from rayito._agentcore._domain import client_token_for
from rayito._agentcore._models import RuntimeSpec
from rayito._agentcore._ports import (
    RuntimeAlreadyExists,
    RuntimeNotFound,
    StackResourceMissing,
)
from rayito._limits import AGENTCORE_REQUEST_TIMEOUT_SECONDS, AGENTCORE_STREAM_MAX_SECONDS
from rayito._stacks._model import StackStatus
from rayito._stacks._port import UpdateOutcome
from rayito.exceptions import (
    AgentCoreException,
    AgentException,
    QuotaExceededException,
    RateLimitException,
    StackException,
)
from tests.unit import fake_agentcore as fake

NOW = datetime(2026, 10, 19, tzinfo=UTC)
TOKEN_VECTORS: list[dict[str, Any]] = json.loads(
    (fake.DATA / "client-tokens.json").read_text(encoding="utf-8")
)["cases"]
RUNTIME_ARN = f"arn:aws:bedrock-agentcore:{fake.REGION}:{fake.ACCOUNT}:runtime/{fake.RUNTIME_ID}"
IMAGE_URI = f"{fake.DEFAULT_OUTPUTS['RepositoryUri']}:{fake.IMAGE_TAG}"
ROLE_ARN = fake.DEFAULT_OUTPUTS["RuntimeRoleArn"]


class Static:
    """Un `LazyClient` que ya trae su cliente."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def get(self) -> Any:
        return self._client


def real_client(service: str) -> Any:
    session = boto3.session.Session(
        region_name=fake.REGION, aws_access_key_id="AKIDEXAMPLE0000000", aws_secret_access_key="x"
    )
    return session.client(service)


@contextmanager
def stubbed(service: str) -> Iterator[tuple[Any, Stubber]]:
    client = real_client(service)
    with Stubber(client) as stubber:
        yield client, stubber
        stubber.assert_no_pending_responses()


def clients_with(**services: Any) -> AgentCoreClients:
    clients = AgentCoreClients(fake.REGION, None)
    for name, client in services.items():
        setattr(clients, name, Static(client))
    return clients


def error(stubber: Stubber, method: str, code: str, params: dict[str, Any] | None = None) -> None:
    stubber.add_client_error(
        method,
        service_error_code=code,
        service_message="texto crudo de AWS",
        expected_params=params,
    )


def spec() -> RuntimeSpec:
    return RuntimeSpec(
        name=fake.RUNTIME_NAME,
        description=fake.runtime_description(),
        container_uri=IMAGE_URI,
        role_arn=ROLE_ARN,
        environment_variables={"AWS_REGION": fake.REGION},
        idle_runtime_session_timeout=900,
        max_lifetime=28800,
        tags={"team": "agents", "rayito:component": "agentcore-runtime"},
    )


def runtime_item(**extra: Any) -> dict[str, Any]:
    return {
        "agentRuntimeArn": RUNTIME_ARN,
        "agentRuntimeId": fake.RUNTIME_ID,
        "agentRuntimeName": fake.RUNTIME_NAME,
        "agentRuntimeVersion": "2",
        "status": "READY",
        "description": fake.runtime_description(),
        "lastUpdatedAt": NOW,
        **extra,
    }


def get_response(**extra: Any) -> dict[str, Any]:
    return {
        **runtime_item(),
        "createdAt": NOW,
        "roleArn": ROLE_ARN,
        "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800},
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": IMAGE_URI}},
        "networkConfiguration": {"networkMode": "PUBLIC"},
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "environmentVariables": {"AWS_REGION": fake.REGION},
        "metadataConfiguration": {"requireMMDSV2": True},
        **extra,
    }


# ------------------------------------------------------------ plano de control


def test_list_runtimes_pages_with_the_documented_parameters() -> None:
    with stubbed("bedrock-agentcore-control") as (client, stubber):
        stubber.add_response(
            "list_agent_runtimes",
            {"agentRuntimes": [runtime_item()], "nextToken": "t1"},
            {"maxResults": 100},
        )
        stubber.add_response(
            "list_agent_runtimes",
            {"agentRuntimes": [runtime_item(agentRuntimeName="rayito_b", agentRuntimeId="b-1")]},
            {"maxResults": 100, "nextToken": "t1"},
        )
        listings = AgentCoreRuntimes(clients_with(control=client)).list_runtimes()
    assert [item.name for item in listings] == [fake.RUNTIME_NAME, "rayito_b"]
    assert listings[0].runtime_id == fake.RUNTIME_ID and listings[0].version == "2"


def test_get_runtime_reads_the_compared_fields() -> None:
    with stubbed("bedrock-agentcore-control") as (client, stubber):
        stubber.add_response(
            "get_agent_runtime", get_response(), {"agentRuntimeId": fake.RUNTIME_ID}
        )
        described = AgentCoreRuntimes(clients_with(control=client)).get_runtime(fake.RUNTIME_ID)
    details = described.details
    assert described.listing.status == "READY"
    assert (details.container_uri, details.role_arn) == (IMAGE_URI, ROLE_ARN)
    assert (details.network_mode, details.server_protocol) == ("PUBLIC", "HTTP")
    assert details.environment_variables == {"AWS_REGION": fake.REGION}
    assert (details.idle_runtime_session_timeout, details.max_lifetime) == (900, 28800)
    assert details.require_mmds_v2 is True


def test_get_runtime_without_metadata_means_no_mmds_v2() -> None:
    with stubbed("bedrock-agentcore-control") as (client, stubber):
        response = get_response()
        del response["metadataConfiguration"]
        stubber.add_response("get_agent_runtime", response)
        described = AgentCoreRuntimes(clients_with(control=client)).get_runtime(fake.RUNTIME_ID)
    assert described.details.require_mmds_v2 is False


def test_create_runtime_sends_the_documented_request_with_a_deterministic_token() -> None:
    expected = {
        "agentRuntimeName": fake.RUNTIME_NAME,
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": IMAGE_URI}},
        "roleArn": ROLE_ARN,
        "networkConfiguration": {"networkMode": "PUBLIC"},
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800},
        "environmentVariables": {"AWS_REGION": fake.REGION},
        "description": fake.runtime_description(),
        "tags": {"team": "agents", "rayito:component": "agentcore-runtime"},
    }
    token = client_token_for(expected)
    response = {
        "agentRuntimeArn": RUNTIME_ARN,
        "agentRuntimeId": fake.RUNTIME_ID,
        "agentRuntimeVersion": "1",
        "createdAt": NOW,
        "status": "CREATING",
    }
    with stubbed("bedrock-agentcore-control") as (client, stubber):
        stubber.add_response("create_agent_runtime", response, {**expected, "clientToken": token})
        ref = AgentCoreRuntimes(clients_with(control=client)).create_runtime(spec())
    assert (ref.runtime_id, ref.arn, ref.version, ref.status) == (
        fake.RUNTIME_ID,
        RUNTIME_ARN,
        "1",
        "CREATING",
    )
    assert token.startswith("rayito-") and 33 <= len(token) <= 256


def test_update_runtime_always_requires_mmds_v2_and_resends_the_full_configuration() -> None:
    sent = {
        "agentRuntimeId": fake.RUNTIME_ID,
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": IMAGE_URI}},
        "roleArn": ROLE_ARN,
        "networkConfiguration": {"networkMode": "PUBLIC"},
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800},
        "environmentVariables": {"AWS_REGION": fake.REGION},
        "description": fake.runtime_description(),
        "metadataConfiguration": {"requireMMDSV2": True},
    }
    with stubbed("bedrock-agentcore-control") as (client, stubber):
        stubber.add_response(
            "update_agent_runtime",
            {
                "agentRuntimeArn": RUNTIME_ARN,
                "agentRuntimeId": fake.RUNTIME_ID,
                "agentRuntimeVersion": "3",
                "createdAt": NOW,
                "lastUpdatedAt": NOW,
                "status": "UPDATING",
            },
            {**sent, "clientToken": client_token_for(sent, "2")},
        )
        ref = AgentCoreRuntimes(clients_with(control=client)).update_runtime(
            fake.RUNTIME_ID, spec(), "2"
        )
    assert (ref.runtime_id, ref.version, ref.status) == (fake.RUNTIME_ID, "3", "UPDATING")


@pytest.mark.parametrize("case", TOKEN_VECTORS, ids=lambda case: case["name"])
def test_client_token_vectors(case: dict[str, Any]) -> None:
    assert client_token_for(case["request"], case.get("baseVersion")) == case["expected"]


def test_an_update_back_to_an_earlier_configuration_does_not_reuse_the_old_token() -> None:
    request = {"agentRuntimeId": fake.RUNTIME_ID, "description": "same"}
    assert client_token_for(request, "2") == client_token_for(request, "2")
    assert client_token_for(request, "2") != client_token_for(request, "3")
    assert client_token_for(request, "2") != client_token_for(request)


def test_the_same_request_gets_the_same_token_and_a_changed_one_does_not() -> None:
    first = {"a": 1, "b": {"c": [1, 2]}}
    assert client_token_for(first) == client_token_for({"b": {"c": [1, 2]}, "a": 1})
    assert client_token_for(first) != client_token_for({**first, "a": 2})
    assert client_token_for(first) == client_token_for({**first, "clientToken": "ignored"})


def test_a_conflict_on_create_is_runtime_already_exists() -> None:
    with stubbed("bedrock-agentcore-control") as (client, stubber):
        error(stubber, "create_agent_runtime", "ConflictException")
        with pytest.raises(RuntimeAlreadyExists):
            AgentCoreRuntimes(clients_with(control=client)).create_runtime(spec())


def test_a_missing_runtime_is_runtime_not_found_and_not_existing() -> None:
    with stubbed("bedrock-agentcore-control") as (client, stubber):
        error(stubber, "get_agent_runtime", "ResourceNotFoundException")
        error(stubber, "get_agent_runtime", "ResourceNotFoundException")
        runtimes = AgentCoreRuntimes(clients_with(control=client))
        with pytest.raises(RuntimeNotFound):
            runtimes.get_runtime(fake.RUNTIME_ID)
        assert runtimes.runtime_exists(fake.RUNTIME_ID) is False


def test_delete_runtime_ignores_an_already_deleted_runtime() -> None:
    with stubbed("bedrock-agentcore-control") as (client, stubber):
        stubber.add_response(
            "delete_agent_runtime", {"status": "DELETING"}, {"agentRuntimeId": fake.RUNTIME_ID}
        )
        error(stubber, "delete_agent_runtime", "ResourceNotFoundException")
        runtimes = AgentCoreRuntimes(clients_with(control=client))
        runtimes.delete_runtime(fake.RUNTIME_ID)
        runtimes.delete_runtime(fake.RUNTIME_ID)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("AccessDeniedException", "access_denied"),
        ("ValidationException", "aws_error"),
        ("ThrottlingException", "aws_error"),
        ("InternalServerException", "aws_error"),
        ("ServiceQuotaExceededException", "aws_error"),
    ],
)
def test_control_plane_errors_are_translated_by_code_without_the_aws_text(
    code: str, expected: str
) -> None:
    with stubbed("bedrock-agentcore-control") as (client, stubber):
        error(stubber, "list_agent_runtimes", code)
        with pytest.raises(AgentCoreException) as raised:
            AgentCoreRuntimes(clients_with(control=client)).list_runtimes()
    assert raised.value.code == expected
    assert raised.value.fields == {"operation": "ListAgentRuntimes", "awsCode": code}
    assert "texto crudo" not in str(raised.value)


# ----------------------------------------------------------------- plano de datos


def body(text: bytes) -> StreamingBody:
    return StreamingBody(io.BytesIO(text), len(text))


def test_invoke_signs_with_the_documented_parameters_and_streams_the_body() -> None:
    with stubbed("bedrock-agentcore") as (client, stubber):
        stubber.add_response(
            "invoke_agent_runtime",
            {"contentType": "application/json", "response": body(b"x" * 20_000)},
            {
                "agentRuntimeArn": RUNTIME_ARN,
                "runtimeSessionId": "rayito-" + "a" * 32,
                "contentType": "application/json",
                "accept": "application/json",
                "payload": b'{"prompt":"hola"}',
            },
        )
        clients = clients_with()
        clients._data[AGENTCORE_REQUEST_TIMEOUT_SECONDS] = client
        response = AgentCoreInvoker(clients).invoke(
            RUNTIME_ARN, "rayito-" + "a" * 32, b'{"prompt":"hola"}', "application/json"
        )
        chunks = list(response.chunks)
        response.close()
    assert len(chunks) > 1 and b"".join(chunks) == b"x" * 20_000


def test_stream_uses_the_long_read_timeout_client() -> None:
    clients = clients_with()
    with stubbed("bedrock-agentcore") as (client, stubber):
        stubber.add_response(
            "invoke_agent_runtime",
            {"contentType": "text/event-stream", "response": body(b"data: {}\n\n")},
        )
        clients._data[AGENTCORE_STREAM_MAX_SECONDS] = client
        response = AgentCoreInvoker(clients).stream(
            RUNTIME_ARN, "rayito-" + "a" * 32, b"{}", "text/event-stream"
        )
        assert b"".join(response.chunks) == b"data: {}\n\n"


def test_the_data_client_is_built_with_the_requested_read_timeout() -> None:
    clients = AgentCoreClients(
        fake.REGION,
        boto3.session.Session(
            region_name=fake.REGION,
            aws_access_key_id="AKIDEXAMPLE0000000",
            aws_secret_access_key="x",
        ),
    )
    assert clients.data(AGENTCORE_REQUEST_TIMEOUT_SECONDS).meta.config.read_timeout == (
        AGENTCORE_REQUEST_TIMEOUT_SECONDS
    )
    assert clients.data(AGENTCORE_STREAM_MAX_SECONDS).meta.config.read_timeout == (
        AGENTCORE_STREAM_MAX_SECONDS
    )
    assert clients.data(AGENTCORE_STREAM_MAX_SECONDS) is clients.data(AGENTCORE_STREAM_MAX_SECONDS)


@pytest.mark.parametrize(
    ("code", "exception"),
    [
        ("ResourceNotFoundException", RuntimeNotFound),
        ("ThrottlingException", RateLimitException),
        ("ServiceQuotaExceededException", QuotaExceededException),
        ("RuntimeClientError", AgentException),
    ],
)
def test_invoke_errors_map_to_the_contracts_exceptions(code: str, exception: type) -> None:
    clients = clients_with()
    with stubbed("bedrock-agentcore") as (client, stubber):
        error(stubber, "invoke_agent_runtime", code)
        clients._data[AGENTCORE_REQUEST_TIMEOUT_SECONDS] = client
        with pytest.raises(exception) as raised:
            AgentCoreInvoker(clients).invoke(
                RUNTIME_ARN, "rayito-" + "a" * 32, b"{}", "application/json"
            )
    assert "texto crudo" not in str(raised.value)
    if isinstance(raised.value, AgentException):
        assert raised.value.reason == "runtime_error"


@pytest.mark.parametrize(
    ("code", "expected"),
    [("AccessDeniedException", "access_denied"), ("ValidationException", "aws_error")],
)
def test_invoke_access_and_validation_errors_are_agentcore_exceptions(
    code: str, expected: str
) -> None:
    clients = clients_with()
    with stubbed("bedrock-agentcore") as (client, stubber):
        error(stubber, "invoke_agent_runtime", code)
        clients._data[AGENTCORE_REQUEST_TIMEOUT_SECONDS] = client
        with pytest.raises(AgentCoreException) as raised:
            AgentCoreInvoker(clients).invoke(
                RUNTIME_ARN, "rayito-" + "a" * 32, b"{}", "application/json"
            )
    assert raised.value.code == expected


def test_stop_session_reports_a_session_that_no_longer_exists() -> None:
    clients = clients_with()
    with stubbed("bedrock-agentcore") as (client, stubber):
        stubber.add_response(
            "stop_runtime_session",
            {},
            {"agentRuntimeArn": RUNTIME_ARN, "runtimeSessionId": "rayito-" + "a" * 32},
        )
        error(stubber, "stop_runtime_session", "ResourceNotFoundException")
        clients._data[AGENTCORE_REQUEST_TIMEOUT_SECONDS] = client
        invoker = AgentCoreInvoker(clients)
        assert invoker.stop_session(RUNTIME_ARN, "rayito-" + "a" * 32) is True
        assert invoker.stop_session(RUNTIME_ARN, "rayito-" + "a" * 32) is False


# ------------------------------------------------------------ build, ECR, S3, logs


def test_start_build_overrides_the_source_and_the_environment() -> None:
    with stubbed("codebuild") as (client, stubber):
        stubber.add_response(
            "start_build",
            {"build": {"id": "p:1", "buildStatus": "IN_PROGRESS"}},
            {
                "projectName": fake.STACK_NAME,
                "sourceLocationOverride": (
                    f"{fake.BUCKET}/rayito/images/agentcore/x/{fake.IMAGE_TAG}.zip"
                ),
                "environmentVariablesOverride": [
                    {"name": "IMAGE_TAG", "value": fake.IMAGE_TAG, "type": "PLAINTEXT"},
                    {"name": "AGENT_BASE_IMAGE", "value": "mirror@sha256:1", "type": "PLAINTEXT"},
                ],
                "idempotencyToken": fake.IMAGE_TAG,
            },
        )
        started = CodeBuildImageBuilder(clients_with(codebuild=client)).start(
            fake.STACK_NAME,
            fake.BUCKET,
            f"rayito/images/agentcore/x/{fake.IMAGE_TAG}.zip",
            fake.IMAGE_TAG,
            "mirror@sha256:1",
        )
    assert (started.build_id, started.status) == ("p:1", "IN_PROGRESS")


def test_get_build_reads_status_phase_and_log_group() -> None:
    with stubbed("codebuild") as (client, stubber):
        stubber.add_response(
            "batch_get_builds",
            {
                "builds": [
                    {
                        "id": "p:1",
                        "buildStatus": "SUCCEEDED",
                        "currentPhase": "COMPLETED",
                        "logs": {"groupName": "/aws/codebuild/x"},
                    }
                ]
            },
            {"ids": ["p:1"]},
        )
        observed = CodeBuildImageBuilder(clients_with(codebuild=client)).get("p:1")
    assert (observed.status, observed.phase, observed.log_group) == (
        "SUCCEEDED",
        "COMPLETED",
        "/aws/codebuild/x",
    )


def test_build_errors_are_build_quota_and_a_missing_project() -> None:
    with stubbed("codebuild") as (client, stubber):
        error(stubber, "start_build", "AccountLimitExceededException")
        error(stubber, "start_build", "ResourceNotFoundException")
        builder = CodeBuildImageBuilder(clients_with(codebuild=client))
        with pytest.raises(AgentCoreException) as quota:
            builder.start("p", "b", "k", "t", "i")
        assert quota.value.code == "build_quota"
        with pytest.raises(StackResourceMissing):
            builder.start("p", "b", "k", "t", "i")


def test_describe_images_reads_the_size_and_treats_a_missing_tag_as_absent() -> None:
    with stubbed("ecr") as (client, stubber):
        stubber.add_response(
            "describe_images",
            {"imageDetails": [{"imageSizeInBytes": 123456}]},
            {"repositoryName": fake.REPOSITORY_NAME, "imageIds": [{"imageTag": "t"}]},
        )
        error(stubber, "describe_images", "ImageNotFoundException")
        error(stubber, "describe_images", "RepositoryNotFoundException")
        registry = EcrImageRegistry(clients_with(ecr=client))
        found = registry.describe(fake.REPOSITORY_NAME, "t")
        assert (found.exists, found.size_bytes) == (True, 123456)
        assert registry.describe(fake.REPOSITORY_NAME, "t").exists is False
        with pytest.raises(StackResourceMissing):
            registry.describe(fake.REPOSITORY_NAME, "t")


def identity_for(sts: Any) -> StsIdentity:
    return StsIdentity(clients_with(sts=sts))


def test_the_source_zip_is_put_with_checksum_and_owner_and_deleted_with_the_owner() -> None:
    data = b"zip bytes"
    digest = base64.b64encode(hashlib.sha256(data).digest()).decode()
    with stubbed("sts") as (sts, sts_stubber), stubbed("s3") as (s3, stubber):
        sts_stubber.add_response("get_caller_identity", {"Account": fake.ACCOUNT})
        stubber.add_response(
            "put_object",
            {},
            {
                "Bucket": fake.BUCKET,
                "Key": "k.zip",
                "ExpectedBucketOwner": fake.ACCOUNT,
                "Body": data,
                "ChecksumSHA256": digest,
            },
        )
        stubber.add_response(
            "delete_object",
            {},
            {"Bucket": fake.BUCKET, "Key": "k.zip", "ExpectedBucketOwner": fake.ACCOUNT},
        )
        clients = clients_with(s3=s3, sts=sts)
        bucket = S3ArtifactBucket(clients, StsIdentity(clients))
        bucket.put(fake.BUCKET, "k.zip", data)
        bucket.delete(fake.BUCKET, "k.zip")


def test_region_of_a_bucket_reads_head_bucket_and_hides_missing_or_foreign_buckets() -> None:
    owner = {"ExpectedBucketOwner": fake.ACCOUNT}
    with stubbed("sts") as (sts, sts_stubber), stubbed("s3") as (s3, stubber):
        sts_stubber.add_response("get_caller_identity", {"Account": fake.ACCOUNT})
        stubber.add_response("head_bucket", {}, {"Bucket": "mine", **owner})
        error(stubber, "head_bucket", "404")
        error(stubber, "head_bucket", "403")
        clients = clients_with(s3=s3, sts=sts)
        bucket = S3ArtifactBucket(clients, StsIdentity(clients))
        assert bucket.region_of("mine") is None
        assert bucket.region_of("missing") is None
        assert bucket.region_of("foreign") is None


def test_a_log_group_that_is_already_gone_is_not_an_error() -> None:
    with stubbed("logs") as (logs, stubber):
        stubber.add_response("delete_log_group", {}, {"logGroupName": "/g"})
        error(stubber, "delete_log_group", "ResourceNotFoundException")
        group = CloudWatchRuntimeLogs(clients_with(logs=logs))
        assert group.delete_log_group("/g") is True
        assert group.delete_log_group("/g") is False


def test_the_account_is_read_once() -> None:
    with stubbed("sts") as (sts, stubber):
        stubber.add_response("get_caller_identity", {"Account": fake.ACCOUNT})
        identity = StsIdentity(clients_with(sts=sts))
        assert identity.account_id() == fake.ACCOUNT
        assert identity.account_id() == fake.ACCOUNT


# --------------------------------------------------------------------------- pila


class _Provisioner:
    """Un `StackProvisioner` en memoria con lo que el adaptador necesita."""

    def __init__(self, state: str | None = None) -> None:
        self.state = state
        self.outcome: UpdateOutcome = "changed"
        self.updated = 0
        self.created = 0
        self.deleted = 0
        self.waits: list[str] = []
        self.wait_error: Exception | None = None

    def describe(self, stack_name: str) -> Any:
        if self.state is None:
            return None
        return StackStatus(stack_name, self.state, dict(fake.DEFAULT_OUTPUTS))

    def create(self, component: Any, **arguments: Any) -> None:
        self.created += 1
        self.state = "CREATE_COMPLETE"
        self.arguments = arguments

    def update(self, component: Any, **arguments: Any) -> UpdateOutcome:
        self.updated += 1
        self.arguments = arguments
        return self.outcome

    def delete(self, stack_name: str) -> None:
        self.deleted += 1
        self.state = None

    def wait(self, stack_name: str, target: str, timeout: float) -> None:
        self.waits.append(target)
        if self.wait_error is not None:
            raise self.wait_error

    def put_artifact(self, bucket: str, key: str, data: bytes) -> None:
        raise AssertionError

    def failure_reason(self, stack_name: str) -> str | None:
        return None


def test_the_stack_is_created_then_updated_and_reports_whether_anything_changed() -> None:
    provisioner = _Provisioner()
    stack = CloudFormationAgentStack(
        provisioner,
        stack_name=fake.STACK_NAME,
        tags={"team": "agents"},
    )
    assert stack.observe().status is None
    parameters = {
        "AgentName": fake.AGENT,
        "AgentDisplayName": fake.AGENT,
        "RuntimeName": fake.RUNTIME_NAME,
        "ArtifactBucket": fake.BUCKET,
    }
    first = stack.deploy(parameters)
    assert first.changed is True and provisioner.created == 1
    assert provisioner.arguments["tags"]["team"] == "agents"
    assert provisioner.arguments["tags"]["rayito:component"] == "agentcore-runtime"
    provisioner.outcome = "no_changes"
    second = stack.deploy(parameters)
    assert second.changed is False and provisioner.updated == 1
    provisioner.outcome = "changed"
    assert stack.deploy(parameters).changed is True


def test_a_failed_stack_is_stack_failed_with_its_state_and_a_failed_delete_is_delete_failed() -> (
    None
):
    provisioner = _Provisioner("CREATE_COMPLETE")
    provisioner.wait_error = StackException("terminó en CREATE_FAILED", code="failed")
    stack = CloudFormationAgentStack(provisioner, stack_name=fake.STACK_NAME)
    with pytest.raises(AgentCoreException) as failed:
        stack.wait()
    assert failed.value.code == "stack_failed"
    assert failed.value.fields["status"] == "CREATE_COMPLETE"
    with pytest.raises(AgentCoreException) as deleted:
        stack.destroy()
    assert deleted.value.code == "delete_failed"
    assert deleted.value.fields["resource"] == "stack"
