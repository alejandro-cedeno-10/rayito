"""Vectores de formato y de entrada de la CLI compartidos con TypeScript
(`testdata/cli/format-vectors.json` e `input-vectors.json`, ADR-027): la CLI
de TypeScript reproduce estos valores, y este test fija que los de Python no
cambian sin que se regeneren a propósito."""

from __future__ import annotations

import contextlib
import io
import json
import shlex
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final

import pytest

from rayito._cloudwatch_logs import epoch_ms, event_timestamp, parse_since
from rayito._models import IdlePolicy, SandboxInfo, SandboxLifecycle, SandboxMetrics
from rayito.cli import _console as console
from rayito.cli._console import age, emit_json, iso_utc, python_text, table, visible_controls
from rayito.cli._tokens import InvalidPairError, parse_pairs
from rayito.cli.sandbox import info_document, launch_document, metrics_document

REPO_ROOT_DEPTH: Final = 5
CLI_TESTDATA: Final = Path(__file__).resolve().parents[REPO_ROOT_DEPTH] / "testdata" / "cli"


def load(name: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((CLI_TESTDATA / name).read_text(encoding="utf-8"))
    return document


def instant(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


FORMAT = load("format-vectors.json")
INPUT = load("input-vectors.json")


@pytest.mark.parametrize("case", FORMAT["age"])
def test_age(case: dict[str, Any]) -> None:
    assert age(instant(case["startedAt"]), instant(case["now"])) == case["expected"]


@pytest.mark.parametrize("case", FORMAT["isoUtc"])
def test_iso_utc(case: dict[str, Any]) -> None:
    assert iso_utc(instant(case["instant"])) == case["expected"]


@pytest.mark.parametrize("case", FORMAT["pythonDatetimeText"])
def test_datetime_text(case: dict[str, Any]) -> None:
    assert python_text(instant(case["instant"])) == case["expected"]


@pytest.mark.parametrize("case", FORMAT["jsonDocuments"], ids=lambda case: case["name"])
def test_json_documents(case: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    floats = set(case["floatKeys"])

    def as_python(value: Any, key: str = "") -> Any:
        if isinstance(value, dict):
            return {name: as_python(item, name) for name, item in value.items()}
        if isinstance(value, list):
            return [as_python(item, key) for item in value]
        is_number = isinstance(value, int | float) and not isinstance(value, bool)
        return float(value) if key in floats and is_number else value

    emit_json(as_python(case["document"]))
    assert capsys.readouterr().out.rstrip("\n") == case["expected"]


@pytest.mark.parametrize("case", FORMAT["eventTimestamp"])
def test_event_timestamp(case: dict[str, Any]) -> None:
    assert event_timestamp({"timestamp": case["epochMs"]}) == case["expected"]


@pytest.mark.parametrize("case", FORMAT["pythonFloatText"])
def test_float_text(case: dict[str, Any]) -> None:
    assert str(float(case["value"])) == case["expected"]


@pytest.mark.parametrize("case", FORMAT["pythonStringRepr"])
def test_string_repr(case: dict[str, Any]) -> None:
    assert repr(case["text"]) == case["expected"]


@pytest.mark.parametrize("case", FORMAT["visibleControls"])
def test_visible_controls(case: dict[str, Any]) -> None:
    assert visible_controls(case["text"]) == case["expected"]


@pytest.mark.parametrize("case", FORMAT["table"])
def test_table(case: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(console, "table_width", lambda: case["width"])
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        table(case["columns"], case["rows"])
    assert buffer.getvalue().rstrip("\n") == case["expected"]


@pytest.mark.parametrize("case", INPUT["shellJoin"])
def test_shell_join(case: dict[str, Any]) -> None:
    assert shlex.join(case["words"]) == case["expected"]


@pytest.mark.parametrize("case", INPUT["pairs"])
def test_pairs(case: dict[str, Any]) -> None:
    assert parse_pairs(case["values"], option="--env") == case["expected"]


@pytest.mark.parametrize("case", INPUT["pairErrors"])
def test_pair_errors(case: dict[str, Any]) -> None:
    with pytest.raises(InvalidPairError) as caught:
        parse_pairs(case["values"], option=case["option"])
    assert str(caught.value) == case["message"]


@pytest.mark.parametrize("case", INPUT["since"])
def test_since(case: dict[str, Any]) -> None:
    assert epoch_ms(parse_since(case["text"], instant(case["now"]))) == case["expectedMs"]


@pytest.mark.parametrize("case", INPUT["sinceErrors"])
def test_since_errors(case: dict[str, Any]) -> None:
    with pytest.raises(ValueError) as caught:
        parse_since(case["text"], instant(case["now"]))
    assert str(caught.value) == case["message"]


def build_info(raw: dict[str, Any]) -> SandboxInfo:
    fields = dict(raw)
    for key in ("started_at", "terminated_at"):
        if fields.get(key) is not None:
            fields[key] = instant(fields[key])
    if fields.get("idle") is not None:
        fields["idle"] = IdlePolicy(**fields["idle"])
    if fields.get("lifecycle") is not None:
        lifecycle = dict(fields["lifecycle"])
        for key in ("deadline", "cap"):
            if lifecycle.get(key) is not None:
                lifecycle[key] = instant(lifecycle[key])
        fields["lifecycle"] = SandboxLifecycle(**lifecycle)
    for key in ("ingress", "egress"):
        if key in fields:
            fields[key] = tuple(fields[key])
    return SandboxInfo(**fields)


DOCUMENTS = load("document-vectors.json")


@pytest.mark.parametrize("case", DOCUMENTS["info"], ids=lambda case: case["name"])
def test_info_document(case: dict[str, Any]) -> None:
    document = info_document(build_info(case["info"]))
    assert json.loads(json.dumps(document, default=str)) == case["expected"]


@pytest.mark.parametrize("case", DOCUMENTS["info"], ids=lambda case: case["name"])
def test_launch_document(case: dict[str, Any]) -> None:
    sandbox: Any = SimpleNamespace(info=build_info(case["info"]))
    assert launch_document(sandbox) == case["launch"]


@pytest.mark.parametrize("case", DOCUMENTS["metrics"], ids=lambda case: case["name"])
def test_metrics_document(case: dict[str, Any]) -> None:
    raw = case["metrics"]
    metrics = SandboxMetrics(**{**raw, "timestamp": instant(raw["timestamp"])})
    assert metrics_document(metrics) == case["expected"]
