# Rayito — Open-source readiness & M7 research report

Read-only research against the repo (`SPEC.md`, `ARCHITECTURE.md`, `MILESTONES.md`, `SECURITY.md`, `README.md`, manifests, protos, `clients/python/src/rayito/`) plus primary sources, 2026-09-16.

## 0. Findings that change the brief

| Finding | Evidence | Implication |
|---|---|---|
| **The repo currently declares MIT, not Apache-2.0** | `Cargo.toml` `license = "MIT"`, `clients/python/pyproject.toml` `license = "MIT"` + `License :: OSI Approved :: MIT License` classifier, `kernel-sidecar/pyproject.toml` MIT, `clients/typescript/package.json` `"license": "MIT"`, `clients/typescript/LICENSE` is MIT text | Decision taken by the owner: **Apache-2.0** (patent grant, NOTICE mechanism, what AWS/E2B-infra/RaitBox/microvms-agentd use). All five places must change together |
| No root `LICENSE`, `NOTICE`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `CODEOWNERS`, issue/PR templates, `.github/dependabot.yml` | `ls` at repo root; `.github/` has only `workflows/{ci,release}.yml` | All of §1 is greenfield |
| **`rayito` is not reserved anywhere** | `pypi.org/pypi/rayito/json` → 404, `registry.npmjs.org/rayito` → 404, `crates.io/api/v1/crates/rayito` → 404 (today) | Reserve on PyPI (0.1.0 via the existing Trusted Publisher flow), npm and crates.io (placeholder) before announcing |
| Runtime Python deps are all permissive | Metadata read from `kernel-sidecar/.venv`: BSD/MIT/PSF/Apache/ISC/MIT-CMU only; `pathspec` (MPL-2.0) is dev-only (mypy) | No GPL/LGPL/AGPL in the image |

## 1. Open-source hygiene (multi-language repo)

### Files and contents

| File | Recommendation | Source |
|---|---|---|
| `LICENSE` (root) | Verbatim Apache-2.0 text from https://www.apache.org/licenses/LICENSE-2.0.txt. Copy into `clients/python/`, `clients/typescript/`, `crates/rayd/` so each artifact ships it (`uv build` and `pnpm pack` include it; `pack-check.mjs` already asserts `package/LICENSE`) | ASF "apply license" https://www.apache.org/legal/apply-license.html |
| `NOTICE` (root) | Short, Apache §4(d)-style: `Rayito\nCopyright 2026 <owner>\n\nThis product includes software developed by third parties:\n- e2b_charts 1.0.0 (MIT) Copyright (c) 2025 FOUNDRYLABS, INC. — kernel-sidecar/.../_vendor/e2b_charts (LICENSE kept alongside)\n`. Keep `VENDORED.md` (already lists upstream, sdist sha256, the single import-path change). Nothing else is vendored; generated protobuf code is yours. Apache-2.0 vendoring MIT is fine: MIT only requires keeping its copyright + permission notice, which the co-located `LICENSE` file satisfies | Apache-2.0 §4(d) https://www.apache.org/licenses/LICENSE-2.0 ; SPDX https://spdx.org/licenses/Apache-2.0.html |
| `clients/python/pyproject.toml` | PEP 639: `license = "Apache-2.0"` (SPDX expression), `license-files = ["LICENSE", "NOTICE"]`, **delete** the `License ::` classifier (deprecated by PEP 639). Verify `uv_build` emits `License-Expression` in the wheel (`scripts/check_wheel.py` already inspects wheel contents; extend it) | https://peps.python.org/pep-0639/ ; https://packaging.python.org/en/latest/guides/licensing-examples-and-user-scenarios/ |
| `Cargo.toml` `[workspace.package]` | `license = "Apache-2.0"`, add `repository`, `homepage`; keep `publish = false` for `rayd`/`rayd-core`; `rayito-proto` may publish later | https://doc.rust-lang.org/cargo/reference/manifest.html#the-license-and-license-file-fields |
| `package.json` | `"license": "Apache-2.0"`, `"files"` add `"LICENSE"`, `"NOTICE"`; add `"repository"`; `publishConfig.provenance` is no longer needed with trusted publishing (see §4) | https://docs.npmjs.com/cli/v10/configuring-npm/package-json#license |
| SPDX headers | **Not recommended** at this size. Machine-readable licensing is already covered by package metadata. If REUSE compliance is wanted later, use a root `REUSE.toml` rather than per-file headers | https://reuse.software/spec-3.3/ |
| `CONTRIBUTING.md` | **DCO, not CLA**: no legal-entity overhead, contributors add `Signed-off-by` via `git commit -s`; enforce with the DCO GitHub App as a required check. Include: how to run `make lint/test`, WSL2 note for adapter tests, the "never mock a milestone" rule, the proto-is-source-of-truth rule, conventional-commit prefixes | DCO text https://developercertificate.org/ ; app https://github.com/apps/dco ; DCO-vs-CLA https://ospo.docs.cern.ch/recommendations/CLAs-and-DCOs/ |
| `CODE_OF_CONDUCT.md` | Contributor Covenant **3.0** (2025-07-28, CC BY-SA 4.0); 2.1 remains fine if a shorter template is preferred | 3.0 https://www.contributor-covenant.org/version/3/0/code_of_conduct/ ; 2.1 https://www.contributor-covenant.org/version/2/1/code_of_conduct/ |
| `SECURITY.md` | Keep the threat model, add a top "Reporting a vulnerability" section: enable GitHub Private Vulnerability Reporting; state supported versions, 90-day disclosure window, no bounty | https://docs.github.com/code-security/security-advisories/working-with-repository-security-advisories/configuring-private-vulnerability-reporting-for-a-repository |
| `GOVERNANCE.md` / `MAINTAINERS.md` | One page: single maintainer now, decisions by ADR (`ARCHITECTURE.md`), how one becomes a maintainer. Skip a formal governance doc until there are ≥ 3 maintainers | — |
| `.github/CODEOWNERS` | `proto/ crates/ @<owner>`; `clients/python/ @<owner>`; `clients/typescript/ @<owner>`; `AWS_API_NOTES.md @<owner>` | — |
| Issue / PR templates | `bug_report.yml` (SDK version, `agent_version` from Health, image version, region, redacted `stateReason`), `feature_request.yml`, `PULL_REQUEST_TEMPLATE.md` with checklist: OpenSpec change linked, `buf breaking` clean, e2e run cost noted, no secrets in logs | — |
| `CHANGELOG.md` | Keep a Changelog 1.1.0 (already used in `clients/python/CHANGELOG.md`); add `clients/typescript/CHANGELOG.md` and `crates/rayd/CHANGELOG.md` | https://keepachangelog.com/en/1.1.0/ |
| README badges | CI, PyPI version, npm version, license, OpenSSF Scorecard once §4 lands | — |

