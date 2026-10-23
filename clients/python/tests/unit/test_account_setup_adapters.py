"""Los adaptadores de AWS de `rayito setup`: la pila de IAM sobre
`OptionalStacks` con el `StackProvisioner` falso, y el bucket y la imagen
sobre clientes boto3 con `Stubber`. Construirlos no llama a AWS."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from rayito._account_setup._artifact_stack import ARTIFACT_STACK_COMPONENT
from rayito._account_setup._aws import (
    MicrovmBaseImages,
    S3ArtifactBuckets,
    artifact_stack_gateway,
    iam_stack_gateway,
)
from rayito._account_setup._domain import StackObservation
from rayito._account_setup._iam_stack import IAM_STACK_COMPONENT
from rayito._account_setup._service import AccountSetup
from rayito._stacks._model import StackComponent, StackStatus
from rayito._stacks._packaging import load_template, template_fingerprint
from rayito._stacks._registry import COMPONENTS
from rayito.cli._publish import PublishSettings
from rayito.cli._session import Clients
from rayito.cli._setup_publisher import PipelineImagePublisher
from rayito.cli.setup import gateways_for
from rayito.exceptions import StackException

from .cli.conftest import ACCOUNT_ID, IMAGE_ARN, REGION, image_summary, version_item
from .fake_stacks import FakeStackProvisioner
from .test_m15_stacks_template_parameters import (
    component_parameter_names,
    template_parameter_names,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
STACK = "rayito-m0-iam"
BUCKET = "amzn-s3-demo-bucket"
OTHER_BUCKET = "amzn-s3-demo-bucket1"
BUILD_ROLE = f"arn:aws:iam::{ACCOUNT_ID}:role/rayito-m0-build-{REGION}"
OTHER_PARAMETERS = (
    "LogGroupPrefix",
    "NetworkConnectorArns",
    "PersistenceBucket",
    "PersistencePrefix",
    "TransferBucket",
    "TransferPrefix",
)


class RecordingProvisioner(FakeStackProvisioner):
    """El falso que además recuerda el componente que se creó."""

    created_component: StackComponent | None = None

    def create(self, component: StackComponent, **kwargs: Any) -> None:
        self.created_component = component
        super().create(component, **kwargs)


def deployed_stack(bucket: str = BUCKET) -> StackStatus:
    parameters = {"ArtifactBucket": bucket, **{name: "" for name in OTHER_PARAMETERS}}
    return StackStatus(
        STACK, "CREATE_COMPLETE", outputs={"BuildRoleArn": BUILD_ROLE}, parameters=parameters
    )


def test_the_iam_component_declares_the_parameters_of_the_packaged_template() -> None:
    template = load_template(IAM_STACK_COMPONENT)
    assert component_parameter_names(IAM_STACK_COMPONENT) == template_parameter_names(template)


def test_the_iam_component_is_not_in_the_stack_catalog() -> None:
    assert IAM_STACK_COMPONENT.name not in {component.name for component in COMPONENTS}


def test_only_the_bucket_is_required_and_the_other_parameters_keep_the_template_default() -> None:
    required = [p.name for p in IAM_STACK_COMPONENT.parameters if p.required]
    assert required == ["ArtifactBucket"]
    assert all(p.default is None for p in IAM_STACK_COMPONENT.parameters)


def test_observing_a_missing_stack() -> None:
    gateway = iam_stack_gateway(FakeStackProvisioner())
    assert gateway.observe() == StackObservation(STACK, None, {}, {})


def test_observing_an_existing_stack_maps_status_outputs_and_parameters() -> None:
    gateway = iam_stack_gateway(FakeStackProvisioner(stacks={STACK: deployed_stack()}))
    observed = gateway.observe()
    assert observed.status == "CREATE_COMPLETE"
    assert observed.outputs == {"BuildRoleArn": BUILD_ROLE}
    assert observed.parameters["ArtifactBucket"] == BUCKET


def test_the_template_fingerprint_is_the_sha256_of_the_repository_template() -> None:
    """El mismo valor que calcula TypeScript: los dos SDK empaquetan
    `infra/iam.yaml` byte a byte."""
    template = REPO_ROOT / "infra" / "iam.yaml"
    expected = hashlib.sha256(template.read_bytes()).hexdigest()
    assert template_fingerprint(IAM_STACK_COMPONENT) == expected
    assert iam_stack_gateway(FakeStackProvisioner()).template_fingerprint() == expected


def test_create_sends_named_iam_the_bucket_and_the_template_fingerprint_then_waits() -> None:
    provisioner = RecordingProvisioner()
    observed = iam_stack_gateway(provisioner).create(BUCKET)
    assert provisioner.created_component is not None
    assert provisioner.created_component.capabilities == ("CAPABILITY_NAMED_IAM",)
    assert provisioner.sent_parameters == {
        "ArtifactBucket": BUCKET,
        "TemplateFingerprint": template_fingerprint(IAM_STACK_COMPONENT),
    }
    assert [call[0] for call in provisioner.calls] == ["describe", "create", "wait", "describe"]
    assert observed.status == "CREATE_COMPLETE"


def test_update_sends_the_bucket_and_the_fingerprint_and_keeps_the_six_others() -> None:
    provisioner = FakeStackProvisioner(stacks={STACK: deployed_stack()})
    iam_stack_gateway(provisioner).update_bucket(OTHER_BUCKET)
    assert provisioner.sent_parameters == {
        "ArtifactBucket": OTHER_BUCKET,
        "TemplateFingerprint": template_fingerprint(IAM_STACK_COMPONENT),
    }
    assert provisioner.sent_keep_previous == tuple(sorted(OTHER_PARAMETERS))
    assert ("update", STACK) in provisioner.calls
    assert not any(call[0] == "create" for call in provisioner.calls)


def test_wait_polls_the_stack_and_observes_it_again() -> None:
    provisioner = FakeStackProvisioner(stacks={STACK: deployed_stack()})
    observed = iam_stack_gateway(provisioner, wait_timeout_seconds=7.0).wait()
    assert provisioner.calls[0] == ("wait", STACK, "deployed")
    assert provisioner.wait_timeouts == [7.0]
    assert observed.status == "CREATE_COMPLETE"


def test_wait_fails_if_the_stack_vanished() -> None:
    with pytest.raises(StackException) as caught:
        iam_stack_gateway(FakeStackProvisioner()).wait()
    assert caught.value.code == "failed"


ARTIFACT_STACK = "rayito-m0-artifacts"


def test_the_artifact_component_declares_the_parameters_of_the_packaged_template() -> None:
    template = load_template(ARTIFACT_STACK_COMPONENT)
    assert component_parameter_names(ARTIFACT_STACK_COMPONENT) == template_parameter_names(template)


def test_the_artifact_component_is_not_in_the_stack_catalog() -> None:
    assert ARTIFACT_STACK_COMPONENT.name not in {component.name for component in COMPONENTS}


def test_only_the_bucket_name_is_required_in_the_artifact_component() -> None:
    required = [p.name for p in ARTIFACT_STACK_COMPONENT.parameters if p.required]
    assert required == ["BucketName"]
    assert all(p.default is None for p in ARTIFACT_STACK_COMPONENT.parameters)


def test_observing_a_missing_artifact_stack() -> None:
    gateway = artifact_stack_gateway(FakeStackProvisioner())
    assert gateway.observe() == StackObservation(ARTIFACT_STACK, None, {}, {})


def test_the_artifact_stack_create_sends_only_the_bucket_name_and_no_capabilities() -> None:
    provisioner = RecordingProvisioner()
    observed = artifact_stack_gateway(provisioner).create(BUCKET)
    assert provisioner.created_component is not None
    assert provisioner.created_component.capabilities == ()
    assert provisioner.sent_parameters == {"BucketName": BUCKET}
    assert ("create", ARTIFACT_STACK) in provisioner.calls
    assert (observed.name, observed.status) == (ARTIFACT_STACK, "CREATE_COMPLETE")


ORG_TAGS = {"team": "platform", "cost-center": "1234"}


def test_the_tags_reach_the_create_of_both_stacks() -> None:
    for gateway in (iam_stack_gateway, artifact_stack_gateway):
        provisioner = FakeStackProvisioner()
        gateway(provisioner, tags=ORG_TAGS).create(BUCKET)
        assert ORG_TAGS.items() <= provisioner.sent_tags.items()


def test_the_tags_reach_the_update_of_the_iam_stack() -> None:
    provisioner = FakeStackProvisioner(stacks={STACK: deployed_stack()})
    iam_stack_gateway(provisioner, tags=ORG_TAGS).update_bucket(OTHER_BUCKET)
    assert ("update", STACK) in provisioner.calls
    assert ORG_TAGS.items() <= provisioner.sent_tags.items()


def test_without_tags_only_the_fixed_rayito_tags_are_sent() -> None:
    provisioner = FakeStackProvisioner()
    iam_stack_gateway(provisioner).create(BUCKET)
    assert set(provisioner.sent_tags) == {
        "rayito:component",
        "rayito:managed-by",
        "rayito:sdk-version",
    }


def stubbed(service: str) -> Any:
    session = boto3.session.Session(
        region_name=REGION, aws_access_key_id="testing", aws_secret_access_key="testing"
    )
    return session.client(service, config=Config(retries={"total_max_attempts": 1}))


@dataclass
class BucketClients:
    s3: Any
    account_id: str = ACCOUNT_ID


def probe(stub_setup: Any) -> Any:
    client = stubbed("s3")
    with Stubber(client) as stub:
        stub_setup(stub)
        result = S3ArtifactBuckets(BucketClients(client)).probe(BUCKET)
        stub.assert_no_pending_responses()
    return result


def test_probe_reports_the_region_of_an_accessible_bucket() -> None:
    result = probe(
        lambda stub: stub.add_response(
            "head_bucket",
            {"BucketRegion": REGION},
            {"Bucket": BUCKET, "ExpectedBucketOwner": ACCOUNT_ID},
        )
    )
    assert (result.state, result.region) == ("found", REGION)


def test_probe_of_a_bucket_without_a_reported_region() -> None:
    result = probe(
        lambda stub: stub.add_response(
            "head_bucket", {}, {"Bucket": BUCKET, "ExpectedBucketOwner": ACCOUNT_ID}
        )
    )
    assert (result.state, result.region) == ("found", None)


@pytest.mark.parametrize(
    ("error_code", "state"),
    [
        ("404", "missing"),
        ("NoSuchBucket", "missing"),
        ("NotFound", "missing"),
        ("403", "forbidden"),
    ],
)
def test_probe_tells_a_missing_bucket_from_a_forbidden_one(error_code: str, state: str) -> None:
    result = probe(
        lambda stub: stub.add_client_error(
            "head_bucket",
            service_error_code=error_code,
            http_status_code=int(error_code) if error_code.isdigit() else 404,
            expected_params={"Bucket": BUCKET, "ExpectedBucketOwner": ACCOUNT_ID},
        )
    )
    assert (result.state, result.region) == (state, None)


def test_probe_lets_any_other_error_through() -> None:
    client = stubbed("s3")
    with Stubber(client) as stub:
        stub.add_client_error(
            "head_bucket",
            service_error_code="500",
            http_status_code=500,
            expected_params={"Bucket": BUCKET, "ExpectedBucketOwner": ACCOUNT_ID},
        )
        with pytest.raises(ClientError):
            S3ArtifactBuckets(BucketClients(client)).probe(BUCKET)


def test_the_account_comes_from_the_clients() -> None:
    assert S3ArtifactBuckets(BucketClients(stubbed("s3"))).account_id() == ACCOUNT_ID


@dataclass
class MicrovmClients:
    microvms: Any


def images(client: Any, *, sleep: Any = lambda _seconds: None) -> MicrovmBaseImages:
    return MicrovmBaseImages(MicrovmClients(client), REGION, lambda: ACCOUNT_ID, sleep=sleep)


def test_observing_a_missing_image_makes_a_single_call() -> None:
    client = stubbed("lambda-microvms")
    with Stubber(client) as stub:
        stub.add_client_error(
            "get_microvm_image",
            service_error_code="ResourceNotFoundException",
            expected_params={"imageIdentifier": IMAGE_ARN},
        )
        observed = images(client).observe("rayito-base")
        stub.assert_no_pending_responses()
    assert observed.state is None
    assert observed.image_arn == IMAGE_ARN
    assert observed.versions == ()


def test_observing_an_existing_image_lists_its_versions() -> None:
    client = stubbed("lambda-microvms")
    with Stubber(client) as stub:
        stub.add_response(
            "get_microvm_image", image_summary(state="UPDATING"), {"imageIdentifier": IMAGE_ARN}
        )
        stub.add_response(
            "list_microvm_image_versions",
            {"items": [version_item(1), version_item(2, state="IN_PROGRESS", status="INACTIVE")]},
            {"imageIdentifier": IMAGE_ARN},
        )
        observed = images(client).observe("rayito-base")
        stub.assert_no_pending_responses()
    assert observed.state == "UPDATING"
    assert [(v.image_version, v.state, v.status) for v in observed.versions] == [
        ("1.0", "SUCCESSFUL", "ACTIVE"),
        ("2.0", "IN_PROGRESS", "INACTIVE"),
    ]
    assert observed.versions[0].created_at == datetime(2026, 9, 1, 1, tzinfo=UTC)


def test_observing_an_image_lets_other_errors_through() -> None:
    client = stubbed("lambda-microvms")
    with Stubber(client) as stub:
        stub.add_client_error(
            "get_microvm_image",
            service_error_code="AccessDeniedException",
            expected_params={"imageIdentifier": IMAGE_ARN},
        )
        with pytest.raises(ClientError):
            images(client).observe("rayito-base")


def test_waiting_polls_the_gate_until_the_version_settles() -> None:
    client = stubbed("lambda-microvms")
    sleeps: list[float] = []
    lines: list[str] = []
    with Stubber(client) as stub:
        for image_state, version_state, status in (
            ("UPDATING", "IN_PROGRESS", "INACTIVE"),
            ("UPDATED", "SUCCESSFUL", "ACTIVE"),
        ):
            stub.add_response(
                "get_microvm_image",
                image_summary(state=image_state),
                {"imageIdentifier": IMAGE_ARN},
            )
            stub.add_response(
                "get_microvm_image_version",
                version_item(2, state=version_state, status=status),
                {"imageIdentifier": IMAGE_ARN, "imageVersion": "2.0"},
            )
        result = images(client, sleep=sleeps.append).wait(IMAGE_ARN, "2.0", lines.append)
        stub.assert_no_pending_responses()
    assert (result.launchable, result.version_state) == (True, "SUCCESSFUL")
    assert len(sleeps) == 1
    assert lines[0].startswith("  image=UPDATING version=2.0 state=IN_PROGRESS status=INACTIVE (")
    assert lines[1].startswith("  image=UPDATED version=2.0 state=SUCCESSFUL status=ACTIVE (")


def test_a_failed_version_is_not_launchable() -> None:
    client = stubbed("lambda-microvms")
    with Stubber(client) as stub:
        stub.add_response(
            "get_microvm_image",
            image_summary(state="UPDATE_FAILED"),
            {"imageIdentifier": IMAGE_ARN},
        )
        stub.add_response(
            "get_microvm_image_version",
            version_item(2, state="FAILED", status="INACTIVE"),
            {"imageIdentifier": IMAGE_ARN, "imageVersion": "2.0"},
        )
        result = images(client).wait(IMAGE_ARN, "2.0", lambda _line: None)
    assert (result.launchable, result.version_state) == (False, "FAILED")


def test_the_default_gateways_and_the_service_make_no_aws_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    original = boto3.session.Session.client

    def recording_client(self: Any, service_name: str, *args: Any, **kwargs: Any) -> Any:
        calls.append(service_name)
        return original(self, service_name, *args, **kwargs)

    monkeypatch.setattr(boto3.session.Session, "client", recording_client)
    session = boto3.session.Session(
        region_name=REGION, aws_access_key_id="testing", aws_secret_access_key="testing"
    )
    AccountSetup(gateways_for(Clients(session=session, region=REGION)))
    assert calls == []


@pytest.mark.parametrize(
    ("name", "os_capabilities"), [("rayito-base", None), ("rayito-base-caps", "ALL")]
)
def test_the_pipeline_publisher_publishes_the_named_image_with_its_capabilities(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str, os_capabilities: str | None
) -> None:
    from rayito.cli import _setup_publisher as publisher_module

    captured: list[PublishSettings] = []

    def fake_build(_clients: Any, settings: PublishSettings, **_kwargs: Any) -> dict[str, Any]:
        captured.append(settings)
        return {
            "launchable": True,
            "imageArn": f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:microvm-image:{name}",
            "imageVersion": "1.0",
            "artifactUploaded": False,
            "gate": {"versionState": "SUCCESSFUL"},
        }

    monkeypatch.setattr(publisher_module, "build_or_reuse", fake_build)
    monkeypatch.setattr(publisher_module, "resolve_base_image_version", lambda *_args: "1.0")
    lines: list[str] = []
    published = PipelineImagePublisher(
        cast(Any, SimpleNamespace(microvms=None, region=REGION))
    ).publish(
        tmp_path / "rayito-image.zip",
        name=name,
        os_capabilities=os_capabilities,
        bucket=BUCKET,
        build_role_arn=BUILD_ROLE,
        on_progress=lines.append,
    )
    (settings,) = captured
    assert (settings.image_name, settings.os_capabilities) == (name, os_capabilities)
    assert settings.variant == "full"
    assert published.image_version == "1.0"
    assert lines == ["base_image_version=1.0"]
