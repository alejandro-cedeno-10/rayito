## MODIFIED Requirements

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
