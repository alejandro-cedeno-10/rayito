# OpenSSF Best Practices badge: registration checklist and answers

Ready-to-paste answers for the **passing** level of the OpenSSF Best
Practices badge (<https://www.bestpractices.dev>), checked against `main` on
2026-10-05 (after 0.7.0). The criteria list is the upstream
`criteria/criteria.yml` of `coreinfrastructure/best-practices-badge`, level
`0`: 67 criteria (43 MUST, 10 SHOULD, 14 SUGGESTED).

Only the maintainer can register the project: the site logs in with the
GitHub account that owns the repository. Nothing here has been submitted.
The reasoning behind each answer is in
[`2026-10-oss-standards-audit.md`](2026-10-oss-standards-audit.md) §5.

## Contents

- Steps for the maintainer
- Answers, criterion by criterion
- After registration
- Keeping the answers true

## Steps for the maintainer

1. Open <https://www.bestpractices.dev/en/projects/new> and log in with
   GitHub (the repository owner's account).
2. Pick `alejandro-cedeno-10/rayito` from the repository list, or paste
   `https://github.com/alejandro-cedeno-10/rayito`. Submit. The site creates
   the project, shows its numeric id in the URL
   (`/en/projects/<id>`) and pre-fills some answers from GitHub.
3. Fill the basic fields:
   - **Name**: `Rayito`
   - **Description**: `Python and TypeScript SDK (E2B-compatible) for
     hardware-isolated AI-agent sandboxes on AWS Lambda MicroVMs in your own
     AWS account, with a Rust in-VM agent (rayd).`
   - **Project URL**: `https://alejandro-cedeno-10.github.io/rayito/`
   - **Repository URL**: `https://github.com/alejandro-cedeno-10/rayito`
   - **Main implementation languages**: `Rust, Python, TypeScript`
   - **License**: `Apache-2.0`
4. Go section by section (Basics, Change Control, Reporting, Quality,
   Security, Analysis). For each criterion set the status in the table below
   and paste the justification into its text box. Where the table gives a
   URL, keep it in the text: criteria marked "URL" in the table are refused
   without one.
5. Press **Save** at the end of every section; the page keeps a draft but
   the badge is only recomputed on save.
6. When every MUST is Met or N/A, the badge turns to "passing". Copy the
   project id from the URL.
7. Open a PR that moves the commented badge in `README.md` (the HTML
   comment right under the badge line) to the end of the badge line,
   replacing `<id>` twice, deletes the comment, and records the project id
   in §5 of the audit. Scorecard's `CII-Best-Practices` check picks it up
   on its next run.

## Answers, criterion by criterion

Status values are those of the form: **Met**, **Unmet**, **N/A**. "URL"
marks a criterion whose Met answer needs a URL.

### Basics

| Criterion | Level | Status | Justification to paste |
|---|---|---|---|
| `description_good` | MUST | Met | The README and the docs home state what Rayito is in the first lines: an E2B-compatible SDK for hardware-isolated sandboxes on AWS Lambda MicroVMs in the user's own account. https://github.com/alejandro-cedeno-10/rayito#readme |
| `interact` | MUST | Met | README links to install (PyPI, npm), the docs site, CONTRIBUTING.md, SUPPORT.md and GitHub issues. https://github.com/alejandro-cedeno-10/rayito#readme |
| `contribution` | MUST, URL | Met | Contribution process (OpenSpec change, gates, PR template, review and merge): https://github.com/alejandro-cedeno-10/rayito/blob/main/CONTRIBUTING.md |
| `contribution_requirements` | SHOULD, URL | Met | Coding conventions, required gates, tests with every change, DCO sign-off and signed commits: https://github.com/alejandro-cedeno-10/rayito/blob/main/CONTRIBUTING.md#4-convenciones |
| `floss_license` | MUST | Met | Apache-2.0 for everything the project produces (SDKs, rayd, image builder). |
| `floss_license_osi` | SUGGESTED | Met | Apache-2.0 is OSI-approved. |
| `license_location` | MUST, URL | Met | https://github.com/alejandro-cedeno-10/rayito/blob/main/LICENSE (copies in each published package, plus NOTICE). |
| `documentation_basics` | MUST | Met | Getting started, how-to guides and concepts: https://alejandro-cedeno-10.github.io/rayito/primeros-pasos/ |
| `documentation_interface` | MUST | Met | API reference for Python and TypeScript, CLI and errors: https://alejandro-cedeno-10.github.io/rayito/referencia/ ; the wire contract is proto/rayito/v1/. |
| `sites_https` | MUST | Met | Project site (GitHub Pages), repository, PyPI, npm and GitHub Releases are all served over HTTPS only. |
| `discussion` | MUST | Met | GitHub issues and pull requests: searchable, URL-addressable, open to anyone with a GitHub account. https://github.com/alejandro-cedeno-10/rayito/issues |
| `english` | SHOULD | Unmet | The documentation and code comments are in Spanish. Bug reports, pull requests and security reports in English are accepted and answered (README section "In English", SUPPORT.md). |
| `maintained` | MUST | Met | Actively maintained: releases 0.3.0 to 0.7.0 between 2026-09-24 and 2026-10-05, Dependabot and CodeQL alerts triaged. |

### Change Control

| Criterion | Level | Status | Justification to paste |
|---|---|---|---|
| `repo_public` | MUST | Met | https://github.com/alejandro-cedeno-10/rayito |
| `repo_track` | MUST | Met | git, with every change landing through a pull request (merge commits keep the history of who changed what). |
| `repo_interim` | MUST | Met | Every pull request merges into `main` between releases; interim states are public. |
| `repo_distributed` | SUGGESTED | Met | git. |
| `version_unique` | MUST | Met | One tag per component and release: `python-vX.Y.Z`, `typescript-vX.Y.Z`, `rayd-vX.Y.Z`. |
| `version_semver` | SUGGESTED | Met | SemVer 2.0.0, with the 0.x rules written down: https://alejandro-cedeno-10.github.io/rayito/limits/#versionado-y-soporte |
| `version_tags` | SUGGESTED | Met | Releases are tagged in git (release-please). |
| `release_notes` | MUST, URL | Met | Human-readable release notes per component (Keep a Changelog), one section per release: https://github.com/alejandro-cedeno-10/rayito/blob/main/clients/python/CHANGELOG.md , clients/typescript/CHANGELOG.md and crates/rayd/CHANGELOG.md; each release also has a GitHub Release: https://github.com/alejandro-cedeno-10/rayito/releases |
| `release_notes_vulns` | MUST | Met | No publicly known vulnerability (CVE/GHSA) has been fixed in a release so far. The rule for when one is: a `### Security` entry naming the CVE/GHSA (docs/RELEASING.md §6, step 8). Internal hardening is already listed under `### Security` (e.g. 0.7.0). |

### Reporting

| Criterion | Level | Status | Justification to paste |
|---|---|---|---|
| `report_process` | MUST, URL | Met | Issue templates for bugs and feature requests; how to report: https://github.com/alejandro-cedeno-10/rayito/blob/main/CONTRIBUTING.md#6-c%C3%B3mo-reportar |
| `report_tracker` | SHOULD | Met | GitHub issues. |
| `report_responses` | MUST | Met | Single active maintainer; reports are acknowledged. No bug reports from outside the project have been filed yet. |
| `enhancement_responses` | SHOULD | Met | Same as above; feature requests use their own issue template. |
| `report_archive` | MUST, URL | Met | Public, searchable issue tracker: https://github.com/alejandro-cedeno-10/rayito/issues?q=is%3Aissue |
| `vulnerability_report_process` | MUST, URL | Met | https://github.com/alejandro-cedeno-10/rayito/blob/main/SECURITY.md#reportar-una-vulnerabilidad |
| `vulnerability_report_private` | MUST, URL | Met | GitHub Private Vulnerability Reporting is enabled: https://github.com/alejandro-cedeno-10/rayito/security/advisories/new (process in SECURITY.md). |
| `vulnerability_report_response` | MUST | Met | Acknowledgement within 7 days, fix or mitigation within 90 days, coordinated disclosure (SECURITY.md). No vulnerability report has been received yet. |

### Quality

| Criterion | Level | Status | Justification to paste |
|---|---|---|---|
| `build` | MUST | Met | `make build` (rayd, static musl via cargo-zigbuild), `uv build` (Python), `pnpm build` (TypeScript); all reproduced in public CI. |
| `build_common_tools` | SUGGESTED | Met | cargo, uv, pnpm and make. |
| `build_floss_tools` | SHOULD | Met | The whole toolchain is FLOSS (Rust, zig, Python, uv, Node.js, pnpm, protoc/buf). |
| `test` | MUST | Met | Automated suites: `cargo test --workspace` (unit and integration), `pytest tests/unit`, `vitest`; e2e suites against real AWS. Documented in CONTRIBUTING.md §3. |
| `test_invocation` | SHOULD | Met | Standard invocations: `cargo test`, `uv run pytest`, `pnpm test` (CONTRIBUTING.md §3). |
| `test_most` | SUGGESTED | Met | Unit tests cover the SDK domain, services and adapters (with fakes) and the agent; integration tests run rayd on Linux; e2e tests cover every public feature on real AWS before an OpenSpec change is archived. |
| `test_continuous_integration` | SUGGESTED | Met | GitHub Actions on every pull request (x86_64 and aarch64, Python 3.11/3.12/3.13, TypeScript, docs, cargo-deny, dependency audit, gitleaks, CodeQL); these checks are required by the `main` ruleset. https://github.com/alejandro-cedeno-10/rayito/actions/workflows/ci.yml |
| `test_policy` | MUST | Met | "Tests con cada cambio": new functionality and fixes come with tests in the same PR, and runtime changes also with real-AWS acceptance. https://github.com/alejandro-cedeno-10/rayito/blob/main/CONTRIBUTING.md#5-commits-y-prs |
| `tests_are_added` | MUST | Met | Recent changes add tests, e.g. https://github.com/alejandro-cedeno-10/rayito/pull/113 (SDK client hardening, unit tests in both SDKs) and https://github.com/alejandro-cedeno-10/rayito/pull/121 (rayd test race). |
| `tests_documented_added` | SUGGESTED | Met | Same policy (CONTRIBUTING.md §5) plus the "tests" checkbox in the PR template. |
| `warnings` | MUST | Met | clippy pedantic with `-D warnings` (unwrap/expect/panic denied outside tests), ruff, mypy `--strict`, Biome, tsc `strict` with `exactOptionalPropertyTypes` and `noUncheckedIndexedAccess`. |
| `warnings_fixed` | MUST | Met | CI fails on any warning; nothing merges with one. |
| `warnings_strict` | SUGGESTED | Met | Same as above: the strictest practical settings of each linter. |

### Security

| Criterion | Level | Status | Justification to paste |
|---|---|---|---|
| `know_secure_design` | MUST | Met | The maintainer keeps a threat model with mitigations and residual risk (SECURITY.md, threats T1-T28), an internal security audit (docs/SECURITY_AUDIT.md) and applies least privilege, defense in depth and fail-safe defaults (cost and network features off by default). |
| `know_common_errors` | MUST | Met | Same documents cover injection, path traversal, SSRF, confused deputy, DNS rebinding, request smuggling and secret leakage in logs, with tests for each mitigation. |
| `crypto_published` | MUST | Met | Only published algorithms and protocols: TLS 1.2/1.3 (rustls), HMAC-SHA256, SHA-256, AWS SigV4; proxy tokens are JWE issued by AWS. |
| `crypto_call` | SHOULD | Met | No cryptography is implemented by the project; it calls rustls/aws-lc-rs, the `hmac`/`sha2` crates, Python `hmac`/`hashlib`/`secrets`, Node `crypto` and the AWS SDKs. |
| `crypto_floss` | MUST | Met | All cryptographic libraries used are FLOSS. |
| `crypto_keylength` | MUST | Met | 256-bit secrets for HMAC and access tokens (minimum 16 bytes for caller-supplied tokens), TLS 1.2+ with library defaults. |
| `crypto_working` | MUST | Met | No broken algorithms (no MD5, SHA-1, DES or RC4) for security purposes. |
| `crypto_weaknesses` | SHOULD | Met | Same: only SHA-256-based constructions and modern TLS cipher suites. |
| `crypto_pfs` | SHOULD | Met | TLS through rustls, which only offers ECDHE key exchange (TLS 1.2) and TLS 1.3. |
| `crypto_password_storage` | MUST | N/A | Rayito does not store user passwords. |
| `crypto_random` | MUST | Met | Tokens and secrets come from the OS CSPRNG (Python `secrets`, Node `crypto.randomBytes`, Rust `getrandom`). |
| `delivery_mitm` | MUST | Met | Delivered over HTTPS only (PyPI, npm, GitHub Releases), with PyPI attestations (PEP 740), npm provenance and cosign keyless signatures plus SHA256SUMS for rayd and the image zip. |
| `delivery_unsigned` | MUST | Met | Hashes are only obtained over HTTPS (SHA256SUMS on the Release, `--require-hashes` pins in the image build); signatures are verified with cosign (https://alejandro-cedeno-10.github.io/rayito/verify/). |
| `vulnerabilities_fixed_60_days` | MUST | Met | No unpatched vulnerability of medium or higher severity has been publicly known for more than 60 days. The only Dependabot alert so far (GHSA-8988-4f7v-96qf, medium, dev-only) was fixed in 3 days; 0 open. |
| `vulnerabilities_critical_fixed` | SHOULD | Met | Policy: critical vulnerabilities are fixed in a patch of the supported line as fast as possible (SECURITY.md, deny.toml); Dependabot security updates are enabled. |
| `no_leaked_credentials` | MUST | Met | No valid private credentials in the repository. GitHub secret scanning with push protection is on; CI runs gitleaks over the whole history and `scripts/check_hygiene.py` (plus a private denylist) over files and PR text. |

### Analysis

| Criterion | Level | Status | Justification to paste |
|---|---|---|---|
| `static_analysis` | MUST | Met | CodeQL (Rust, Python, JavaScript/TypeScript, GitHub Actions) on every PR and push to main, plus clippy pedantic, ruff, mypy --strict, Biome, cargo-deny and actionlint in CI. |
| `static_analysis_common_vulnerabilities` | SUGGESTED | Met | CodeQL security queries. |
| `static_analysis_fixed` | MUST | Met | 0 open CodeQL alerts: 6 fixed, 6 dismissed with a written reason (test-only code or a dev-only simulator). https://github.com/alejandro-cedeno-10/rayito/security/code-scanning |
| `static_analysis_often` | SUGGESTED | Met | CodeQL runs on every PR and push; Scorecard weekly. |
| `dynamic_analysis` | SUGGESTED | Unmet | No fuzzing yet. Planned: cargo-fuzz targets for the pure parsers in rayd-core (audit §8 #8). |
| `dynamic_analysis_unsafe` | SUGGESTED | N/A | The code is written in memory-safe languages (Rust, Python, TypeScript). The few `unsafe` blocks in rayd adapters wrap libc FFI (PTY, FUSE) and are covered by integration tests on Linux. |
| `dynamic_analysis_enable_assertions` | SUGGESTED | Met | Tests run debug builds with `debug_assert!` enabled, and the integration tests assert invariants of the running agent. |
| `dynamic_analysis_fixed` | MUST | N/A | No dynamic analysis tool is run yet, so there are no findings to fix (see `dynamic_analysis`). |

## After registration

`README.md` carries the badge, commented out, right under the badge line.
Append it to the end of that line with both `<id>` replaced, and delete the
comment:

```markdown
[![OpenSSF Best Practices](https://www.bestpractices.dev/projects/<id>/badge)](https://www.bestpractices.dev/projects/<id>)
```

## Keeping the answers true

- **Supported versions.** `SECURITY.md` names the supported `MAJOR.MINOR`
  line; bump it with every minor release.
- **`vulnerabilities_fixed_60_days` and `static_analysis_fixed`** stay Met
  only while Dependabot and CodeQL alerts are triaged: fixed, or dismissed
  with a reason.
- **Signed releases and provenance.** If the release workflow changes,
  re-check `delivery_mitm` and `delivery_unsigned`.
- **`english`** turns Met with an English quickstart on the docs site
  (audit §8 #13); **`dynamic_analysis`** with fuzzing (audit §8 #8).
