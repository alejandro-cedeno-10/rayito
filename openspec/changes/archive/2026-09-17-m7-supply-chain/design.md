## Context

State on 2026-09-16 (M6 accepted; `rayito` Python 0.1.0, TypeScript
0.0.5, `rayd` 0.1.0 from `[workspace.package]`; no git repository yet, so
nothing here is committed or tagged):

- `.github/workflows/ci.yml` has five jobs (`check`, `typescript`,
  `sidecar`, `build`, `docs`) on `ubuntu-24.04`; every action is pinned by
  major tag (`actions/checkout@v4`, `bufbuild/buf-action@v1`,
  `Swatinem/rust-cache@v2`, `astral-sh/setup-uv@v6`,
  `pnpm/action-setup@v4`, `actions/setup-node@v4`, `mlugg/setup-zig@v2`,
  `actions/upload-artifact@v4`); no `permissions:` block. `release.yml`
  (PyPI only, tags `python-v*`, `workflow_dispatch` dry-run) already has
  `permissions: contents: read` and a `publish` job with `id-token: write`
  in environment `pypi`. Both files are `actionlint` clean today
  (`actionlint 1.7.12`, exit 0).
- No `deny.toml`, no SBOM, no audits, no ARM job, no `e2e.yml`, no
  `scorecard.yml`, no signing, no release automation. The sibling
  `m7-oss-hygiene` adds `.github/dependabot.yml` (ecosystems `cargo`, `uv`,
  `pip`, `npm`, `github-actions`, `docker`), a `check_license.py` step in
  `ci.yml`'s `check` job, `docs/RELEASING.md` and `crates/rayd/CHANGELOG.md`.
- `image/Dockerfile` starts `FROM public.ecr.aws/lambda/microvms:al2023-minimal`
  with a comment saying the digest will be fixed later. Resolved today:
  the tag is a manifest list (`application/vnd.docker.distribution.manifest.list.v2+json`)
  with digest `sha256:05cb9b38d841e7ff1b693dc9e894909612f340bf99ec97d426e8000a5bbe96c3`
  containing a single `linux/arm64` manifest
  `sha256:63831a97f9e498f7693c6f42951fe5d935947e6ae25a11fcab1fe0f5ae60b2a7`
  (`docker buildx imagetools inspect`, Docker 29.1.3; the same two digests
  come back from the OCI registry API without Docker: token from
  `https://public.ecr.aws/token/?scope=repository:lambda/microvms:pull`,
  then `HEAD /v2/lambda/microvms/manifests/al2023-minimal` →
  `Docker-Content-Digest`).
- `scripts/publish_image.py` accepts an optional `--base-image-version`
  and sends `baseImageVersion` only when given. `AWS_API_NOTES.md` §4:
  `baseImageArn` is `arn:aws:lambda:<region>:aws:microvm-image:al2023-1`;
  `list-managed-microvm-image-versions` lists `imageVersion` `0` and `1`
  (re-checked today), while `get-microvm-image-version` on `rayito-base`
  16.0 echoes `baseImageVersion: "1.0"`; the CLI documents
  `--base-image-version` as a string with `min: 1`. Which of the two
  spellings the request accepts has not been measured.
- Local measurements taken for this design (Windows host, tools on the
  `PATH`): `cargo-deny 0.20.2` with the `deny.toml` of D4 →
  `advisories ok, bans ok, licenses ok, sources ok` (7 duplicate-version
  warnings: `base64`, `hashbrown`, `logos`, `logos-codegen`,
  `logos-derive`, `syn`, `windows-sys`); the report's allowlist alone
  rejects `notify 8.2.0` (`CC0-1.0`) and `wildcards = "deny"` flags the
  two `{ workspace = true }` path dependencies of `rayd`.
  `pip-audit 2.10.0 -r kernel-sidecar/requirements.txt --no-deps` → "No
  known vulnerabilities found"; the same over `uv export --frozen --no-dev
  --no-emit-project` of `clients/python` (101 lines) and `kernel-sidecar`
  → clean. `pnpm audit --prod` (pnpm 9.15.4) → clean; `pnpm audit` (all
  scopes) → 2 moderate in dev only (`vitest 3.2.7` and `@vitest/mocker`,
  GHSA-82fw-gwwq-j7x9, patched ≥ 4.1.11).
  `cargo auditable 0.7.6` + `cargo-zigbuild 0.23.4`: `cargo auditable
  zigbuild --release --target aarch64-unknown-linux-musl -p rayd` links a
  binary of 4 700 984 B (plain zigbuild: 4 698 744 B) whose ELF section
  `.dep-v0` decompresses to 145 packages with root `rayd 0.1.0`; the
  `linker stderr: ignoring deprecated linker optimization setting '1'`
  warning is pre-existing (zig 0.16 with plain zigbuild too).
  `cargo-cyclonedx 0.5.9`: `cargo cyclonedx --manifest-path
  crates/rayd/Cargo.toml --target aarch64-unknown-linux-musl --format json
  --no-build-deps --spec-version 1.5` writes `crates/rayd/rayd.cdx.json`
  (113 components, metadata component `rayd 0.1.0`); `--override-filename`
  cannot be combined with `--describe`, so the default name is kept.
  `uvx cfn-lint` 1.56.3 is available; `aws lambda-microvms list-microvms
  --image-identifier <arn> --query "items[?state!='TERMINATED' &&
  state!='TERMINATING'].microvmId" --output text | wc -w` counts across
  pages (the CLI applies `--query` per page for this service, so a
  `length()` expression prints one number per page and cannot be used).
- release-please 17.x facts read from its source today: the `rust`
  strategy calls the `CargoToml` updater on every workspace member and on
  the root manifest, and that updater throws `is not a package manifest`
  on a root that has `[workspace.package]` but no `[package]`, and would
  overwrite `version.workspace = true` in members; the `python` strategy
  updates `pyproject.toml`, `setup.*`, `<pkg>/__init__.py`,
  `src/<pkg>/__init__.py` and any `version.py` (not `_version.py`); the
  `node` strategy updates `package.json` and `package-lock.json` (not
  `pnpm-lock.yaml`, which does not store the root version). `extra-files`
  support `{"type": "toml", "path", "jsonpath"}` (jsonpath-plus, filters
  allowed, formatting preserved by `replaceTomlValue`) and the generic
  updater (a line containing `x-release-please-version`); a path starting
  with `/` is repository-relative regardless of the component path. The
  `linked-versions` plugin bumps every component of the group to the
  highest resulting version. Tags/releases created with the default
  `GITHUB_TOKEN` do not trigger other workflows.
