"""`scripts/gen_cdk_vectors.py`: la normalización de referencia de
`testdata/agentcore-cdk/contract.json` y la forma de los casos."""

from __future__ import annotations

import importlib.util
import json
import sys
import zipfile
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "gen_cdk_vectors.py"
CASE_FIELDS = {"construct", "stack", "id", "props", "expected"}
CONSTRUCTS = {"RayitoAgent", "RayitoMicrovmImage"}
HASH = "a" * 64


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_cdk_vectors", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_normalise_template_applies_every_rule() -> None:
    script = load_script()
    template = {
        "Parameters": {"BootstrapVersion": {"Type": "String"}},
        "Rules": {"CheckBootstrapVersion": {}},
        "Conditions": {"CDKMetadataAvailable": {}, "Keep": {}},
        "Resources": {
            "CDKMetadata": {"Type": "AWS::CDK::Metadata"},
            "Image": {
                "Type": "AWS::Lambda::MicrovmImage",
                "Metadata": {"aws:cdk:path": "Stack/Image"},
                "Properties": {
                    "CodeArtifact": {
                        "Uri": f"s3://cdk-{script.BOOTSTRAP_QUALIFIER}-assets-123456789012-us-east-1/{HASH}.zip"
                    },
                    "Tags": [
                        {"Key": "rayito:sdk-version", "Value": "0.15.0"},
                        {"Key": "team", "Value": "demo"},
                    ],
                },
            },
            "Runtime": {"Properties": {"Tags": {"rayito:sdk-version": "0.15.0"}}},
        },
    }
    normalised = script.normalise_template(template)
    assert set(normalised) == {"Conditions", "Resources"}
    assert normalised["Conditions"] == {"Keep": {}}
    image = normalised["Resources"]["Image"]
    assert "Metadata" not in image
    assert image["Properties"]["CodeArtifact"]["Uri"] == (
        "s3://cdk-<QUALIFIER>-assets-123456789012-us-east-1/<ASSET_HASH>.zip"
    )
    assert image["Properties"]["Tags"][0] == {"Key": "rayito:sdk-version", "Value": "<SDK_VERSION>"}
    assert normalised["Resources"]["Runtime"]["Properties"]["Tags"] == {
        "rayito:sdk-version": "<SDK_VERSION>"
    }
    assert template["Resources"]["Image"]["Metadata"]


def test_cases_are_well_formed() -> None:
    script = load_script()
    cases = script.load_cases()
    assert cases
    contract = json.loads(script.CONTRACT_FILE.read_text(encoding="utf-8"))
    prop_names = contract["sdk"]["propNames"]
    for name, case in cases.items():
        assert CASE_FIELDS <= set(case), name
        assert case["construct"] in CONSTRUCTS, name
        assert set(case["props"]) <= set(prop_names), name
        if case["construct"] == "RayitoAgent":
            assert (script.TESTDATA_DIR / case["project"]).is_dir(), name
        else:
            assert (script.CONTRACT_DIR / case["artifact"]["fixture"]).is_dir(), name
        expected = case["expected"]
        if expected != script.EXPECTED_TEMPLATE:
            code = expected["error"]["code"]
            assert code in contract["errors"]["messages"] or expected["error"]["exception"] == (
                "AgentCoreException"
            ), name


def test_fixture_zip_is_deterministic(tmp_path: Path) -> None:
    script = load_script()
    fixture = script.CONTRACT_DIR / "fixtures" / "image-slim"
    first = script.write_fixture_zip(fixture, tmp_path / "a.zip").read_bytes()
    second = script.write_fixture_zip(fixture, tmp_path / "b.zip").read_bytes()
    assert first == second
    with zipfile.ZipFile(tmp_path / "a.zip") as archive:
        assert archive.read("kernel-sidecar/ipython/startup/warmup_variant") == b"slim\n"


def test_pending_without_harness(capsys: object) -> None:
    script = load_script()
    if script.python_synthesizer() is None:
        assert script.main(["--check"]) == 0
