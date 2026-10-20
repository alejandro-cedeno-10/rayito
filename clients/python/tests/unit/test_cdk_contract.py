"""Los casos de `testdata/agentcore-cdk/` (ADR-032 D12.2) con el constructo
de Python: cada caso `"template"` sintetiza, normalizado, exactamente su
`expected.template.json`, y cada caso de error lanza su excepción con sus
campos sin dejar nada en el árbol. La síntesis no llama a AWS."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import boto3
import pytest

pytest.importorskip("aws_cdk")

from rayito.cdk import RayitoCdkException
from rayito.cdk._errors import ERROR_MESSAGES
from rayito.exceptions import AgentCoreException
from tests.cdk_contract_harness import (
    CONTRACT,
    CONTRACT_DIR,
    build_case,
    generator,
    new_stack,
    synthesize_case,
)

CASES_DIR = CONTRACT_DIR / "cases"
CASES = {path.parent.name: path for path in sorted(CASES_DIR.glob("*/input.json"))}
EXCEPTIONS: dict[str, type[Exception]] = {
    "RayitoCdkException": RayitoCdkException,
    "AgentCoreException": AgentCoreException,
}


def load(name: str) -> dict[str, Any]:
    case: dict[str, Any] = json.loads(CASES[name].read_text(encoding="utf-8"))
    return case


def template_cases() -> list[str]:
    return [name for name in CASES if load(name)["expected"] == "template"]


def error_cases() -> list[str]:
    return [name for name in CASES if load(name)["expected"] != "template"]


@pytest.fixture
def no_aws(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    created: list[str] = []

    def refuse(_self: Any, service: str, *_args: Any, **_kwargs: Any) -> Any:
        created.append(service)
        raise AssertionError(f"la síntesis creó un cliente de {service}")

    monkeypatch.setattr(boto3.session.Session, "client", refuse)
    for variable in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_PROFILE"):
        monkeypatch.delenv(variable, raising=False)
    return created


def test_every_case_is_covered() -> None:
    assert len(CASES) == len(template_cases()) + len(error_cases()) > 0


def test_the_error_messages_are_the_contracts() -> None:
    assert dict(ERROR_MESSAGES) == CONTRACT["errors"]["messages"]


@pytest.mark.parametrize("name", template_cases())
def test_a_template_case_synthesizes_its_golden(
    name: str, tmp_path: Path, no_aws: list[str]
) -> None:
    module = generator()
    synthesized = synthesize_case(load(name), tmp_path)
    document = {
        "root": module.normalise_template(synthesized["root"]),
        "nested": {
            logical_id: module.normalise_template(template)
            for logical_id, template in sorted(synthesized["nested"].items())
        },
    }
    expected = (CASES_DIR / name / "expected.template.json").read_text(encoding="utf-8")
    assert module.render(document) == expected
    assert no_aws == []


@pytest.mark.parametrize("name", error_cases())
def test_an_error_case_raises_before_adding_anything(
    name: str, tmp_path: Path, no_aws: list[str]
) -> None:
    case = load(name)
    error = case["expected"]["error"]
    with pytest.raises(EXCEPTIONS[error["exception"]]) as raised:
        build_case(case, tmp_path)
    exception = raised.value
    assert type(exception) is EXCEPTIONS[error["exception"]]
    assert isinstance(exception, RayitoCdkException | AgentCoreException)
    assert exception.code == error["code"]
    for field, value in error["fields"].items():
        assert exception.fields[field] == value
    assert no_aws == []


def test_a_failed_construct_leaves_no_child(tmp_path: Path) -> None:
    case = load("image-reserved-name")
    _, stack = new_stack(case, tmp_path)
    from rayito.cdk import RayitoMicrovmImage
    from tests.cdk_contract_harness import fixture_artifact

    with pytest.raises(RayitoCdkException):
        RayitoMicrovmImage(
            stack,
            "Image",
            name="rayito-base",
            artifact=fixture_artifact(case, tmp_path),
            base_image_version="1",
        )
    assert stack.node.try_find_child("Image") is None


def test_every_public_name_documents_its_cost_and_activation() -> None:
    import rayito.cdk as module

    assert "Coste y activación" in (module.__doc__ or "")
    for name in module.__all__:
        if name == "RayitoCdkException":
            continue
        assert "Coste y activación" in (getattr(module, name).__doc__ or ""), name
    assert sorted(module.__all__) == sorted(CONTRACT["sdk"]["python"]["exports"])
