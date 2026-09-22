## Why

M0–M6 are accepted against real AWS and `rayito` 0.1.0 is release-ready, but
the repository cannot be opened to the public as it stands.
`docs/research/2026-09-m7-oss-readiness.md` (the M7 research report, §0 and
§1) found that the licence is declared as **MIT in five places** (root
`Cargo.toml` `[workspace.package]`, `clients/python/pyproject.toml` with a
`License ::` classifier, `kernel-sidecar/pyproject.toml`,
`clients/typescript/package.json` and `clients/typescript/LICENSE`) while the
owner's final decision is **Apache-2.0**; there is no root `LICENSE`, no
`NOTICE` crediting the vendored MIT `e2b_charts`, no `CONTRIBUTING.md`,
`CODE_OF_CONDUCT.md`, `GOVERNANCE.md`, `CODEOWNERS`, issue or PR templates,
no Dependabot configuration, `SECURITY.md` has a threat model but no way to
report a vulnerability, `clients/typescript` and `crates/rayd` have no
changelog, and the wheel and npm tarball checks do not assert the licence
metadata they ship. Separately, the owner asked for the architecture to be
readable at a glance by newcomers: which process runs where inside the
MicroVM, that the SDK never runs inside the sandbox, and which languages can
use Rayito.

Report §5 lists `m7-oss-hygiene` as item 1 of M7 and, together with
`m7-supply-chain`, the gate for going public.

## What Changes

Track 1 of M7 (report §0 + §1), decided in full by `design.md`:

- **Apache-2.0 everywhere.** `license = "Apache-2.0"` in the root
  `Cargo.toml` (plus `repository`/`homepage`), PEP 639 metadata in
  `clients/python/pyproject.toml` (`license = "Apache-2.0"`,
  `license-files = ["LICENSE", "NOTICE"]`, the `License :: OSI Approved ::
  MIT License` classifier **deleted**), `kernel-sidecar/pyproject.toml`,
  `clients/typescript/package.json` (`"license"`, `files` gains `LICENSE`
  and `NOTICE`, `repository`), and `clients/typescript/LICENSE` replaced by
  the Apache-2.0 text.
- **Licence files.** Root `LICENSE` with the verbatim Apache-2.0 text
  (sha256 `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30`,
  fetched from apache.org on 2026-09-16), copied byte-identical into
  `clients/python`, `clients/typescript` and `crates/rayd`; root `NOTICE`
  (Rayito copyright 2026 + `e2b_charts` 1.0.0 MIT attribution;
  `VENDORED.md` kept) copied into `clients/python` and
  `clients/typescript`. `scripts/check_license.py` asserts the five
  manifests and the copies, and runs in `make lint` and CI.
- **Packaging proof.** `scripts/check_wheel.py` additionally asserts
  `License-Expression: Apache-2.0`, `License-File: LICENSE`, `License-File:
  NOTICE`, both files under `*.dist-info/licenses/` and no `License ::`
  classifier (verified 2026-09-16 on a probe project: `uv_build` 0.7.21
  emits Metadata-Version 2.4 exactly so);
  `clients/typescript/scripts/pack-check.mjs` additionally requires
  `package/NOTICE`.
- **Community health files.** `CONTRIBUTING.md` (DCO with `git commit -s`,
  how to run every gate on Windows and WSL2, proto-is-source-of-truth,
  no-mock milestones, conventional commits, the OpenSpec flow),
  `CODE_OF_CONDUCT.md` (Contributor Covenant 3.0 verbatim with the
  reporting section filled in), a "Reportar una vulnerabilidad" section at
  the top of `SECURITY.md` (GitHub Private Vulnerability Reporting,
  supported versions, 90-day window, no bounty; threat model untouched),
  `GOVERNANCE.md` (single maintainer, ADR-driven, path to maintainership),
  `.github/CODEOWNERS`, `.github/ISSUE_TEMPLATE/{bug_report.yml,
  feature_request.yml, config.yml}`, `.github/PULL_REQUEST_TEMPLATE.md`
  with the report's checklist, `.github/dependabot.yml` (`cargo`, `uv`,
  `pip`, `npm`, `github-actions`, `docker`, grouped per ecosystem), README
  badges (CI, PyPI, npm, licence), `clients/typescript/CHANGELOG.md` and
  `crates/rayd/CHANGELOG.md` (Keep a Changelog 1.1.0) plus a top-level
  `CHANGELOG.md` pointer, and `docs/RELEASING.md` with the exact manual
  release steps (PyPI Trusted Publisher, tags `python-v*` / `typescript-v*`
  / `rayd-v*`, npm trusted-publishing prerequisites, crates.io later).
