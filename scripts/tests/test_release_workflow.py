"""``.github/workflows/release.yml`` parsed as data (PyYAML, like
``test_iam_template.py``): the credential boundaries of the release close on
the job graph itself, not on a `grep` of the text, so a revert that quietly
merges a build step back into a job that holds a credential turns red here
even though `actionlint` and `check_pins.py` stay green (they validate shape
and pinning, not which job holds which token). The fixed points this file
pins:

- only ``python-publish``, ``typescript-publish`` and ``rayd-sign`` hold
  ``permissions.id-token: write``, and only ``rayd-upload`` holds
  ``contents: write`` (M10 C-10 for Python/TypeScript; ``sec-supply-chain-ci``
  splits ``rayd`` the same way);
- every job holding ``id-token: write`` declares an ``environment`` and runs
  only behind the publish gate, so no Fulcio certificate or registry token
  is issued for the release identity before the reviewer approves (a dry
  run stops at the build jobs; ``sec-supply-chain-followups``, SC-A01);
- no job holding a credential checks out the repository or runs third-party
  build tooling (``pnpm``, ``uv``, ``uvx``, ``pip``, ``npm install``,
  ``npm ci``, ``cargo``, ``make``), and each verifies a ``sha256sum -c`` of
  what the credential-free build job produced before using its credential;
- the build jobs declare no ``environment`` and no ``id-token``;
- no job restores a cache: no ``actions/cache``, ``Swatinem/rust-cache`` or
  ``mlugg/setup-zig``, and every ``setup-uv``/``setup-node`` turns its cache
  off, because a tag run restores caches any default-branch job can write;
- ``rayd-build`` installs its cargo tools with ``--force`` into a fresh
  ``--root`` and ``CARGO_HOME`` under ``RUNNER_TEMP``, never into a
  ``~/.cargo/bin`` that a cache could have planted;
- ``python-build`` hashes ``dist/`` right after ``uv build``, before any
  third-party check, re-checks the hash at the end, installs twine only from
  hash-pinned requirements, and gets uv from ``./.github/actions/setup-uv``
  (version and checksum pinned there);
- ``python-build`` and ``python-publish`` refuse a ``dist/`` that holds
  anything but the wheel and sdist of the tag's version, all listed in
  ``SHA256SUMS``: ``sha256sum -c`` ignores unlisted files, and the publish
  action uploads every distribution in the directory (SC-A04);
- ``rayd-upload`` runs in the ``release`` environment, never replaces an
  asset (no ``--clobber``) and publishing needs the tagged commit on
  ``main``;
- the cosign self-check binds the exact workflow ref, never a regexp;
- ``rayd-build`` generates ``THIRD_PARTY_LICENSES.md`` from the tag's
  ``Cargo.lock`` (``make image-licenses``, never a committed copy) and
  checks it covers the binary's ``.dep-v0`` before zipping it, puts
  ``LICENSE``, ``NOTICE`` and the notices into the zip and among the assets,
  and ``rayd-sign`` signs the ``SHA256SUMS`` that lists them (openspec
  third-party-licenses);
- every ``pnpm install`` and ``npm publish`` carries ``--ignore-scripts``,
  and ``python-publish`` downloads inside ``GITHUB_WORKSPACE`` (the publish
  action runs twine in a Docker container that mounts only the workspace)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"
PUBLISH_JOBS = frozenset({"python-publish", "typescript-publish"})
BUILD_JOBS = frozenset({"python-build", "typescript-build", "rayd-build"})
CREDENTIAL_JOBS = frozenset(
    {"python-publish", "typescript-publish", "rayd-sign", "rayd-upload"}
)
ID_TOKEN_WRITE_JOBS = frozenset({"python-publish", "typescript-publish", "rayd-sign"})
CONTENTS_WRITE_JOBS = frozenset({"rayd-upload"})
FORBIDDEN_IN_CREDENTIAL_RUN = re.compile(
    r"\b(pnpm|uv |uvx|pip|npm install|npm ci|cargo|make)\b"
)
CHECKOUT_ACTION = "actions/checkout"
PUBLISH_GATE = "needs.resolve.outputs.publish == 'true'"
PYPI_PUBLISH_ACTION = "pypa/gh-action-pypi-publish"
DOWNLOAD_ACTION = "actions/download-artifact"
RUNNER_TEMP = "runner.temp"
GITHUB_WORKSPACE_PREFIX = "${{ github.workspace }}/"
SHA256_CHECK = "sha256sum -c"
CACHE_ACTIONS = ("actions/cache", "Swatinem/rust-cache", "mlugg/setup-zig")
SETUP_UV = "astral-sh/setup-uv"
LOCAL_SETUP_UV = "./.github/actions/setup-uv"
SETUP_NODE = "actions/setup-node"
PNPM_SETUP = "pnpm/action-setup"
LOCAL_ZIG_ACTION = "./.github/actions/zig"
CARGO_INSTALL = "cargo install"
TOOLS_ROOT = '--root "$RUNNER_TEMP/'
RELEASE_ENVIRONMENT = "release"
TAG_ON_MAIN = 'git merge-base --is-ancestor "$GITHUB_SHA" origin/main'
EXACT_IDENTITY = (
    '--certificate-identity "https://github.com/${GITHUB_REPOSITORY}'
    '/.github/workflows/release.yml@${GITHUB_REF}"'
)
HASHED_TWINE = "--require-hashes"
ENVIRONMENT_KEY = "environment"
EXPECTED_WHEEL = "rayito-%s-py3-none-any.whl"
EXPECTED_SDIST = "rayito-%s.tar.gz"
DIST_LISTING = "find dist -mindepth 1 -maxdepth 1"
SUMS_LISTING = "SHA256SUMS"
UPLOAD_ACTION = "actions/upload-artifact"
TWINE_REQUIREMENTS = ".github/release/requirements-twine.txt"


def load_workflow() -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return document


def load_jobs() -> dict[str, dict[str, Any]]:
    jobs: dict[str, dict[str, Any]] = load_workflow()["jobs"]
    return jobs


def job_permissions(job: dict[str, Any]) -> dict[str, str]:
    return job.get("permissions") or {}


def step_runs(job: dict[str, Any]) -> list[str]:
    return [step["run"] for step in job.get("steps", []) if "run" in step]


def step_uses(job: dict[str, Any]) -> list[str]:
    return [step["uses"] for step in job.get("steps", []) if "uses" in step]


def steps_using(job: dict[str, Any], action: str) -> list[dict[str, Any]]:
    return [step for step in job.get("steps", []) if step.get("uses", "").startswith(action)]


def first_index(steps: list[dict[str, Any]], fragment: str) -> int:
    return next(i for i, step in enumerate(steps) if fragment in step.get("run", ""))


def last_index(steps: list[dict[str, Any]], fragment: str) -> int:
    return max(i for i, step in enumerate(steps) if fragment in step.get("run", ""))


def is_off(value: object) -> bool:
    return value is False or str(value).lower() == "false"


def test_only_the_publish_jobs_and_rayd_sign_hold_id_token_write() -> None:
    jobs = load_jobs()

    holders = {
        name
        for name, job in jobs.items()
        if job_permissions(job).get("id-token") == "write"
    }

    assert holders == ID_TOKEN_WRITE_JOBS


def test_every_id_token_job_waits_for_an_environment_and_the_publish_gate() -> None:
    """A dry run (``publish=false``) from a ``rayd-v*`` tag signed with the
    exact identity users verify, and nothing asked a reviewer first: the
    environment's approval has to come before the OIDC token exists."""
    jobs = load_jobs()

    for name, job in jobs.items():
        if job_permissions(job).get("id-token") != "write":
            continue
        assert job.get(ENVIRONMENT_KEY), name
        assert PUBLISH_GATE in (job.get("if") or ""), name


