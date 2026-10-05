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
Every `uvx` invocation under `.github/workflows/` and in the `Makefile` SHALL name its tool with an exact version (`uvx ruff==0.16.7 check .`), including the `--from <package>==<version>` form, because those invocations resolve and execute third-party code from PyPI inside jobs on `main` and on the maintainer's workstation. Because `==` pins only the top-level tool and its transitive graph resolves fresh on every run (no hash, no cooldown), `uvx` SHALL run only tools listed in `scripts/check_pins.py`'s `DEPENDENCY_FREE_UVX_TOOLS` (today `ruff`, a binary with no Python dependencies); every other tool (twine, pip-audit, cfn-lint) SHALL be installed with `uv pip install --require-hashes --no-deps --only-binary :all:` from a hash-pinned file under `.github/release/requirements-*.txt` into a fresh virtual environment. `scripts/check_pins.py` SHALL report every `uvx` invocation in those files whose tool carries no `==`, and every pinned `uvx` of a tool outside that list, and exit 1; the CI `check` job and `make lint` SHALL run it, and the maintainer recipes in `docs/RELEASING.md` and `CONTRIBUTING.md` SHALL install the same tools from the same hashed files. User-facing install recipes under `docs/site/` (`uvx --from "rayito[mcp]" rayito-mcp`) SHALL NOT be part of the gate's scope, since they install the published Rayito rather than a tool of this build.

#### Scenario: an unpinned tool fails the gate
- **WHEN** the gate runs over `uvx twine check clients/python/dist/*` and `uvx cfn-lint --version`
- **THEN** both lines are reported with their file and line number and the gate exits 1

#### Scenario: a pinned tool with dependencies fails the gate
- **WHEN** the gate runs over `uvx pip-audit==2.10.1 -r req.txt`, `uvx twine==7.0.0 check dist/*`, `uvx cfn-lint==1.56.3 -- infra/iam.yaml` and `uvx --from pkg==1.0 tool`
- **THEN** each line is reported with the reason that `uvx` resolves the tool's graph on the fly, and the gate exits 1

#### Scenario: pinned invocations pass
- **WHEN** the gate runs over `uvx ruff==0.16.7 format --check .` and `uvx --from ruff==0.16.7 ruff check .`
- **THEN** nothing is reported and the gate exits 0

#### Scenario: the repository is clean
- **WHEN** `python3 scripts/check_pins.py` runs over the real `.github/workflows/`, the local actions and the `Makefile`
- **THEN** it exits 0, and the only `uvx` left in those files runs `ruff`

### Requirement: Tracked files carry no environment identifiers
No tracked file SHALL carry an identifier of the environment the project was developed or accepted in. `scripts/check_hygiene.py` (standard library only, no network) SHALL scan every file listed by `git ls-files`, or the files given as arguments, skipping only binary files (a NUL byte or invalid UTF-8), and SHALL report, as `KO <path>:<line>: <rule>` without echoing the line, every occurrence of: a 12-digit account ID in an ARN or in an S3 bucket or ECR registry name other than the placeholders `123456789012`, `000000000000`, `111122223333` and `444455556666`; the default CDK bootstrap qualifier after `cdk-`; an SSO profile or role name carrying account data (`AdministratorAccess-<digit>`, `<PermissionSet>Access-<12 digits>`, `AWSReservedSSO_<set>_<16 hex>`); a MicroVM ID (`microvm-` followed by eight hex digits) or endpoint (`<uuid>.lambda-microvm.`) whose UUID is not `00000000-0000-0000-0000-` followed by twelve decimal digits (the truncated prefix `00000000` of those fakes also passes); a `vpc-`, `subnet-`, `sg-` or `eni-` ID of 8 or 17 hex digits other than `0123456789abcdef0`; a Windows drive path or Git Bash drive path into `Users`, `tools` or `Projects`; a macOS home path (`/Users/` followed by a name of letters, digits, `.`, `_` or `-`, not preceded by a word character, `.` or `-`) or a macOS temporary path (`tmp/` or `var/` directly under `/private/`); and an AWS access key ID (`AKIA`/`ASIA` followed by 16 uppercase letters or digits) that does not end in `EXAMPLE`. It SHALL exit 1 when it reports anything and 0 otherwise. The CI `check` job and `make lint` SHALL run it next to `scripts/check_pins.py`, and its unit tests SHALL live in `scripts/tests/test_check_hygiene.py`, with a positive and a negative case per rule and a run over the real repository.

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

#### Scenario: a macOS workstation path fails the gate
- **WHEN** a tracked file contains an absolute path into a macOS home directory (`/Users/<a name>/…`) or into the `tmp` or `var` directory under `/private/`
- **THEN** the gate reports the local-path rule for that line, while `/Users/<tu-usuario>`, a relative `docs/Users/…` and the URL `https://example.com/a/Users/list` pass

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