- **Architecture readability.** `ARCHITECTURE.md` gains a "Qué corre
  dónde" section (ASCII process diagram inside the MicroVM, a
  piece / language / where it runs / who uses it table, and one paragraph
  on why the sidecar is Python per ADR-002); the root `README.md` gains a
  short "Cómo funciona" section with a client ↔ VM diagram stating that the
  SDK never runs inside the sandbox and which languages can use Rayito;
  `docs/site/docs/concepts.md` gains the expanded version with the
  languages comparison table vs E2B, Daytona and Modal from report §2.

**Not in this change** (owner calls or other M7 changes): reserving
`rayito` on PyPI, npm or crates.io and any actual publish; the npm release
workflow, release-please, SHA-pinned actions, `cargo-deny`, SBOMs, cosign
and the OIDC e2e workflow (`m7-supply-chain`); enabling Private
Vulnerability Reporting, the DCO app and branch protection in GitHub
settings (documented as manual steps); SPDX headers or REUSE (the report
recommends against them at this size); version bumps (0.2.0 lockstep is
`m7-supply-chain`); any change to `rayd`, the sidecar or SDK runtime
behaviour. No AWS API is touched, so `AWS_API_NOTES.md` does not change.

## Capabilities

### New Capabilities
- `oss-licensing`: Apache-2.0 declared in the five manifests, the
  `LICENSE`/`NOTICE` files and their copies, the `check_license.py` gate,
  and the wheel and npm tarball licence assertions.
- `community-health`: `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, the
  vulnerability-reporting section of `SECURITY.md`, `GOVERNANCE.md`,
  `CODEOWNERS`, issue and PR templates, Dependabot, badges, per-package
  changelogs and `docs/RELEASING.md`.
- `architecture-docs`: the "Qué corre dónde" section of `ARCHITECTURE.md`,
  the "Cómo funciona" section of `README.md` and the expanded
  `docs/site/docs/concepts.md` with the languages table.

### Modified Capabilities
- `python-release`: the package metadata requirement drops the MIT
  classifier in favour of PEP 639 `license` / `license-files`; the wheel
  checks requirement adds the licence metadata and file assertions.
- `typescript-sdk`: the package layout requirement's tarball listing adds
  `NOTICE` (and `LICENSE` now holds the Apache-2.0 text), and
  `pack-check.mjs` asserts it.

## Impact

- New files: `LICENSE`, `NOTICE`, `CHANGELOG.md`, `CONTRIBUTING.md`,
  `CODE_OF_CONDUCT.md`, `GOVERNANCE.md`, `.github/CODEOWNERS`,
  `.github/ISSUE_TEMPLATE/bug_report.yml`,
  `.github/ISSUE_TEMPLATE/feature_request.yml`,
  `.github/ISSUE_TEMPLATE/config.yml`, `.github/PULL_REQUEST_TEMPLATE.md`,
  `.github/dependabot.yml`, `clients/python/LICENSE`,
  `clients/python/NOTICE`, `clients/typescript/NOTICE`,
  `clients/typescript/CHANGELOG.md`, `crates/rayd/LICENSE`,
  `crates/rayd/CHANGELOG.md`, `docs/RELEASING.md`,
  `scripts/check_license.py`, `scripts/tests/test_check_license.py`.
- Edited: `Cargo.toml`, `clients/python/pyproject.toml`,
  `kernel-sidecar/pyproject.toml`, `clients/typescript/package.json`,
  `clients/typescript/LICENSE` (MIT → Apache-2.0 text),
  `clients/typescript/scripts/pack-check.mjs`, `scripts/check_wheel.py`,
  `clients/python/tests/unit/test_packaging.py` (classifier assertion),
  `SECURITY.md` (new top section), `README.md` (badges + "Cómo funciona"),
  `ARCHITECTURE.md` ("Qué corre dónde"), `docs/site/docs/concepts.md`,
  `Makefile` (`lint` runs `check_license.py`),
  `.github/workflows/ci.yml` (same), `MILESTONES.md` (M7 section with the
  report's change list and this change's row), `clients/python/CHANGELOG.md`
  (`Unreleased`: licence change, pointer to `docs/RELEASING.md`).
- Behaviour: none at runtime. `rayd`, the sidecar, the image and both SDKs
  execute exactly as in 0.1.0; only metadata, documentation and gates
  change. Wheel `METADATA` moves from `License: MIT` + classifier to
  Metadata-Version 2.4 with `License-Expression: Apache-2.0` (PyPI accepts
  it; `uvx twine check` passes on the probe wheel).
- Gates after the change: every existing gate unchanged, plus
  `python scripts/check_license.py`, the extended `check_wheel.py` and
  `pnpm pack:check`; `mkdocs build --strict` still green with the expanded
  `concepts.md`.
