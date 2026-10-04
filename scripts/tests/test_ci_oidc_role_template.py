"""``infra/ci-oidc-role.yaml`` parsed as data: the trust of the e2e role
compares the OIDC ``sub`` with ``StringEquals`` against
``<GitHubSubjectPrefix>:environment:<GitHubEnvironment>``.

A repository with immutable subjects (GitHub's
``actions/oidc/customization/sub`` returns ``use_immutable_subject: true``)
issues ``repo:<owner>@<owner_id>/<repo>@<repo_id>:...``, which the old
``repo:<owner>/<repo>`` parameter could not spell: its ``AllowedPattern``
rejected ``@``, so the role failed closed. The fix is a prefix parameter
that takes the exact value GitHub reports, never a looser operator or a
wildcard (``sec-supply-chain-followups``, SC-A10). No default: the numeric
ids belong to the deployer's repository and are read from the API at
deploy time (``infra/README.md``)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from test_iam_template import TemplateLoader

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "infra" / "ci-oidc-role.yaml"
SUBJECT_PREFIX = "GitHubSubjectPrefix"
SUB_CLAIM = "token.actions.githubusercontent.com:sub"
EXPECTED_SUBJECT = "${GitHubSubjectPrefix}:environment:${GitHubEnvironment}"
IMMUTABLE_PREFIX = "repo:octo-org@123456/octo-repo@789012"
LEGACY_PREFIX = "repo:octo-org/octo-repo"
LOOSE_OPERATORS = ("StringLike", "ForAnyValue:StringLike")


def template() -> dict[str, Any]:
    document = yaml.load(TEMPLATE.read_text(encoding="utf-8"), Loader=TemplateLoader)
    assert isinstance(document, dict)
    return document


def trust_condition() -> dict[str, Any]:
    statements = template()["Resources"]["E2eRole"]["Properties"][
        "AssumeRolePolicyDocument"
    ]["Statement"]
    assert len(statements) == 1
    condition: dict[str, Any] = statements[0]["Condition"]
    return condition


def test_the_trust_compares_the_exact_subject_prefix() -> None:
    condition = trust_condition()

    assert condition["StringEquals"][SUB_CLAIM] == {"Fn::Sub": EXPECTED_SUBJECT}
    for operator in LOOSE_OPERATORS:
        assert operator not in condition


def test_the_subject_prefix_accepts_immutable_and_legacy_subjects() -> None:
    parameter = template()["Parameters"][SUBJECT_PREFIX]
    pattern = re.compile(parameter["AllowedPattern"])

    assert "Default" not in parameter
    assert pattern.fullmatch(IMMUTABLE_PREFIX)
    assert pattern.fullmatch(LEGACY_PREFIX)
    for loose in (
        "repo:octo-org/*",
        "repo:*",
        "octo-org/octo-repo",
        f"{LEGACY_PREFIX}:ref:x",
    ):
        assert not pattern.fullmatch(loose), loose


def test_the_old_repository_parameter_is_gone() -> None:
    document = template()

    assert "GitHubRepository" not in document["Parameters"]
    assert document["Outputs"]["TrustedSubject"]["Value"] == {
        "Fn::Sub": EXPECTED_SUBJECT
    }
