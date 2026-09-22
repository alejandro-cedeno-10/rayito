## ADDED Requirements

### Requirement: release-please manifest mode with one lockstep version
`release-please-config.json` and `.release-please-manifest.json` SHALL define the components `clients/python` (component `python`, release type `python`), `clients/typescript` (component `typescript`, release type `node`) and `crates/rayd` (component `rayd`, release type `simple` with TOML `extra-files` updating `$.workspace.package.version` of the root `Cargo.toml` and the `[[package]]` `version` of `rayd`, `rayd-core` and `rayito-proto` in `Cargo.lock`), linked by a `linked-versions` plugin so every release bumps the three to the same version; `include-component-in-tag`, `include-v-in-tag` and `tag-separator: "-"` SHALL yield the tags `python-v<version>`, `typescript-v<version>` and `rayd-v<version>`; `bump-minor-pre-major` SHALL be `true` and `separate-pull-requests` `false`. `clients/python/src/rayito/_version.py` and `clients/typescript/src/version.ts` SHALL carry the `x-release-please-version` annotation on their version line and be listed as generic `extra-files`. The manifest SHALL start at the versions currently in the manifests (`0.1.0`, `0.0.5`, `0.1.0`). `.github/workflows/release-please.yml` SHALL run `googleapis/release-please-action` on pushes to `main` with `contents: write` and `pull-requests: write`, using `secrets.RELEASE_PLEASE_TOKEN` when present and `github.token` otherwise.

#### Scenario: config validates
- **WHEN** `check-jsonschema` validates `release-please-config.json` against the published release-please config schema
- **THEN** it passes, and `grep -c x-release-please-version` returns 1 for `_version.py` and for `version.ts`

#### Scenario: first release PR
- **WHEN** release-please runs on `main` after conventional commits containing at least one `feat:` since the import commit
- **THEN** it opens one pull request that sets `pyproject.toml`, `_version.py`, `package.json`, `version.ts`, the root `Cargo.toml` and the three `Cargo.lock` entries to `0.2.0` and updates the three `CHANGELOG.md` files, and CI is green on that pull request with `--locked`

### Requirement: release.yml publishes each component from its tag
`.github/workflows/release.yml` SHALL trigger on tags `python-v*`, `typescript-v*` and `rayd-v*` and on `workflow_dispatch` with inputs `tag` (optional) and `dry_run` (default `true`); a `resolve` job SHALL derive the component and version from the tag and fail on any other tag shape; each component job SHALL run only for its component and SHALL publish only on a tag push or a dispatch with `tag` set and `dry_run: false`, always uploading its artefacts. The `python` job SHALL keep the existing build, wheel check, `twine check` and tag-parity steps and publish with `pypa/gh-action-pypi-publish` in environment `pypi` with `id-token: write` (attestations enabled by default). The `typescript` job SHALL use Node 24, assert `npm --version` is at least 11.5.1, assert the tag equals `package.json`'s version, run `pnpm install --frozen-lockfile`, `pnpm build`, `pnpm pack:check` and `pnpm pack`, and publish the tarball with `npm publish --access public` in environment `npm` with `id-token: write` and no token (npm trusted publishing with automatic provenance); this SHALL be the only `npm` invocation in the repository and its header comment SHALL say why.

#### Scenario: dispatch dry-run
- **WHEN** the workflow is dispatched with `tag: rayd-v0.2.0` and `dry_run: true`
- **THEN** the `rayd` job builds, signs and uploads artefacts to the workflow run, and no release asset, PyPI or npm publication happens

#### Scenario: npm too old
- **WHEN** the `typescript` job finds `npm --version` below 11.5.1
- **THEN** it fails before packing with a message naming the required version

#### Scenario: tag mismatch
- **WHEN** the workflow runs on `typescript-v0.3.0` while `package.json` says `0.2.0`
- **THEN** the `typescript` job fails before `pnpm pack`

### Requirement: rayd release artefacts carry an embedded dependency list, an SBOM and cosign bundles
The `rayd` job of `release.yml` and the `build` job of `ci.yml` SHALL build with `cargo auditable zigbuild --release --locked --target aarch64-unknown-linux-musl -p rayd` (cargo-auditable 0.7.6, cargo-zigbuild 0.23.4) and verify with `scripts/check_auditable.py` that the ELF section `.dep-v0` names root package `rayd` at the version of the root `Cargo.toml` and includes `tonic`, `tokio`, `axum` and `nix`; SHALL produce `crates/rayd/rayd.cdx.json` with `cargo cyclonedx --manifest-path crates/rayd/Cargo.toml --target aarch64-unknown-linux-musl --format json --no-build-deps --spec-version 1.5` (cargo-cyclonedx 0.5.9); and the CI artifact `rayd-aarch64-musl` SHALL include the SBOM. The `rayd` release job SHALL additionally assert the tag equals `[workspace.package].version`, build `image/rayito-image.zip` with `scripts/copy_sidecar.py` and `scripts/image_zip.py`, sign `rayd` and `rayito-image.zip` with `cosign sign-blob --yes --bundle <file>.sigstore.json` (keyless, `id-token: write`), verify both bundles in the same job with `cosign verify-blob --bundle … --certificate-identity-regexp '^https://github.com/alejandro-cedeno-10/rayito/\.github/workflows/release\.yml@refs/tags/rayd-v' --certificate-oidc-issuer https://token.actions.githubusercontent.com`, write `SHA256SUMS`, and upload `rayd`, `rayd.sigstore.json`, `rayito-image.zip`, `rayito-image.zip.sigstore.json`, `rayd.cdx.json` and `SHA256SUMS` to the GitHub release of the tag with `gh release upload --clobber`. `docs/site/docs/verify.md` SHALL document the user-side verification (cosign command with the identity regexp, `cargo audit bin`, PyPI attestations, `npm view … dist.attestations`) and the README SHALL link it.

#### Scenario: embedded dependency list
- **WHEN** `python3 scripts/check_auditable.py target/aarch64-unknown-linux-musl/release/rayd` runs on the built binary
- **THEN** it prints the package count and root `rayd <version>` and exits 0; on a binary built without `cargo auditable` it exits 1 naming the missing `.dep-v0` section

#### Scenario: verified from a clean machine
- **WHEN** a user downloads `rayito-image.zip` and `rayito-image.zip.sigstore.json` from the `rayd-v<version>` release and runs the documented `cosign verify-blob` command
- **THEN** verification succeeds, and it fails when either the zip or the bundle is altered