- Tool versions resolved today (GitHub API): actions/checkout v7.0.1,
  bufbuild/buf-action v1.5.0, Swatinem/rust-cache v2.9.2, astral-sh/setup-uv
  v10.1.0, pnpm/action-setup v6.1.0, actions/setup-node v7.0.0,
  mlugg/setup-zig v2.2.1, actions/upload-artifact v7.0.1,
  actions/download-artifact v8.0.1, pypa/gh-action-pypi-publish v1.14.2,
  step-security/harden-runner v2.21.1, ossf/scorecard-action v2.4.4,
  EmbarkStudios/cargo-deny-action v2.1.1, aws-actions/configure-aws-credentials
  v6.3.0, googleapis/release-please-action v5.0.0 (node24),
  sigstore/cosign-installer v4.1.2, github/codeql-action v4.38.0; cosign
  v3.1.3 (`sign-blob --yes --bundle FILE`, new bundle format by default;
  `verify-blob --bundle --certificate-identity-regexp
  --certificate-oidc-issuer`), cargo-auditable v0.7.6, cargo-cyclonedx
  0.5.9, pip-audit 2.10.1, actionlint 1.7.12, cfn-lint 1.56.3. Node 22.21.1
  ships npm 10.9.4 (below the 11.5.1 that npm trusted publishing needs);
  Node 24 ships npm 11.x.

Constraints: `openspec/project.md` hard rules (no invented AWS
parameters, one milestone at a time, ARM64 only, identifiers in English,
no inline comments in function bodies, never log tokens); `pnpm` only for
package management; no `git init` in this change (files only; the first
commit, the tags and the GitHub settings are manual steps listed in the
Migration Plan); Apache-2.0 everywhere (sibling change, final).

## Goals / Non-Goals

Goals:

- Every workflow in `.github/workflows` is SHA-pinned, read-only by
  default, hardened and `actionlint`-clean; Scorecard runs.
- Licences, advisories and sources of the Rust graph, and advisories of
  the Python pins and the npm production graph, are checked in CI and
  weekly, with a written policy for findings.
- `rayd` ships with an embedded dependency list (`cargo auditable`) and a
  CycloneDX SBOM; release artefacts are signed keyless with cosign and
  verifiable with one documented command.
- The `cfg(unix)` adapter suites and the sidecar's real-kernel tests run
  on real aarch64 in CI.
- The acceptance suites run on AWS from CI through OIDC with hard cost
  guardrails; the IAM role is a validated template.
- One lockstep version across the three components, cut by release-please
  and published by tag-driven jobs (PyPI, npm, GitHub release).
- The base image is pinned by digest and by explicit version on every
  publish; `SECURITY.md` says exactly what is in place.

Non-goals (report §5 and the brief):

- Licence text, `NOTICE`, PEP 639 metadata, community files, Dependabot,
  badges other than Scorecard, `docs/RELEASING.md` content beyond the
  workflow-filename alignment of D14: `m7-oss-hygiene`.
- Creating the git repository, pushing, tagging, registering trusted
  publishers, creating GitHub environments, deploying
  `infra/ci-oidc-role.yaml`, creating the AWS Budget: manual steps
  (Migration Plan), never done by this change.
- Publishing an image from CI, `harden-runner` in `block` mode, Scorecard
  score targets, SLSA provenance beyond what PyPI attestations, npm
  provenance and cosign bundles give, signing the Python wheel or the npm
  tarball with cosign (their registries attest), CodeQL analysis,
  container scanning of the MicroVM image, a Rust client SDK, crates.io
  publishing.
- Any runtime change to `rayd`, the sidecar, the image contents or the
  SDKs; any `.proto` edit.

## Decisions

### D1. Every action pinned to a full commit SHA with a version comment

Rule: `uses: owner/repo@<40-hex-sha> # vX.Y.Z` for every action in every
workflow, including reusable ones referenced by path (`github/codeql-action/upload-sarif@<sha> # v4.38.0`).
Never a tag, never a branch (`pypa/gh-action-pypi-publish@release/v1`
goes). The SHA is the commit the release tag points at (annotated tags
are dereferenced). Resolution recipe, used by the implementer and by
reviewers of Dependabot bumps:

```bash
r=actions/checkout; t=$(gh release view --repo $r --json tagName --jq .tagName)
o=$(gh api repos/$r/git/ref/tags/$t --jq '.object | "\(.type) \(.sha)"')
case $o in tag*) sha=$(gh api repos/$r/git/tags/${o#tag } --jq .object.sha);; *) sha=${o#commit };; esac
echo "$r@$sha # $t"
```

Values resolved 2026-09-16 (the implementer re-runs the recipe and uses
whatever is current at implementation time; a newer patch of the same
major is preferred, a newer major is taken only if CI stays green):

| Action | Tag | Commit |
|---|---|---|
| `actions/checkout` | v7.0.1 | `3d3c42e5aac5ba805825da76410c181273ba90b1` |
| `bufbuild/buf-action` | v1.5.0 | `8c6a16e16f12ba20b6470afa9c2ba9b5ba8c97c3` |
| `Swatinem/rust-cache` | v2.9.2 | `6323deb102c322ba6fcbdcafc7e3dddab59af2b6` |
| `astral-sh/setup-uv` | v10.1.0 | `bec219d24cd3e171d82865faccec33120bb574f4` |
| `pnpm/action-setup` | v6.1.0 | `ea17c68df8912ef543352723c149a84f56e3d413` |
| `actions/setup-node` | v7.0.0 | `820762786026740c76f36085b0efc47a31fe5020` |
| `mlugg/setup-zig` | v2.2.1 | `d1434d08867e3ee9daa34448df10607b98908d29` |
| `actions/upload-artifact` | v7.0.1 | `043fb46d1a93c77aae656e7c1c64a875d1fc6a0a` |
| `actions/download-artifact` | v8.0.1 | `3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c` |
| `pypa/gh-action-pypi-publish` | v1.14.2 | `dc37677b2e1c63e2034f94d8a5b11f265b73ba33` |
| `step-security/harden-runner` | v2.21.1 | `e14015d583714f6e62063499dc959a02595150a1` |
| `ossf/scorecard-action` | v2.4.4 | `2d1146689b8cda280b9bc96326124645441f03bc` |
| `github/codeql-action` | v4.38.0 | `b96794f015dfd88f77b49b1c93e0fa7110f94c63` |
| `EmbarkStudios/cargo-deny-action` | v2.1.1 | `3c6349835b2b7b196a839186cb8b78e02f7b5f25` |
| `aws-actions/configure-aws-credentials` | v6.3.0 | `e1253824e5c10ff9df46874f81ed3ec929e19cfd` |
| `googleapis/release-please-action` | v5.0.0 | `45996ed1f6d02564a971a2fa1b5860e934307cf7` |
| `sigstore/cosign-installer` | v4.1.2 | `6f9f17788090df1f26f669e9d70d6ae9567deba6` |

