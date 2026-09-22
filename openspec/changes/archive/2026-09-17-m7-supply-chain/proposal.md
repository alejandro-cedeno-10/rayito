## Why

M1–M6 are accepted against real AWS and the repository is about to go
public (M7, `docs/research/2026-09-m7-oss-readiness.md`). Its §4 table
lists what a 2026 open-source baseline expects and what is missing today:
`ci.yml` pins every action by major tag and declares no `permissions:`
(the tj-actions compromise of March 2025 turned a mutable tag into a
credential-stealing step in thousands of repositories), no SBOM is
produced for the binary that runs as root inside every MicroVM, no
dependency is audited for licences or advisories in CI, the `cfg(unix)`
adapter suites and the sidecar's real-kernel tests have never run on an
aarch64 host even though the product is ARM64-only,
`.github/workflows/e2e.yml` (promised since M1 in `MILESTONES.md`) does
not exist, the release artefacts (`rayd`, `rayito-image.zip`) carry no
signature, npm has no publishing path at all, three components are
versioned by hand, and the `Dockerfile` still says "the digest is fixed
when `image-publish` exists" while `image-publish` has existed since M1
and `SECURITY.md` T10 already claims `FROM …@sha256:<digest>` and an
explicit `--base-image-version`.

This change is **Track 2** of M7 (report §5, item 2, "the gate for going
public"): everything in §4 of the report plus release tooling. Licensing,
community files, Dependabot and the release runbook are the sibling change
`m7-oss-hygiene`; the product changes of M7 (`m7-suspended-pool`,
`m7-s3-persistence`, `m7-mcp-server`, `m7-cli`, `m7-poly-kernels`) are not
touched.

## What Changes

Every decision is closed in `design.md`; the summary:

- **Actions hardening.** Every `uses:` in every workflow is pinned to a
  full 40-character commit SHA with a `# vX.Y.Z` comment; every workflow
  declares `permissions: contents: read` at the top level and elevates
  only per job; every job starts with `step-security/harden-runner`
  (`egress-policy: audit`); a `scorecard.yml` workflow runs
  `ossf/scorecard-action` weekly and on `main`; `actionlint` becomes a
  local gate (`make lint` step and a CI step) so the YAML is validated
  without `act`.
- **Dependency audits.** `deny.toml` (licence allowlist from the report,
  one crate exception for `notify`'s CC0-1.0, RustSec advisories, crates.io
  as the only source, duplicate versions as warnings) checked by
  `EmbarkStudios/cargo-deny-action` in CI; `pip-audit` over
  `kernel-sidecar/requirements.txt` and the `uv export` of both Python
  projects; `pnpm audit --prod` for the TypeScript client; the same three
  audits in a weekly cron workflow (`audit.yml`) that also reports the
  dev-dependency scope.
- **SBOM and provenance of the binary.** `rayd` is built with
  `cargo auditable zigbuild` (the `.dep-v0` section embeds the exact crate
  graph, verified locally: 145 packages, root `rayd 0.1.0`), and
  `cargo cyclonedx` produces `rayd.cdx.json` (CycloneDX 1.5) for the
  aarch64 target; both travel with the CI `build` artefact and with the
  GitHub release.
- **ARM CI.** A `ubuntu-24.04-arm` job runs `cargo test --workspace`
  (the `cfg(unix)` adapter suites on real aarch64) and the sidecar's host
  and `-m kernel` tests with the exact `manylinux_2_28_aarch64` wheels the
  image installs.
- **e2e on AWS.** `.github/workflows/e2e.yml` (`workflow_dispatch` +
  nightly) assumes an OIDC role with `aws-actions/configure-aws-credentials`,
  fails pre-flight when more than 10 MicroVMs of the test image are alive,
  runs the Python (and optionally TypeScript) acceptance suites, and sweeps
  leftovers in an `always()` step; `concurrency: e2e`, `timeout-minutes`, a
  cost note (≈ $0.03 per run from `AWS_API_NOTES.md` §12). The IAM trust
  policy and the least-privilege policy scoped to the test image ARN ship as
  `infra/ci-oidc-role.yaml` (`cfn-lint` clean, **not deployed** by this
  change). The workflow never publishes an image: publishing stays a
  maintainer action from a workstation.
- **Release automation.** release-please in manifest mode
  (`release-please-config.json`, `.release-please-manifest.json`) with
  components `clients/python`, `clients/typescript`, `crates/rayd`, the
  `linked-versions` plugin (one lockstep version), tags `python-v*`,
  `typescript-v*`, `rayd-v*`, wired into `release-please.yml`.
  `release.yml` is rewritten around the three tags: Python keeps the PyPI
  Trusted Publishing job (attestations automatic with
  `pypa/gh-action-pypi-publish` ≥ v1.11); TypeScript publishes with npm
  trusted publishing (npm ≥ 11.5.1 asserted, `pnpm` stays the package
  manager for everything but the publish command); `rayd` builds with
  `cargo auditable`, produces `rayito-image.zip`, signs binary and zip with
  `cosign sign-blob` keyless (`--bundle`), and uploads binary, zip, both
  bundles and the SBOM to the GitHub release created by release-please.
- **Base image provenance.** `image/Dockerfile` pins
  `FROM public.ecr.aws/lambda/microvms:al2023-minimal@sha256:05cb9b38…`
  (the manifest-list digest recorded 2026-09-16 with
  `docker buildx imagetools inspect`; the arm64 manifest inside it is
  `sha256:63831a97…`), `scripts/publish_image.py` refuses to run without
  `--base-image-version`, and the Makefile passes `BASE_IMAGE_VERSION` on
  every publish target.
- **`SECURITY.md`** T10 and the "Cadena de suministro" table state the new
  controls with their file names instead of "M1" and "pinneadas por
  versión mayor".

## Capabilities

### New Capabilities
- `ci-hardening`: SHA-pinned actions with version comments, top-level
  read-only permissions, harden-runner, Scorecard, the `actionlint` gate,
  `--locked` cargo invocations and the `ubuntu-24.04-arm` job.
- `dependency-audit`: `deny.toml` and cargo-deny in CI, `pip-audit` over
  the sidecar pins and both `uv` exports, `pnpm audit --prod`, the weekly
  audit workflow and the policy for findings.
- `release-automation`: release-please manifest mode with linked versions
  and the three tags, `release.yml` per component (PyPI, npm trusted
  publishing, `rayd` artefacts with `cargo auditable`, CycloneDX SBOM and
  cosign bundles), and the verification recipe users run.
- `e2e-workflow`: `e2e.yml` with OIDC, the pre-flight and sweeper
  guardrails, the cost note, and `infra/ci-oidc-role.yaml`.

### Modified Capabilities
- `image-lifecycle`: the base image is pinned by digest in the
  `Dockerfile` and by `--base-image-version` on every publish (added
  requirement; the existing `image_prune`/warm-up requirements are
  untouched).
- `python-release`: "Release workflow with Trusted Publishing, dry-run
  only" becomes the Python job of the three-component `release.yml`; the
  wheel and docs requirements are untouched (the sibling change modifies
  the metadata requirements).

## Impact

- New: `.github/workflows/{scorecard,audit,e2e,release-please}.yml`,
  `deny.toml`, `release-please-config.json`,
  `.release-please-manifest.json`, `infra/ci-oidc-role.yaml`,
  `crates/rayd/CHANGELOG.md` (created with the Keep a Changelog header if
  the sibling change has not created it), `docs/site/docs/verify.md` (how
  to verify a release artefact).
- Edited: `.github/workflows/ci.yml` (pins, permissions, harden-runner,
  `actionlint`, `deny`, `audit`, `arm` jobs, `cargo auditable` + SBOM in
  `build`), `.github/workflows/release.yml` (three components),
  `image/Dockerfile` (`FROM …@sha256:`), `scripts/publish_image.py`
  (`--base-image-version` required) and `scripts/tests/test_publish_image.py`,
  `Makefile` (`BASE_IMAGE_VERSION`, `lint` runs `actionlint` and
  `cargo deny`, new `sbom` target), `clients/python/src/rayito/_version.py`
  and `clients/typescript/src/version.ts` (release-please annotation
  comment on the version line), `SECURITY.md`, `infra/README.md`,
  `README.md` (Scorecard badge, verify snippet), `docs/RELEASING.md`
  (workflow filename and the automated flow), `MILESTONES.md` (M7 row),
  `AWS_API_NOTES.md` (§16 Q52: accepted `baseImageVersion` format and the
  digest-pinned build).
- Behaviour: none at runtime. `rayd`, the sidecar, the image contents and
  both SDKs execute exactly as in 0.1.0; the binary gains a `.dep-v0`
  section (+2 240 B measured locally) and the next image version is built
  from the digest-pinned base.
- Cost: the nightly e2e ≈ $0.03/run (≈ $1/month); the ARM job is free on
  public repositories; Scorecard, cargo-deny, audits and release-please are
  GitHub-hosted minutes only.
