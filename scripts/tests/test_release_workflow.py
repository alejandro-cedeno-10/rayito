"""``.github/workflows/release.yml`` parsed as data (PyYAML, like
``test_iam_template.py``): the build/publish split of M10 (C-10, the residue
of H-02) closes on the job graph itself, not on a `grep` of the text, so a
revert that quietly merges a build step back into a publish job turns red
here even though `actionlint` and `check_pins.py` stay green (they validate
shape and pinning, not which job holds the OIDC token). The four fixed points
this file pins:

- only ``python-publish``, ``typescript-publish`` and ``rayd`` hold
  ``permissions.id-token: write``;
- neither publish job checks out the repository or runs third-party build
  tooling (``pnpm``, ``uv``, ``uvx``, ``pip``, ``npm install``, ``npm ci``,
  ``cargo``, ``make``);
- the two ``*-build`` jobs declare no ``environment`` and no ``id-token``;
- every ``pnpm install`` carries ``--ignore-scripts`` (belt-and-suspenders
  with ``clients/typescript/.npmrc``), ``npm publish`` too, and both publish
  jobs are gated on ``needs.resolve.outputs.publish == 'true'`` and verify a
  ``sha256sum -c`` before publishing anything."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"
PUBLISH_JOBS = frozenset({"python-publish", "typescript-publish"})
BUILD_JOBS = frozenset({"python-build", "typescript-build"})
ID_TOKEN_WRITE_JOBS = frozenset({"python-publish", "typescript-publish", "rayd"})
FORBIDDEN_IN_PUBLISH_RUN = re.compile(
    r"\b(pnpm|uv |uvx|pip|npm install|npm ci|cargo|make)\b"
)
CHECKOUT_ACTION = "actions/checkout"
PUBLISH_GATE = "needs.resolve.outputs.publish == 'true'"


def load_jobs() -> dict[str, dict[str, Any]]:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return document["jobs"]


def job_permissions(job: dict[str, Any]) -> dict[str, str]:
    return job.get("permissions") or {}


def step_runs(job: dict[str, Any]) -> list[str]:
    return [step["run"] for step in job.get("steps", []) if "run" in step]


def step_uses(job: dict[str, Any]) -> list[str]:
    return [step["uses"] for step in job.get("steps", []) if "uses" in step]


def test_only_the_publish_jobs_and_rayd_hold_id_token_write() -> None:
    jobs = load_jobs()

    holders = {
        name
        for name, job in jobs.items()
        if job_permissions(job).get("id-token") == "write"
    }

    assert holders == ID_TOKEN_WRITE_JOBS


def test_publish_jobs_never_checkout_or_run_third_party_tooling() -> None:
    jobs = load_jobs()

    for name in PUBLISH_JOBS:
        job = jobs[name]
        uses = step_uses(job)
        assert not any(u.startswith(CHECKOUT_ACTION) for u in uses), name
        for run in step_runs(job):
            match = FORBIDDEN_IN_PUBLISH_RUN.search(run)
            assert match is None, f"{name}: {match.group(0) if match else ''}\n{run}"


def test_build_jobs_declare_no_environment_and_no_id_token() -> None:
    jobs = load_jobs()

    for name in BUILD_JOBS:
        job = jobs[name]
        assert "environment" not in job, name
        assert job_permissions(job).get("id-token") is None, name


def test_every_pnpm_install_carries_ignore_scripts() -> None:
    jobs = load_jobs()

    installs = [
        run
        for job in jobs.values()
        for run in step_runs(job)
        if "pnpm install" in run
    ]

    assert installs, "no pnpm install found in release.yml"
    assert all("--ignore-scripts" in run for run in installs)


def test_npm_publish_carries_ignore_scripts() -> None:
    jobs = load_jobs()

    publishes = [
        run
        for job in jobs.values()
        for run in step_runs(job)
        if "npm publish" in run
    ]

    assert publishes, "no npm publish found in release.yml"
    assert all("--ignore-scripts" in run for run in publishes)


def test_publish_jobs_are_gated_and_verify_a_sha256_first() -> None:
    jobs = load_jobs()

    for name in PUBLISH_JOBS:
        job = jobs[name]
        assert PUBLISH_GATE in (job.get("if") or ""), name
        runs = step_runs(job)
        publish_index = next(
            i
            for i, run in enumerate(runs)
            if "npm publish" in run
        ) if name == "typescript-publish" else None
        assert any("sha256sum -c" in run for run in runs), name
        if publish_index is not None:
            verify_index = next(
                i for i, run in enumerate(runs) if "sha256sum -c" in run
            )
            assert verify_index < publish_index, name


def test_python_publish_verifies_before_the_publish_action() -> None:
    job = load_jobs()["python-publish"]
    steps = job["steps"]

    verify_indices = [
        i for i, step in enumerate(steps) if "sha256sum -c" in step.get("run", "")
    ]
    publish_index = next(
        i
        for i, step in enumerate(steps)
        if step.get("uses", "").startswith("pypa/gh-action-pypi-publish")
    )

    assert verify_indices, "python-publish never runs sha256sum -c"
    assert all(i < publish_index for i in verify_indices)


def test_the_repository_itself_is_clean() -> None:
    jobs = load_jobs()

    assert {
        name
        for name, job in jobs.items()
        if job_permissions(job).get("id-token") == "write"
    } == ID_TOKEN_WRITE_JOBS
    for name in PUBLISH_JOBS:
        assert jobs[name]["needs"] and PUBLISH_GATE in jobs[name]["if"]