Tools installed by command keep their exact pins: `cargo install --locked
cargo-zigbuild@0.23.4 cargo-auditable@0.7.6 cargo-cyclonedx@0.5.9`,
`uvx pip-audit==2.10.1`, `uvx cfn-lint==1.56.3`, `buf` 1.73.0 through
`buf-action`'s `version:`, zig 0.16.0 through `setup-zig`'s `version:`,
`pnpm` 9.15.4 through `action-setup`'s `version:` (and `packageManager`),
`actionlint` 1.7.12 downloaded from its release with the checksum in the
Makefile recipe. The Dependabot `github-actions` ecosystem (sibling)
bumps the SHA and rewrites the version comment; `actionlint` fails on a
malformed pin, and a CI step `grep -nE 'uses: [^@]+@(v[0-9]|main|master|release/)'` fails on any
non-SHA reference so a hand edit cannot regress the rule.

### D2. Read-only by default, elevated per job; harden-runner in audit mode

Every workflow declares `permissions: contents: read` at the top. Jobs
that need more declare exactly what they need:

| Workflow · job | Extra permissions | Why |
|---|---|---|
| `scorecard.yml · analysis` | `security-events: write`, `id-token: write`, `actions: read` | SARIF upload, signed results publication |
| `release-please.yml · release-please` | `contents: write`, `pull-requests: write` | release PR, tags, GitHub releases |
| `release.yml · python` | `id-token: write` | PyPI OIDC (environment `pypi`) |
| `release.yml · typescript` | `id-token: write` | npm OIDC (environment `npm`) |
| `release.yml · rayd` | `id-token: write`, `contents: write` | Sigstore OIDC, uploading assets to the release |
| `e2e.yml · python`, `e2e.yml · typescript` | `id-token: write` | AWS OIDC (environment `e2e`) |

`ci.yml` and `audit.yml` stay at `contents: read` everywhere.
`actions/checkout` gets `persist-credentials: false` in every workflow
except `release-please.yml` (the action needs no checkout at all). Every
job's first step is `step-security/harden-runner` with
`egress-policy: audit` (the report's "audit mode"); `block` mode and
`allowed-endpoints` are deliberately out of scope until the audit
insights of a month of runs exist (the recommended StepSecurity path).
The ARM job runs harden-runner too (StepSecurity supports the
`ubuntu-24.04-arm` hosted runners); if the step fails to start there, the
implementer removes it from that one job and records why in the task
note, nowhere else.

### D3. Scorecard workflow

`.github/workflows/scorecard.yml`: `on: branch_protection_rule`,
`schedule: '23 6 * * 1'` (weekly, Monday), `push: branches: [main]`;
single job `analysis` on `ubuntu-24.04` with the permissions of D2,
steps harden-runner → checkout (`persist-credentials: false`) →
`ossf/scorecard-action` (`results_file: results.sarif`, `results_format:
sarif`, `publish_results: true`) → `actions/upload-artifact` (name
`SARIF file`, `retention-days: 5`) → `github/codeql-action/upload-sarif`
(`sarif_file: results.sarif`). `publish_results: true` requires a public
repository and the default branch; before the repository is public the
workflow still runs and only the publication step is refused by the
action, which is accepted. The README badge
(`https://api.scorecard.dev/projects/github.com/alejandro-cedeno-10/rayito/badge`) is
added next to the sibling's badges.

### D4. `deny.toml` and cargo-deny

Repository root `deny.toml` exactly as validated locally:

```toml
[graph]
targets = ["aarch64-unknown-linux-musl", "x86_64-unknown-linux-gnu", "x86_64-pc-windows-gnu"]
all-features = false

[advisories]
db-path = "$CARGO_HOME/advisory-dbs"
db-urls = ["https://github.com/rustsec/advisory-db"]
version = 2
yanked = "deny"
ignore = []

[licenses]
version = 2
allow = ["MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "Unicode-3.0", "Zlib", "MPL-2.0"]
confidence-threshold = 0.8
unused-allowed-license = "allow"

[[licenses.exceptions]]
crate = "notify"
allow = ["CC0-1.0"]

[licenses.private]
ignore = true

[bans]
multiple-versions = "warn"
wildcards = "deny"
allow-wildcard-paths = true
highlight = "all"

[sources]
unknown-registry = "deny"
unknown-git = "deny"
allow-registry = ["https://github.com/rust-lang/crates.io-index"]
```

Decisions inside it: the allowlist is the report's list verbatim; the
only crate outside it is `notify` (CC0-1.0, a public-domain dedication
that is "FSF Free/Libre" but not on the allowlist), handled as a named
exception rather than by widening the list, so a new CC0 crate still
fails; `allow-wildcard-paths = true` because the workspace's
`{ workspace = true }` path dependencies carry no version by design;
duplicates stay warnings (report: `multiple-versions = "warn"`), the seven
current ones are known and are not to be "fixed" here; the three `targets`
are the real build target plus the two developer hosts, so host-only
crates (`windows-sys`) are still audited. CI: job `deny` in `ci.yml`
using `EmbarkStudios/cargo-deny-action` with `command: check`
(all four checks) and `rust-version: "1.98"`; `audit.yml` runs the same
weekly with a fresh advisory database. Locally `make lint` runs
`cargo deny check` when `cargo-deny` is on `PATH` and prints how to
install it otherwise (the tool is not part of `rust-toolchain.toml`).
Policy for an advisory finding: fix by bumping the crate in the same PR
if a patched version exists; otherwise add an `ignore` entry with the
advisory id, the reason and a date, in the same PR, and open an issue.

### D5. Python and npm audits, in CI and weekly

`ci.yml` job `audit` (`ubuntu-24.04`, `astral-sh/setup-uv`,
`pnpm/action-setup`, `actions/setup-node`):

1. `uvx pip-audit==2.10.1 -r kernel-sidecar/requirements.txt --no-deps
   --strict` (the exact pins the image installs; `--no-deps` because the
   file is already a complete lock and `pip-audit` would otherwise resolve
   on x86).
2. `(cd clients/python && uv export --frozen --no-dev --no-emit-project
   -o /tmp/rayito-client.txt)` then `uvx pip-audit==2.10.1 -r
   /tmp/rayito-client.txt --no-deps --strict` (the SDK's runtime graph:
   `grpcio`, `protobuf`, `boto3` and their transitive pins); the same over
   `kernel-sidecar`'s `uv.lock` export (the sidecar's own locked
   environment, audited for completeness even though the image installs
   `requirements.txt`).
3. `cd clients/typescript && pnpm install --frozen-lockfile && pnpm audit
   --prod --audit-level moderate`.

