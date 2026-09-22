# community-health Specification

## Purpose
TBD - created by archiving change m7-oss-hygiene. Update Purpose after archive.
## Requirements
### Requirement: Contributing guide with DCO, gates and the project rules
The repository SHALL have a Spanish `CONTRIBUTING.md` that contains, in this order: the read order (`CLAUDE.md`, `SPEC.md`, `ARCHITECTURE.md`, `AWS_API_NOTES.md`, `MILESTONES.md`) and the four hard rules (never invent an AWS parameter; the `.proto` is the source of truth; one milestone at a time; a milestone never closes on mocks) plus ARM64-only; the OpenSpec flow (`openspec/changes/<name>/` with `proposal.md`, `design.md`, `tasks.md` and delta specs, `openspec validate <name> --strict --no-interactive` before implementing, ticking `tasks.md`, `openspec archive <name> --yes` after acceptance); the toolchain and every gate command for Rust, the Python client, the kernel sidecar, TypeScript and the docs site copied from `.github/workflows/ci.yml`, with a Linux/WSL2 section and a Windows section (no `make`, run the Makefile steps by hand, toolchain outside `C:`, the `RUSTUP_HOME` / `CARGO_HOME` / `PATH` exports and zig linker variables, `pnpm` only, `uv`); the code conventions (English identifiers, Spanish only in user-facing text, comment WHY, no inline comments in function bodies, no ticket tags, clippy pedantic, no `unwrap`/`expect`/`panic` outside tests, typed Python with sync/async parity, strict TypeScript, never log file contents, executed code, PTY bytes, tokens or hook bodies); commits and PRs (Conventional Commits prefixes, branch naming, **DCO** with `git commit -s` and the Developer Certificate of Origin 1.1 quoted verbatim, no force-push to `main`, the PR checklist, e2e runs cost money and the PR says whether one ran); and where to report bugs, features, vulnerabilities and conduct issues.

#### Scenario: a newcomer can run the gates from the guide alone
- **WHEN** a contributor on WSL2 follows only the commands in `CONTRIBUTING.md`
- **THEN** the commands are the same strings as the corresponding `run:` steps of `.github/workflows/ci.yml` and each exits 0 on the accepted tree

#### Scenario: sign-off is documented
- **WHEN** a reader searches `CONTRIBUTING.md` for `Signed-off-by`
- **THEN** the DCO 1.1 text is present verbatim and `git commit -s` is shown as the way to add the trailer