### Release tooling and versioning

- **Tooling: release-please (manifest mode)**, not changesets. release-please natively has `release-type` `python`, `rust`, `node`, a `cargo-workspace` plugin and a **`linked-versions` plugin** that bumps a group of components to the same version (https://github.com/googleapis/release-please/blob/main/docs/manifest-releaser.md). Components: `clients/python`, `clients/typescript`, `crates/rayd` (tags `python-v…`, `typescript-v…`, `rayd-v…`, matching the existing `python-v*` trigger in `release.yml`).
- **Versioning policy (what peers do):** E2B releases `e2b` 2.50.0 on PyPI and npm in lockstep with per-package tags while the in-VM agent is versioned separately (`envd-v0.6.13`) and SDK behaviour is gated at runtime on the reported envd version (https://github.com/e2b-dev/e2b/releases, https://github.com/e2b-dev/infra/releases). Daytona ships Python `daytona` and npm `@daytona/sdk` at the identical version 0.214.0. Modal does not (`modal` 1.5.5 PyPI vs `modal` 0.10.1 npm).
  **Recommendation:** lockstep `MAJOR.MINOR` for Python SDK, TS SDK and `rayd` (0.2.0 across the board), patch may diverge; `rayd` reports `agent_version` in `Health` already, so the SDKs gate features on it and `docs/site/docs/limits.md` gets a compatibility table (SDK ↔ minimum `agent_version` ↔ minimum `rayito-base` image version). AWS image versions stay as opaque build numbers.

### License compatibility

| Item | Status |
|---|---|
| Apache-2.0 project vendoring MIT `e2b_charts` | Fine; keep MIT `LICENSE` beside the code (done) and list it in `NOTICE` |
| Rust deps | Add `deny.toml` (`[licenses] allow = ["MIT","Apache-2.0","BSD-2-Clause","BSD-3-Clause","ISC","Unicode-3.0","Zlib","MPL-2.0"]`, `[advisories]` on, `[sources] allow-registry = crates.io`, `[bans] multiple-versions = "warn"`) and `EmbarkStudios/cargo-deny-action` in CI (https://github.com/EmbarkStudios/cargo-deny) |
| Python runtime deps (sidecar image) | All permissive. `matplotlib` is PSF-style. **pyzmq** 27.2.0 is BSD-3; its wheels bundle libzmq 4.3.5, relicensed **LGPL → MPL-2.0** in pyzmq 26.0 (https://pyzmq.readthedocs.io/en/latest/changelog.html; https://zeromq.org/license/). MPL-2.0 is file-level copyleft: bundling an unmodified library inside a MicroVM image creates no obligation beyond keeping its notices (https://www.mozilla.org/en-US/MPL/2.0/FAQ/). No concern |
| Sidecar itself | `Private :: Do Not Upload` classifier; never on PyPI — correct |
| Trademark | Prior research found no software collision. Optional: USPTO TESS / EUIPO eSearch check for class 9/42 before a public launch; not blocking |

## 2. Which languages can use Rayito

**Architecture reminder:** `rayd` (Rust) is a *server* inside the MicroVM; any language that can (a) call the Lambda MicroVMs control plane and (b) speak gRPC over HTTPS/2 with four metadata headers can use it. The `.proto` files are the contract; the Python and TS SDKs are generated clients plus lifecycle/reconnect logic.

| Product | Agent in VM | Official client SDKs |
|---|---|---|
| E2B | `envd` (Go, https://github.com/e2b-dev/infra) | Python, JS/TS only; Go SDKs exist but are community forks |
| Daytona | Go daemon (https://github.com/daytonaio/daytona) | Python, TypeScript, Ruby, Go, Java + CLI (https://www.daytona.io/docs/en/getting-started/) |
| Modal | proprietary | Python; JS and Go via libmodal/modal-client (https://modal.com/docs/guide/sdk-javascript-go) |
| Rayito | `rayd` (Rust) | Python, TypeScript |

**Rust client SDK — later, not now.** Feasibility is trivial: `aws-sdk-lambdamicrovms` **exists on crates.io, 1.10.0, Apache-2.0** (https://crates.io/crates/aws-sdk-lambdamicrovms), and `tonic` 0.14 client + the protos via `tonic-prost-build` are already in the workspace; a `rayito` crate is ~1.5k LOC. Demand is thin: Rig is the only Rust agent framework with real traction (rig-core 0.42), Swiftide 84k, `strands-agents` Rust is 0.1.0; the only comparable Rust sandbox client (`microvms-agentd`) has 4 stars. Ship it when a Rig user asks; meanwhile publish `rayito-proto` to crates.io so anyone can build one.

**Go client SDK — no.** `github.com/aws/aws-sdk-go-v2/service/lambdamicrovms` v1.7.0 exists (https://pkg.go.dev/github.com/aws/aws-sdk-go-v2/service/lambdamicrovms), but E2B ships none officially and the target buyer writes Python/TS. Document "bring your own client from the proto" with a `buf.gen.yaml` Go example instead.

## 3. Gaps vs E2B and how to close them on Lambda MicroVMs

**(a) `upload_url` / `download_url`.** E2B: the SDK computes `signature = "v1_" + b64(sha256(f"{path}:{operation}:{user}:{envd_access_token}[:{expiration}]"))` and appends it to `https://<sandbox-host>/files` (https://github.com/e2b-dev/E2B/blob/main/packages/python-sdk/e2b/sandbox/signature.py). Our constraint: AWS allows the JWE **only** in the `X-aws-proxy-auth` header (https://docs.aws.amazon.com/lambda/latest/dg/microvms-networking.html); no query/cookie form, so a bare URL can never authenticate at the proxy. Options: (1) S3 presigned URLs + in-VM sync (needs a role and IMDS exposure, or the sync runs in `rayd` as root); (2) `rayd` HTTP `/files` with an E2B-style HMAC in the query, JWE still required (only useful for clients that can set headers, which already have `files.read/write`). **Recommend:** keep `UnimplementedError` in the shim (honest), and in v0.2 add a *non-E2B* helper: `sbx.files.download_url()` → `(url, headers)` pair. Revisit S3 sync under (g).

**(b) Extra kernels.** E2B's template installs `python3` (ipykernel), `r` (IRkernel), `javascript` (E2B's fork of ijavascript on Node 20), `bash` (`bash_kernel`), `java` (IJava on JDK 11) (https://github.com/e2b-dev/code-interpreter/blob/main/template/template.py). Sidecar impact: `AsyncKernelManager(kernel_name=…)` already abstracts the kernelspec; adding a language = a kernelspec in the image + `language` in `CreateContextRequest` + warm-up per kernel. Zero Rust changes beyond passing the field through. Cost: bash_kernel ≈ 0 MB; ijavascript needs Node (+~100 MB disk); R ≈ 300–400 MB; Java worst. Measurements show restore time is dominated by warm-up, not snapshot size (`rayito-base-slim`: −225 MB, −2.2 s). **Recommend for v0.2:** `bash` and `javascript` only, lazy start on first `create_code_context(language=)`, shipped as `rayito-base-poly` so `rayito-base` stays lean.

**(c) `set_timeout`.** Confirmed impossible: no `UpdateMicrovm` (ADR-007). Document the **recreate pattern** with the S3 sync of (g) and a `reincarnate()` helper. Kernel memory is lost by design; say so.

**(d) Metadata/list filters.** `runHookPayload` echo via `Health` + client-side O(n) filter over `RUNNING` is fine and honest. Keep.

**(e) Template CLI.** E2B's `e2b template build` (https://e2b.dev/docs/sdk-reference/cli/v2.2.10/template). We already have `scripts/publish_image.py`, `image_zip.py`, `image_prune.py`, `bench_cold_start.py`, `hooks-sim.py`. **Recommend:** a thin `rayito` CLI (typer, `[cli]` extra) with `image publish|list|prune`, `sandbox list|kill|logs`, `doctor`. Skip declarative `rayito.toml` until asked.

**(f) Integrations.** E2B's MCP server exposes a single `run_code` tool (https://github.com/e2b-dev/mcp-server); RaitBox ships `python -m raitbox.mcp`; LangChain has `E2BDataAnalysisTool`. **Cheapest high-leverage: an MCP server** (`rayito-mcp`, stdio + streamable HTTP, tools `run_code`, `run_command`, `read_file`, `write_file`, `list_sandboxes`, one sandbox per MCP session with pause on idle) — plugs into Claude Code/Desktop, Cursor, Strands, LangChain and the AI SDK at once (https://modelcontextprotocol.io/specification/2025-06-18/server/tools). LangChain/AI SDK adapters are 50-line examples in `docs/`, not packages.

**(g) Persistence beyond 8 h.** Options: (1) S3 sync from inside the image as uid 1000 → exposes the role (T1); (2) **`rayd`-native S3 adapter** (`aws-sdk-s3` in the musl binary, role from IMDS as root, scoped `s3:PutObject/GetObject` on `rayito/<sandbox_id>/*`): keeps IMDS closed for user code, but adds a TLS crate to `rayd` (rustls + aws-lc-rs cross-compile fine with zig; binary grows several MB); (3) EFS via NFS through a VPC egress connector: not documented for MicroVMs by AWS; RaitBox claims it (needs `additionalOsCapabilities: ["ALL"]` + VPC). **Recommend:** (2) as the M7 design, behind `Sandbox.create(persist=S3Prefix(...))`, with `checkpoint_files()`/`restore_files()`; measure the binary delta and the 4 MB/s egress cap first.

## 4. Security / ops gaps (2026 baseline)

| Practice | Status | Do |
|---|---|---|
| SBOM | none | `cargo auditable build` for `rayd` + `cargo cyclonedx` attached to the release (https://github.com/rust-secure-code/cargo-auditable, https://github.com/CycloneDX/cyclonedx-rust-cargo); `uv export` → `pip-audit -r` for sidecar pins |
| `cargo-deny` | none | `deny.toml` + `cargo-deny-action` (see §1) |
| `pip-audit` / `npm audit` | none | `uvx pip-audit -r kernel-sidecar/requirements.txt` and `pnpm audit --prod` in CI, weekly cron |
| Dependabot | none | `.github/dependabot.yml` with ecosystems `cargo`, `uv`, `pip`, `npm`, `github-actions`, `docker`; `groups` per ecosystem (https://docs.github.com/en/code-security/dependabot/dependabot-version-updates/configuration-options-for-the-dependabot.yml-file) |
| Actions hardening | `ci.yml` pins by major tag, no `permissions:` | Pin every action to a full SHA (tj-actions compromise, March 2025: https://www.wiz.io/blog/github-actions-security-guide), top-level `permissions: contents: read`, `step-security/harden-runner`, `ossf/scorecard-action` |
| e2e on AWS | planned (`e2e.yml` with OIDC role) but file absent | `aws-actions/configure-aws-credentials` OIDC role scoped to `lambda-microvms` on the test image ARN only; `concurrency: e2e`, `timeout-minutes`, pre-flight "> 10 live MicroVMs → fail", the sweeper, plus an AWS Budget alarm on `Lambda-MicroVM-*` |
| ARM CI | x86 only | `ubuntu-24.04-arm` hosted runners are free for public repos (https://github.blog/changelog/2025-08-07-arm64-hosted-runners-for-public-repositories-are-now-generally-available/) → run the `cfg(unix)` adapter suites on real aarch64 |
| Release signing | PyPI Trusted Publishing wired; nothing for npm/zip | PyPI attestations automatic with `pypa/gh-action-pypi-publish` ≥ v1.11; npm trusted publishing (needs npm ≥ 11.5.1: https://docs.npmjs.com/trusted-publishers/); `cosign sign-blob` keyless on `rayito-image.zip` + `rayd` (https://docs.sigstore.dev/cosign/verifying/verify/) |
| Image digest pinning | `FROM …@sha256:<digest>` planned | Record digest + `--base-image-version` per published image version; Dependabot `docker` bumps the digest |
| `rayito doctor` | none | checks: credentials/region, `list-managed-microvm-images` reachability, applied Service Quotas, bucket access, image three-state gate, `iam:SimulatePrincipalPolicy`, mint a token, count orphan `RUNNING` VMs, SDK ↔ `agent_version` compatibility |

## 5. M7 proposal (one milestone, ordered by leverage)

| # | OpenSpec change | Scope | Acceptance test | Size |
|---|---|---|---|---|
| 1 | `m7-oss-hygiene` | Apache-2.0 applied in 5 places, `LICENSE`/`NOTICE`/`CONTRIBUTING` (DCO)/`CODE_OF_CONDUCT` (CC 3.0)/`SECURITY` reporting section/`CODEOWNERS`/templates/`dependabot.yml`; PEP 639 metadata; reserve `rayito` on PyPI/npm/crates.io; badges | `uv build` wheel has `License-Expression: Apache-2.0` + `NOTICE`; `pnpm pack` contains LICENSE+NOTICE; DCO check green on a PR | S |
| 2 | `m7-supply-chain` | SHA-pinned actions, `permissions`, harden-runner, scorecard, `cargo-deny`, `cargo auditable`, CycloneDX, `pip-audit`, `pnpm audit`, `ubuntu-24.04-arm` job, `e2e.yml` with OIDC + cost guardrails, cosign on release artefacts, npm trusted publishing, release-please manifest with `linked-versions` | CI green with all checks; `cosign verify-blob` passes on the release zip; release-please opens a PR bumping the three components together | M |
| 3 | `m7-suspended-pool` | Already decided (ADR-008): client-side pool of suspended VMs with token hand-off design | `Sandbox.create(pool=…)` p95 to first cell < 1.5 s over 20 takes; slots recycled before 8 h | M |
| 4 | `m7-s3-persistence` | `rayd`-native S3 checkpoint/restore of `/home/user` (execution role as root, IMDS still blocked for uid 1000), `Sandbox.create(persist=)`, `reincarnate()` pattern for the 8 h wall | Write 50 MB, `checkpoint_files()`, kill, `create(persist=)` → same sha256; `imds_blocked` still true; binary delta reported | L |
| 5 | `m7-mcp-server` | `rayito-mcp` (stdio + streamable HTTP; `run_code`, `run_command`, `read_file`, `write_file`, `list_sandboxes`) | Claude Code drives a sandbox end-to-end via MCP Inspector against AWS | S |
| 6 | `m7-cli` | `rayito` typer CLI over existing scripts + `doctor` | `rayito doctor` reports all checks on a fresh account; `rayito image publish` reproduces `make image-publish` | S |
| 7 | `m7-poly-kernels` | `language` in `CreateContextRequest`; `bash` + `javascript` (ijavascript) kernelspecs in a `rayito-base-poly` variant, lazy start | `run_code("echo hi", language="bash")` and JS cell return results; `rayito-base` snapshot size unchanged | M |
| — | Deferred | Rust/Go client SDKs (publish `rayito-proto` to crates.io only), R/Java kernels, signed bare URLs, EFS | — | — |

Items 1–2 are the gate for going public; 3–5 are the product leverage; 6–7 fill in if capacity remains.
