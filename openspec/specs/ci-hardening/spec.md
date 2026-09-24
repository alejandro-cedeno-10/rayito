# ci-hardening Specification

## Purpose
TBD - created by archiving change m7-supply-chain. Update Purpose after archive.

## Requirements

### Requirement: Every action is pinned to a full commit SHA with a version comment
Every `uses:` reference in every file under `.github/workflows/` SHALL name a 40-character commit SHA followed by a comment with the release tag it was resolved from (`owner/repo@<sha> # vX.Y.Z`), including path-scoped actions such as `github/codeql-action/upload-sarif`. No workflow SHALL reference an action by tag, major alias, branch or truncated SHA. The gate SHALL be an allowlist, not a denylist of spellings: `scripts/check_pins.py` SHALL report every `uses:` line of `.github/workflows/*.yml` and `*.yaml` whose reference does not match `[^@\s]+@[0-9a-f]{40}( +#.*)?` in full, skipping only commented lines and local actions whose reference starts with `./`, and SHALL exit 1 when it reports anything. The CI `check` job SHALL run it, and SHALL run `actionlint` 1.7.12 (downloaded from its release with its checksum verified) over every workflow. `make lint` SHALL run `python scripts/check_pins.py` and `actionlint -no-color` when the binary is on `PATH`, printing the install hint for `actionlint` otherwise.

#### Scenario: a tag reference is rejected
- **WHEN** a workflow contains `uses: actions/checkout@v7`
- **THEN** the pinning gate of the `check` job fails naming the file and line

#### Scenario: the spellings the old denylist missed
- **WHEN** the gate runs over `uses: owner/action@1.2.3`, `uses: owner/action@latest`, `uses: owner/action@release-v2` and `uses: owner/action@ab12cd34`
- **THEN** all four are reported and the gate exits 1

#### Scenario: a full SHA and a local action pass
- **WHEN** the gate runs over `uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1`, the same line without the comment, and `uses: ./.github/actions/local`
- **THEN** nothing is reported and the gate exits 0

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

