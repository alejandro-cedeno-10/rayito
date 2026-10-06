## ADDED Requirements

### Requirement: Third-party notices travel with every copy of rayd
`THIRD_PARTY_LICENSES.md` SHALL be generated at the repository root, and never committed (`.gitignore` lists it), by `make licenses` with cargo-about 0.9.2 (`cargo about generate --frozen --fail -c about.toml -m crates/rayd/Cargo.toml about.hbs`) after `cargo fetch --locked`. `about.toml` SHALL filter the graph to `aarch64-unknown-linux-musl`, ignore build and dev dependencies and private crates, accept exactly the licenses of `deny.toml`'s `[licenses].allow`, and accept `CC0-1.0` only for `notify`. The file SHALL list every third-party crate as a line "- `<name> <version>`" under the full text of its license. `make licenses` SHALL fail when the listed `(name, version)` pairs differ in either direction from the third-party crates of `cargo tree --frozen -p rayd --target aarch64-unknown-linux-musl -e normal --prefix none --format '{p}'` (`scripts/check_third_party_licenses.py --tree`). `scripts/check_third_party_licenses.py` SHALL also fail when a listed crate is absent from the crates.io packages of the `.dep-v0` section of the built `rayd` (`--binary`; `.dep-v0` is a superset, since `cargo metadata` unifies dev-dependency features, so only this direction is required), and when a listed crate's source directory holds a `NOTICE` file whose crate the root `NOTICE` does not name (`--metadata`). `make image-licenses` SHALL depend on `make licenses`, so every staging regenerates the file; CI's `build` job and `release.yml`'s `rayd-build` SHALL run `make image-licenses` before the `--binary` and `--metadata` check and before zipping, and no workflow SHALL write to a pull request branch to keep the file current, with cargo-about from `.github/actions/cargo-about` (release binary verified against a pinned sha256). `LICENSE`, `NOTICE` and `THIRD_PARTY_LICENSES.md` SHALL travel under `licenses/` in `rayito-image.zip` (`make image-licenses`, run by every `make image-zip*` target, CI and the release), SHALL be copied by `image/Dockerfile` to `/usr/share/doc/rayd/` (root, 0644) and SHALL be `rayd-v*` release assets.

#### Scenario: a Dependabot lock bump needs no extra commit
- **WHEN** a Dependabot pull request bumps a crate in `Cargo.lock` and touches nothing else
- **THEN** the `build` job generates `THIRD_PARTY_LICENSES.md` from that lock with the new version listed, the coverage checks pass, and the pull request needs no commit other than Dependabot's

#### Scenario: a compiled crate escapes cargo-about's graph
- **WHEN** `cargo tree` for `rayd` on the release target names a crates.io crate that `THIRD_PARTY_LICENSES.md` does not list at that version
- **THEN** `make licenses` exits 1 naming the crate and version

#### Scenario: dev-only crates recorded in .dep-v0
- **WHEN** the binary's `.dep-v0` records `ring`, which only the `rcgen` dev-dependency brings in and the release binary does not link
- **THEN** `check_third_party_licenses.py --binary` passes, because it only requires every listed crate to be recorded

#### Scenario: an upstream NOTICE appears
- **WHEN** a listed crate starts shipping a `NOTICE` file and the root `NOTICE` does not name the crate
- **THEN** `check_third_party_licenses.py --metadata` exits 1 naming the crate and the file

#### Scenario: the image carries the notices
- **WHEN** an image is built from a `rayito-image.zip` produced by `make image-zip` or the release
- **THEN** `/usr/share/doc/rayd/` holds `LICENSE`, `NOTICE` and `THIRD_PARTY_LICENSES.md`
