# release-automation Specification

## Purpose
TBD - created by archiving change m7-supply-chain. Update Purpose after archive.

## Requirements

### Requirement: release-please manifest mode with one lockstep version
`release-please-config.json` and `.release-please-manifest.json` SHALL define the components `clients/python` (component `python`, release type `python`), `clients/typescript` (component `typescript`, release type `node`) and `crates/rayd` (component `rayd`, release type `simple` with TOML `extra-files` updating `$.workspace.package.version` of the root `Cargo.toml` and the `[[package]]` `version` of `rayd`, `rayd-core` and `rayito-proto` in `Cargo.lock`), linked by a `linked-versions` plugin so every release bumps the three to the same version; `include-component-in-tag`, `include-v-in-tag` and `tag-separator: "-"` SHALL yield the tags `python-v<version>`, `typescript-v<version>` and `rayd-v<version>`; `bump-minor-pre-major` SHALL be `true` and `separate-pull-requests` `false`. `clients/python/src/rayito/_version.py` and `clients/typescript/src/version.ts` SHALL carry the `x-release-please-version` annotation on their version line and be listed as generic `extra-files`. The manifest SHALL start at the versions currently in the manifests (`0.1.0`, `0.0.5`, `0.1.0`). `.github/workflows/release-please.yml` SHALL run `googleapis/release-please-action` on pushes to `main` with `contents: write` and `pull-requests: write`, using `secrets.RELEASE_PLEASE_TOKEN` when present and `github.token` otherwise.

#### Scenario: config validates
- **WHEN** `check-jsonschema` validates `release-please-config.json` against the published release-please config schema
- **THEN** it passes, and `grep -c x-release-please-version` returns 1 for `_version.py` and for `version.ts`

#### Scenario: first release PR
- **WHEN** release-please runs on `main` after conventional commits containing at least one `feat:` since the import commit
- **THEN** it opens one pull request that sets `pyproject.toml`, `_version.py`, `package.json`, `version.ts`, the root `Cargo.toml` and the three `Cargo.lock` entries to `0.2.0` and updates the three `CHANGELOG.md` files, and CI is green on that pull request with `--locked`

### Requirement: release.yml publishes each component from its tag
`.github/workflows/release.yml` SHALL trigger on tags `python-v*`, `typescript-v*` and `rayd-v*` and on `workflow_dispatch` with inputs `tag` (optional) and `dry_run` (default `true`), with `concurrency: { group: release-${{ github.ref }}, cancel-in-progress: false }`; a `resolve` job SHALL derive the component and version from the tag and fail on any other tag shape; each component job SHALL run only for its component and SHALL publish only on a tag push or a dispatch with `tag` set and `dry_run: false`, always uploading its artefacts. When it will publish, `resolve` SHALL check out the repository with `fetch-depth: 0` and `persist-credentials: false` and fail unless `git merge-base --is-ancestor "$GITHUB_SHA" origin/main` succeeds, so a tag pushed on a commit that is not on `main` publishes nothing. The Python, TypeScript and `rayd` components SHALL each be split so that no job holding `id-token: write` or `contents: write` ever checks out the repository or runs third-party build tooling (C-10, the residue of H-02; `sec-supply-chain-ci` for `rayd`).