def test_rayd_sign_runs_in_the_release_environment() -> None:
    job = load_jobs()["rayd-sign"]

    assert job.get(ENVIRONMENT_KEY) == RELEASE_ENVIRONMENT


def dist_fileset_index(steps: list[dict[str, Any]]) -> int:
    return next(
        i
        for i, step in enumerate(steps)
        if DIST_LISTING in step.get("run", "")
        and EXPECTED_WHEEL in step["run"]
        and EXPECTED_SDIST in step["run"]
        and SUMS_LISTING in step["run"]
    )


def test_python_dist_holds_exactly_the_wheel_and_sdist_of_the_tag() -> None:
    jobs = load_jobs()
    build_steps = jobs["python-build"]["steps"]
    publish_steps = jobs["python-publish"]["steps"]

    build_check = dist_fileset_index(build_steps)
    upload = next(
        i
        for i, step in enumerate(build_steps)
        if step.get("uses", "").startswith(UPLOAD_ACTION)
    )
    publish_check = dist_fileset_index(publish_steps)
    publish = next(
        i
        for i, step in enumerate(publish_steps)
        if step.get("uses", "").startswith(PYPI_PUBLISH_ACTION)
    )

    assert build_check < upload
    assert last_index(publish_steps, SHA256_CHECK) < publish_check < publish
    assert "VERSION" in (jobs["python-publish"].get("env") or {})