### Requirement: Code of Conduct is Contributor Covenant 3.0
The repository SHALL have `CODE_OF_CONDUCT.md` containing the Contributor Covenant 3.0 text verbatim (sections Our Pledge, Encouraged Behaviors, Restricted Behaviors, Reporting an Issue, Addressing and Repairing Harm, Scope, Attribution, with the CC BY-SA 4.0 attribution kept), where the only edits are the replacement of the two bracketed `[NOTE: …]` placeholders: the reporting means (email to the address published on the maintainer's GitHub profile, or GitHub Support when the report concerns the maintainer) and a sentence adopting the suggested remedies as written; and a one-line Spanish preface above the title pointing to `CONTRIBUTING.md`.

#### Scenario: placeholders are filled
- **WHEN** a reader searches `CODE_OF_CONDUCT.md` for `[NOTE`
- **THEN** there is no match, the "Reporting an Issue" section names a concrete reporting means, and the Attribution section still credits Contributor Covenant 3.0 and CC BY-SA 4.0

### Requirement: SECURITY.md starts with how to report a vulnerability
`SECURITY.md` SHALL open, before `## Modelo`, with a section `## Reportar una vulnerabilidad` that (a) directs reports to GitHub Private Vulnerability Reporting at `https://github.com/alejandro-cedeno-10/rayito/security/advisories/new` and forbids public issues and pasting JWEs, access tokens or `runHookPayload`s; (b) lists what a useful report contains (SDK and version, `agent_version` from `get_health()`, image name and version, region, minimal reproduction); (c) has a supported-versions table stating that only the latest published release of each component (`rayito` on PyPI, `rayito` on npm, the `rayito-base` image with its `rayd`) receives fixes, with the current versions; (d) states acknowledgement within 7 days and a 90-day coordinated-disclosure window (earlier if a fix is released), with credit on request; (e) states there is no bug bounty. Everything from `## Modelo` to the end of the file SHALL be unchanged by this change.

#### Scenario: threat model untouched
- **WHEN** the pre-change and post-change `SECURITY.md` are compared from the line `## Modelo` onward
- **THEN** the only difference is the "Código vendorizado" row of the supply-chain table gaining "y listado en `NOTICE`"

#### Scenario: reporting section content
- **WHEN** a reporter reads the first section of `SECURITY.md`
- **THEN** they find the advisory URL, the list of fields to include, the supported-versions table, the 7-day and 90-day figures and the no-bounty statement

### Requirement: Governance, CODEOWNERS, issue and PR templates, Dependabot
The repository SHALL have: `GOVERNANCE.md` (Spanish; single maintainer `@alejandro-cedeno-10`; decisions in writing as numbered ADRs in `ARCHITECTURE.md` and scoped work as OpenSpec changes; the path to maintainership; revisited at three maintainers); `.github/CODEOWNERS` with `*`, `/proto/`, `/crates/`, `/clients/python/`, `/clients/typescript/` and `/AWS_API_NOTES.md` owned by `@alejandro-cedeno-10`; `.github/ISSUE_TEMPLATE/bug_report.yml` (form with SDK dropdown, SDK version, `agent_version`, image name and version, region, redacted `stateReason`, reproduction, expected vs actual, logs with a no-secrets reminder, and a not-a-vulnerability checkbox), `feature_request.yml` (problem, proposal, E2B equivalent and its `e2b-compat.md` status, AWS primitive needed, willingness to write the OpenSpec change) and `config.yml` (`blank_issues_enabled: false`, contact links to the security advisory form and the docs); `.github/PULL_REQUEST_TEMPLATE.md` with a checklist covering the linked OpenSpec change with ticked tasks, `buf breaking` clean or justified, gates run locally and on which OS, e2e against AWS run with cost noted or "sin cambio de runtime", no secrets or file contents in logs, tests or fixtures, docs updated, the package changelog updated, and every commit signed off; and `.github/dependabot.yml` version 2 with weekly entries `cargo` at `/`, `uv` at `/clients/python` and `/kernel-sidecar`, `pip` at `/kernel-sidecar`, `npm` at `/clients/typescript`, `github-actions` at `/`, `docker` at `/image`, each with one group (`patterns: ["*"]`, `update-types: ["minor", "patch"]`), `open-pull-requests-limit: 5` and commit prefix `chore(deps)`. All YAML files SHALL parse.

#### Scenario: templates parse and cover the fields
- **WHEN** `uv run --with pyyaml python -c "import sys, yaml; [yaml.safe_load(open(p, encoding='utf-8')) for p in sys.argv[1:]]" .github/ISSUE_TEMPLATE/*.yml .github/dependabot.yml` runs
- **THEN** it exits 0, `bug_report.yml` has required fields for SDK, version, `agent_version`, image and reproduction, and `dependabot.yml` lists exactly seven `updates` entries

#### Scenario: PR template checklist
- **WHEN** a contributor opens a pull request on GitHub
- **THEN** the description is pre-filled with the checklist above and every item is an unchecked `- [ ]`

### Requirement: Changelogs, badges and the release runbook
`clients/typescript/CHANGELOG.md` and `crates/rayd/CHANGELOG.md` SHALL exist in Keep a Changelog 1.1.0 format (Spanish, `[Unreleased]` first, a dated entry for the current version: `0.0.5` unpublished for TypeScript, `0.1.0` for `rayd` with the `agent_version` / `rayito-base` ≥ 10.0 compatibility line), both `[Unreleased]` sections and `clients/python/CHANGELOG.md` `[Unreleased]` recording the MIT → Apache-2.0 change; a root `CHANGELOG.md` SHALL point to the three component changelogs and state the tag prefixes `python-v`, `typescript-v`, `rayd-v`. `README.md` SHALL show four badges under the title in the order CI, PyPI, npm, licence. `docs/RELEASING.md` SHALL document the manual release steps: what is published under which tag; PyPI Trusted Publisher registration (project `rayito`, repo `alejandro-cedeno-10/rayito`, workflow `release.yml`, environment `pypi`, pending publisher allowed before the project exists); npm prerequisites (npm CLI ≥ 11.5.1, Node ≥ 22.14.0, `id-token: write`, automatic provenance), the manual first publish by the owner (`pnpm pack` then `npm publish <tgz> --access public` with a granular token and 2FA) and the trusted-publisher configuration afterwards (workflow `release-npm.yml`, environment `npm`, delivered by `m7-supply-chain`); crates.io deferred with `cargo publish --dry-run -p rayito-proto` as the rehearsal; one-time GitHub settings (Private Vulnerability Reporting, DCO app as required check, branch protection, environments `pypi` and `npm`); and a per-component release checklist. It SHALL state explicitly that no name is reserved and nothing is published by this change and that the badges render "not found" until the first publish.

#### Scenario: release runbook is complete
- **WHEN** a maintainer follows `docs/RELEASING.md` for the Python package
- **THEN** every step is either an existing command in the repo (`uv build`, `scripts/check_wheel.py`, `git tag python-v<version>`) or a named GitHub/PyPI UI action, and no step requires an API token

#### Scenario: changelog format
- **WHEN** `clients/typescript/CHANGELOG.md` and `crates/rayd/CHANGELOG.md` are opened
- **THEN** each starts with the Keep a Changelog preamble, has `## [Unreleased]` first with the licence change under `### Changed`, and a dated version heading matching the manifest version