`python-build` SHALL declare no `environment` and no `id-token`; it SHALL use `astral-sh/setup-uv` with a pinned `version` and `checksum` and `enable-cache: false`, check out the repository, assert the tag equals `pyproject.toml`'s version, run `uv build`, and **immediately afterwards** write a `SHA256SUMS` of the built wheel and sdist as a sibling of `dist/` (never inside it) and expose that `SHA256SUMS`'s own sha256 as a job output; only then SHALL it run `scripts/check_wheel.py` and `twine check`, with twine and its dependencies installed by `uv pip install --require-hashes --no-deps --only-binary :all: -r .github/release/requirements-twine.txt` (every pin with `--hash=sha256:`, covered by `scripts/check_pins.py`), and end with `sha256sum -c SHA256SUMS` before uploading one artifact containing both `dist/` and `SHA256SUMS`. `python-publish` SHALL depend on `resolve` and `python-build`, run only when `needs.resolve.outputs.publish == 'true'`, declare `environment: pypi` and `permissions: {id-token: write, contents: read}`, hold no `actions/checkout` step and no `run:` invoking `pnpm`, `uv`, `uvx`, `pip`, `npm install`, `npm ci`, `cargo` or `make`; it SHALL download the build job's artifact with a pinned `actions/download-artifact` (a 40-hex commit SHA, v4 or newer so it can read a v7 `upload-artifact`), verify with `sha256sum -c` first that the downloaded `SHA256SUMS` matches the build job's output and then that `dist/`'s files match `SHA256SUMS`, and only then publish with `pypa/gh-action-pypi-publish` using a `packages-dir` that contains only the wheel and sdist (never `SHA256SUMS`). The artifact SHALL be downloaded inside `GITHUB_WORKSPACE` (a relative path or one under `${{ github.workspace }}`, never `runner.temp`) and `packages-dir` SHALL point inside it, because the publish action runs twine in a Docker container that mounts only the workspace.

`typescript-build` SHALL declare no `environment` and no `id-token`; it SHALL check out the repository, use Node 24 with `package-manager-cache: false`, assert the tag equals `package.json`'s version, run `pnpm install --frozen-lockfile --ignore-scripts`, `pnpm build`, `pnpm pack:check` and `pnpm pack`, compute the packed tarball's sha256 as a job output, and upload it as an artifact. `typescript-publish` SHALL depend on `resolve` and `typescript-build`, run only when `needs.resolve.outputs.publish == 'true'`, declare `environment: npm` and `permissions: {id-token: write, contents: read}`, hold no `actions/checkout` step, no `pnpm` invocation and no `npm install`/`npm ci`; it SHALL assert `npm --version` is at least 11.5.1, download the build job's tarball artifact with the same pinned `actions/download-artifact`, verify its sha256 against the build job's output with `sha256sum -c` (the digest passed through `env:`, never interpolated directly inside the shell script), and only then publish with `npm publish "rayito-$VERSION.tgz" --access public --ignore-scripts` — the only `npm` invocation in the repository. `docs/RELEASING.md` SHALL say that PyPI's and npm's Trusted Publisher bind the token to the workflow file and the `environment`, not the job name, so neither registry needs reconfiguring after the split.

#### Scenario: dispatch dry-run
- **WHEN** the workflow is dispatched with `tag: rayd-v0.2.0` and `dry_run: true`
- **THEN** `rayd-build` builds and `rayd-sign` signs and uploads artefacts to the workflow run, `rayd-upload` is skipped, and no release asset, PyPI or npm publication happens

#### Scenario: dispatch dry-run builds without publishing (python/typescript split)
- **WHEN** the workflow is dispatched with `tag: typescript-v0.3.2` (or `python-v0.3.2`) and `dry_run: true`
- **THEN** the `*-build` job builds, uploads its artefact and exposes its digest output, and the `*-publish` job is skipped by its own `if:` condition — no PyPI or npm publication happens

#### Scenario: npm too old
- **WHEN** the `typescript-publish` job finds `npm --version` below 11.5.1
- **THEN** it fails before downloading the tarball artifact, naming the required version

#### Scenario: tag mismatch
- **WHEN** the workflow runs on `typescript-v0.3.0` while `package.json` says `0.2.0`
- **THEN** the `typescript-build` job fails before `pnpm pack`, and `typescript-publish` never runs

#### Scenario: a tampered artefact fails the digest check
- **WHEN** the artefact `python-publish`/`typescript-publish` downloads does not match the sha256 the corresponding `*-build` job produced
- **THEN** the `sha256sum -c` step fails before the publish action/CLI ever runs