def test_only_rayd_upload_holds_contents_write() -> None:
    jobs = load_jobs()

    holders = {
        name
        for name, job in jobs.items()
        if job_permissions(job).get("contents") == "write"
    }

    assert holders == CONTENTS_WRITE_JOBS
    assert job_permissions(jobs["rayd-upload"]).get("id-token") is None


def test_credential_jobs_never_checkout_or_run_third_party_tooling() -> None:
    jobs = load_jobs()

    for name in CREDENTIAL_JOBS:
        job = jobs[name]
        uses = step_uses(job)
        assert not any(u.startswith(CHECKOUT_ACTION) for u in uses), name
        for run in step_runs(job):
            match = FORBIDDEN_IN_CREDENTIAL_RUN.search(run)
            assert match is None, f"{name}: {match.group(0) if match else ''}\n{run}"


def test_build_jobs_declare_no_environment_and_no_id_token() -> None:
    jobs = load_jobs()

    for name in BUILD_JOBS:
        job = jobs[name]
        assert "environment" not in job, name
        assert job_permissions(job).get("id-token") is None, name
        assert job_permissions(job).get("contents", "read") == "read", name


def test_no_release_job_restores_a_cache() -> None:
    """A tag run restores caches from the default-branch scope, which any
    job on ``main`` can write: a planted ``~/.cargo/bin``, zig tarball or uv
    cache would land in a signed release."""
    jobs = load_jobs()

    for name, job in jobs.items():
        for uses in step_uses(job):
            assert not uses.startswith(CACHE_ACTIONS), f"{name}: {uses}"
        for step in steps_using(job, SETUP_UV):
            assert is_off(step.get("with", {}).get("enable-cache")), name
        for step in steps_using(job, LOCAL_SETUP_UV):
            assert is_off((step.get("with") or {}).get("enable-cache", False)), name
        for step in steps_using(job, SETUP_NODE):
            options = step.get("with", {})
            assert "cache" not in options, name
            assert is_off(options.get("package-manager-cache")), name
        for step in steps_using(job, PNPM_SETUP):
            assert str(step.get("with", {}).get("cache", "false")).lower() != "true", name


def test_rayd_build_gets_zig_from_the_verified_local_action() -> None:
    job = load_jobs()["rayd-build"]

    assert LOCAL_ZIG_ACTION in step_uses(job)


def test_rayd_build_installs_its_tools_into_a_fresh_root() -> None:
    job = load_jobs()["rayd-build"]
    installs = [step for step in job["steps"] if CARGO_INSTALL in step.get("run", "")]

    assert installs, "rayd-build installs no cargo tool"
    for step in installs:
        run = step["run"]
        assert "--locked" in run and "--force" in run and TOOLS_ROOT in run, run
        assert RUNNER_TEMP in step.get("env", {}).get("CARGO_HOME", ""), step


def test_python_build_hashes_dist_before_any_third_party_check() -> None:
    steps = load_jobs()["python-build"]["steps"]

    build = first_index(steps, "uv build")
    sums = first_index(steps, "> SHA256SUMS")
    wheel_check = first_index(steps, "check_wheel.py")
    twine = first_index(steps, "twine")
    recheck = last_index(steps, SHA256_CHECK)

    assert build < sums < wheel_check < recheck
    assert sums < twine < recheck