`.github/workflows/audit.yml`: `schedule: '41 6 * * 1'` + `workflow_dispatch`;
jobs `rust` (cargo-deny-action, `command: check advisories`), `python`
(steps 1–2) and `node` (step 3 plus `pnpm audit --audit-level high` over
all scopes, so dev-only findings are reported weekly but only fail at
`high`). The two moderate dev-only findings measured today (`vitest
3.2.7`, patched in 4.1.11) are handled in this change: bump `vitest` to
the latest 4.x in `clients/typescript/package.json` and `pnpm-lock.yaml`;
if the 292 unit tests do not pass unchanged, keep 3.2.7, record the
failure in the task note and let the weekly job report it (it is below
`high`). Policy for a runtime finding: same as D4 (bump in the same PR,
else a documented ignore with `--ignore-vuln <id>` / pnpm `auditConfig`
and an issue).

### D6. ARM job

`ci.yml` job `arm` (`name: test on aarch64 (ubuntu-24.04-arm)`,
`runs-on: ubuntu-24.04-arm`, `needs: []`, free on public repositories;
until the repository is public the job is billed at the private rate,
accepted for the short window): harden-runner → checkout → `rustup show
active-toolchain` (installs the aarch64-unknown-linux-gnu host toolchain
from `rust-toolchain.toml`) → `Swatinem/rust-cache` → `cargo test
--workspace --locked` (the `cfg(unix)` adapter suites run natively; the
tests already self-skip what needs root, as on the x86 `check` job) →
`astral-sh/setup-uv` (`python-version: "3.12"`) → `cd kernel-sidecar &&
uv run --with-requirements requirements.txt pytest` and the same with `-m
kernel` (the `manylinux_2_28_aarch64` wheels pinned for the image are the
ones installed, which the x86 job cannot exercise). `cargo zigbuild` is
not repeated on ARM: the release binary is produced once, on x86, by the
`build` job (D7). The job does not gate `build` (`needs: check` stays as
is) so an ARM runner shortage cannot block the artefact; it does gate the
branch through required checks (Migration Plan).

### D7. `cargo auditable` and the CycloneDX SBOM

`ci.yml` `build` job and `release.yml` `rayd` job build with

```
cargo install --locked cargo-zigbuild@0.23.4 cargo-auditable@0.7.6 cargo-cyclonedx@0.5.9
cargo auditable zigbuild --release --locked --target aarch64-unknown-linux-musl -p rayd
cargo cyclonedx --manifest-path crates/rayd/Cargo.toml --target aarch64-unknown-linux-musl --format json --no-build-deps --spec-version 1.5
```

(measured locally, see Context). `cargo auditable` wraps `rustc` through
`RUSTC_WORKSPACE_WRAPPER`, so `zigbuild` is a transparent subcommand.
A step `python3 scripts/check_auditable.py target/aarch64-unknown-linux-musl/release/rayd`
parses the ELF section headers with the standard library, decompresses
`.dep-v0` and asserts the root package is `rayd` with the version of
`Cargo.toml` and that `tonic`, `tokio`, `axum` and `nix` are present (the
same check `make build` runs; no third-party tool on the dev machine).
`crates/rayd/rayd.cdx.json` (the tool's default name and location,
added to `.gitignore`) is uploaded with the CI artefact
`rayd-aarch64-musl` and attached to the GitHub release. It is never
written under `image/`: `image_zip.py` excludes only `.zip`/`.pyc` and
a JSON file there would be zipped into the image artefact. The SBOM
describes the crate graph only; the image's Python pins are already a
complete list (`requirements.txt`) and the sidecar is never published, so
no second SBOM format is produced. `Makefile`: `build` becomes
`cargo auditable zigbuild …` when `cargo-auditable` is on `PATH` and
plain `cargo zigbuild` otherwise (printing which), so `make image-zip`
keeps working on a machine without the tool; a new `sbom` target runs the
cyclonedx command and prints the path.

### D8. `e2e.yml` with OIDC and the cost guardrails

`.github/workflows/e2e.yml`:

- `on: workflow_dispatch` with inputs `suite` (choice `python` |
  `typescript` | `both`, default `python`), `template_version` (string,
  optional, mapped to `RAYITO_TEMPLATE_VERSION`) and `expression` (string,
  optional, `-k` filter for pytest); `schedule: '17 5 * * *'` (nightly,
  05:17 UTC, suite `python`).
- `concurrency: { group: e2e, cancel-in-progress: false }` (a second run
  queues; the pre-flight would fail it anyway if sandboxes are alive).
- `permissions: contents: read`; jobs `python` (`timeout-minutes: 75`:
  the seven e2e files measured 5–9 min each) and `typescript`
  (`timeout-minutes: 30`, `needs: python` when `both`), both with
  `environment: e2e` and `id-token: write`.
- Steps: harden-runner → checkout → `aws-actions/configure-aws-credentials`
  with `role-to-assume: ${{ vars.RAYITO_E2E_ROLE_ARN }}`, `aws-region:
  ${{ vars.RAYITO_E2E_REGION || 'us-east-1' }}`, `role-session-name:
  rayito-e2e-${{ github.run_id }}`, `role-duration-seconds: 3600` →
  pre-flight step:

  ```bash
  live=$(aws lambda-microvms list-microvms --image-identifier "$RAYITO_TEMPLATE" \
    --query "items[?state!='TERMINATED' && state!='TERMINATING'].microvmId" --output text | wc -w)
  test "$live" -le 10 || { echo "pre-flight: $live live MicroVMs of $RAYITO_TEMPLATE (> 10)"; exit 1; }
  ```

  → `astral-sh/setup-uv` → `cd clients/python && RAYITO_E2E=1 uv run
  pytest tests/e2e -m e2e -v` with `RAYITO_TEMPLATE: ${{ vars.RAYITO_E2E_TEMPLATE_ARN }}`
  (an ARN, never a name, so the role policy of D8b matches exactly) →
  sweeper step with `if: always()`: the same listing piped to
  `xargs -r -n1 aws lambda-microvms terminate-microvm --microvm-identifier`
  (idempotent; `TerminateMicrovm` is 10 TPS and the listing is at most a
  handful of ids). The TypeScript job mirrors it with `pnpm install
  --frozen-lockfile && pnpm test:e2e`. `RAYITO_EXECUTION_ROLE_ARN` is not
  set (no runtime logs in CI; the tests that need it self-skip).
- Header comment (Spanish, like `release.yml`) with the cost note:
  "≈ $0,03 por ejecución (`AWS_API_NOTES.md` §12: lectura de snapshot +
  cómputo de ~12 sandboxes cortos); nightly ≈ $1/mes; nunca construye
  una versión de imagen (+$0,037/semana cada una)", the three repository
  variables, and the manual steps (deploy the role, create the `e2e`
  environment with required reviewers, set the variables, create an AWS
  Budget of $10/month filtered on service `AWS Lambda` with an email alert
  at 80 %).