#### Scenario: a check that rewrites dist/ is caught
- **WHEN** code run by `check_wheel.py` or twine modifies a file under `dist/` after `uv build`
- **THEN** the final `sha256sum -c SHA256SUMS` of `python-build` fails and no artifact is uploaded

#### Scenario: a tag outside main publishes nothing
- **WHEN** a `rayd-v*` (or `python-v*`, `typescript-v*`) tag is pushed on a commit that is not an ancestor of `origin/main`
- **THEN** `resolve` fails and no component job runs

#### Scenario: only the publish jobs and rayd hold id-token: write
- **WHEN** a reviewer reads every job's `permissions` in `release.yml`
- **THEN** exactly `python-publish`, `typescript-publish` and `rayd-sign` declare `id-token: write`, only `rayd-upload` declares `contents: write`, and `python-build`, `typescript-build` and `rayd-build` declare neither `environment` nor `id-token`

### Requirement: rayd release artefacts carry an embedded dependency list, an SBOM and cosign bundles
The `rayd-build` job of `release.yml` and the `build` job of `ci.yml` SHALL build with `cargo auditable zigbuild --release --locked --target aarch64-unknown-linux-musl -p rayd` (cargo-auditable 0.7.6, cargo-zigbuild 0.23.4, zig 0.16.0 from `.github/actions/zig`) and the same `--remap-path-prefix` rustflags `make build`'s `REMAP_CONFIG` applies locally (`CARGO_HOME` and the build target directory rewritten to `/cargo` and `/target` via `--config build.rustflags=[...]`), so the published `rayd` embeds no build-machine path; both jobs SHALL add a step that fails if `strings` finds `/home/runner` in the built binary. Both SHALL verify with `scripts/check_auditable.py` that the ELF section `.dep-v0` names root package `rayd` at the version of the root `Cargo.toml` and includes `tonic`, `tokio`, `axum` and `nix`; SHALL produce `crates/rayd/rayd.cdx.json` with `cargo cyclonedx --manifest-path crates/rayd/Cargo.toml --target aarch64-unknown-linux-musl --format json --no-build-deps --spec-version 1.5` (cargo-cyclonedx 0.5.9); and the CI artifact `rayd-aarch64-musl` SHALL include the SBOM.

The release SHALL be three jobs. `rayd-build` (`permissions: contents: read`, no `environment`) SHALL assert the tag equals `[workspace.package].version`, build `image/rayito-image.zip` with `scripts/copy_sidecar.py` and `scripts/image_zip.py`, stage `rayd`, `rayito-image.zip` and `rayd.cdx.json` with a `SHA256SUMS` of the three, expose that `SHA256SUMS`'s sha256 as a job output and upload the staged directory as an artifact. `rayd-sign` (`permissions: {id-token: write, contents: read}`) SHALL hold no `actions/checkout` step and no `run:` invoking build tooling; it SHALL download that artifact, verify the digest of `SHA256SUMS` against `rayd-build`'s output and then `sha256sum -c SHA256SUMS`, sign `rayd` and `rayito-image.zip` with `cosign sign-blob --yes --bundle <file>.sigstore.json` (keyless), verify both bundles with `cosign verify-blob --bundle … --certificate-identity "https://github.com/${GITHUB_REPOSITORY}/.github/workflows/release.yml@${GITHUB_REF}" --certificate-oidc-issuer https://token.actions.githubusercontent.com` (an exact identity, never `--certificate-identity-regexp`), write a `SHA256SUMS` over `rayd`, `rayd.sigstore.json`, `rayito-image.zip`, `rayito-image.zip.sigstore.json` and `rayd.cdx.json`, expose its digest and upload the six files. `rayd-upload` SHALL run only when `needs.resolve.outputs.publish == 'true'`, declare `environment: release` and `permissions: contents: write` (no `id-token`), hold no checkout and no build tooling, verify the digest and `sha256sum -c SHA256SUMS` again, fail if the release of the tag already carries any of the six asset names, and upload them with `gh release upload` without `--clobber`. `docs/site/docs/verify.md` SHALL document the user-side verification (cosign command with `--certificate-identity "https://github.com/alejandro-cedeno-10/rayito/.github/workflows/release.yml@refs/tags/rayd-v${RAYD_VERSION}"`, `cargo audit bin`, PyPI attestations, `npm view … dist.attestations`) and the README SHALL link it and use the same exact identity.

