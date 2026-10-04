"""`rayito._stacks._cloudformation.CloudFormationProvisioner`: el contrato de
parámetros de `AWS_API_NOTES.md` §21, comprobado con `Stubber` contra el
modelo de botocore 1.43.103 (`CreateStack`, `UpdateStack`, `DescribeStacks`,
`DeleteStack`), y `wait`/`put_artifact` con reloj y sueño inyectables."""

from __future__ import annotations

import base64
import hashlib
import io
from datetime import UTC, datetime

import boto3
import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber

from rayito._stacks._cloudformation import CloudFormationProvisioner
from rayito._stacks._model import CostStatement, StackComponent
from rayito.exceptions import StackException

#: `DescribeStacksOutput.Stacks[].CreationTime` is required by the shape;
#: its value never matters to the adapter.
FIXED_TIME = datetime(2026, 1, 1, tzinfo=UTC)

COMPONENT = StackComponent(
    name="metadata-index", description="x", cost=CostStatement(creates=(), idle_monthly="$0")
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def provisioner() -> tuple[CloudFormationProvisioner, Stubber, Stubber]:
    cfn_client = boto3.client("cloudformation", region_name="us-east-1")
    s3_client = boto3.client("s3", region_name="us-east-1")
    provisioner = CloudFormationProvisioner.__new__(CloudFormationProvisioner)
    provisioner._cloudformation = _FrozenLazyClient(cfn_client)
    provisioner._s3 = _FrozenLazyClient(s3_client)
    clock = FakeClock()
    provisioner._clock = clock
    provisioner._sleep = clock.sleep
    provisioner._poll_interval = 1.0
    return provisioner, Stubber(cfn_client), Stubber(s3_client)


def provisioner_with_sts() -> tuple[CloudFormationProvisioner, Stubber, Stubber, Stubber]:
    adapter, cfn_stub, s3_stub = provisioner()
    sts_client = boto3.client("sts", region_name="us-east-1")
    adapter._sts = _FrozenLazyClient(sts_client)
    return adapter, cfn_stub, s3_stub, Stubber(sts_client)


class _FrozenLazyClient:
    """Como `LazyClient`, pero sobre un cliente ya construido (el que
    lleva el `Stubber`), para no abrir una sesión real en el test."""

    def __init__(self, client: object) -> None:
        self._client = client

    def get(self) -> object:
        return self._client


def test_describe_returns_none_for_a_missing_stack() -> None:
    adapter, cfn_stub, _s3_stub = provisioner()
    with cfn_stub:
        cfn_stub.add_client_error(
            "describe_stacks",
            service_error_code="ValidationError",
            service_message="Stack with id rayito-metadata-index does not exist",
            expected_params={"StackName": "rayito-metadata-index"},
        )
        assert adapter.describe("rayito-metadata-index") is None


def test_describe_maps_state_and_outputs() -> None:
    adapter, cfn_stub, _s3_stub = provisioner()
    with cfn_stub:
        cfn_stub.add_response(
            "describe_stacks",
            {
                "Stacks": [
                    {
                        "StackName": "rayito-metadata-index",
                        "CreationTime": FIXED_TIME,
                        "StackStatus": "CREATE_COMPLETE",
                        "Outputs": [
                            {"OutputKey": "TableArn", "OutputValue": "arn:aws:dynamodb:..."}
                        ],
                    }
                ]
            },
            expected_params={"StackName": "rayito-metadata-index"},
        )
        status = adapter.describe("rayito-metadata-index")
    assert status is not None
    assert status.state == "CREATE_COMPLETE"
    assert status.outputs == {"TableArn": "arn:aws:dynamodb:..."}


def test_create_stack_sends_parameters_tags_and_capabilities() -> None:
    adapter, cfn_stub, _s3_stub = provisioner()
    with cfn_stub:
        cfn_stub.add_response(
            "create_stack",
            {"StackId": "arn:aws:cloudformation:..."},
            expected_params={
                "StackName": "rayito-metadata-index",
                "TemplateBody": "AWSTemplateFormatVersion: ...",
                "Parameters": [{"ParameterKey": "TableName", "ParameterValue": "rayito-sandboxes"}],
                "Tags": [{"Key": "rayito:component", "Value": "metadata-index"}],
                "Capabilities": ["CAPABILITY_IAM"],
            },
        )
        adapter.create(
            StackComponent(
                name="metadata-index",
                description="x",
                capabilities=("CAPABILITY_IAM",),
                cost=CostStatement(creates=(), idle_monthly="$0"),
            ),
            stack_name="rayito-metadata-index",
            template_body="AWSTemplateFormatVersion: ...",
            parameters={"TableName": "rayito-sandboxes"},
            tags={"rayito:component": "metadata-index"},
        )


def test_update_with_no_changes_is_reported_as_no_changes() -> None:
    adapter, cfn_stub, _s3_stub = provisioner()
    with cfn_stub:
        cfn_stub.add_client_error(
            "update_stack",
            service_error_code="ValidationError",
            service_message="No updates are to be performed.",
        )
        outcome = adapter.update(
            COMPONENT,
            stack_name="rayito-metadata-index",
            template_body="x",
            parameters={},
            tags={},
        )
    assert outcome == "no_changes"


def test_update_with_real_changes_is_reported_as_changed() -> None:
    adapter, cfn_stub, _s3_stub = provisioner()
    with cfn_stub:
        cfn_stub.add_response("update_stack", {"StackId": "arn:..."})
        outcome = adapter.update(
            COMPONENT,
            stack_name="rayito-metadata-index",
            template_body="x",
            parameters={},
            tags={},
        )
    assert outcome == "changed"


def test_an_unrelated_validation_error_on_update_still_raises() -> None:
    adapter, cfn_stub, _s3_stub = provisioner()
    with cfn_stub:
        cfn_stub.add_client_error(
            "update_stack",
            service_error_code="ValidationError",
            service_message="Something else entirely went wrong",
        )
        with pytest.raises(StackException):
            adapter.update(COMPONENT, stack_name="x", template_body="x", parameters={}, tags={})


def test_delete_is_a_plain_call() -> None:
    adapter, cfn_stub, _s3_stub = provisioner()
    with cfn_stub:
        cfn_stub.add_response(
            "delete_stack", {}, expected_params={"StackName": "rayito-metadata-index"}
        )
        adapter.delete("rayito-metadata-index")


ACCOUNT = "123456789012"
DATA = b"data"
DATA_SHA256_B64 = base64.b64encode(hashlib.sha256(DATA).digest()).decode("ascii")
LOCATED = {"Bucket": "b", "Key": "k", "ExpectedBucketOwner": ACCOUNT}


def _identity(sts_stub: Stubber) -> None:
    sts_stub.add_response(
        "get_caller_identity",
        {"Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/x", "UserId": "x"},
        expected_params={},
    )


def test_put_artifact_skips_the_upload_only_when_the_content_matches() -> None:
    adapter, _cfn_stub, s3_stub, sts_stub = provisioner_with_sts()
    with s3_stub, sts_stub:
        _identity(sts_stub)
        s3_stub.add_response(
            "get_object", {"Body": StreamingBody(io.BytesIO(DATA), len(DATA))}, LOCATED
        )
        adapter.put_artifact("b", "k", DATA)


def test_put_artifact_overwrites_a_planted_object_under_the_same_key() -> None:
    # The key is public (the sha256 of the zip that ships with the SDK):
    # an object that merely exists there is never trusted.
    adapter, _cfn_stub, s3_stub, sts_stub = provisioner_with_sts()
    planted = b"not the sdk's code"
    with s3_stub, sts_stub:
        _identity(sts_stub)
        s3_stub.add_response(
            "get_object", {"Body": StreamingBody(io.BytesIO(planted), len(planted))}, LOCATED
        )
        s3_stub.add_response(
            "put_object", {}, {**LOCATED, "Body": DATA, "ChecksumSHA256": DATA_SHA256_B64}
        )
        adapter.put_artifact("b", "k", DATA)


def test_put_artifact_uploads_when_missing_with_owner_and_checksum() -> None:
    adapter, _cfn_stub, s3_stub, sts_stub = provisioner_with_sts()
    with s3_stub, sts_stub:
        _identity(sts_stub)
        s3_stub.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)
        s3_stub.add_response(
            "put_object", {}, {**LOCATED, "Body": DATA, "ChecksumSHA256": DATA_SHA256_B64}
        )
        adapter.put_artifact("b", "k", DATA)


