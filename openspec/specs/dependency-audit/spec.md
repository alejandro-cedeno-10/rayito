# dependency-audit Specification

## Purpose
TBD - created by archiving change m7-supply-chain. Update Purpose after archive.
## Requirements
### Requirement: cargo-deny checks licences, advisories, sources and bans
A `deny.toml` at the repository root SHALL allow exactly the licences `MIT`, `Apache-2.0`, `BSD-2-Clause`, `BSD-3-Clause`, `ISC`, `Unicode-3.0`, `Zlib` and `MPL-2.0` (confidence threshold 0.8), with a single named exception allowing `CC0-1.0` for the crate `notify`; SHALL enable RustSec advisories with `yanked = "deny"` and an empty `ignore` list; SHALL allow only the crates.io registry (`unknown-registry = "deny"`, `unknown-git = "deny"`); SHALL set `multiple-versions = "warn"`, `wildcards = "deny"` and `allow-wildcard-paths = true`; and SHALL list the targets `aarch64-unknown-linux-musl`, `x86_64-unknown-linux-gnu` and `x86_64-pc-windows-gnu`. A CI job `deny` SHALL run `EmbarkStudios/cargo-deny-action` with `command: check`; `make lint` SHALL run `cargo deny check` when the tool is on `PATH`. An advisory finding SHALL be fixed by bumping the crate in the same pull request when a patched version exists, otherwise recorded as an `ignore` entry with the advisory id, reason and date, plus an issue.

#### Scenario: current graph passes
- **WHEN** `cargo deny check` runs against the workspace as of this change
- **THEN** it reports `advisories ok, bans ok, licenses ok, sources ok`, with duplicate-version warnings only (`base64`, `hashbrown`, `logos`, `logos-codegen`, `logos-derive`, `syn`, `windows-sys`)

#### Scenario: a new CC0 crate is refused
- **WHEN** a dependency other than `notify` declares `CC0-1.0`
- **THEN** `cargo deny check licenses` fails with `rejected` for that crate

### Requirement: Python pins and the npm production graph are audited in CI
The CI job `audit` SHALL run `pip-audit` 2.10.1 with `--no-deps --strict` over `kernel-sidecar/requirements.txt` and over `uv export --frozen --no-dev --no-emit-project` of `clients/python` and `kernel-sidecar`, and SHALL run `pnpm audit --prod --audit-level moderate` in `clients/typescript` after `pnpm install --frozen-lockfile`. A runtime finding SHALL be fixed by bumping in the same pull request when a patched version exists, otherwise ignored explicitly (`--ignore-vuln <id>` or pnpm `auditConfig`) with an issue.

#### Scenario: clean today
- **WHEN** the `audit` job runs on the pins of this change
- **THEN** every `pip-audit` invocation prints `No known vulnerabilities found` and `pnpm audit --prod` prints `No known vulnerabilities found`

#### Scenario: dev-only finding does not block
- **WHEN** `pnpm audit` reports a moderate advisory reachable only through a `devDependency`
- **THEN** the `audit` job stays green and the weekly workflow reports it

### Requirement: Weekly audit workflow
`.github/workflows/audit.yml` SHALL run on a weekly schedule and on `workflow_dispatch` with `permissions: contents: read`, and SHALL run cargo-deny advisories with a fresh database, the same `pip-audit` invocations as CI, `pnpm audit --prod --audit-level moderate` and `pnpm audit --audit-level high` over every scope.

#### Scenario: weekly run
- **WHEN** the schedule fires
- **THEN** three jobs (`rust`, `python`, `node`) run and a new advisory published since the last CI run turns the corresponding job red without any code change