#### Scenario: embedded dependency list
- **WHEN** `python3 scripts/check_auditable.py target/aarch64-unknown-linux-musl/release/rayd` runs on the built binary
- **THEN** it prints the package count and root `rayd <version>` and exits 0; on a binary built without `cargo auditable` it exits 1 naming the missing `.dep-v0` section

#### Scenario: verified from a clean machine
- **WHEN** a user downloads `rayito-image.zip` and `rayito-image.zip.sigstore.json` from the `rayd-v<version>` release and runs the documented `cosign verify-blob` command with `RAYD_VERSION=<version>`
- **THEN** verification succeeds, and it fails when either the zip or the bundle is altered, or when the bundle was signed by the run of another `rayd-v*` tag

#### Scenario: the published binary carries no runner path
- **WHEN** `ci.yml`'s `build` job or `release.yml`'s `rayd-build` job builds `target/aarch64-unknown-linux-musl/release/rayd` and runs `strings <binary> | grep -c /home/runner`
- **THEN** the count is `0`, and the job fails otherwise

#### Scenario: a published asset is never replaced
- **WHEN** `rayd-upload` runs for a tag whose release already has an asset named `rayd`
- **THEN** it fails before uploading anything and no asset of that release changes

### Requirement: Signing waits for the release environment
Every job of `.github/workflows/release.yml` that holds `permissions.id-token: write` SHALL declare an `environment` with a required reviewer and SHALL run only when `needs.resolve.outputs.publish == 'true'`. `rayd-sign` SHALL run in the `release` environment, so GitHub issues no OIDC token, and Sigstore no certificate for `https://github.com/<repo>/.github/workflows/release.yml@refs/tags/rayd-v<version>`, before the maintainer approves the run. A `workflow_dispatch` dry run SHALL run the build jobs only and produce no signature. `scripts/tests/test_release_workflow.py` SHALL assert the rule for every `id-token: write` job.

#### Scenario: dry run from a release tag
- **WHEN** `release.yml` is dispatched from a `rayd-v*` tag with `dry_run: true`
- **THEN** `rayd-build` runs and uploads the unsigned assets, `rayd-sign` and `rayd-upload` are skipped, and no Fulcio certificate is issued

#### Scenario: a tag push before approval
- **WHEN** a `rayd-v*` tag is pushed and `release.yml` starts with `publish=true`
- **THEN** `rayd-sign` waits for the `release` environment's reviewer before it runs, and without approval nothing is signed

### Requirement: Published Python distributions are exactly the tag's wheel and sdist
`python-build`, before uploading its artifact, and `python-publish`, after its `sha256sum -c` checks and before `pypa/gh-action-pypi-publish`, SHALL each verify that `dist/` holds exactly `rayito-<version>-py3-none-any.whl` and `rayito-<version>.tar.gz` (with `<version>` from `needs.resolve.outputs.version`) and that `SHA256SUMS` lists exactly those two files, failing otherwise, because `sha256sum -c` ignores unlisted files and the publish action uploads every distribution in `packages-dir`.

#### Scenario: an extra wheel appears after hashing
- **WHEN** a process writes `rayito-9.9.9-py3-none-any.whl` into `dist/` after `SHA256SUMS` was computed
- **THEN** the file-set step fails in `python-build`, and again in `python-publish` if the artifact was altered, and nothing is published
