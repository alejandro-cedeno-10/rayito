## ADDED Requirements

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
