## MODIFIED Requirements

### Requirement: cargo-deny checks licences, advisories, sources and bans
A `deny.toml` at the repository root SHALL allow exactly the licences `MIT`, `Apache-2.0`, `BSD-2-Clause`, `BSD-3-Clause`, `ISC`, `Unicode-3.0`, `Zlib` and `MPL-2.0` (confidence threshold 0.8), with a single named exception allowing `CC0-1.0` for the crate `notify`; SHALL enable RustSec advisories with `yanked = "deny"` and an empty `ignore` list; SHALL allow only the crates.io registry (`unknown-registry = "deny"`, `unknown-git = "deny"`); SHALL set `multiple-versions = "warn"`, `wildcards = "deny"` and `allow-wildcard-paths = true`; and SHALL list the targets `aarch64-unknown-linux-musl`, `x86_64-unknown-linux-gnu` and `x86_64-pc-windows-gnu`. A CI job `deny` SHALL install cargo-deny 0.20.2 with the local composite action `.github/actions/cargo-deny` (release binary checked against a pinned sha256) and run `cargo deny --log-level warn --manifest-path ./Cargo.toml --all-features check`, the same invocation the former `EmbarkStudios/cargo-deny-action` made; `audit.yml`'s `rust` job SHALL do the same with `check advisories`. `make lint` SHALL run `cargo deny check` when the tool is on `PATH`. An advisory finding SHALL be fixed by bumping the crate in the same pull request when a patched version exists, otherwise recorded as an `ignore` entry with the advisory id, reason and date, plus an issue.

#### Scenario: current graph passes
- **WHEN** `cargo deny check` runs against the workspace as of this change
- **THEN** it reports `advisories ok, bans ok, licenses ok, sources ok`, with duplicate-version warnings only (`base64`, `hashbrown`, `logos`, `logos-codegen`, `logos-derive`, `syn`, `windows-sys`)

#### Scenario: a new CC0 crate is refused
- **WHEN** a dependency other than `notify` declares `CC0-1.0`
- **THEN** `cargo deny check licenses` fails with `rejected` for that crate

#### Scenario: a tampered cargo-deny binary never runs
- **WHEN** the downloaded cargo-deny archive does not match `CARGO_DENY_SHA256`
- **THEN** the `sha256sum -c` of `.github/actions/cargo-deny` fails and no `cargo deny` command runs