- The workflow never runs `make image-publish` (MILESTONES M1 promised it;
  this design supersedes that line and the task edits `MILESTONES.md`):
  publishing needs `s3:PutObject`, image creation and `iam:PassRole` on
  the build role, which contradicts "scoped to the test image ARN"; the
  nightly runs the latest `ACTIVE` version of `rayito-base` published by
  the maintainer.

**D8b. `infra/ci-oidc-role.yaml`** (CloudFormation, `cfn-lint` clean,
not deployed):

- Parameters: `GitHubRepository` (default `alejandro-cedeno-10/rayito`),
  `GitHubEnvironment` (default `e2e`), `TestImageArns` (CommaDelimitedList,
  the `rayito-base` ARN and optionally `rayito-base-caps`),
  `CreateOidcProvider` (`true`/`false`, default `true`; `false` reuses the
  account's existing `token.actions.githubusercontent.com` provider),
  `ExecutionRoleArn` (optional, empty by default), `RoleName` (default
  `rayito-e2e-github`).
- `AWS::IAM::OIDCProvider` (conditional): `Url:
  https://token.actions.githubusercontent.com`, `ClientIdList:
  [sts.amazonaws.com]`, `ThumbprintList` with GitHub's two documented
  thumbprints (`6938fd4d98bab03faadb97b34396831e3780aea1`,
  `1c58a3a8518e8759bf075b76b750d4f2df264fcd`).
- `AWS::IAM::Role`: trust `Federated` = provider ARN,
  `sts:AssumeRoleWithWebIdentity`, `Condition: StringEquals:
  token.actions.githubusercontent.com:aud: sts.amazonaws.com` and
  `token.actions.githubusercontent.com:sub:
  repo:${GitHubRepository}:environment:${GitHubEnvironment}` (exact match,
  no wildcard: only that environment of that repository assumes the role),
  `MaxSessionDuration: 3600`.
- Inline policy, only the actions the SDKs and the e2e call (read from the
  code: `run_microvm`, `get_microvm`, `list_microvms`, `suspend_microvm`,
  `resume_microvm`, `terminate_microvm`, `create_microvm_auth_token`,
  `sts:GetCallerIdentity`): `lambda:RunMicrovm`, `GetMicrovm`,
  `SuspendMicrovm`, `ResumeMicrovm`, `TerminateMicrovm`,
  `CreateMicrovmAuthToken` on `TestImageArns`; `lambda:ListMicrovms` on
  `*`; `lambda:PassNetworkConnector` on
  `arn:aws:lambda:${AWS::Region}:aws:network-connector:aws-network-connector:*`
  (the managed connectors are passed by default, `AWS_API_NOTES.md` §10);
  `iam:PassRole` on `ExecutionRoleArn` only when the parameter is set
  (condition). No image, S3, quota or tagging permissions (`ListMicrovmImages`
  is not needed: the workflow passes an ARN and `resolve_template_arn`
  returns ARNs untouched).
- Outputs: `RoleArn` (the value of the `RAYITO_E2E_ROLE_ARN` variable).
- `infra/README.md` gains the row and the deploy command
  (`aws cloudformation deploy --stack-name rayito-ci-oidc --template-file
  infra/ci-oidc-role.yaml --capabilities CAPABILITY_NAMED_IAM
  --parameter-overrides TestImageArns=<arn>`), and `make infra-lint`
  lints both templates.

### D9. release-please, manifest mode, one lockstep version

`release-please-config.json`:

```json
{
  "$schema": "https://raw.githubusercontent.com/googleapis/release-please/main/schemas/config.json",
  "bump-minor-pre-major": true,
  "bump-patch-for-minor-pre-major": false,
  "include-component-in-tag": true,
  "include-v-in-tag": true,
  "tag-separator": "-",
  "separate-pull-requests": false,
  "changelog-sections": [
    {"type": "feat", "section": "Added"},
    {"type": "fix", "section": "Fixed"},
    {"type": "perf", "section": "Changed"},
    {"type": "refactor", "section": "Changed"},
    {"type": "docs", "section": "Documentation"},
    {"type": "chore", "section": "Miscellaneous", "hidden": true},
    {"type": "ci", "section": "Miscellaneous", "hidden": true},
    {"type": "test", "section": "Miscellaneous", "hidden": true}
  ],
  "packages": {
    "clients/python": {
      "component": "python",
      "release-type": "python",
      "package-name": "rayito",
      "changelog-path": "CHANGELOG.md",
      "extra-files": [{"type": "generic", "path": "src/rayito/_version.py"}]
    },
    "clients/typescript": {
      "component": "typescript",
      "release-type": "node",
      "package-name": "rayito",
      "changelog-path": "CHANGELOG.md",
      "extra-files": [{"type": "generic", "path": "src/version.ts"}]
    },
    "crates/rayd": {
      "component": "rayd",
      "release-type": "simple",
      "package-name": "rayd",
      "changelog-path": "CHANGELOG.md",
      "extra-files": [
        {"type": "toml", "path": "/Cargo.toml", "jsonpath": "$.workspace.package.version"},
        {"type": "toml", "path": "/Cargo.lock", "jsonpath": "$.package[?(@.name=='rayd')].version"},
        {"type": "toml", "path": "/Cargo.lock", "jsonpath": "$.package[?(@.name=='rayd-core')].version"},
        {"type": "toml", "path": "/Cargo.lock", "jsonpath": "$.package[?(@.name=='rayito-proto')].version"}
      ]
    }
  },
  "plugins": [
    {"type": "linked-versions", "groupName": "rayito", "components": ["python", "typescript", "rayd"]}
  ]
}
```

`.release-please-manifest.json`: `{"clients/python": "0.1.0",
"clients/typescript": "0.0.5", "crates/rayd": "0.1.0"}` (the versions in
the manifests today). Decisions:

- **`simple` for `rayd`, not `rust`**: the `rust` strategy cannot handle
  `[workspace.package].version` with `version.workspace = true` members
  (Context); `simple` with the `version.txt` update silently skipped
  (`createIfMissing: false`) plus typed TOML `extra-files` updates the
  root `Cargo.toml` and the three `[[package]]` entries of `Cargo.lock`
  with formatting preserved. The `cargo-workspace` plugin is not used
  (same limitation). `rayd`'s `agent_version` is `CARGO_PKG_VERSION`, so
  nothing else carries the Rust version.
- **Generic annotations**: `clients/python/src/rayito/_version.py`
  becomes `__version__ = "0.1.0"  # x-release-please-version` and
  `clients/typescript/src/version.ts` becomes `export const VERSION =
  "0.0.5"; // x-release-please-version` (module-level trailing comments,
  allowed by the comment rules; the existing parity unit tests keep
  guarding `pyproject.toml`/`package.json` against them).
- **Lockstep**: the `linked-versions` group makes the first release PR
  bump all three to the highest result (a `feat:` since 0.1.0 →
  **0.2.0**, TypeScript included, as the report recommends); afterwards
  every release moves the three together. `bump-minor-pre-major` keeps
  breaking changes at `0.x` minor bumps until 1.0 is a decision.
- **Components in tags**: `python-v0.2.0`, `typescript-v0.2.0`,
  `rayd-v0.2.0` (`include-component-in-tag`, separator `-`), matching
  `release.yml`'s triggers and the sibling's `docs/RELEASING.md`.
- **Workflow** `.github/workflows/release-please.yml`: `on: push:
  branches: [main]`; job `release-please` with D2 permissions;
  `googleapis/release-please-action` with `config-file:
  release-please-config.json`, `manifest-file:
  .release-please-manifest.json`, `token: ${{ secrets.RELEASE_PLEASE_TOKEN
  || github.token }}`. A fine-grained PAT (`contents: write`,
  `pull-requests: write`, this repository only) stored as
  `RELEASE_PLEASE_TOKEN` is the documented way to have the tags trigger
  `release.yml`; with the default token the tags are created but
  `release.yml` must be run through `workflow_dispatch` with the `tag`
  input (D10). Both paths are supported so the first release cannot be
  blocked by the missing secret.
- **No commits, no bootstrap SHA**: the repository has no history; the
  first run considers every conventional commit since the initial one.
  The initial import commit must be `chore: import rayito 0.1.0` (not
  `feat:`) so the first release PR reflects only the M7 work; documented
  in the Migration Plan. The config is validated locally against the
  published JSON schema (task) because `release-please` itself needs a
  GitHub repository to dry-run.

### D10. `release.yml`: three tag-driven jobs

Triggers: `push: tags: ["python-v*", "typescript-v*", "rayd-v*"]` and
`workflow_dispatch` with inputs `tag` (string, optional: a tag to
publish when release-please used the default token) and `dry_run`
(boolean, default `true`). A first job `resolve` (`contents: read`)
computes `component` (`python` | `typescript` | `rayd`), `version` and
`publish` (`true` only for a tag push, or a dispatch with `tag` set and
`dry_run: false`) from `github.ref_name` / `inputs.tag` with a shell
`case`, and fails on any other tag shape. Each component job runs
`if: needs.resolve.outputs.component == '<name>'`:

- **`python`** (environment `pypi`, `id-token: write`): the existing
  steps unchanged (`uv build`, `scripts/check_wheel.py`, `twine check`,
  tag-equals-`pyproject.toml` check) then
  `pypa/gh-action-pypi-publish@<sha>` with `packages-dir:
  clients/python/dist` (attestations default `true`); the publish step is
  skipped when `publish` is `false`. The trusted publisher stays
  registered on `release.yml` / environment `pypi` (sibling runbook).
- **`typescript`** (environment `npm`, `id-token: write`):
  `pnpm/action-setup` (9.15.4), `actions/setup-node` with `node-version:
  24` and `registry-url: https://registry.npmjs.org`; step "npm ≥ 11.5.1"
  runs `npm --version` and compares with `sort -V` (fails otherwise; Node
  24 bundles 11.x, documented in the header); tag-equals-`package.json`
  check with `node -p`; `pnpm install --frozen-lockfile`, `pnpm build`,
  `pnpm pack:check`, `pnpm pack` → `rayito-<version>.tgz`; publish step
  `npm publish rayito-<version>.tgz --access public` (no token: with
  `id-token: write` and the trusted publisher registered for
  `release.yml` / environment `npm`, npm ≥ 11.5.1 exchanges the OIDC token
  and attaches provenance automatically). This is the single place the
  repository invokes `npm` instead of `pnpm`, because trusted publishing
  is implemented in the npm CLI only (pnpm 9.15.4 has no OIDC exchange);
  the header comment says so. The sibling's runbook names the workflow
  `release-npm.yml`; D14 aligns it to `release.yml`.
- **`rayd`** (`id-token: write`, `contents: write`): `mlugg/setup-zig`
  0.16.0, `Swatinem/rust-cache`, the D7 build and SBOM, tag-equals-root-
  `Cargo.toml` `[workspace.package].version` check (`python3 -c "import
  tomllib…"`), `python3 scripts/check_auditable.py`, `scripts/copy_sidecar.py`
  + `scripts/image_zip.py image image/rayito-image.zip` (the same artefact
  the maintainer publishes), `sigstore/cosign-installer`, then

  ```
  cosign sign-blob --yes --bundle rayd.sigstore.json target/aarch64-unknown-linux-musl/release/rayd
  cosign sign-blob --yes --bundle rayito-image.zip.sigstore.json image/rayito-image.zip
  cosign verify-blob --bundle rayd.sigstore.json \
    --certificate-identity-regexp '^https://github.com/alejandro-cedeno-10/rayito/\.github/workflows/release\.yml@refs/tags/rayd-v' \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com target/aarch64-unknown-linux-musl/release/rayd
  ```

  (and the same verification for the zip) as a self-check, `sha256sum`
  of the four files into `SHA256SUMS`, and `gh release upload
  "rayd-v<version>" rayd rayd.sigstore.json rayito-image.zip
  rayito-image.zip.sigstore.json rayd.cdx.json SHA256SUMS --clobber`
  (`GH_TOKEN: ${{ github.token }}`; the release exists because
  release-please created it with the tag). The binary is renamed to
  `rayd` (no target suffix: the project has one target). On dry-run the
  signing runs (OIDC works on any workflow run) and only the upload is
  skipped.

Every job uploads its artefacts with `actions/upload-artifact` so a
dry-run leaves inspectable output. The header comment lists the manual
steps: PyPI and npm trusted publishers on `release.yml`, environments
`pypi`, `npm` (both with required reviewers), `RELEASE_PLEASE_TOKEN`.

**Verification recipe for users** (`docs/site/docs/verify.md`, linked from
`README.md` and `docs/RELEASING.md`):

```
cosign verify-blob --bundle rayito-image.zip.sigstore.json \
  --certificate-identity-regexp '^https://github.com/alejandro-cedeno-10/rayito/\.github/workflows/release\.yml@refs/tags/rayd-v' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com rayito-image.zip
cargo audit bin rayd            # reads .dep-v0 (cargo-audit ≥ 0.17)
pip download rayito==<v> --no-deps && python -m pypi_attestations verify …   # or the PyPI "Verified" badge
npm view rayito@<v> dist.attestations
```

### D11. Base image pinned by digest and by version

- `image/Dockerfile`: `FROM public.ecr.aws/lambda/microvms:al2023-minimal@sha256:05cb9b38d841e7ff1b693dc9e894909612f340bf99ec97d426e8000a5bbe96c3`
  (the manifest-list digest; it is what `docker buildx imagetools
  inspect`, `docker manifest inspect` and the Dependabot `docker`
  ecosystem resolve for the tag, so bumps stay mechanical; the arm64
  manifest `sha256:63831a97…` is recorded in the comment as the image
  actually pulled). The comment above `FROM` is rewritten: how the digest
  was recorded (the two commands of Context, one with Docker and one
  without), the date, and that `AWS_API_NOTES.md` §4 documents the base
  version separately. The tag is kept in front of `@sha256:` for
  readability; a digest pin is honoured by any OCI-compliant puller,
  including AWS's image builder (verified by the acceptance publish).
- `scripts/publish_image.py`: `--base-image-version` becomes required
  (argparse `required=True`; a missing flag exits 2 with the usage line,
  before any AWS call), `PublishSettings.base_image_version: str`, and
  the `configuration` always carries `baseImageVersion`. Value passed by
  the Makefile: `BASE_IMAGE_VERSION ?= 1`, the `imageVersion` string that
  `list-managed-microvm-image-versions` returns for the newest version
  (today `1`; `0` also exists). The accepted spelling is measured by the
  acceptance publish (task 0.2): if `update-microvm-image` rejects `1`
  with a `ValidationException`, the Makefile default becomes the echoed
  form `1.0` and both facts go to `AWS_API_NOTES.md` §16 Q52. All four
  publish targets (`image-publish`, `-slim`, `-caps`, and the `PUBLISH_ARGS`
  path) pass `--base-image-version $(BASE_IMAGE_VERSION)`. Unit tests in
  `scripts/tests/test_publish_image.py`: the parser refuses a call without
  the flag, the request body carries `baseImageVersion`, existing tests
  updated to pass it.
- `docs/site/docs/security.md` and `SECURITY.md` (D12) say how to refresh
  the digest when AWS rolls the tag (Dependabot PR, or the two commands),
  and that a digest bump is a new image version, published and accepted
  with the e2e like any other.

### D12. `SECURITY.md`, docs and milestone bookkeeping

- T10 row, mitigation column: replace the list with the file names now in
  place: `image/Dockerfile` `FROM …@sha256:` + `--base-image-version`
  obligatoria (`scripts/publish_image.py`), `deny.toml` + `cargo deny`
  (licencias, advisories, fuentes), `pip-audit` sobre `requirements.txt`
  y los `uv export`, `pnpm audit --prod`, `cargo auditable` (`.dep-v0`) +
  SBOM CycloneDX, cosign keyless sobre `rayd` y `rayito-image.zip`,
  acciones fijadas por SHA con `permissions` mínimos y `harden-runner`,
  Scorecard, Dependabot (sibling). Hito column: `M1 / M7 ✔` once accepted.
- "Cadena de suministro" table: the `CI` row becomes "acciones fijadas por
  SHA de commit con comentario de versión, `permissions: contents: read`
  global, `harden-runner` (audit), `actionlint` en `make lint` y CI,
  `ubuntu-24.04` x86 + `ubuntu-24.04-arm`, e2e sólo con rol OIDC
  (`infra/ci-oidc-role.yaml`, `environment: e2e`) y `workflow_dispatch`/
  nightly con pre-flight (> 10 VMs vivos → falla) y sweeper"; new rows
  `Dependencias` (cargo-deny, pip-audit, pnpm audit, semanal), `SBOM y
  firma` (`.dep-v0`, `rayd.cdx.json`, bundles cosign, verificación en
  `docs/site/docs/verify.md`), `Publicación` (release-please lockstep,
  PyPI Trusted Publishing + attestations, npm trusted publishing +
  provenance, npm ≥ 11.5.1); `Imagen base` row gains the digest value and
  the refresh recipe.
- `MILESTONES.md`: the M1 CI paragraph's `e2e.yml` sentence gets a note
  "(M7: sin `make image-publish`, ver `m7-supply-chain` D8)"; the M7
  section created by the sibling gets this change's row (scope,
  acceptance, size M) and, after acceptance, the measured numbers.
