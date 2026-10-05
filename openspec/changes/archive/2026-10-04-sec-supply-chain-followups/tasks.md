## 1. Tests first (each red before its fix)

- [x] 1.1 `test_release_workflow.py`: every `id-token: write` job has an
  environment and the publish gate; `rayd-sign` in `release`; exact
  `dist/` check in both Python jobs; uv only from the local action.
- [x] 1.2 `test_workflow_hardening.py`: privileged jobs restore no cache;
  no direct `setup-uv`; the local action pins a version and a checksum per
  architecture; `UV_LOCKED`; `uv lock --check`; pip-audit from hashed
  requirements; every tool file audited.
- [x] 1.3 `test_check_pins.py`: a pinned `uvx` of a tool with dependencies
  is a finding; `ruff` passes.
- [x] 1.4 `test_dependabot_cooldown.py`: `pip` on `/.github/release`, the
  composite actions watched.
- [x] 1.5 `test_artifact.py`: symlinked file and directory refused; links
  inside excluded directories ignored.
- [x] 1.6 `test_ci_oidc_role_template.py`: `StringEquals` on the prefix,
  pattern accepts immutable and legacy, rejects wildcards, no default.
- [x] 1.7 `test_security_docs.py`: the recipe verifies before publishing;
  `verify.md` says what a signature proves.

## 2. Fixes

- [x] 2.1 `release.yml`: `rayd-sign` environment + gate, exact `dist/`,
  local setup-uv, `UV_LOCKED`, header.
- [x] 2.2 `.github/actions/setup-uv/action.yml`; every workflow uses it.
- [x] 2.3 `docs.yml`, `e2e.yml` without caches; `UV_LOCKED` in the
  workflows that run uv; `uv lock --check` in `ci.yml`.
- [x] 2.4 `requirements-pip-audit.txt`, `requirements-cfn-lint.txt`;
  `ci.yml` and `audit.yml` audit jobs; Makefile `infra-lint` and `wheel`.
- [x] 2.5 `check_pins.py` gate 2; `dependabot.yml`.
- [x] 2.6 `_artifact.py` refuses symlinks.
- [x] 2.7 `infra/ci-oidc-role.yaml` `GitHubSubjectPrefix`.

## 3. Docs

- [x] 3.1 `verify.md`, `configurar-aws.md`, `security.md`, `SECURITY.md`
  T10 and the supply-chain table, `docs/RELEASING.md`, `infra/README.md`,
  `CONTRIBUTING.md`, the gates reference, the Python client README.
- [x] 3.2 `[Unreleased]` → `Security` in the `rayd` and Python changelogs.

## 4. Verification

- [x] 4.1 Gates: Python (3023 unit, ruff, format, mypy, 386 scripts and
  Lambda tests), TypeScript (lint, typecheck, 1470 tests), docs strict and
  examples, OpenSpec `--all --strict`, root scripts, actionlint, cfn-lint
  from the hashed file (macOS; no Rust change). The `python-build` file-set
  step ran on Linux against a real `uv build` output: passes, and fails on
  an extra wheel or an extra `SHA256SUMS` line.
- [x] 4.2 Green CI on the pull request (the first run of the composite
  setup-uv on x86_64 and aarch64, the hashed pip-audit and `uv lock
  --check`).
