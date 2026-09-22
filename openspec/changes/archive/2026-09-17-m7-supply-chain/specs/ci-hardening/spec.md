## ADDED Requirements

### Requirement: Every action is pinned to a full commit SHA with a version comment
Every `uses:` reference in every file under `.github/workflows/` SHALL name a 40-character commit SHA followed by a comment with the release tag it was resolved from (`owner/repo@<sha> # vX.Y.Z`), including path-scoped actions such as `github/codeql-action/upload-sarif`. No workflow SHALL reference an action by tag, major alias or branch. The CI `check` job SHALL fail when `grep -nE 'uses: [^@]+@(v[0-9]|main|master|release/)' .github/workflows/*.yml` matches anything, and SHALL run `actionlint` 1.7.12 (downloaded from its release with its checksum verified) over every workflow. `make lint` SHALL run `actionlint -no-color` when the binary is on `PATH` and print the install hint otherwise.

#### Scenario: a tag reference is rejected
- **WHEN** a workflow contains `uses: actions/checkout@v7`
- **THEN** the "no unpinned actions" step of the `check` job fails naming the file and line

#### Scenario: every workflow is actionlint clean
- **WHEN** `actionlint -no-color .github/workflows/*.yml` runs locally or in CI
- **THEN** it exits 0

### Requirement: Workflows are read-only by default and elevate per job
Every workflow SHALL declare `permissions: contents: read` at the top level. A job SHALL declare additional permissions only when it needs them: `security-events: write`, `id-token: write` and `actions: read` for the Scorecard job; `contents: write` and `pull-requests: write` for the release-please job; `id-token: write` for the PyPI, npm, Sigstore and AWS OIDC jobs; `contents: write` only for the job that uploads release assets. Every `actions/checkout` step SHALL set `persist-credentials: false`. Every job SHALL start with `step-security/harden-runner` with `egress-policy: audit`.

#### Scenario: ci.yml stays read-only
- **WHEN** a reviewer reads `.github/workflows/ci.yml` and `.github/workflows/audit.yml`
- **THEN** no job declares any permission beyond the top-level `contents: read`, and every job's first step is `step-security/harden-runner`

#### Scenario: harden-runner cannot start on ARM
- **WHEN** the `arm` job fails at the `step-security/harden-runner` step because the agent does not start on `ubuntu-24.04-arm`
- **THEN** the step is removed from that job only, with the reason in the task note, and every other job keeps it

### Requirement: Scorecard runs weekly and on main
`.github/workflows/scorecard.yml` SHALL run on `branch_protection_rule`, on a weekly schedule and on pushes to `main`, using `ossf/scorecard-action` with `results_format: sarif` and `publish_results: true`, upload the SARIF file as a 5-day artifact and to code scanning through `github/codeql-action/upload-sarif`. The README SHALL carry the Scorecard badge.

#### Scenario: weekly analysis
- **WHEN** the schedule fires on a public repository
- **THEN** the job completes, the SARIF artifact exists and the code-scanning alerts page lists Scorecard checks

### Requirement: Cargo runs with --locked in CI and the adapter suites run on aarch64
The CI invocations of `cargo clippy`, `cargo test` and `cargo zigbuild` SHALL pass `--locked`. A job `arm` on `ubuntu-24.04-arm` SHALL run `cargo test --workspace --locked` natively (the `cfg(unix)` adapter suites on real aarch64, non-root, with the same self-skips as on x86) and the kernel sidecar's host tests and `-m kernel` tests with `uv run --with-requirements requirements.txt`, so the `manylinux_2_28_aarch64` wheels pinned for the image are the ones exercised. The job SHALL NOT be a prerequisite of the `build` job.

#### Scenario: stale lockfile fails fast
- **WHEN** a pull request changes a crate version in `Cargo.toml` without updating `Cargo.lock`
- **THEN** `cargo test --workspace --locked` fails in the `check` and `arm` jobs instead of silently rewriting the lockfile

#### Scenario: aarch64 kernel tests
- **WHEN** the `arm` job runs `cd kernel-sidecar && uv run --with-requirements requirements.txt pytest -m kernel`
- **THEN** the resolution log shows aarch64 wheels and the tests pass