- `docs/RELEASING.md` (sibling): the automated flow paragraph (release
  PR → merge → tags → `release.yml`), workflow filename `release.yml` for
  the npm trusted publisher, `RELEASE_PLEASE_TOKEN`, and the dry-run
  dispatch.
- `README.md`: Scorecard badge and a three-line "Verificar una release"
  snippet linking `docs/site/docs/verify.md`; `docs/site/mkdocs.yml` nav
  gains `verify.md` (strict build must stay green).
- `AWS_API_NOTES.md` §16 Q52: accepted `baseImageVersion` spelling, the
  echoed value, and that the digest-pinned `FROM` built `SUCCESSFUL`
  (version number, build time, snapshot sizes next to 16.0's).

### D13. Local validation without `act`

`act` is unavailable and would not exercise OIDC anyway. What is
validated locally, by task, before anything runs on GitHub:

- `actionlint` 1.7.12 (`-no-color`; `-shellcheck=` empty when shellcheck
  is absent, as on the Windows host) over every workflow: exit 0. Added
  to `make lint` (skips with a message when the binary is absent) and as
  a CI step in `check` (`actionlint` downloaded by its release script,
  pinned version and checksum).
- `cargo deny check` with the final `deny.toml`: all four `ok`.
- `pip-audit` over the three requirement sets: "No known vulnerabilities
  found". `pnpm audit --prod`: clean.
- `cargo auditable zigbuild` + `scripts/check_auditable.py`: root `rayd`
  + version; `cargo cyclonedx`: `rayd.cdx.json` parses, `specVersion`
  `1.5`, metadata component `rayd`.
- `uvx cfn-lint==1.56.3 infra/ci-oidc-role.yaml` and `aws cloudformation
  validate-template` (server side, free): clean.
- `release-please-config.json` validated with `uvx check-jsonschema
  --schemafile https://raw.githubusercontent.com/googleapis/release-please/main/schemas/config.json
  release-please-config.json` and the manifest with `python3 -m
  json.tool`; a throwaway script in the scratchpad (Python `tomllib`)
  asserts that `Cargo.toml` has exactly one `workspace.package.version`
  and `Cargo.lock` exactly one `[[package]]` entry for each of `rayd`,
  `rayd-core`, `rayito-proto`, and `grep -c x-release-please-version`
  returns 1 for `_version.py` and `version.ts`.
- `scripts/tests/test_publish_image.py`, `cargo test --workspace`, the
  Python/TypeScript gates and `mkdocs --strict` as in `ci.yml`.

What can only be verified on GitHub/AWS is the acceptance list below.

### D14. Coordination with `m7-oss-hygiene`

Both changes edit `ci.yml`, `SECURITY.md`, `README.md`, `MILESTONES.md`
and `docs/RELEASING.md`. Rules: this change is applied **after** the
sibling's edits to those files land (its `check_license.py` step in
`check`, its `SECURITY.md` reporting section, its badges and M7 section
are kept verbatim and only extended); the workflow filename for the npm
trusted publisher is `release.yml` (this design), so the sibling's
`docs/RELEASING.md` sentence naming `release-npm.yml` is corrected here;
`crates/rayd/CHANGELOG.md` is created by whichever change lands first
with the same Keep a Changelog header; the Dependabot `github-actions`
and `docker` entries of the sibling are what keep D1 and D11 pins fresh.
The Apache-2.0 relicensing is assumed complete (the `deny.toml`
allowlist and the `NOTICE` of the sibling are consistent).

