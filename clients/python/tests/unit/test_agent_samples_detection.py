"""La detección (`ai-agent-detection`) sobre los dieciséis agentes de ejemplo de
`testdata/agent-samples/`: cada uno se decide sin ninguna opción, igual que lo
dice el manifiesto que comparten los dos SDK. Sin AWS ni red: sólo se leen
ficheros."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import pytest

from rayito import AgentProject
from rayito._agent._detect_domain import detect
from rayito._agent._detect_fs import LocalProjectTree
from rayito._agent._project_domain import AgentProjectException

ROOT: Final = Path(__file__).parents[4] / "testdata" / "agent-samples"
MANIFEST: Final = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
SAMPLES: Final[list[dict[str, Any]]] = MANIFEST["samples"]
HTTP_ALTERNATIVE_WARNING: Final = "http_app_alternative"


def _ids() -> list[str]:
    return [sample["id"] for sample in SAMPLES]


def _report(sample: dict[str, Any]) -> Any:
    return detect(LocalProjectTree(ROOT / sample["dir"]))


@pytest.mark.parametrize("sample", SAMPLES, ids=_ids())
def test_detection_decides_the_sample_without_options(sample: dict[str, Any]) -> None:
    expected = sample["expected"]
    report = _report(sample)
    assert report.framework == expected["framework"]
    assert report.kind == expected["kind"]
    assert report.protocol == expected["protocol"]
    assert report.entrypoint == expected["entry"]
    manager = None if report.install is None else report.install.manager
    assert manager == expected["installManager"]


@pytest.mark.parametrize("sample", SAMPLES, ids=_ids())
def test_detection_language_is_the_sample_language(sample: dict[str, Any]) -> None:
    report = _report(sample)
    if sample["lockfile"] is None:
        assert report.language is None
    else:
        assert report.language == sample["language"]


@pytest.mark.parametrize(
    "sample", [s for s in SAMPLES if "httpAlternative" in s], ids=lambda s: s["id"]
)
def test_a_framework_object_wins_and_the_app_is_listed_as_an_alternative(
    sample: dict[str, Any],
) -> None:
    report = _report(sample)
    assert report.app is None
    assert HTTP_ALTERNATIVE_WARNING in {warning.code for warning in report.warnings}
    assert sample["httpAlternative"]["framework"] in {a.framework for a in report.alternatives}


@pytest.mark.parametrize("sample", [s for s in SAMPLES if "app" in s], ids=lambda s: s["id"])
def test_a_custom_http_sample_reports_its_routes(sample: dict[str, Any]) -> None:
    report = _report(sample)
    assert report.app is not None
    assert report.app.health_path == sample["app"]["health"]
    assert report.app.invoke_path == sample["app"]["invoke"]
    assert report.app.request_field == sample["app"]["requestField"]
    assert report.app.response_field == sample["app"]["responseField"]


@pytest.mark.parametrize("sample", SAMPLES, ids=_ids())
def test_the_report_holds_no_file_content_or_secret(sample: dict[str, Any]) -> None:
    report = _report(sample)
    text = json.dumps(
        [
            report.evidence and [e.to_dict() for e in report.evidence],
            report.providers and [p.to_dict() for p in report.providers],
        ]
    )
    assert "MODEL_ID=" not in text
    assert "rayito" not in text.lower()


REGION: Final = "us-east-1"
EXPECTED: Final = ROOT / "expected"


@pytest.mark.parametrize("sample", SAMPLES, ids=_ids())
def test_the_detected_project_is_the_shared_document(sample: dict[str, Any]) -> None:
    project, _ = AgentProject.detect(ROOT / sample["dir"], region=REGION)
    expected = json.loads((EXPECTED / f"{sample['id']}.json").read_text(encoding="utf-8"))
    assert project.to_dict() == expected


def test_a_project_without_a_model_id_names_the_key_that_fixes_it(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("strands-agents==1.0.0\n", encoding="utf-8")
    (tmp_path / "agent.py").write_text(
        "from strands import Agent\nagent = Agent()\n", encoding="utf-8"
    )
    with pytest.raises(AgentProjectException) as raised:
        AgentProject.detect(tmp_path, region=REGION)
    assert raised.value.code == "not_detected"
    assert raised.value.path == "model.id"


def test_the_region_comes_from_the_argument_or_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AWS_REGION", raising=False)
    sample = ROOT / "python" / "strands"
    with pytest.raises(AgentProjectException) as raised:
        AgentProject.detect(sample)
    assert raised.value.path == "deploy.region"
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    project, _ = AgentProject.detect(sample)
    assert project.spec.model.region == "eu-west-1"
