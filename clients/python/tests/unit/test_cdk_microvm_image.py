"""`RayitoMicrovmImage` y `MicrovmImageArtifact` sin AWS (ADR-032 D9): cada
imagen lleva la configuración de `rayito image publish` con los nombres del
esquema, los tamaños se llaman `<nombre>-<tamaño>`, el rol de build sólo lee
el objeto del asset y escribe sus grupos de logs, las validaciones fallan
antes de añadir nada y `download_release` reutiliza su caché sin red."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("aws_cdk")

import aws_cdk as cdk

from rayito._account_setup._domain import (
    CHECKSUMS_ASSET_NAME,
    IMAGE_ASSET_NAME,
    AccountSetupException,
    DownloadedRelease,
)
from rayito._images_publish_domain import (
    IMAGE_HOOKS,
    ImageBuildSettings,
    desired_configuration,
    sized_settings,
)
from rayito.cdk import MicrovmImageArtifact, RayitoCdkException, RayitoMicrovmImage
from rayito.cdk import _microvm_image as image_module
from rayito.cdk._image_properties import (
    camel_case,
    cfn_image_properties,
    pascal_case,
)
from tests.cdk_contract_harness import APP_CONTEXT, CONTRACT_DIR, generator

ACCOUNT = "123456789012"
REGION = "us-east-1"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/build"
URI = "s3://amzn-s3-demo-bucket/rayd.zip"


def fixture_zip(tmp_path: Path, fixture: str = "image-full") -> Path:
    destination = tmp_path / f"{fixture}.zip"
    source = CONTRACT_DIR / "fixtures" / fixture
    generated: Path = generator().write_fixture_zip(source, destination)
    return generated


def new_stack(env: cdk.Environment | None = None) -> cdk.Stack:
    app = cdk.App(context=APP_CONTEXT)
    return cdk.Stack(app, "ImageStack", env=env or cdk.Environment(account=ACCOUNT, region=REGION))


def resources(stack: cdk.Stack, kind: str) -> dict[str, Any]:
    template = cdk.assertions.Template.from_stack(stack).to_json()
    return {k: v for k, v in template["Resources"].items() if v["Type"] == kind}


def image(stack: cdk.Stack, tmp_path: Path, **overrides: Any) -> RayitoMicrovmImage:
    props: dict[str, Any] = {
        "name": "team-sandbox",
        "artifact": MicrovmImageArtifact.from_file(fixture_zip(tmp_path)),
        "base_image_version": "1",
        **overrides,
    }
    return RayitoMicrovmImage(stack, "Image", **props)


# ------------------------------------------------------- configuración pura


def test_the_properties_are_the_publish_configuration_with_schema_names() -> None:
    settings = ImageBuildSettings(
        image_name="team",
        base_image_version="1",
        memory_mib=2048,
        os_capabilities="ALL",
        environment_variables={"B": "2", "A": "1"},
    )
    configuration = desired_configuration(
        region=REGION, settings=settings, artifact_uri=URI, build_role_arn=ROLE
    )
    properties = cfn_image_properties(configuration, name="team", description="d")
    assert properties["Hooks"] == pascal_case(IMAGE_HOOKS)
    assert properties["Hooks"]["MicrovmImageHooks"]["ReadyTimeoutInSeconds"] == 600
    assert properties["Resources"] == [{"MinimumMemoryInMiB": 2048}]
    assert properties["CpuConfigurations"] == [{"Architecture": "ARM_64"}]
    assert properties["Logging"] == {"CloudWatch": {"LogGroup": "/rayito/team"}}
    assert properties["CodeArtifact"] == {"Uri": URI}
    assert properties["AdditionalOsCapabilities"] == ["ALL"]
    assert properties["EgressNetworkConnectors"] == []
    assert properties["EnvironmentVariables"] == [
        {"Key": "A", "Value": "1"},
        {"Key": "B", "Value": "2"},
    ]
    assert camel_case(pascal_case(IMAGE_HOOKS)) == IMAGE_HOOKS


def test_without_capabilities_or_variables_the_lists_are_empty() -> None:
    settings = ImageBuildSettings(image_name="team", base_image_version="1", memory_mib=2048)
    configuration = desired_configuration(
        region=REGION, settings=settings, artifact_uri=URI, build_role_arn=ROLE
    )
    properties = cfn_image_properties(configuration, name="team", description="d")
    assert properties["AdditionalOsCapabilities"] == []
    assert properties["EnvironmentVariables"] == []
    assert len(properties) == 13


# --------------------------------------------------------------- síntesis


def test_each_image_carries_the_publish_configuration(tmp_path: Path) -> None:
    stack = new_stack()
    construct = image(
        stack,
        tmp_path,
        name="team-caps",
        os_capabilities=True,
        sizes=["512mb", "4gb"],
        allowed_mount_buckets=["amzn-s3-demo-bucket"],
        environment={"TEAM": "demo"},
    )
    images = {
        value["Properties"]["Name"]: value["Properties"]
        for value in resources(stack, "AWS::Lambda::MicrovmImage").values()
    }
    assert set(images) == {"team-caps", "team-caps-512mb", "team-caps-4gb"}
    base = ImageBuildSettings(
        image_name="team-caps",
        base_image_version="1",
        memory_mib=2048,
        os_capabilities="ALL",
        environment_variables={
            "TEAM": "demo",
            "RAYITO_ALLOWED_MOUNT_BUCKETS": "amzn-s3-demo-bucket",
        },
    )
    for settings in (base, sized_settings(base, "512mb"), sized_settings(base, "4gb")):
        expected = cfn_image_properties(
            desired_configuration(
                region=REGION, settings=settings, artifact_uri=URI, build_role_arn=ROLE
            ),
            name=settings.image_name,
            description=f"Rayito image {settings.image_name} (rayito.microvm-image/1+cdk)",
        )
        found = images[settings.image_name]
        for key in (
            "Hooks",
            "Resources",
            "CpuConfigurations",
            "AdditionalOsCapabilities",
            "Logging",
            "EnvironmentVariables",
            "Description",
            "BaseImageArn",
            "BaseImageVersion",
            "EgressNetworkConnectors",
        ):
            assert found[key] == expected[key], key
    assert set(construct.size_image_arns) == {"512mb", "4gb"}
    assert construct.image_name == "team-caps"
    assert construct.log_group_name == "/rayito/team-caps"


def test_the_build_role_reads_only_the_asset_and_writes_only_its_log_groups(
    tmp_path: Path,
) -> None:
    stack = new_stack()
    image(stack, tmp_path, sizes=["4gb"])
    (role,) = resources(stack, "AWS::IAM::Role").values()
    trust = role["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]
    assert trust["Principal"] == {"Service": "lambda.amazonaws.com"}
    assert trust["Action"] == ["sts:AssumeRole", "sts:TagSession"]
    assert trust["Condition"] == {"StringEquals": {"aws:SourceAccount": ACCOUNT}}
    (policy,) = role["Properties"]["Policies"]
    read, write = policy["PolicyDocument"]["Statement"]
    assert read["Action"] == "s3:GetObject"
    assert len(write["Resource"]) == 2
    assert len(resources(stack, "AWS::Logs::LogGroup")) == 2


def test_the_artifact_is_one_asset_for_every_size(tmp_path: Path) -> None:
    stack = new_stack()
    image(stack, tmp_path, sizes=["512mb", "4gb"])
    uris = {
        value["Properties"]["CodeArtifact"]["Uri"]
        for value in resources(stack, "AWS::Lambda::MicrovmImage").values()
    }
    assert len(uris) == 1


# ---------------------------------------------------------------- errores


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"name": "rayito-base-team"}, "image_name_reserved"),
        ({"name": "bad name"}, "image_name_invalid"),
        ({"name": "x" * 60, "sizes": ["512mb"]}, "image_name_invalid"),
        ({"base_image_version": "latest"}, "option_invalid"),
        ({"base_image_version": ""}, "option_invalid"),
        ({"variant": "tiny"}, "option_invalid"),
        ({"with_efs": True}, "option_invalid"),
        ({"sizes": ["2gb"]}, "option_invalid"),
        ({"sizes": ["9gb"]}, "option_invalid"),
        ({"sizes": ["512mb", "512mb"]}, "option_invalid"),
        ({"sizes": ["512mb"], "memory_mib": 1024}, "option_invalid"),
        ({"memory_mib": 0}, "option_invalid"),
        ({"environment": {"bad key": "x"}}, "option_invalid"),
        ({"environment": {"RAYITO_BASELINE_MEMORY_MIB": "1"}}, "option_invalid"),
        ({"allowed_mount_buckets": ["Not_A_Bucket"]}, "option_invalid"),
        ({"tags": {"bad": "{}"}}, "option_invalid"),
        ({"variant": "slim"}, "artifact_variant_mismatch"),
        ({"with_efs": True, "os_capabilities": True}, "artifact_efs_mismatch"),
    ],
)
def test_invalid_props_fail_before_any_child(
    tmp_path: Path, overrides: dict[str, Any], code: str
) -> None:
    stack = new_stack()
    with pytest.raises(RayitoCdkException) as raised:
        image(stack, tmp_path, **overrides)
    assert raised.value.code == code
    assert stack.node.try_find_child("Image") is None


def test_an_environment_agnostic_stack_and_a_bad_account_are_refused(tmp_path: Path) -> None:
    app = cdk.App()
    with pytest.raises(RayitoCdkException) as raised:
        image(cdk.Stack(app, "NoEnv"), tmp_path)
    assert raised.value.code == "region_unresolved"
    with pytest.raises(RayitoCdkException) as raised:
        image(new_stack(cdk.Environment(account="1", region=REGION)), tmp_path)
    assert raised.value.code == "account_invalid"


def test_an_account_token_is_accepted(tmp_path: Path) -> None:
    stack = new_stack(cdk.Environment(region=REGION))
    image(stack, tmp_path)
    (role,) = resources(stack, "AWS::IAM::Role").values()
    condition = role["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]["Condition"]
    assert condition == {"StringEquals": {"aws:SourceAccount": {"Ref": "AWS::AccountId"}}}


def test_from_file_checks_existence_and_checksum(tmp_path: Path) -> None:
    with pytest.raises(RayitoCdkException) as raised:
        MicrovmImageArtifact.from_file(tmp_path / "missing.zip")
    assert raised.value.code == "artifact_not_found"
    path = fixture_zip(tmp_path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert MicrovmImageArtifact.from_file(path, sha256=digest.upper()).sha256 == digest
    with pytest.raises(RayitoCdkException) as raised:
        MicrovmImageArtifact.from_file(path, sha256="0" * 64)
    assert raised.value.code == "artifact_checksum_mismatch"


# ------------------------------------------------------- download_release


class FakeSource:
    """Una release en memoria: cuenta las descargas."""

    def __init__(self, payload: bytes, listed_sha256: str | None = None) -> None:
        self.payload = payload
        self.listed = listed_sha256 or hashlib.sha256(payload).hexdigest()
        self.fetches = 0

    def fetch(self, tag: str, directory: Path, on_progress: Any) -> DownloadedRelease:
        self.fetches += 1
        artifact = directory / IMAGE_ASSET_NAME
        artifact.write_bytes(self.payload)
        return DownloadedRelease(
            checksums_text=f"{self.listed}  {IMAGE_ASSET_NAME}\n",
            artifact=artifact,
            artifact_sha256=hashlib.sha256(self.payload).hexdigest(),
        )


def test_download_release_verifies_and_then_works_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = FakeSource(fixture_zip(tmp_path).read_bytes())
    monkeypatch.setattr(image_module, "release_source_factory", lambda: source)
    cache = tmp_path / "cache"
    first = MicrovmImageArtifact.download_release(version="0.15.0", directory=cache)
    second = MicrovmImageArtifact.download_release(version="0.15.0", directory=cache)
    assert source.fetches == 1
    assert first.sha256 == second.sha256
    assert (cache / CHECKSUMS_ASSET_NAME).is_file()
    (cache / IMAGE_ASSET_NAME).write_bytes(b"corrupt")
    with pytest.raises(RayitoCdkException) as raised:
        MicrovmImageArtifact.download_release(version="0.15.0", directory=cache)
    assert raised.value.code == "artifact_checksum_mismatch"


def test_download_release_refuses_a_mismatched_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = FakeSource(b"zip", listed_sha256="0" * 64)
    monkeypatch.setattr(image_module, "release_source_factory", lambda: source)
    with pytest.raises(RayitoCdkException) as raised:
        MicrovmImageArtifact.download_release(directory=tmp_path / "cache")
    assert raised.value.code == "artifact_checksum_mismatch"
    assert not (tmp_path / "cache" / IMAGE_ASSET_NAME).exists()
    assert not (tmp_path / "cache" / CHECKSUMS_ASSET_NAME).exists()


def test_download_release_maps_a_download_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Failing:
        def fetch(self, tag: str, directory: Path, on_progress: Any) -> DownloadedRelease:
            raise AccountSetupException(
                "release_download", asset=IMAGE_ASSET_NAME, release=tag, reason="HTTP 404"
            )

    monkeypatch.setattr(image_module, "release_source_factory", Failing)
    with pytest.raises(RayitoCdkException) as raised:
        MicrovmImageArtifact.download_release(version="0.15.0", directory=tmp_path)
    assert raised.value.code == "release_download_failed"
    assert raised.value.fields == {
        "asset": IMAGE_ASSET_NAME,
        "release": "rayd-v0.15.0",
        "reason": "HTTP 404",
    }
