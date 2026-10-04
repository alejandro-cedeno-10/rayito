## Context

`sec-supply-chain-ci` split the `rayd` release into build, sign and upload,
removed caches from `release.yml` and pinned the release's uv. The review
of 2026-10 (SC-A01..SC-A14) re-read the workflows, the repository API
(rulesets, environments, OIDC subject customization, immutable releases)
and run logs, and confirmed the gaps below. This change closes what the
tree can close and records the repository settings that only the
maintainer can change.

## Goals / Non-Goals

**Goals:** no OIDC token for a publishing identity before a reviewer
approves; no privileged job restores a cache; every uv is pinned and never
re-locks; no third-party graph resolves unpinned in CI or in the
maintainer's `make` targets; the published Python files are exactly the
tag's two distributions; the image builder ships regular files only; the
e2e trust matches the real `sub` without loosening it; the docs state what
holds.

**Non-Goals (pending maintainer decisions, `docs/RELEASING.md`):** a
`creation` rule for release tags; a GitHub App instead of
`RELEASE_PLEASE_TOKEN` (or the secret in an environment); immutable
releases; `allowed_actions=selected`, `sha_pinning_required`,
`egress-policy: block`; `rayito image publish --verify-release`; checking
the mount-s3 RPM signature at its next bump; fuzzing and the CII badge
(Scorecard #5, #8).

## Decisions

### D1. Every `id-token: write` job is environment-gated and publish-gated

`rayd-sign` gets `environment: release` and the publish gate, like
`python-publish` and `typescript-publish`. The environment's reviewer
approves before GitHub issues the OIDC token, so Fulcio never issues a
certificate for `release.yml@refs/tags/rayd-v*` on an unapproved run. A
dry run stops after `rayd-build` (its self-check is lost, which is the
point: the ensayo must not mint the production identity). `rayd-upload`
keeps its own environment: two approvals per `rayd` release, accepted for a
release that ships root code. A test asserts the rule for every job of
`release.yml`, not just `rayd-sign`.

The identity is still fixed by the workflow file and the tag. Restricting
tag creation (a `creation` rule, in a separate ruleset so the bypass never
reaches `update`/`deletion`) is a repository setting with a trade-off: the
creator of release tags is the maintainer's PAT, so its bypass would cover a
leaked PAT too. Left to the maintainer, together with the GitHub App.

### D2. Privileged jobs restore no cache, decided by a rule, not a list

A job is privileged when its workflow's output is published or holds a
credential (`release.yml`, `docs.yml`, `e2e.yml`) or the job holds
`id-token`/`pages`/`contents: write` or an environment. The test derives
the set from the YAML, so a new privileged job is covered without editing
the test. Unprivileged `ci.yml` jobs keep `enable-cache: auto`.

### D3. One composite action owns the uv pin

`.github/actions/setup-uv` holds `UV_VERSION` and the sha256 of the
x86_64 and aarch64 glibc tarballs in one step, maps `runner.arch` to the
checksum (an unknown architecture fails), and passes both to the pinned
`astral-sh/setup-uv`. Its `enable-cache` input defaults to `false`. A root
`uv.toml` with `required-version` was rejected: it would force every
contributor's local uv to one exact version and still not carry a
checksum. Dependabot's `github-actions` entry gains
`/.github/actions/*` so the inner `uses:` stays updated.

### D4. `UV_LOCKED=1` at workflow level plus an explicit lock check

Set in every workflow that runs uv (including `release.yml` and
`audit.yml`, where it is harmless with `uv build`, `--no-project` and
`uv export --frozen`, checked locally with uv 0.12.18). `ci.yml`'s `check`
job also runs `uv lock --check` for both projects so a stale lock fails in
one named step.

### D5. Tools with dependencies install from hashed files

`requirements-pip-audit.txt` (linux x86_64, like twine's) and
`requirements-cfn-lint.txt` (`--universal`, because `make infra-lint` runs
on macOS too) are compiled with `--generate-hashes --exclude-newer <today
- 7 days>`. Install: `uv pip install --require-hashes --no-deps
--only-binary :all:` into a venv under `RUNNER_TEMP` (CI) or `mktemp -d`
(Makefile, deleted on exit). Gate 2 flags a pinned `uvx` unless the tool is
in `DEPENDENCY_FREE_UVX_TOOLS` (`ruff`). The `--with-requirements`
exception suggested by the review was not taken: `uvx` still resolves the
tool's own graph without hashes. Every `requirements-*.txt` of
`.github/release/` is audited by pip-audit (CI and weekly) and watched by
a Dependabot `pip` entry with the same cooldown.

### D6. Exact `dist/` in both Python jobs

After `sha256sum -c`, a step compares `find dist -mindepth 1 -maxdepth 1`
and the names in `SHA256SUMS` with `rayito-<v>-py3-none-any.whl` and
`rayito-<v>.tar.gz`. The two jobs repeat the check on purpose: the publish
job has no checkout, so it cannot call a shared script, and it is the job
whose check matters.

### D7. Symlinks refused in `shipped_files`

Excluded directories are skipped first (also when the excluded directory
itself is a link, such as a relocated `.venv`), then any symlink raises
`SystemExit`. `rglob` does not descend into a linked directory, so the
link entry is where it is caught. No TypeScript counterpart exists (the
image builder is Python-only).

### D8. `GitHubSubjectPrefix` replaces `GitHubRepository`

The parameter takes the `sub_claim_prefix` that
`GET /repos/{owner}/{repo}/actions/oidc/customization/sub` returns; its
pattern accepts the immutable (`repo:<o>@<id>/<r>@<id>`) and the legacy
form and nothing looser; no default, so no numeric id lands in the tree.
The condition stays `StringEquals`.

### D9. CodeQL and Scorecard

No CodeQL alert is open: the six dismissals and the five fixes of
`sec-supply-chain-ci` were re-checked and stand. The five open Scorecard
alerts are not CodeQL false positives and stay open (BranchProtection and
CodeReview follow from a single maintainer, Maintained resolves with age,
Fuzzing and CII Best Practices are follow-ups).