def test_python_build_installs_twine_only_from_hashed_requirements() -> None:
    runs = step_runs(load_jobs()["python-build"])
    twine_runs = [run for run in runs if "twine" in run]

    assert twine_runs
    assert not any("uvx twine" in run for run in twine_runs)
    installs = [run for run in twine_runs if "pip install" in run]
    assert installs and all(
        HASHED_TWINE in run and TWINE_REQUIREMENTS in run for run in installs
    )
    assert (REPO_ROOT / TWINE_REQUIREMENTS).is_file()


def test_uv_comes_only_from_the_pinned_local_action() -> None:
    """Version and checksum live once, in ``.github/actions/setup-uv``
    (``test_workflow_hardening.py`` pins its contents)."""
    jobs = load_jobs()

    assert not any(steps_using(job, SETUP_UV) for job in jobs.values())
    assert steps_using(jobs["python-build"], LOCAL_SETUP_UV)


def test_rayd_upload_is_gated_and_never_replaces_an_asset() -> None:
    job = load_jobs()["rayd-upload"]
    runs = step_runs(job)

    assert job.get("environment") == RELEASE_ENVIRONMENT
    assert PUBLISH_GATE in (job.get("if") or "")
    uploads = [run for run in runs if "gh release upload" in run]
    assert uploads and not any("--clobber" in run for run in uploads)
    steps = job["steps"]
    assert first_index(steps, SHA256_CHECK) < first_index(steps, "gh release upload")


def test_rayd_sign_verifies_the_build_output_before_signing() -> None:
    steps = load_jobs()["rayd-sign"]["steps"]

    assert first_index(steps, SHA256_CHECK) < first_index(steps, "cosign sign-blob")


def test_publishing_needs_the_tagged_commit_on_main() -> None:
    job = load_jobs()["resolve"]
    checkouts = steps_using(job, CHECKOUT_ACTION)

    assert checkouts
    for step in checkouts:
        assert step["with"]["persist-credentials"] is False
        assert step["with"]["fetch-depth"] == 0
    assert any(TAG_ON_MAIN in run for run in step_runs(job))


def test_cosign_self_check_binds_the_exact_workflow_ref() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    runs = step_runs(load_jobs()["rayd-sign"])

    assert "certificate-identity-regexp" not in text
    verifies = [run for run in runs if "cosign verify-blob" in run]
    assert verifies and all(EXACT_IDENTITY in run for run in verifies)


def test_release_runs_never_overlap_on_the_same_ref() -> None:
    concurrency = load_workflow()["concurrency"]

    assert "github.ref" in concurrency["group"]
    assert concurrency["cancel-in-progress"] is False


def test_every_pnpm_install_carries_ignore_scripts() -> None:
    jobs = load_jobs()

    installs = [
        run for job in jobs.values() for run in step_runs(job) if "pnpm install" in run
    ]

    assert installs, "no pnpm install found in release.yml"
    assert all("--ignore-scripts" in run for run in installs)


def test_npm_publish_carries_ignore_scripts() -> None:
    jobs = load_jobs()

    publishes = [
        run for job in jobs.values() for run in step_runs(job) if "npm publish" in run
    ]

    assert publishes, "no npm publish found in release.yml"
    assert all("--ignore-scripts" in run for run in publishes)


def test_publish_jobs_are_gated_and_verify_a_sha256_first() -> None:
    jobs = load_jobs()

    for name in PUBLISH_JOBS:
        job = jobs[name]
        assert PUBLISH_GATE in (job.get("if") or ""), name
        runs = step_runs(job)
        publish_index = (
            next(i for i, run in enumerate(runs) if "npm publish" in run)
            if name == "typescript-publish"
            else None
        )
        assert any(SHA256_CHECK in run for run in runs), name
        if publish_index is not None:
            verify_index = next(
                i for i, run in enumerate(runs) if SHA256_CHECK in run
            )
            assert verify_index < publish_index, name