### Requirement: Every uvx invocation names an exact version
Every `uvx` invocation under `.github/workflows/` and in the `Makefile` SHALL name its tool with an exact version (`uvx twine==7.0.0 check …`, `uvx ruff==0.16.7 check .`, `uvx cfn-lint==1.56.3 -- …`, `uvx pip-audit==2.10.1 -r …`), including the `--from <package>==<version>` form, because those invocations resolve and execute third-party code from PyPI inside jobs that hold publishing credentials (`release.yml`'s Python job declares `environment: pypi` and `id-token: write`). `scripts/check_pins.py` SHALL report every `uvx` invocation in those files whose tool carries no `==` and exit 1, the CI `check` job and `make lint` SHALL run it, and the maintainer recipes in `docs/RELEASING.md` and `CONTRIBUTING.md` SHALL quote the same pinned commands. User-facing install recipes under `docs/site/` (`uvx --from "rayito[mcp]" rayito-mcp`) SHALL NOT be part of the gate's scope, since they install the published Rayito rather than a tool of this build.

#### Scenario: an unpinned tool fails the gate
- **WHEN** the gate runs over `uvx twine check clients/python/dist/*` and `uvx cfn-lint --version`
- **THEN** both lines are reported with their file and line number and the gate exits 1

#### Scenario: pinned invocations pass
- **WHEN** the gate runs over `uvx pip-audit==2.10.1 -r kernel-sidecar/requirements.txt --no-deps --strict`, `uvx ruff==0.16.7 format --check .` and `uvx --from pkg==1.0 tool`
- **THEN** nothing is reported and the gate exits 0

#### Scenario: the repository is clean
- **WHEN** `python3 scripts/check_pins.py` runs over the real `.github/workflows/` and `Makefile`
- **THEN** it exits 0, and `grep -rn "uvx " .github/workflows Makefile` shows a version on every line

### Requirement: Tracked files carry no environment identifiers
No tracked file SHALL carry an identifier of the environment the project was developed or accepted in. `scripts/check_hygiene.py` (standard library only, no network) SHALL scan every file listed by `git ls-files`, or the files given as arguments, skipping only binary files (a NUL byte or invalid UTF-8), and SHALL report, as `KO <path>:<line>: <rule>` without echoing the line, every occurrence of: a 12-digit account ID in an ARN or in an S3 bucket or ECR registry name other than the placeholders `123456789012`, `000000000000`, `111122223333` and `444455556666`; the default CDK bootstrap qualifier after `cdk-`; an SSO profile or role name carrying account data (`AdministratorAccess-<digit>`, `<PermissionSet>Access-<12 digits>`, `AWSReservedSSO_<set>_<16 hex>`); a MicroVM ID (`microvm-` followed by eight hex digits) or endpoint (`<uuid>.lambda-microvm.`) whose UUID is not `00000000-0000-0000-0000-` followed by twelve decimal digits (the truncated prefix `00000000` of those fakes also passes); a `vpc-`, `subnet-`, `sg-` or `eni-` ID of 8 or 17 hex digits other than `0123456789abcdef0`; a Windows drive path or Git Bash drive path into `Users`, `tools` or `Projects`; and an AWS access key ID (`AKIA`/`ASIA` followed by 16 uppercase letters or digits) that does not end in `EXAMPLE`. It SHALL exit 1 when it reports anything and 0 otherwise. The CI `check` job and `make lint` SHALL run it next to `scripts/check_pins.py`, and its unit tests SHALL live in `scripts/tests/test_check_hygiene.py`, with a positive and a negative case per rule and a run over the real repository.

#### Scenario: a real account in an ARN fails the gate
- **WHEN** a tracked file contains `arn:aws:iam::<a 12-digit account that is not a placeholder>:role/deployer`
- **THEN** the gate prints the file, the line and the rule, and exits 1

#### Scenario: documented placeholders pass
- **WHEN** a tracked file contains `arn:aws:iam::123456789012:role/x`, `arn:aws:lambda:us-east-1:aws:microvm-image:al2023-1`, `amzn-s3-demo-bucket`, `microvm-00000000-0000-0000-0000-000000000142`, `vpc-0123456789abcdef0` and `AKIAIOSFODNN7EXAMPLE`
- **THEN** nothing is reported

#### Scenario: an access key is never echoed
- **WHEN** a tracked file contains an access key ID
- **THEN** the gate reports its file and line and the output does not contain the key

#### Scenario: untracked files are out of scope
- **WHEN** an untracked, ignored file such as `.claude/notes.jsonl` contains account IDs and MicroVM IDs
- **THEN** the gate run without arguments does not read it

#### Scenario: the repository is clean
- **WHEN** `python3 scripts/check_hygiene.py` runs at the root of the repository
- **THEN** it exits 0

### Requirement: Every download in a Dockerfile is verified against a pinned sha256
`scripts/check_pins.py` SHALL run a third gate over `image/Dockerfile` (added to its default paths) and over any file named `Dockerfile` it is given. The gate joins backslash-continued lines into instructions and skips comment lines. Every instruction that contains `curl` SHALL:
- assign a `<NAME>_SHA256=` value of exactly 64 lowercase hex characters;
- contain `sha256sum -c`;
- name no floating release (`/releases/latest` or `/latest/download/`).

Any other `curl` instruction SHALL be reported as `KO <path>:<line>` with the reason `la descarga no está verificada contra un sha256 fijado`, and the script SHALL exit 1. The gate SHALL use only the standard library and no network, like the other two gates, and CI and `make lint` SHALL keep running the script.

#### Scenario: the real tree passes
- **WHEN** `python scripts/check_pins.py` runs from the repository root after the Deno layer landed
- **THEN** it prints `OK` naming the checked files, `image/Dockerfile` among them, and exits 0

#### Scenario: unpinned downloads are findings
- **WHEN** the unit test runs `unpinned_downloads` over instructions with a `curl` and no `_SHA256=`, with a sha256 but no `sha256sum -c`, with a 63-hex sha256, and with a `releases/latest` URL
- **THEN** each yields one finding with the download reason, while the Deno instruction of the Dockerfile and a commented-out `curl` line yield none