## Risks / Trade-offs

- **Pinned SHAs go stale / a bump breaks CI.** Dependabot (sibling) opens
  the PRs; CI on the PR is the test. A major-version jump that breaks a
  job is reverted to the previous SHA in the same PR, never to a tag.
- **`harden-runner` on ARM or with the AWS SDK's egress.** Audit mode
  never blocks; the only failure mode is the agent not starting on
  `ubuntu-24.04-arm`, handled per D2.
- **cargo-deny false positives on licence detection** (a crate with a
  non-standard `LICENSE` text below the 0.8 confidence). Handled by a
  `[[licenses.clarify]]` entry citing the file, never by lowering the
  threshold.
- **`pip-audit --no-deps` audits only what is listed.** `requirements.txt`
  and the `uv export`s are complete locks, so nothing is hidden; the
  weekly job re-runs with the latest vulnerability data.
- **release-please TOML jsonpath on `Cargo.lock`.** If a future release
  PR fails to update a `[[package]]` entry, `cargo test --locked` on the
  release PR fails visibly (that is why CI uses `--locked`); the fix is
  the jsonpath, never a hand-edited lockfile.
- **Tags created with `GITHUB_TOKEN` do not trigger `release.yml`.**
  Both paths exist (PAT or `workflow_dispatch` with `tag`); the runbook
  says which is in use.