def test_put_artifact_fails_on_a_bucket_of_another_account() -> None:
    adapter, _cfn_stub, s3_stub, sts_stub = provisioner_with_sts()
    with s3_stub, sts_stub:
        _identity(sts_stub)
        s3_stub.add_client_error(
            "get_object", service_error_code="AccessDenied", http_status_code=403
        )
        with pytest.raises(StackException):
            adapter.put_artifact("b", "k", DATA)


def test_wait_for_deployed_returns_once_the_stack_completes() -> None:
    adapter, cfn_stub, _s3_stub = provisioner()
    with cfn_stub:
        cfn_stub.add_response(
            "describe_stacks",
            {
                "Stacks": [
                    {
                        "StackName": "x",
                        "CreationTime": FIXED_TIME,
                        "StackStatus": "CREATE_IN_PROGRESS",
                    }
                ]
            },
        )
        cfn_stub.add_response(
            "describe_stacks",
            {
                "Stacks": [
                    {"StackName": "x", "CreationTime": FIXED_TIME, "StackStatus": "CREATE_COMPLETE"}
                ]
            },
        )
        adapter.wait("x", "deployed", timeout=10.0)


def test_wait_raises_on_a_terminal_failure_state() -> None:
    adapter, cfn_stub, _s3_stub = provisioner()
    with cfn_stub:
        cfn_stub.add_response(
            "describe_stacks",
            {
                "Stacks": [
                    {
                        "StackName": "x",
                        "CreationTime": FIXED_TIME,
                        "StackStatus": "ROLLBACK_COMPLETE",
                    }
                ]
            },
        )
        with pytest.raises(StackException, match="ROLLBACK_COMPLETE"):
            adapter.wait("x", "deployed", timeout=10.0)


