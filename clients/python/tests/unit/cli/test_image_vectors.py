"""Los contratos puros de `rayito image publish`, `prune` y `zip` de
`testdata/images/*.json`: la CLI de TypeScript reproduce estos valores
(ADR-027), y este test fija que los de Python no cambian sin que se
regeneren a propósito."""

from __future__ import annotations

import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest
import typer

from rayito._images import DEFAULT_MEMORY_MIB, VersionGate
from rayito._limits import MAX_CONCURRENT_IMAGE_BUILDS
from rayito.cli import _artifact, _prune, _publish
from rayito.cli import image as image_cli

REPO_ROOT_DEPTH: Final = 5
TESTDATA: Final = Path(__file__).resolve().parents[REPO_ROOT_DEPTH] / "testdata" / "images"


def load(name: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((TESTDATA / name).read_text(encoding="utf-8"))
    return document


PUBLISH: Final = load("publish-vectors.json")
PRUNE: Final = load("prune-vectors.json")
ARTIFACTS: Final = load("artifact-cases.json")


class FakeClients:
    region = "us-east-1"
    account_id = "123456789012"
    microvms: Any = None
    s3: Any = None
    cloudformation: Any = None
    logs: Any = None


def settings(**overrides: object) -> _publish.PublishSettings:
    base: dict[str, object] = {
        "artifact": Path("image/rayito-image.zip"),
        "image_name": "rayito-base",
        "variant": "full",
        "bucket": "bucket",
        "stack_name": "stack",
        "build_role_arn": "arn:aws:iam::123456789012:role/build",
        "base_image_version": "1",
        "memory_mib": 2048,
        "force": False,
        "timeout_seconds": 10.0,
    }
    base.update(overrides)
    return _publish.PublishSettings(**base)  # type: ignore[arg-type]


def message_of(exc: Exception) -> str:
    return str(getattr(exc, "message", None) or exc)


@pytest.mark.parametrize(
    "case", PUBLISH["defaultNames"], ids=lambda c: f"{c['variant']}-{c['withEfs']}"
)
def test_default_names(case: dict[str, Any]) -> None:
    if "error" in case:
        with pytest.raises(ValueError) as raised:
            _publish.default_image_name(case["variant"], with_efs=case["withEfs"])
        assert message_of(raised.value) == case["error"]
    else:
        assert (
            _publish.default_image_name(case["variant"], with_efs=case["withEfs"]) == case["value"]
        )


@pytest.mark.parametrize(
    "case", PUBLISH["efsCombinations"], ids=lambda c: f"{c['variant']}-{c['osCapabilities']}"
)
def test_efs_combinations(case: dict[str, Any]) -> None:
    error = case["error"]
    if error is None:
        _publish.require_efs_combination(case["variant"], case["osCapabilities"])
    else:
        with pytest.raises(ValueError) as raised:
            _publish.require_efs_combination(case["variant"], case["osCapabilities"])
        assert message_of(raised.value) == error


@pytest.mark.parametrize("case", PUBLISH["runIds"], ids=lambda c: repr(c["runId"])[:20])
def test_run_ids(case: dict[str, Any]) -> None:
    if "error" in case:
        with pytest.raises(ValueError) as raised:
            _publish.validate_run_id(case["runId"])
        assert message_of(raised.value) == case["error"]
    else:
        assert _publish.validate_run_id(case["runId"]) == case["runId"]


@pytest.mark.parametrize(
    "case", PUBLISH["artifactKeys"], ids=lambda c: f"{c['payload']}-{c['runId']}"
)
def test_artifact_keys(case: dict[str, Any]) -> None:
    if "error" in case:
        with pytest.raises(ValueError):
            _publish.artifact_key(case["payload"].encode(), case["runId"])
    else:
        assert _publish.artifact_key(case["payload"].encode(), case["runId"]) == case["value"]


@pytest.mark.parametrize("case", PUBLISH["configurations"], ids=lambda c: c["name"])
def test_desired_configuration(case: dict[str, Any]) -> None:
    built = settings(**case["settings"])
    assert (
        _publish.desired_configuration(FakeClients(), built, case["artifactUri"])
        == case["expected"]
    )


@pytest.mark.parametrize("case", PUBLISH["logGroups"], ids=lambda c: c["imageName"])
def test_log_groups(case: dict[str, Any]) -> None:
    assert settings(image_name=case["imageName"]).log_group == case["expected"]


@pytest.mark.parametrize("case", PUBLISH["sized"], ids=lambda c: c["size"])
def test_sized_settings(case: dict[str, Any]) -> None:
    sized = _publish.sized_settings(
        settings(environment_variables=case["base"]["environment_variables"]), case["size"]
    )
    assert sized.image_name == case["imageName"]
    assert sized.memory_mib == case["memoryMib"]
    assert dict(sized.environment_variables) == case["environmentVariables"]


@pytest.mark.parametrize("case", PUBLISH["sizeEnvVars"], ids=lambda c: c["size"])
def test_size_env_vars(case: dict[str, Any]) -> None:
    assert _publish.size_env_var(case["size"]) == case["expected"]


@pytest.mark.parametrize("case", PUBLISH["summaries"], ids=lambda c: c["name"])
def test_summaries(case: dict[str, Any]) -> None:
    artifact = _publish.UploadedArtifact("s3://bucket/rayito/images/rayd-abc.zip", case["uploaded"])
    gate = (
        None
        if case["gate"] is None
        else VersionGate(
            case["gate"]["imageState"],
            case["gate"]["versionState"],
            case["gate"]["versionStatus"],
            case["gate"]["stateReason"],
        )
    )
    got = _publish.publish_summary(
        "arn:x", "3.0", artifact, case["buildSeconds"], case["build"], gate
    )
    assert got == case["expected"]


@pytest.mark.parametrize("case", PUBLISH["aggregates"], ids=lambda c: c["name"])
def test_aggregates(case: dict[str, Any]) -> None:
    assert _publish.aggregate_summary(case["baseline"], case["sized"]) == case["expected"]


@pytest.mark.parametrize("case", PUBLISH["sizes"], ids=lambda c: repr(c["raw"]))
def test_sizes(case: dict[str, Any]) -> None:
    if "error" in case:
        with pytest.raises(typer.BadParameter) as raised:
            image_cli.validate_sizes(case["raw"])
        assert message_of(raised.value) == case["error"]
        assert raised.value.param_hint == case["hint"]
    else:
        assert list(image_cli.validate_sizes(case["raw"])) == case["value"]


@pytest.mark.parametrize(
    "case", PUBLISH["baselineMemory"], ids=lambda c: f"{c['memoryMib']}-{c['sizes']}"
)
def test_baseline_memory(case: dict[str, Any]) -> None:
    sizes = tuple(case["sizes"])
    if "error" in case:
        with pytest.raises(typer.BadParameter) as raised:
            image_cli.validate_baseline_memory_mib(case["memoryMib"], sizes)
        assert message_of(raised.value) == case["error"]
    else:
        image_cli.validate_baseline_memory_mib(case["memoryMib"], sizes)


@pytest.mark.parametrize("case", PUBLISH["environment"], ids=lambda c: repr(c["raw"]))
def test_environment_assignments(case: dict[str, Any]) -> None:
    if "error" in case:
        with pytest.raises(typer.BadParameter) as raised:
            image_cli.parse_environment_assignments(case["raw"])
        assert message_of(raised.value) == case["error"]
    else:
        assert image_cli.parse_environment_assignments(case["raw"]) == case["value"]


def test_publish_constants() -> None:
    constants = PUBLISH["constants"]
    assert constants["defaultStackName"] == _publish.DEFAULT_STACK_NAME
    assert constants["buildRoleOutputKey"] == _publish.BUILD_ROLE_OUTPUT_KEY
    assert constants["baselineMemoryEnvVar"] == _publish.BASELINE_MEMORY_ENV_VAR
    assert constants["maxConcurrentImageBuilds"] == MAX_CONCURRENT_IMAGE_BUILDS
    assert constants["hooks"] == _publish.IMAGE_HOOKS
    assert constants["efsImageName"] == _publish.EFS_IMAGE_NAME
    assert constants["hooksPort"] == _publish.HOOKS_PORT
    assert constants["runScopedKeySegment"] == _publish.RUN_SCOPED_KEY_SEGMENT
    assert constants["imageArtifactKeyPrefix"] == _publish.S3_KEY_PREFIX
    assert constants["defaultMemoryMib"] == DEFAULT_MEMORY_MIB
    assert list(_artifact.VARIANTS) == PUBLISH["variants"]
    assert list(_publish.OS_CAPABILITY_CHOICES) == PUBLISH["osCapabilities"]


def version_of(raw: dict[str, str]) -> _prune.Version:
    return _prune.Version(
        image_version=raw["imageVersion"],
        state=raw["state"],
        status=raw["status"],
        created_at=datetime.fromisoformat(raw["createdAt"].replace("Z", "+00:00")).astimezone(UTC),
    )


@pytest.mark.parametrize("case", PRUNE["plans"], ids=lambda c: c["name"])
def test_prune_plans(case: dict[str, Any]) -> None:
    plan = _prune.build_plan(
        [version_of(v) for v in case["versions"]], set(case["live"]), case["keep"]
    )
    assert [[v.image_version, why] for v, why in plan.keep] == case["kept"]
    assert [v.image_version for v in plan.delete] == case["delete"]
    assert [list(row) for row in plan.rows()] == case["rows"]
    assert _prune.format_table(plan) == case["table"]


def test_prune_constants() -> None:
    constants = PRUNE["constants"]
    assert constants["defaultKeep"] == _prune.DEFAULT_KEEP
    assert constants["defaultWaitTimeoutSeconds"] == _prune.DEFAULT_WAIT_TIMEOUT_SECONDS
    assert constants["pollIntervalSeconds"] == _prune.POLL_INTERVAL_SECONDS
    assert list(_prune.RETRY_BACKOFF_SECONDS) == constants["retryBackoffSeconds"]
    assert sorted(_prune.IN_FLIGHT_STATES) == constants["inFlightStates"]
    assert sorted(_prune.BUSY_IMAGE_STATES) == constants["busyImageStates"]
    assert sorted(_prune.LIVE_MICROVM_EXCLUDED_STATES) == constants["liveMicrovmExcludedStates"]
    assert list(_prune.TABLE_HEADER) == constants["tableHeader"]


def build_tree(tree: dict[str, str], root: Path) -> None:
    for relative, text in tree.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


@pytest.mark.parametrize("case", ARTIFACTS["cases"], ids=lambda c: c["name"])
def test_artifact_cases(case: dict[str, Any], tmp_path: Path) -> None:
    tree = ARTIFACTS["tree"]
    if case["tree"] != "default":
        tree = {name: tree[name] for name in case["tree"]}
    root = tmp_path / "image"
    root.mkdir()
    build_tree(tree, root)
    destination = tmp_path / "out.zip"
    if "errorKind" in case:
        with pytest.raises(SystemExit):
            _artifact.write_zip(root, destination, case["variant"], with_efs=case["withEfs"])
        return
    count = _artifact.write_zip(root, destination, case["variant"], with_efs=case["withEfs"])
    assert count == case["count"]
    with zipfile.ZipFile(destination) as archive:
        entries = [
            {
                "name": info.filename,
                "mode": info.external_attr >> 16,
                "content": archive.read(info.filename).decode("utf-8"),
            }
            for info in archive.infolist()
        ]
    assert entries == case["entries"]
    assert _artifact.marker_variant(destination) == case["markerVariant"]
    assert _artifact.marker_has_efs(destination) == case["hasEfs"]


def test_artifact_constants() -> None:
    constants = ARTIFACTS["constants"]
    assert list(_artifact.VARIANTS) == constants["variants"]
    assert list(_artifact.FIXED_DATE_TIME) == constants["fixedDateTime"]
    assert constants["warmupMarker"] == _artifact.WARMUP_MARKER_ENTRY
    assert constants["kernelsMarker"] == _artifact.KERNELS_MARKER_ENTRY
    assert constants["efsMarker"] == _artifact.EFS_MARKER_ENTRY
    assert constants["slimContent"] == _artifact.SLIM_MARKER_CONTENT
    assert constants["polyContent"] == _artifact.POLY_MARKER_CONTENT
    assert constants["efsContent"] == _artifact.EFS_MARKER_CONTENT
    assert list(_artifact.EXCLUDED_SUFFIXES) == constants["excludedSuffixes"]
    assert sorted(_artifact.EXCLUDED_DIRECTORIES) == constants["excludedDirectories"]
    assert sorted(_artifact.EXCLUDED_FILES) == constants["excludedFiles"]
    assert constants["rayd"] == _artifact.RAYD_ENTRY
    assert constants["noticesDirectory"] == _artifact.NOTICES_DIRECTORY
    assert list(_artifact.RAYD_NOTICE_FILES) == constants["noticeFiles"]
    assert list(_artifact.REQUIREMENTS_FILES) == constants["requirementsFiles"]