- **npm trusted publishing needs the package to exist and npm ≥ 11.5.1.**
  The first publish is manual (sibling runbook); the workflow asserts the
  npm version and fails early with a clear message.
- **Digest pin refused by AWS's builder.** Not expected (standard OCI
  reference); if the acceptance build fails with
  `CONTAINER_BUILD_FAILED` on the `FROM` line, the fallback is pinning
  the arm64 manifest digest `63831a97…` instead, recorded in Q52.
- **`ubuntu-24.04-arm` billing on a private repository.** Free once
  public; a few weeks of private minutes are accepted.
- **A nightly e2e that hangs** is bounded by `timeout-minutes`, and the
  `always()` sweeper plus every sandbox's `maximumDurationInSeconds ≤
  1800` bound the cost to cents.
- **OIDC role too narrow for a future e2e** (own egress connector, caps
  image). `TestImageArns` is a list and the connector ARN can be added as
  a parameter later; the template is versioned like the code.

## Migration Plan

Manual, one-time steps (documented in `docs/RELEASING.md` and
`infra/README.md`; none executed by this change):

1. Create the git repository with the initial commit `chore: import
   rayito 0.1.0`; push `main`; enable branch protection with required
   checks `lint + test (x86_64)`, `typescript client …`, `kernel-sidecar
   …`, `rayd aarch64-unknown-linux-musl`, `docs site …`, `cargo-deny`,
   `dependency audit`, `test on aarch64 (ubuntu-24.04-arm)`, `actionlint`.
2. Deploy `infra/ci-oidc-role.yaml` (`TestImageArns` = the `rayito-base`
   ARN); create the GitHub environment `e2e` with required reviewers and
   the repository variables `RAYITO_E2E_ROLE_ARN`,
   `RAYITO_E2E_TEMPLATE_ARN`, `RAYITO_E2E_REGION`; create the AWS Budget.
3. Create environments `pypi` and `npm`; register the PyPI trusted
   publisher (`release.yml`, `pypi`) and, after the manual first npm
   publish, the npm trusted publisher (`release.yml`, `npm`); store
   `RELEASE_PLEASE_TOKEN` (optional).
4. Merge the first release-please PR (0.2.0 across the three
   components); watch `release.yml` on the three tags; run the
   verification recipe on the published assets.

Rollback: every workflow is a file; reverting the commit restores the
previous CI. `publish_image.py`'s required flag is the only local
behaviour change and the Makefile carries the default.

## Open Questions

None left open at design time. Two facts are converted into
measurements with a fixed rule: the accepted spelling of
`baseImageVersion` (D11 → Q52) and whether AWS's builder honours the
digest-pinned `FROM` (D11 fallback → Q52). Whether `harden-runner`
starts on `ubuntu-24.04-arm` has a fixed fallback (D2).

## Acceptance test list

The change is accepted when, on the real repository and the real AWS
account:

1. `ci.yml` is green on a PR with every job including `deny`, `audit`,
   `arm` and the `actionlint` step; the `rayd-aarch64-musl` artefact
   contains `rayd`, `rayito-image.zip` and `rayd.cdx.json`, and
   `scripts/check_auditable.py` passed in the log.
2. `scorecard.yml` ran on `main` and uploaded SARIF (publication may be
   refused while private).
3. `make image-publish` from a workstation with the digest-pinned
   `Dockerfile` and `--base-image-version` produced a `SUCCESSFUL`
   version of `rayito-base` (Q52 recorded), and `RAYITO_TEMPLATE_VERSION`
   set to it passes `pytest tests/e2e -m e2e` locally.
4. `e2e.yml` dispatched from GitHub assumed the role
   (`aws sts get-caller-identity` shows `rayito-e2e-<run_id>`), passed
   pre-flight, ran the Python suite green and left zero non-terminated
   MicroVMs of the template (checked with the pre-flight expression after
   the run); the run's cost in Cost Explorer the next day is recorded in
   the M7 row.
5. release-please opened a PR titled `chore: release 0.2.0` (or the
   configured title) bumping `pyproject.toml`, `_version.py`,
   `package.json`, `version.ts`, root `Cargo.toml` and the three
   `Cargo.lock` entries together, with three changelogs; CI is green on
   that PR with `--locked`.
6. After merging it, `release.yml` ran once per tag: the PyPI release
   shows attestations, `npm view rayito@0.2.0 dist.attestations` lists
   the provenance, and the GitHub release `rayd-v0.2.0` carries `rayd`,
   `rayito-image.zip`, both `.sigstore.json` bundles, `rayd.cdx.json` and
   `SHA256SUMS`; `cosign verify-blob` with the documented identity regexp
   passes on the downloaded zip and binary from a clean machine.
7. `actionlint`, `cargo deny check`, `pip-audit` and `pnpm audit --prod`
   are green locally and their outputs are pasted in the task notes.