def test_wait_raises_once_the_timeout_is_exhausted() -> None:
    adapter, cfn_stub, _s3_stub = provisioner()
    with cfn_stub:
        for _ in range(3):
            cfn_stub.add_response(
                "describe_stacks",
                {
                    "Stacks": [
                        {
                            "StackName": "x",
                            "CreationTime": FIXED_TIME,
                            "StackStatus": "CREATE_IN_PROGRESS",
                        }
                    ]
                },
            )
        with pytest.raises(StackException, match="tiempo agotado"):
            adapter.wait("x", "deployed", timeout=2.0)


def test_wait_for_deleted_accepts_a_missing_stack() -> None:
    adapter, cfn_stub, _s3_stub = provisioner()
    with cfn_stub:
        cfn_stub.add_client_error(
            "describe_stacks",
            service_error_code="ValidationError",
            service_message="Stack with id x does not exist",
        )
        adapter.wait("x", "deleted", timeout=10.0)


def test_failure_reason_forwards_the_stacks_own_reason() -> None:
    adapter, cfn_stub, _s3_stub = provisioner()
    with cfn_stub:
        cfn_stub.add_response(
            "describe_stacks",
            {
                "Stacks": [
                    {
                        "StackName": "x",
                        "CreationTime": FIXED_TIME,
                        "StackStatus": "ROLLBACK_COMPLETE",
                        "StackStatusReason": "boom",
                    }
                ]
            },
        )
        assert adapter.failure_reason("x") == "boom"


def test_update_sends_kept_parameters_with_use_previous_value() -> None:
    adapter, cfn_stub, _s3_stub = provisioner()
    with cfn_stub:
        cfn_stub.add_response(
            "update_stack",
            {"StackId": "arn:..."},
            expected_params={
                "StackName": "rayito-s3-mounts",
                "TemplateBody": "x",
                "Parameters": [
                    {"ParameterKey": "BucketName", "ParameterValue": "b"},
                    {"ParameterKey": "Prefixes", "UsePreviousValue": True},
                    {"ParameterKey": "ReadOnly", "UsePreviousValue": True},
                ],
                "Tags": [],
                "Capabilities": [],
            },
        )
        adapter.update(
            COMPONENT,
            stack_name="rayito-s3-mounts",
            template_body="x",
            parameters={"BucketName": "b"},
            tags={},
            keep_previous=("Prefixes", "ReadOnly"),
        )


def test_describe_maps_the_deployed_parameters() -> None:
    adapter, cfn_stub, _s3_stub = provisioner()
    with cfn_stub:
        cfn_stub.add_response(
            "describe_stacks",
            {
                "Stacks": [
                    {
                        "StackName": "rayito-s3-mounts",
                        "CreationTime": FIXED_TIME,
                        "StackStatus": "UPDATE_COMPLETE",
                        "Parameters": [
                            {"ParameterKey": "Prefixes", "ParameterValue": "team7/*"},
                        ],
                    }
                ]
            },
            expected_params={"StackName": "rayito-s3-mounts"},
        )
        status = adapter.describe("rayito-s3-mounts")
    assert status is not None
    assert status.parameters == {"Prefixes": "team7/*"}