def test_python_publish_verifies_before_the_publish_action() -> None:
    job = load_jobs()["python-publish"]
    steps = job["steps"]

    verify_indices = [
        i for i, step in enumerate(steps) if SHA256_CHECK in step.get("run", "")
    ]
    publish_index = next(
        i
        for i, step in enumerate(steps)
        if step.get("uses", "").startswith(PYPI_PUBLISH_ACTION)
    )

    assert verify_indices, "python-publish never runs sha256sum -c"
    assert all(i < publish_index for i in verify_indices)


def is_inside_the_workspace(path: str) -> bool:
    """Relative (the runner resolves it against ``GITHUB_WORKSPACE``) or under
    ``${{ github.workspace }}``; never ``runner.temp`` nor absolute."""
    if RUNNER_TEMP in path:
        return False
    if path.startswith(GITHUB_WORKSPACE_PREFIX):
        return True
    return not path.startswith(("/", "~", "$"))


def test_python_publish_packages_dir_is_inside_the_workspace() -> None:
    steps = load_jobs()["python-publish"]["steps"]
    publish = next(
        step for step in steps if step.get("uses", "").startswith(PYPI_PUBLISH_ACTION)
    )
    download = next(
        step for step in steps if step.get("uses", "").startswith(DOWNLOAD_ACTION)
    )
    packages_dir = publish["with"]["packages-dir"]
    download_path = download["with"]["path"]

    assert is_inside_the_workspace(packages_dir), packages_dir
    assert is_inside_the_workspace(download_path), download_path
    assert packages_dir.startswith(download_path.rstrip("/") + "/")
    for step in steps:
        if "working-directory" in step:
            assert is_inside_the_workspace(step["working-directory"])


def test_workspace_check_rejects_runner_temp_and_absolute_paths() -> None:
    assert not is_inside_the_workspace("${{ runner.temp }}/python/dist")
    assert not is_inside_the_workspace("/home/runner/work/_temp/python/dist")
    assert is_inside_the_workspace("python-dist/dist")
    assert is_inside_the_workspace("${{ github.workspace }}/python-dist/dist")


LICENSE_ASSETS = ("LICENSE", "NOTICE", "THIRD_PARTY_LICENSES.md")
SUMS_BUNDLE = "SHA256SUMS.sigstore.json"


def test_rayd_assets_include_the_license_notices_and_the_sums_bundle() -> None:
    assets = load_workflow()["env"]["RAYD_ASSETS"].split()

    for asset in (*LICENSE_ASSETS, "SHA256SUMS", SUMS_BUNDLE):
        assert asset in assets, asset


def test_rayd_build_checks_the_notices_before_shipping_them() -> None:
    job = load_jobs()["rayd-build"]
    steps = job["steps"]

    assert "./.github/actions/cargo-about" in step_uses(job)
    generated = first_index(steps, "make image-licenses")
    coverage = first_index(steps, "check_third_party_licenses.py")
    zipped = first_index(steps, "image_zip.py")
    staged = first_index(steps, "> SHA256SUMS")
    assert first_index(steps, "cargo auditable zigbuild") < coverage
    assert generated < coverage < zipped < staged
    stage_run = steps[staged]["run"]
    for asset in LICENSE_ASSETS:
        assert asset in stage_run.split("> SHA256SUMS")[0], asset


def test_rayd_sign_signs_the_sums_that_list_the_notices() -> None:
    job = load_jobs()["rayd-sign"]
    steps = job["steps"]

    sums = last_index(steps, "> SHA256SUMS")
    for asset in LICENSE_ASSETS:
        assert asset in steps[sums]["run"], asset
    sign = first_index(steps, f"--bundle {SUMS_BUNDLE} SHA256SUMS")
    assert sums < sign
    assert "cosign sign-blob" in steps[sign]["run"]
    assert "bundle_sha256" in job["outputs"]["sha256sums_bundle_sha256"]


def test_rayd_upload_checks_the_sums_bundle_digest() -> None:
    steps = load_jobs()["rayd-upload"]["steps"]

    check = first_index(steps, f"SHA256SUMS_BUNDLE_SHA256}}  {SUMS_BUNDLE}")
    assert SHA256_CHECK in steps[check]["run"]
    assert check < first_index(steps, "gh release upload")