### Requirement: Dockerfile pip installs are hash-pinned, dependency-free and wheels-only
`scripts/check_pins.py` SHALL run a fifth gate. Every `pip install`/`python3[.x] -m pip install` instruction of `image/Dockerfile` (and any file named `Dockerfile` it is given) (whether it names a requirements file with `-r`/`--requirement` or bare packages) SHALL carry `--require-hashes`, `--no-deps` and `--only-binary=:all:` (`--only-binary all` and `--only-binary=:all:` both count); any such instruction missing one of the three SHALL be reported as `KO <path>:<line>` with the reason naming the missing flags, and the script SHALL exit 1. Separately, every pin (`name==version`, its `--hash=` continuation lines joined the same way a Dockerfile's `\`-continued instruction is) of a file whose name starts with `requirements` and ends in `.txt` SHALL carry at least one `--hash=sha256:` of 64 lowercase hex characters; a pin without one SHALL be reported the same way. `kernel-sidecar/requirements.txt` and `kernel-sidecar/requirements-poly.txt` SHALL be added to the gate's default paths so a bare `python3 scripts/check_pins.py` covers both without arguments.

#### Scenario: an unflagged pip install is a finding
- **WHEN** the gate runs over a Dockerfile instruction `RUN pip install --no-cache-dir -r requirements.txt`
- **THEN** it is reported with the missing-flags reason and the gate exits 1

#### Scenario: a bare pip install is a finding
- **WHEN** the gate runs over a Dockerfile instruction `RUN pip install pandas`
- **THEN** it is reported with the missing-flags reason and the gate exits 1

#### Scenario: the three flags pass
- **WHEN** the gate runs over `RUN pip install --require-hashes --no-deps --only-binary=:all: -r requirements.txt`
- **THEN** nothing is reported

#### Scenario: an unhashed pin is a finding
- **WHEN** the gate runs over a requirements file containing `unhashed-package==1.2.3` with no `--hash=` line
- **THEN** it is reported with the unhashed-pin reason

#### Scenario: the real sidecar pins are clean
- **WHEN** `python3 scripts/check_pins.py` runs over `image/Dockerfile`, `kernel-sidecar/requirements.txt` and `kernel-sidecar/requirements-poly.txt`
- **THEN** it exits 0, naming all three files

### Requirement: Release jobs restore no cache
No job of `.github/workflows/release.yml` SHALL restore or save a GitHub Actions cache, because a tag run restores caches from the default-branch scope, which any job on `main` can write. No step SHALL use `actions/cache` (or a sub-action of it), `Swatinem/rust-cache` or `mlugg/setup-zig`; every `astral-sh/setup-uv` step SHALL set `enable-cache: false`; every `actions/setup-node` step SHALL set no `cache` input and SHALL set `package-manager-cache: false`; no `pnpm/action-setup` step SHALL set `cache: true`. zig SHALL come from the local composite action `.github/actions/zig`, which downloads `zig-x86_64-linux-<version>.tar.xz` from ziglang.org, checks it with `sha256sum -c` against a pinned `ZIG_SHA256`, and adds it to `PATH`. The `rayd-build` job SHALL install `cargo-zigbuild`, `cargo-auditable` and `cargo-cyclonedx` with `cargo install --locked --force --root "$RUNNER_TEMP/<dir>"` and `CARGO_HOME` under `RUNNER_TEMP`, and prepend that root's `bin` to `PATH`. `scripts/tests/test_release_workflow.py` SHALL check all of this on the parsed job graph and run in the CI `check` job.

#### Scenario: a cache action comes back
- **WHEN** a pull request adds `Swatinem/rust-cache` or `mlugg/setup-zig` to any job of `release.yml`, or drops `enable-cache: false` from a `setup-uv` step
- **THEN** `test_no_release_job_restores_a_cache` fails naming the job

#### Scenario: tools never come from a restored bin directory
- **WHEN** a reviewer reads the `cargo install` step of `rayd-build`
- **THEN** it carries `--locked`, `--force` and `--root "$RUNNER_TEMP/…"`, and its `env` sets `CARGO_HOME` under `runner.temp`

### Requirement: Every download in a workflow is verified against a pinned sha256
`scripts/check_pins.py` SHALL run a sixth gate over every file of `.github/workflows/*.yml`/`*.yaml` and of the local actions `.github/actions/*/action.yml`/`action.yaml` (both added to its default paths, so the first gate also covers the actions' `uses:`). A step is a YAML list item and its script the `run:` block or line (continuation lines joined, comment lines skipped). Every step whose script contains `curl` or `wget` SHALL declare a `<NAME>_SHA256:` of 64 lowercase hex characters in its `env:` (or assign `<NAME>_SHA256=` in the script), and its script SHALL satisfy the download rule of the third gate: each `curl` writes with `-o`/`--output` to a path that a `sha256sum -c` of the same script names, no pipe, no `wget`, no floating release, and no more downloads than checks. Any other step SHALL be reported as `KO <path>:<line>` from the step's first line with the download reason, and the script SHALL exit 1. `cargo-deny` SHALL be installed in CI by the local composite action `.github/actions/cargo-deny`, which downloads the pinned release with a pinned `CARGO_DENY_SHA256`, never by a Docker action that fetches an unpinned binary at run time.

#### Scenario: an unverified workflow download is a finding
- **WHEN** the gate runs over a step `run: curl --silent -L https://example.com/tool.tgz | tar -xzv -C /usr/bin`
- **THEN** it is reported from the step's first line and the gate exits 1

#### Scenario: the actionlint, zig and cargo-deny downloads pass
- **WHEN** `python3 scripts/check_pins.py` runs over the real tree
- **THEN** it exits 0, and the three downloading steps are among the steps it checked

### Requirement: Dependabot waits before proposing a fresh release
Every `updates` entry of `.github/dependabot.yml` SHALL set `cooldown.default-days` to at least 7, and the `cargo`, `uv` and `npm` entries SHALL set `cooldown.semver-major-days` to at least 14, so that a version published hours earlier (the window in which hijacked releases are detected and pulled) is not proposed; security updates bypass the cooldown. `scripts/tests/test_dependabot_cooldown.py` SHALL check every entry.

#### Scenario: a new ecosystem entry without cooldown
- **WHEN** a pull request adds an `updates` entry without `cooldown`
- **THEN** `test_every_ecosystem_waits_before_proposing_a_fresh_release` fails naming the ecosystem and directory

### Requirement: Privileged jobs restore no cache
No job of `release.yml`, `docs.yml` (its build becomes the GitHub Pages site, deployed without a reviewer) or `e2e.yml` (AWS credentials), and no job in any workflow that holds `id-token: write`, `pages: write` or `contents: write` or declares an `environment`, SHALL restore a GitHub Actions cache: no `actions/cache`, `Swatinem/rust-cache` or `mlugg/setup-zig`, `setup-uv` with its cache off, `setup-node` without `cache` and with `package-manager-cache: false`, and no `pnpm/action-setup` cache. Unprivileged jobs of `ci.yml` MAY keep their caches. `scripts/tests/test_workflow_hardening.py` SHALL derive the privileged set from the workflow files, so a new privileged job is covered without editing the test.

#### Scenario: the Pages build restores a poisoned cache
- **WHEN** a `ci.yml` job on a push to `main` has written a main-scope uv cache entry with the key `docs.yml`'s build would compute
- **THEN** `docs.yml`'s build does not restore it, because its `setup-uv` runs with the cache off, and the test fails if the cache is turned back on

#### Scenario: a new job with a token
- **WHEN** a job with `id-token: write` is added to any workflow with `cache: pnpm` on `setup-node`
- **THEN** `test_privileged_jobs_restore_no_cache` fails naming the workflow, the job and the cache

### Requirement: uv is pinned in every workflow and never re-locks
Every workflow job SHALL install uv through the local composite action `.github/actions/setup-uv`, which SHALL hold the uv version and the sha256 of its x86_64 and aarch64 Linux tarballs in one place, select the checksum from `runner.arch` (failing on any other architecture), pass both to a SHA-pinned `astral-sh/setup-uv`, and default its `enable-cache` input to `false`. No workflow SHALL call `astral-sh/setup-uv` directly. Every workflow that runs `uv` SHALL set `UV_LOCKED: "1"` at the workflow level, and `ci.yml`'s `check` job SHALL run `uv lock --check` in `clients/python` and in `kernel-sidecar`. Dependabot's `github-actions` entry SHALL read `/.github/actions/*` as well as `/`.

#### Scenario: a stale lock
- **WHEN** a pull request changes a dependency range in `clients/python/pyproject.toml` without regenerating `uv.lock`
- **THEN** `uv lock --check` fails in the `check` job, and any `uv run` in a workflow refuses to re-lock instead of installing newer versions

#### Scenario: uv is installed on an arm runner
- **WHEN** the `arm` job of `ci.yml` uses `./.github/actions/setup-uv`
- **THEN** setup-uv downloads the pinned version and verifies it against the aarch64 checksum

### Requirement: Hash-pinned tool requirements are audited and watched
Every `.github/release/requirements-*.txt` SHALL carry `==` and `--hash=sha256:` on every pin (gate 5 of `scripts/check_pins.py`), SHALL state in its header how to regenerate it with `uv pip compile --generate-hashes --exclude-newer <today - 7 days>`, SHALL be audited by pip-audit (itself installed from `requirements-pip-audit.txt` with `--require-hashes`) in `ci.yml`'s `audit` job and in the weekly `audit.yml`, and SHALL be watched by a Dependabot `pip` entry for `/.github/release` with the same cooldown as the other ecosystems.

#### Scenario: a CVE in a pin of twine's graph
- **WHEN** an advisory is published for a package pinned in `requirements-twine.txt`
- **THEN** the weekly audit fails and Dependabot proposes the patched pin

#### Scenario: a new tool file
- **WHEN** a `requirements-<tool>.txt` is added under `.github/release/`
- **THEN** the audit jobs' loop covers it without a workflow change and the gate requires hashes on its pins
