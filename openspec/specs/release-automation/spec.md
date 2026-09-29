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
`.github/workflows/release.yml` SHALL trigger on tags `python-v*`, `typescript-v*` and `rayd-v*` and on `workflow_dispatch` with inputs `tag` (optional) and `dry_run` (default `true`); a `resolve` job SHALL derive the component and version from the tag and fail on any other tag shape; each component job SHALL run only for its component and SHALL publish only on a tag push or a dispatch with `tag` set and `dry_run: false`, always uploading its artefacts. The Python and TypeScript components SHALL each be split into a `*-build` job and a `*-publish` job so that no job holding `id-token: write` ever checks out the repository or runs third-party build tooling (C-10, the residue of H-02).

`python-build` SHALL declare no `environment` and no `id-token`; it SHALL check out the repository, assert the tag equals `pyproject.toml`'s version, run `uv build`, `scripts/check_wheel.py` and `twine check`, write a `SHA256SUMS` of the built wheel and sdist as a sibling of `dist/` (never inside it), expose that `SHA256SUMS`'s own sha256 as a job output, and upload one artifact containing both `dist/` and `SHA256SUMS`. `python-publish` SHALL depend on `resolve` and `python-build`, run only when `needs.resolve.outputs.publish == 'true'`, declare `environment: pypi` and `permissions: {id-token: write, contents: read}`, hold no `actions/checkout` step and no `run:` invoking `pnpm`, `uv`, `uvx`, `pip`, `npm install`, `npm ci`, `cargo` or `make`; it SHALL download the build job's artifact with a pinned `actions/download-artifact` (a 40-hex commit SHA, v4 or newer so it can read a v7 `upload-artifact`), verify with `sha256sum -c` first that the downloaded `SHA256SUMS` matches the build job's output and then that `dist/`'s files match `SHA256SUMS`, and only then publish with `pypa/gh-action-pypi-publish` using a `packages-dir` that contains only the wheel and sdist (never `SHA256SUMS`). The artifact SHALL be downloaded inside `GITHUB_WORKSPACE` (a relative path or one under `${{ github.workspace }}`, never `runner.temp`) and `packages-dir` SHALL point inside it, because the publish action runs twine in a Docker container that mounts only the workspace.

`typescript-build` SHALL declare no `environment` and no `id-token`; it SHALL check out the repository, use Node 24, assert the tag equals `package.json`'s version, run `pnpm install --frozen-lockfile --ignore-scripts`, `pnpm build`, `pnpm pack:check` and `pnpm pack`, compute the packed tarball's sha256 as a job output, and upload it as an artifact. `typescript-publish` SHALL depend on `resolve` and `typescript-build`, run only when `needs.resolve.outputs.publish == 'true'`, declare `environment: npm` and `permissions: {id-token: write, contents: read}`, hold no `actions/checkout` step, no `pnpm` invocation and no `npm install`/`npm ci`; it SHALL assert `npm --version` is at least 11.5.1, download the build job's tarball artifact with the same pinned `actions/download-artifact`, verify its sha256 against the build job's output with `sha256sum -c` (the digest passed through `env:`, never interpolated directly inside the shell script), and only then publish with `npm publish "rayito-$VERSION.tgz" --access public --ignore-scripts` — the only `npm` invocation in the repository. `docs/RELEASING.md` SHALL say that PyPI's and npm's Trusted Publisher bind the token to the workflow file and the `environment`, not the job name, so neither registry needs reconfiguring after the split.

The `resolve` and `rayd` jobs SHALL be unaffected by the split.

#### Scenario: dispatch dry-run
- **WHEN** the workflow is dispatched with `tag: rayd-v0.2.0` and `dry_run: true`
- **THEN** the `rayd` job builds, signs and uploads artefacts to the workflow run, and no release asset, PyPI or npm publication happens

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

#### Scenario: only the publish jobs and rayd hold id-token: write
- **WHEN** a reviewer reads every job's `permissions` in `release.yml`
- **THEN** exactly `python-publish`, `typescript-publish` and `rayd` declare `id-token: write`; `python-build` and `typescript-build` declare neither `environment` nor `id-token`

### Requirement: rayd release artefacts carry an embedded dependency list, an SBOM and cosign bundles
The `rayd` job of `release.yml` and the `build` job of `ci.yml` SHALL build with `cargo auditable zigbuild --release --locked --target aarch64-unknown-linux-musl -p rayd` (cargo-auditable 0.7.6, cargo-zigbuild 0.23.4) and the same `--remap-path-prefix` rustflags `make build`'s `REMAP_CONFIG` applies locally (`CARGO_HOME` and the build target directory rewritten to `/cargo` and `/target` via `--config build.rustflags=[...]`), so the published `rayd` embeds no build-machine path; both jobs SHALL add a step that fails if `strings` finds `/home/runner` in the built binary. Both SHALL verify with `scripts/check_auditable.py` that the ELF section `.dep-v0` names root package `rayd` at the version of the root `Cargo.toml` and includes `tonic`, `tokio`, `axum` and `nix`; SHALL produce `crates/rayd/rayd.cdx.json` with `cargo cyclonedx --manifest-path crates/rayd/Cargo.toml --target aarch64-unknown-linux-musl --format json --no-build-deps --spec-version 1.5` (cargo-cyclonedx 0.5.9); and the CI artifact `rayd-aarch64-musl` SHALL include the SBOM. The `rayd` release job SHALL additionally assert the tag equals `[workspace.package].version`, build `image/rayito-image.zip` with `scripts/copy_sidecar.py` and `scripts/image_zip.py`, sign `rayd` and `rayito-image.zip` with `cosign sign-blob --yes --bundle <file>.sigstore.json` (keyless, `id-token: write`), verify both bundles in the same job with `cosign verify-blob --bundle … --certificate-identity-regexp '^https://github.com/alejandro-cedeno-10/rayito/\.github/workflows/release\.yml@refs/tags/rayd-v' --certificate-oidc-issuer https://token.actions.githubusercontent.com`, write `SHA256SUMS`, and upload `rayd`, `rayd.sigstore.json`, `rayito-image.zip`, `rayito-image.zip.sigstore.json`, `rayd.cdx.json` and `SHA256SUMS` to the GitHub release of the tag with `gh release upload --clobber`. `docs/site/docs/verify.md` SHALL document the user-side verification (cosign command with the identity regexp, `cargo audit bin`, PyPI attestations, `npm view … dist.attestations`) and the README SHALL link it.

#### Scenario: embedded dependency list
- **WHEN** `python3 scripts/check_auditable.py target/aarch64-unknown-linux-musl/release/rayd` runs on the built binary
- **THEN** it prints the package count and root `rayd <version>` and exits 0; on a binary built without `cargo auditable` it exits 1 naming the missing `.dep-v0` section

#### Scenario: verified from a clean machine
- **WHEN** a user downloads `rayito-image.zip` and `rayito-image.zip.sigstore.json` from the `rayd-v<version>` release and runs the documented `cosign verify-blob` command
- **THEN** verification succeeds, and it fails when either the zip or the bundle is altered

#### Scenario: the published binary carries no runner path
- **WHEN** `ci.yml`'s `build` job or `release.yml`'s `rayd` job builds `target/aarch64-unknown-linux-musl/release/rayd` and runs `strings <binary> | grep -c /home/runner`
- **THEN** the count is `0`, and the job fails otherwise
