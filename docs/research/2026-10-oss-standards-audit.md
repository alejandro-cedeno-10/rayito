# Rayito: open-source library standards audit (2026-10)

How Rayito is built and published as an open-source library: licensing,
third-party compliance, community health files, release standards and the
OpenSSF Best Practices badge. The audit was run against `main` at `4dc0ee0`,
2026-10-03. The fixes that were clearly correct and low risk landed in the
`chore/oss-standards` PR; everything else is ranked in §8.

Scope: the Python package `rayito` (PyPI), the TypeScript package `rayito`
(npm), and the Rust agent `rayd`. `rayd` ships with `rayito-image.zip` as
cosign-signed GitHub Release assets. No AWS resource was used.

Primary sources: OpenSSF Best Practices criteria
(<https://www.bestpractices.dev/en/criteria/0>), OpenSSF Scorecard checks
(<https://github.com/ossf/scorecard/blob/main/docs/checks.md>), REUSE 3.3
(<https://reuse.software/spec-3.3/>), SPDX license list
(<https://spdx.org/licenses/>), choosealicense.com, GitHub community health
files
(<https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions>),
PyPA core metadata and PEP 639
(<https://packaging.python.org/en/latest/specifications/core-metadata/>),
npm `package.json`
(<https://docs.npmjs.com/cli/v11/configuring-npm/package-json>), the Cargo
manifest reference and Rust API guidelines C-METADATA
(<https://rust-lang.github.io/api-guidelines/documentation.html#c-metadata>),
Keep a Changelog 1.1.0, SemVer 2.0.0, DCO 1.1
(<https://developercertificate.org/>), Apache-2.0 §4
(<https://www.apache.org/licenses/LICENSE-2.0>), and SLSA v1.0
(<https://slsa.dev/spec/v1.0/levels>).

Legend: **OK** compliant · **GAP** open (ranked in §8) · **FIXED** fixed in
the `chore/oss-standards` PR.

## 1. License

| Item | Status | Evidence / fix |
|---|---|---|
| Choice: Apache-2.0 for an SDK | OK | Apache-2.0 is permissive and OSI-approved, with an explicit patent grant (§3) and a NOTICE mechanism (§4(d)). It suits an SDK that companies embed, and E2B's SDKs use the same license. **No license change is recommended.** |
| `LICENSE` text exact | OK | Byte-identical to `https://www.apache.org/licenses/LICENSE-2.0.txt` (diffed during this audit; sha256 is pinned in `scripts/check_license.py:28`). The copies in `clients/python/`, `clients/typescript/` and `crates/rayd/` are byte-identical. |
| Root / Cargo | OK, FIXED | `Cargo.toml:9` `license = "Apache-2.0"`, `rust-version` 1.98 (`:7`). Every crate inherits `license`. The crates now also inherit `repository` and `homepage` (C-METADATA). |
| Python PEP 639 | OK | `clients/python/pyproject.toml:7-8`: `license = "Apache-2.0"`, `license-files = ["LICENSE", "NOTICE"]`, and no `License ::` classifier. The built wheel has `Metadata-Version: 2.4`, `License-Expression: Apache-2.0`, two `License-File` entries, and `dist-info/licenses/{LICENSE,NOTICE}`. The sdist ships both files. |
| npm | OK | `clients/typescript/package.json:5` `"license": "Apache-2.0"`, and `files` includes `LICENSE` and `NOTICE`. `pnpm pack` contains `package/LICENSE` and `package/NOTICE` (asserted by `scripts/pack-check.mjs`). |
| `kernel-sidecar` | OK | `license = "Apache-2.0"`, with the `Private :: Do Not Upload` classifier. It is never published. |
| README badge | OK | Apache-2.0 badge linking to `LICENSE` (`README.md:3`). |
| Docs site license | FIXED | `docs/site/mkdocs.yml:10` adds a `copyright` footer: Apache-2.0, Contribuir, Seguridad, Código de conducta, and a non-affiliation line. |
| NOTICE where required | OK, FIXED | The root `NOTICE` is copied byte-identical into both packages. The E2B line now keeps upstream's copyright ("Copyright 2023 FoundryLabs, Inc.") and says the code was adapted "with changes", as Apache-2.0 §4(b) and §4(c) require (see §2.3). |
| SPDX headers | GAP (low) | There are no per-file `SPDX-License-Identifier` headers. Package metadata already carries the SPDX expression, so this is not legally required. See §8 #10. |
| REUSE | GAP (low) | There is no `REUSE.toml` and no `LICENSES/` directory. A pragmatic subset is ready to paste in §8 #10. The m7 design explicitly deferred this, so it was not added unilaterally. |

## 2. Third-party license compliance

### 2.1 Reports

| Ecosystem | Tool | Result |
|---|---|---|
| Rust (rayd, all targets in `deny.toml`) | `cargo deny check` 0.20.2: `licenses ok`, `advisories ok`, `bans ok`, `sources ok` | 259 crates. Each is MIT, Apache-2.0, BSD-2/3, ISC, Unicode-3.0, Zlib, or dual-licensed with one of those (0BSD, BSL-1.0, Unlicense, MIT-0, CC0 `dunce`, Apache-2.0 WITH LLVM-exception). The only named exception is `notify` (CC0-1.0, `deny.toml:31`). No copyleft. |
| Python SDK runtime + all extras (`uv export --no-dev --all-extras`) | `pip-licenses` | 44 packages: MIT, BSD-2/3, Apache-2.0, ISC, PSF-2.0 (`typing_extensions`), MIT-0 (`cffi`), and `Apache-2.0 OR BSD-3-Clause` (`cryptography`). No copyleft. |
| Sidecar image pins (`requirements.txt` + `requirements-poly.txt`) | `pip-licenses` | 52 packages: BSD, MIT, ISC, PSF (`matplotlib`), MIT-CMU (`pillow`), Apache-2.0. `numpy` is `BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0`. `pyzmq` is BSD-3, and the libzmq it bundles is MPL-2.0 (file-level copyleft, unmodified, no obligation beyond keeping notices). No GPL or AGPL. |
| npm production deps (`pnpm licenses list --prod`) | pnpm | Apache-2.0 (31, the AWS SDK), MIT (9), BSD-3 (`ieee754`), ISC (`inherits`), 0BSD (`tslib`), and `Apache-2.0 AND BSD-3-Clause` (`@bufbuild/protobuf`). No copyleft. |

### 2.2 What the shipped artifacts contain

| Artifact | Contents | Obligation |
|---|---|---|
| Wheel `rayito-*.whl` / sdist | Only first-party code plus `LICENSE`, `NOTICE` and `py.typed`. Dependencies are declared, not bundled. | Met. |
| npm tarball (22 files) | `dist/*` (ESM + CJS + d.ts + maps), `README.md`, `LICENSE`, `NOTICE`, `package.json`. `tsdown` leaves every dependency external: the bundle only `import`s `@aws-sdk/*`, `@bufbuild/*`, `@connectrpc/*` and `@smithy/*`. The `"e2b"` strings are JSDoc comments. | Met. |
| `rayito-image.zip` | `Dockerfile`, the `rayd` static binary and `kernel-sidecar/` (including the vendored MIT `e2b_charts` with its `LICENSE` and `VENDORED.md`). System packages, pip wheels, Deno and mount-s3 are fetched inside the user's account at image build time and are not redistributed by us. | **DONE** (`third-party-licenses`, after 0.7.0; was a GAP): `rayd` statically links about 220 crates for `aarch64-unknown-linux-musl` (259 across every target `deny.toml` audits). MIT ("shall be included in all copies or substantial portions"), BSD-2/3 (binary-form clause), ISC and Zlib all require their notices to travel with binary redistribution. The zip now carries `licenses/LICENSE`, `licenses/NOTICE` and `licenses/THIRD_PARTY_LICENSES.md` (cargo-about), the image copies them to `/usr/share/doc/rayd/`, and the zip builder refuses a `rayd` without them. See §8 #1. |
| `rayd` release asset | Binary, `rayd.cdx.json` (CycloneDX 1.5), cosign bundles, `SHA256SUMS`, and from the release after 0.7.0 `LICENSE`, `NOTICE`, `THIRD_PARTY_LICENSES.md` and `SHA256SUMS.sigstore.json` | **DONE** (`third-party-licenses`): the notices are release assets listed in the signed `SHA256SUMS`. 0.7.0 and earlier still lack them. |

### 2.3 Vendored or copied code, and E2B

- **E2B's license**: `e2b-dev/E2B` and `e2b-dev/code-interpreter` are
  Apache-2.0 per GitHub's license API. The E2B `LICENSE` ends with "Copyright
  2023 FoundryLabs, Inc." Neither repo has a `NOTICE` file, so there is no
  upstream NOTICE text to carry over (§4(d)). (E2B's Python SDK `pyproject`
  declares `license = "MIT"`, but the repository license is Apache-2.0, and
  Rayito follows the stricter one.)
- **Adapted code**: `clients/python/src/rayito/_git_base.py:5-7` and
  `clients/typescript/src/sandbox/git-args.ts:1-8` port E2B's git
  argv/parsers. Both headers state the adaptation and point to `NOTICE`
  (the TS header now cites the source and license, **FIXED**). `NOTICE` now
  also retains E2B's copyright line (§4(c)) and says "with changes" (§4(b)),
  **FIXED**.
- **Vendored `e2b_charts` 1.0.0** (MIT, FOUNDRYLABS, INC.):
  `kernel-sidecar/src/rayito_kernel_sidecar/_vendor/e2b_charts/` has its
  `LICENSE`, and `VENDORED.md` records the upstream, the sdist sha256 and the
  one modification. It is listed in `NOTICE`. **OK**.
- **OpenTelemetry proto subset**
  (`crates/rayito-proto/vendor/opentelemetry/`, Apache-2.0): listed in
  `NOTICE`. **OK**.
- **E2B API shapes** (`rayito.e2b`, `rayito/e2b`): Rayito reimplements the
  method names and signatures for compatibility; no E2B code is copied there.
  API compatibility does not need copyright attribution, but using the E2B
  name does call for a trademark note. **FIXED**: README §"Licencia, estado
  y soporte", the package READMEs, the docs footer, and a `!!! note
  "Marcas"` admonition on `migrar-desde-e2b/index.md` and `e2b-compat.md`.
  This is nominative use only, with a non-affiliation and non-endorsement
  disclaimer.
- **Tests**: no E2B test files are copied.
  `clients/typescript/tests/unit/secrets-e2b-shim.test.ts` only mentions
  E2B docs URLs.

## 3. Community health files

GitHub's community profile scores 100 %.

| File | Status | Notes |
|---|---|---|
| `README.md` | OK, FIXED | It already covered what, why, install, quickstart, links and badges (CI, PyPI, npm, license, Scorecard, docs). Fixed in the PR: the status block claimed "M0–M7 / 0.2.0" as the current state (now "alfa (serie 0.x)", with M7 marked as history); the `RAYD_VERSION=0.5.0` example was stale (it now reads the installed SDK's version); new sections "Licencia, estado y soporte" and "In English" (OpenSSF `english`). |
| `CONTRIBUTING.md` | OK, FIXED | Already had DCO sign-off with the full DCO 1.1 text, dev setup on Linux/WSL2/macOS/Windows, and gates. Fixed: (a) it said that signing (`-S`) means accepting the DCO, which conflates the two: the `Signed-off-by` trailer accepts the DCO and the signature proves authorship; (b) "cuando el repositorio sea público" was stale; (c) it had no explicit test policy, which OpenSSF `test_policy` requires (new bullet "Tests con cada cambio", §5). |
| `CODE_OF_CONDUCT.md` | OK | Contributor Covenant 3.0, unmodified, with a reporting channel and a fallback to GitHub Support when the maintainer is the subject. |
| `SECURITY.md` | OK, FIXED | Private reporting via GitHub PVR (enabled, verified through the API). Acknowledgement in 7 days, fix within 90, coordinated disclosure. **Fixed**: the supported-versions table still listed 0.2.0. It now lists the 0.6.x line for all three components, matches the compat table in `docs/site/docs/limits.md`, and states that older lines get no fixes. |
| `SUPPORT.md` | FIXED (new) | Docs, `rayito doctor`, issues, security, no SLA, English welcome. Also linked from the issue chooser (`.github/ISSUE_TEMPLATE/config.yml`). |
| `GOVERNANCE.md` (maintainers inline) | OK | Single-maintainer model, ADRs, OpenSpec, how to become a maintainer. |
| Issue / PR templates | OK, FIXED | `bug_report.yml`, `feature_request.yml`, `blank_issues_enabled: false`. The docs contact link now points to the docs site. The PR template gains a "tests" checkbox. |
| `.github/CODEOWNERS` | OK | One owner. `require_code_owner_review` is false in the ruleset, which is expected with a single maintainer. |
| `FUNDING.yml` | n/a | Not wanted; not invented. |
| `CITATION.cff` | GAP (optional) | §8 #12. |

## 4. Release standards

| Item | Status | Evidence / fix |
|---|---|---|
| SemVer | OK, FIXED | Release-please manifest mode with `linked-versions` (`release-please-config.json`). The policy was implicit; it is now written down in `docs/site/docs/limits.md:113` "Versionado y soporte": public API definition, 0.x rules, PATCH never breaks. |
| Deprecation policy | FIXED (new) | Same section: `DeprecationWarning` / `@deprecated` plus a `Deprecated` changelog entry at least one MINOR before removal. The exceptions are AWS removing the capability or a security fix. **Maintainer: review this commitment.** |
| Supported-versions policy | FIXED | `SECURITY.md:20` and `limits.md` "Soporte": only the latest `MAJOR.MINOR` line. |
| CHANGELOGs (Keep a Changelog) | OK, FIXED | One per component, with `[Unreleased]` and dated `## [x.y.z] - YYYY-MM-DD` headings. **Fixed**: the link-reference footers were stale. `[Unreleased]` compared from `*-v0.2.0`, and `[0.1.0]`/`[0.2.0]` pointed at tags that do not exist (the first tags are `*-v0.3.0`). Every tagged version now has its compare link, and 0.1.0/0.2.0 point at `docs/RELEASE_NOTES_0.x.md`. Remaining gap: `prepare_release_pr.py` does not maintain these links (§8 #7). |
| Release notes | OK / GAP | The changelog block is the curated note. The GitHub Release body is release-please's raw commit list, with duplicate lines for squash-plus-merge commits (see `python-v0.6.0`). `docs/RELEASING.md` §6 step 8 now states the rule for `### Security` (CVE/GHSA), `Changed`/`Removed` and `Deprecated`. §8 #6 covers publishing the curated block as the Release body. |
| Signed commits | OK | Ruleset `main-protegida` has `required_signatures`, PR required (merge commits only), no force-push or deletion, no bypass actors, and 13 required checks with a strict up-to-date policy (updated 2026-10-05, see §5). |
| Signed tags | GAP | Release-please creates lightweight, unsigned tags through the API. Mitigated by provenance below. §8 #9. |
| npm provenance | OK | Trusted publishing over OIDC with automatic provenance: `release.yml:261` `id-token: write`, `:294` `npm publish … --ignore-scripts`. Build and publish jobs are split. |
| PyPI Trusted Publishing + PEP 740 attestations | OK | `release.yml:178`, `:201` `pypa/gh-action-pypi-publish` v1.14.2 (attestations on by default). |
| Sigstore for rayd | OK | `cosign sign-blob --bundle` keyless on `rayd` and `rayito-image.zip`, verified in-job (`release.yml:357-374`). The user recipe is in `docs/site/docs/verify.md`. |
| SLSA provenance for rayd/zip | GAP | cosign proves who signed, not how the artifact was built. There is no SLSA v1 provenance statement for the GitHub Release assets. npm already has SLSA provenance. §8 #4. |
| SBOMs | OK / GAP | `rayd`: `cargo auditable` plus a CycloneDX 1.5 SBOM on the Release. There is none for the wheel or the npm tarball. Lower value, since nothing is bundled (§8 #11). |
| Reproducible builds | Partial | The zip uses a fixed timestamp (`FIXED_DATE_TIME`), `uv_build` writes 1980 timestamps, `--locked` everywhere, pinned toolchain and zig. Nothing checks bit-for-bit reproducibility (§8 #11). |
| Python metadata | OK, FIXED | Classifiers include `Typing :: Typed`, keywords, `py.typed` ships, `requires-python >=3.11`. **Fixed**: `Documentation` URL is now the docs site, and `Issues` was added. **GAP**: classifiers claim 3.11 and 3.13, but CI only runs the runner's 3.12 (§8 #5). |
| npm metadata | OK, FIXED | `engines.node >=20`, dual `exports` with per-condition `types`, `types`, `sideEffects: false`, `files`, `repository.directory`, `packageManager`. **Fixed**: added `homepage` and `bugs`. |
| Rust metadata | OK, FIXED | `rust-version`, `license`, `description` and `publish = false` on every crate. `repository` and `homepage` are now inherited. |
| Trademark note about E2B | FIXED | See §2.3. |

## 5. OpenSSF Best Practices, passing level: self-assessment

Result: **every MUST criterion can be answered Met or N/A.** The two
caveats found on 2026-10-03 are cleared (see below). The SHOULD gap is
`english`, and the SUGGESTED gap is `dynamic_analysis`; both may be Unmet at
passing level. Register at <https://www.bestpractices.dev/en/projects/new>
(maintainer only; nothing has been registered). The step-by-step checklist
and the exact text to paste per criterion are in
[`openssf-badge-answers.md`](openssf-badge-answers.md); the table below is
the summary.

**Current state (re-checked 2026-10-05, after 0.7.0):**

- **CodeQL (was C1):** 0 open alerts. Of the 12 CodeQL alerts raised so
  far, 6 are fixed and 6 are dismissed with a written reason (5 in
  `#[cfg(test)]` code, 1 in the dev-only `scripts/hooks-sim.py`). The 5
  open code-scanning alerts are Scorecard findings, not CodeQL
  (`BranchProtection`, `CodeReview`, `Maintained`, `Fuzzing`,
  `CIIBestPractices`).
- **Dependabot (was C2):** 0 open alerts. The only one so far,
  GHSA-8988-4f7v-96qf (medium, dev-only `@opentelemetry/core`), was fixed
  on 2026-10-04, three days after it was raised. Dependabot security
  updates, secret scanning and push protection are enabled.
- **Leaks:** `.github/workflows/leaks.yml` runs pinned gitleaks over the
  whole history and `check_hygiene.py` (plus a private denylist held in a
  repository secret) over each PR's title and body. Both are required
  checks.
- **Branch protection:** ruleset `main-protegida` on the default branch:
  pull request required (0 approvals, a single maintainer), merge commits
  only, conversations resolved, 13 required checks pinned to their app
  (CI jobs, cargo-deny, dependency audit, gitleaks, PR text, CodeQL) with
  the branch up to date, signed commits, no force push, no deletion, and
  no bypass actors.
- **License notices:** `LICENSE` and `NOTICE` ship in the wheel, the sdist
  and the npm tarball. Since `third-party-licenses` (after 0.7.0), the
  `rayd` release assets and `rayito-image.zip` also carry `LICENSE`,
  `NOTICE` and `THIRD_PARTY_LICENSES.md` (§2.2, §8 #1); 0.7.0 and earlier
  do not. This does not affect any badge criterion.
- **Supported versions:** `SECURITY.md` lists the 0.7.x line.

| Criterion | Answer | Justification / URL |
|---|---|---|
| description_good | Met | README opening lines and the docs home: <https://alejandro-cedeno-10.github.io/rayito/> |
| interact | Met | README links to the docs, `CONTRIBUTING.md`, `SUPPORT.md`, issues |
| contribution | Met | `CONTRIBUTING.md` §2 (OpenSpec) and §5 (PRs) |
| contribution_requirements | Met | `CONTRIBUTING.md` §3–§5: gates, conventions, DCO, signed commits, tests |
| floss_license | Met | Apache-2.0 |
| floss_license_osi | Met | Apache-2.0 is OSI-approved |
| license_location | Met | `/LICENSE` |
| documentation_basics | Met | Docs site: Primeros pasos, Guías |
| documentation_interface | Met | Docs site Referencia (mkdocstrings for Python, TS reference, `proto/rayito/v1/`) |
| sites_https | Met | GitHub, GitHub Pages, PyPI and npm are all HTTPS |
| discussion | Met | GitHub issues (searchable, URL-addressable) |
| english | Unmet (SHOULD) | Docs are in Spanish. English issues, PRs and security reports are accepted (README "In English", `SUPPORT.md`) |
| maintained | Met | Releases 0.3.0–0.7.0 between 2026-09-24 and 2026-10-05 |
| repo_public | Met | <https://github.com/alejandro-cedeno-10/rayito> |
| repo_track | Met | git |
| repo_interim | Met | Every PR lands on `main` between releases |
| repo_distributed | Met | git |
| version_unique | Met | `python-v*`, `typescript-v*`, `rayd-v*` |
| version_semver | Met | `docs/site/docs/limits.md` "Versionado y soporte" |
| version_tags | Met | git tags per component |
| release_notes | Met | Per-component `CHANGELOG.md` (Keep a Changelog), GitHub Releases |
| release_notes_vulns | Met | No public vulnerability has been fixed yet. The rule is in `docs/RELEASING.md` §6 step 8 (`### Security` with CVE/GHSA) |
| report_process | Met | Issue templates, `CONTRIBUTING.md` §6 |
| report_tracker | Met | GitHub issues |
| report_responses | Met | Single maintainer, active. (No external bug reports yet; answer from the history once some exist) |
| enhancement_responses | Met | Same as above |
| report_archive | Met | GitHub issues (public) |
| vulnerability_report_process | Met | `SECURITY.md` "Reportar una vulnerabilidad" |
| vulnerability_report_private | Met | GitHub Private Vulnerability Reporting is enabled |
| vulnerability_report_response | Met | 7-day acknowledgement (`SECURITY.md`), within the 14-day limit |
| build | Met | `make build`, `uv build`, `pnpm build`, `cargo zigbuild` |
| build_common_tools | Met | cargo, uv, pnpm, make |
| build_floss_tools | Met | All FLOSS |
| test | Met | `cargo test`, `pytest`, `vitest`, in public CI |
| test_invocation | Met | `make test`, `uv run pytest`, `pnpm test` |
| test_most | Met | 3000 Python unit tests, 1449 TS unit tests, rayd integration suites, e2e on AWS |
| test_continuous_integration | Met | `.github/workflows/ci.yml`, `leaks.yml` and CodeQL on every PR (13 required checks in the `main` ruleset) |
| test_policy | Met | `CONTRIBUTING.md` §5 "Tests con cada cambio" and §1 rule 4 (acceptance against real AWS) |
| tests_are_added | Met | E.g. PR #92 (reincarnate replay) added unit tests in both SDKs |
| tests_documented_added | Met | Same as `test_policy`, plus the PR template checkbox |
| warnings | Met | clippy pedantic with `unwrap_used`/`expect_used`/`panic` denied, ruff, mypy `--strict`, Biome, tsc `strict` |
| warnings_fixed | Met | CI fails on warnings |
| warnings_strict | Met | Same as above |
| know_secure_design | Met | `SECURITY.md` threat model T1–T28, `docs/SECURITY_AUDIT.md` |
| know_common_errors | Met | Same as above, plus the logging hygiene rules |
| crypto_published | Met | TLS (rustls/aws-lc-rs), HMAC-SHA256, SigV4, JWE issued by AWS |
| crypto_call | Met | No hand-rolled crypto (`hmac`, `sha2`, `aws-sigv4`, `rustls`) |
| crypto_floss | Met | All FLOSS |
| crypto_keylength | Met | 256-bit HMAC keys, TLS 1.2+ |
| crypto_working | Met | No MD5/SHA-1/DES for security purposes |
| crypto_weaknesses | Met | Same as above |
| crypto_pfs | Met | TLS 1.2 ECDHE / TLS 1.3 (rustls defaults) |
| crypto_password_storage | N/A | Rayito stores no user passwords |
| crypto_random | Met | OS CSPRNG (`getrandom`, Python `secrets`, Node `crypto.randomBytes`) |
| delivery_mitm | Met | HTTPS on PyPI/npm/GitHub, plus Sigstore, PEP 740 and npm provenance |
| delivery_unsigned | Met | Hashes come only over HTTPS (`SHA256SUMS` on the Release, `--require-hashes` in the image) |
| vulnerabilities_fixed_60_days | Met | Dependabot alerts: 0 open; the only one so far was fixed in 3 days. Scorecard `Vulnerabilities`: 0 |
| vulnerabilities_critical_fixed | Met | Policy in `deny.toml` header and `SECURITY.md` |
| no_leaked_credentials | Met | Secret scanning and push protection on; gitleaks over the whole history and `check_hygiene.py` over files and PR text (`leaks.yml`) |
| static_analysis | Met | CodeQL default setup, clippy, ruff, mypy, Biome, cargo-deny |
| static_analysis_common_vulnerabilities | Met | CodeQL |
| static_analysis_fixed | Met | 0 open CodeQL alerts (6 fixed, 6 dismissed with a reason) |
| static_analysis_often | Met | CodeQL on push/PR, Scorecard weekly |
| dynamic_analysis | Unmet (SUGGESTED) | No fuzzing (Scorecard `Fuzzing` = 0). §8 #8 |
| dynamic_analysis_unsafe | N/A (argued) | Memory-safe languages. The 29 `unsafe` blocks in `rayd` adapters wrap libc FFI (FUSE device, PTY); Miri/ASan are not run (§8 #8) |
| dynamic_analysis_enable_assertions | Met | Tests run debug builds (`debug_assert!` on) |
| dynamic_analysis_fixed | N/A | No dynamic analysis tool runs yet, so there are no findings |

After registration, uncomment the badge line already in `README.md` (next
to the Scorecard badge) and replace `<id>` with the project id. Scorecard's
`CII-Best-Practices` check (currently 0) then picks it up.

### Scorecard checks still low (2026-10-05, overall 7.0)

| Check | Why | Action |
|---|---|---|
| Code-Review (0) | Single maintainer, 0/9 approved changesets | Structural; improves with a second maintainer |
| Branch-Protection (4) | The ruleset blocks force push and deletion, requires a PR, status checks and signed commits, with no bypass; the missing points are approving reviews (and code-owner review), which one maintainer cannot give themself | Fine for one maintainer. Require 1 approval (and code-owner review) when there are two |
| Maintained | The repo is under 90 days old | Clears with time |
| Fuzzing | No fuzzers | §8 #8 |
| CII-Best-Practices | Not registered | §5 |
| Vulnerabilities (10) | Cleared: 0 known vulnerabilities | None |

## 6. Docs site

| Item | Status |
|---|---|
| License footer | FIXED (`mkdocs.yml` `copyright`) |
| "Not affiliated with E2B" disclaimer | FIXED: footer on every page, plus admonitions on the two E2B pages |
| Contribution links | FIXED: footer links to CONTRIBUTING, SECURITY and CODE_OF_CONDUCT. `edit_uri` already offers "edit this page" |

## 7. What the PR changed

`NOTICE` ×3 (E2B copyright, "with changes"); the `git-args.ts` header;
`SECURITY.md` supported versions and sidecar-pins row; new `SUPPORT.md`; README (status, run
example, docs table, "Licencia, estado y soporte", "In English"); footers in
both package READMEs; `pyproject.toml` URLs; `package.json` `homepage`/`bugs`;
crate `repository`/`homepage`; Keep a Changelog link footers and
`[Unreleased]` entries; `docs/site/docs/limits.md` "Versionado y soporte";
`docs/RELEASING.md` (policy pointer, release-notes rule, stale intro);
`CONTRIBUTING.md` (DCO vs signature, test policy); PR template; issue
chooser; mkdocs footer; trademark admonitions.

## 8. Remaining recommendations, ranked

1. **DONE (`third-party-licenses`)**: `THIRD_PARTY_LICENSES.md` is
   generated by `make licenses` (cargo-about 0.9.2, `about.toml` with
   `deny.toml`'s allowlist, `--frozen`), committed, and checked for
   staleness and exact coverage of `cargo tree`'s graph
   (`make licenses-check`) and against the binary's `.dep-v0`
   (`scripts/check_third_party_licenses.py`) in CI's `build` job
   and in `rayd-build`. It travels with `LICENSE` and `NOTICE` in the zip,
   the image (`/usr/share/doc/rayd/`) and the release assets, and
   `rayd-sign` now also signs `SHA256SUMS`. The sidecar's Python
   dependencies are not redistributed (pip installs them in the user's
   account and each wheel keeps its own `dist-info/licenses/`; pyzmq's
   includes libzmq's unmodified MPL-2.0). Original recommendation:
   **Ship third-party notices with `rayd` and `rayito-image.zip`** (license
   compliance, high). Generate `THIRD_PARTY_LICENSES` from the exact
   `Cargo.lock` with `cargo about generate` (an `about.toml` reusing the
   `deny.toml` allowlist). Add it, plus `LICENSE` and `NOTICE`, to the zip
   (`clients/python/src/rayito/cli/_artifact.py`
   `shipped_files`/`write_zip`), upload it as a Release asset next to `rayd`,
   and `COPY` it to `/usr/share/doc/rayd/` in `image/Dockerfile`. Gate it in
   CI so it is never stale, and keep it inside the signed zip.
2. ~~**Triage the 11 CodeQL alerts** (C1)~~ Done: 0 open CodeQL alerts
   (2026-10-05).
3. ~~**Merge Dependabot #86** (C2)~~ Done. **Register the OpenSSF badge**
   with [`openssf-badge-answers.md`](openssf-badge-answers.md).
4. **SLSA build provenance for GitHub Release assets**: add
   `actions/attest-build-provenance` (with `attestations: write`) for `rayd`,
   `rayito-image.zip` and `rayd.cdx.json` in the `rayd` job, and document
   `gh attestation verify` in `docs/site/docs/verify.md`. That reaches SLSA
   Build L2. L3 needs the build in a reusable workflow (or
   `slsa-github-generator`) isolated from the caller.
5. ~~**Python version matrix**~~ Done: the `python-versions` job of
   `ci.yml` runs the SDK unit tests on 3.11 and 3.13 next to the runner's
   3.12. Still to consider: 3.14.
6. **GitHub Release body = curated changelog block**: in `release.yml`
   (after tagging), extract `## [x.y.z]` from the component's
   `CHANGELOG.md` and run `gh release edit <tag> --notes-file`. The current
   bodies duplicate every commit (squash plus merge).
7. **Keep a Changelog link footers in `scripts/prepare_release_pr.py`**:
   when it converts `[Unreleased]` to `[x.y.z]`, it should also add
   `[x.y.z]: …/compare/<c>-v<prev>...<c>-v<x.y.z>` and move `[Unreleased]`'s
   base. The PR fixed the footers by hand; 0.6.1 will make `[Unreleased]`
   stale again.
8. **Fuzzing** (Scorecard and OpenSSF SUGGESTED): `cargo fuzz` targets for
   the pure parsers in `rayd-core` (PROXY protocol, event-line framing,
   path validation), run under ClusterFuzzLite on PRs.
9. **DCO enforcement and signed tags**: install the DCO app, or add a small
   CI job that checks `Signed-off-by` on PR commits and exempts
   `dependabot[bot]`. Dependabot commits carry no trailer, so a naive gate
   would block them. Make it required. For tags, have
   `prepare_release_pr.py` (or a post-merge step) create annotated, signed
   tags (`git tag -s`) instead of release-please's API tags, or rely on
   provenance (#4) and document that.
10. **REUSE / SPDX, pragmatic subset**: a root `REUSE.toml` instead of
    per-file headers, plus `LICENSES/Apache-2.0.txt` and `LICENSES/MIT.txt`,
    and `uvx reuse lint` in CI. Sketch:

    ```toml
    version = 1
    [[annotations]]
    path = "**"
    precedence = "aggregate"
    SPDX-FileCopyrightText = "2026 Rayito contributors"
    SPDX-License-Identifier = "Apache-2.0"

    [[annotations]]
    path = "kernel-sidecar/src/rayito_kernel_sidecar/_vendor/e2b_charts/**"
    precedence = "override"
    SPDX-FileCopyrightText = "2025 FOUNDRYLABS, INC."
    SPDX-License-Identifier = "MIT"

    [[annotations]]
    path = ["clients/python/src/rayito/_git_base.py", "clients/typescript/src/sandbox/git-args.ts"]
    precedence = "aggregate"
    SPDX-FileCopyrightText = "2023 FoundryLabs, Inc."
    SPDX-License-Identifier = "Apache-2.0"
    ```

    Root `LICENSE` stays byte-identical, since `check_license.py` pins it.
11. **SBOM for the SDK artifacts and a reproducibility check**: optional
    CycloneDX for the wheel and tarball (`cyclonedx-py`, `cyclonedx-npm`) as
    Release assets, and a weekly job that rebuilds `rayd` and the zip from
    the tag and compares sha256 against `SHA256SUMS`.
12. **`CITATION.cff`** (optional), so GitHub shows "Cite this repository".
13. **Public English docs** (OpenSSF `english` SHOULD): at least an English
    quickstart page on the docs site.
14. **Version numbers copied into prose go stale.** The PR fixed three:
    the `SECURITY.md` supported-versions table (0.2.0), the sidecar pins row
    in `SECURITY.md` "Cadena de suministro" (ipykernel 6.31.0 and others, vs
    7.3.0 in `requirements.txt`; it now points at the file), and README's
    `RAYD_VERSION=0.5.0`. Prefer pointing at the source of truth (or adding
    a test, like `test_compat.py` does for the compat table).
