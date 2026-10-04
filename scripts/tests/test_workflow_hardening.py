"""Every workflow under ``.github/workflows/`` parsed as data (PyYAML, like
``test_release_workflow.py``), for the rules that hold across files rather
than inside the release graph. ``actionlint`` and ``check_pins.py`` validate
shape and pinning; these tests pin which job may restore what and which
tool versions a job can end up running:

- **no cache where it can reach a credential or a published artefact**
  (``sec-supply-chain-followups``, SC-A02): every job of ``release.yml``,
  ``docs.yml`` (its build becomes the Pages site) and ``e2e.yml`` (AWS
  credentials), and every job anywhere that holds ``id-token``, ``pages``
  or ``contents: write`` or declares an ``environment``, restores no Actions
  cache. ``ci.yml`` writes the main-scope uv and pnpm caches on every push
  to ``main``, and an exact key match would hand those entries to the
  privileged job;
- **uv is pinned** (SC-A06): no workflow calls ``astral-sh/setup-uv``
  directly; every job goes through ``./.github/actions/setup-uv``, which
  fixes one version and the sha256 of its tarball per runner architecture;
- **uv never re-locks** (SC-A05): every workflow that runs ``uv`` sets
  ``UV_LOCKED=1`` at the top level, and ``ci.yml`` fails a ``uv.lock`` that
  no longer matches its ``pyproject.toml``;
- **audit tools come from hashed requirements** (SC-A03, SC-A12): pip-audit
  is installed with ``--require-hashes`` from
  ``.github/release/requirements-pip-audit.txt`` (never ``uvx``), and every
  hash-pinned tool file under ``.github/release/`` is itself audited in
  ``ci.yml`` and in the weekly ``audit.yml``."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
LOCAL_SETUP_UV = "./.github/actions/setup-uv"
LOCAL_SETUP_UV_ACTION = REPO_ROOT / ".github" / "actions" / "setup-uv" / "action.yml"
SETUP_UV = "astral-sh/setup-uv"
SETUP_NODE = "actions/setup-node"
PNPM_SETUP = "pnpm/action-setup"
CACHE_ACTIONS = ("actions/cache", "Swatinem/rust-cache", "mlugg/setup-zig")
PRIVILEGED_WORKFLOWS = frozenset({"release.yml", "docs.yml", "e2e.yml"})
ELEVATED_PERMISSIONS = (
    ("id-token", "write"),
    ("pages", "write"),
    ("contents", "write"),
)
UV_LOCKED = "UV_LOCKED"
UV_COMMAND = re.compile(r"\buv (run|sync|export|build|pip|venv|lock)\b")
LOCK_CHECKED_PROJECTS = ("clients/python", "kernel-sidecar")
LOCK_CHECK = "uv lock --check"
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
RUNNER_ARCHITECTURES = ("X64", "ARM64")
RELEASE_REQUIREMENTS = REPO_ROOT / ".github" / "release"
PIP_AUDIT_REQUIREMENTS = ".github/release/requirements-pip-audit.txt"
PIP_AUDIT = "pip-audit"
REQUIRE_HASHES = "--require-hashes"
AUDIT_JOBS = {"ci.yml": "audit", "audit.yml": "python"}


def workflow_files() -> list[Path]:
    return sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])


def load(path: Path) -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return document


def jobs_of(path: Path) -> dict[str, dict[str, Any]]:
    jobs: dict[str, dict[str, Any]] = load(path)["jobs"]
    return jobs


def steps_using(job: dict[str, Any], action: str) -> list[dict[str, Any]]:
    return [
        step for step in job.get("steps", []) if step.get("uses", "").startswith(action)
    ]


def step_runs(job: dict[str, Any]) -> list[str]:
    return [step["run"] for step in job.get("steps", []) if "run" in step]


def is_off(value: object) -> bool:
    return value is False or str(value).lower() == "false"


def is_privileged(workflow: Path, job: dict[str, Any]) -> bool:
    permissions = job.get("permissions") or {}
    if workflow.name in PRIVILEGED_WORKFLOWS or "environment" in job:
        return True
    return any(permissions.get(scope) == level for scope, level in ELEVATED_PERMISSIONS)


def privileged_jobs() -> list[tuple[str, dict[str, Any]]]:
    return [
        (f"{path.name}:{name}", job)
        for path in workflow_files()
        for name, job in jobs_of(path).items()
        if is_privileged(path, job)
    ]


def cache_findings(label: str, job: dict[str, Any]) -> list[str]:
    """Every way ``job`` could restore an Actions cache, as readable labels."""
    findings: list[str] = []
    for step in job.get("steps", []):
        uses = step.get("uses", "")
        options = step.get("with", {}) or {}
        if uses.startswith(CACHE_ACTIONS):
            findings.append(f"{label}: {uses}")
        if uses.startswith((SETUP_UV, LOCAL_SETUP_UV)) and not is_off(
            options.get("enable-cache", False)
        ):
            findings.append(
                f"{label}: setup-uv enable-cache={options.get('enable-cache')}"
            )
        if uses.startswith(SETUP_NODE) and (
            "cache" in options or not is_off(options.get("package-manager-cache"))
        ):
            findings.append(f"{label}: setup-node restores the package-manager cache")
        if (
            uses.startswith(PNPM_SETUP)
            and str(options.get("cache", "false")).lower() == "true"
        ):
            findings.append(f"{label}: pnpm/action-setup cache")
    return findings


def test_privileged_jobs_restore_no_cache() -> None:
    jobs = privileged_jobs()

    assert any(label.startswith("docs.yml:") for label, _ in jobs)
    assert any(label.startswith("e2e.yml:") for label, _ in jobs)
    findings = [
        finding for label, job in jobs for finding in cache_findings(label, job)
    ]
    assert findings == []


def test_the_cache_rule_reports_a_restoring_job() -> None:
    restoring = {
        "steps": [
            {"uses": f"{LOCAL_SETUP_UV}", "with": {"enable-cache": "auto"}},
            {"uses": f"{SETUP_NODE}@sha", "with": {"cache": "pnpm"}},
            {"uses": f"{CACHE_ACTIONS[0]}@sha"},
        ]
    }
    quiet = {
        "steps": [
            {"uses": f"{LOCAL_SETUP_UV}", "with": {"python-version": "3.12"}},
            {"uses": f"{SETUP_NODE}@sha", "with": {"package-manager-cache": False}},
        ]
    }

    assert len(cache_findings("x", restoring)) == 3
    assert cache_findings("x", quiet) == []


def test_no_workflow_installs_uv_without_the_pinned_local_action() -> None:
    direct = [
        f"{path.name}:{name}"
        for path in workflow_files()
        for name, job in jobs_of(path).items()
        if steps_using(job, SETUP_UV)
    ]
    users = [
        name
        for path in workflow_files()
        for name, job in jobs_of(path).items()
        if steps_using(job, LOCAL_SETUP_UV)
    ]

    assert direct == []
    assert users


def test_the_local_setup_uv_pins_a_version_and_a_checksum_per_architecture() -> None:
    action = load(LOCAL_SETUP_UV_ACTION)
    steps = action["runs"]["steps"]
    setups = [step for step in steps if step.get("uses", "").startswith(SETUP_UV)]

    assert len(setups) == 1
    options = setups[0]["with"]
    assert options.get("version") and options.get("checksum")
    assert "steps." in str(options["checksum"]) and "steps." in str(options["version"])
    assert str(action["inputs"]["enable-cache"]["default"]).lower() == "false"
    pins = [step for step in steps if step.get("id") and "env" in step]
    assert len(pins) == 1
    pin = pins[0]
    for architecture in RUNNER_ARCHITECTURES:
        assert architecture in pin["run"], architecture
    sums = [value for key, value in pin["env"].items() if key.endswith("_SHA256")]
    assert len(sums) == len(RUNNER_ARCHITECTURES)
    assert all(SHA256_HEX.match(str(value)) for value in sums)
    assert re.fullmatch(r"\d+\.\d+\.\d+", str(pin["env"]["UV_VERSION"]))


def runs_uv(path: Path) -> bool:
    return any(
        UV_COMMAND.search(run)
        for job in jobs_of(path).values()
        for run in step_runs(job)
    )


def test_every_workflow_that_runs_uv_never_relocks() -> None:
    running = [path for path in workflow_files() if runs_uv(path)]

    assert {path.name for path in running} >= {"ci.yml", "docs.yml", "e2e.yml"}
    for path in running:
        env = load(path).get("env") or {}
        assert str(env.get(UV_LOCKED)) == "1", path.name


def test_ci_fails_a_stale_lock() -> None:
    runs = step_runs(jobs_of(WORKFLOWS / "ci.yml")["check"])
    checks = [run for run in runs if LOCK_CHECK in run]

    assert checks
    for project in LOCK_CHECKED_PROJECTS:
        assert any(project in run for run in checks), project


def test_pip_audit_comes_from_hashed_requirements() -> None:
    for workflow, job_name in AUDIT_JOBS.items():
        runs = step_runs(jobs_of(WORKFLOWS / workflow)[job_name])
        assert not any(f"uvx {PIP_AUDIT}" in run for run in runs), workflow
        installs = [run for run in runs if "pip install" in run]
        assert installs, workflow
        assert all(
            REQUIRE_HASHES in run and PIP_AUDIT_REQUIREMENTS in run for run in installs
        ), workflow
    assert (REPO_ROOT / PIP_AUDIT_REQUIREMENTS).is_file()


def test_every_hashed_tool_file_is_audited() -> None:
    tool_files = sorted(
        path.relative_to(REPO_ROOT).as_posix()
        for path in RELEASE_REQUIREMENTS.glob("requirements-*.txt")
    )

    assert len(tool_files) >= 3
    for workflow, job_name in AUDIT_JOBS.items():
        audits = " ".join(
            run
            for run in step_runs(jobs_of(WORKFLOWS / workflow)[job_name])
            if PIP_AUDIT in run
        )
        for tool_file in tool_files:
            assert (
                tool_file in audits or ".github/release/requirements-*.txt" in audits
            ), f"{workflow}: {tool_file}"
