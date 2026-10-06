# oss-licensing Specification

## Purpose
TBD - created by archiving change m7-oss-hygiene. Update Purpose after archive.

## Requirements

### Requirement: Apache-2.0 is declared in every manifest
The repository SHALL declare the Apache-2.0 licence in all five manifests and nowhere else: `Cargo.toml` `[workspace.package].license = "Apache-2.0"` (inherited by `rayito-proto`, `rayd-core` and `rayd` through `license.workspace = true`, with `repository` and `homepage` set to `https://github.com/alejandro-cedeno-10/rayito` and `publish = false` unchanged); `clients/python/pyproject.toml` `[project].license = "Apache-2.0"` with `license-files = ["LICENSE", "NOTICE"]` and **no** classifier starting with `License ::`; `kernel-sidecar/pyproject.toml` `[project].license = "Apache-2.0"`; `clients/typescript/package.json` `"license": "Apache-2.0"` with `files` containing `LICENSE` and `NOTICE` and a `repository` object pointing at the repo with `directory: "clients/typescript"`; and `clients/typescript/LICENSE` holding the Apache-2.0 text. No manifest SHALL contain the string `MIT` as a licence value.

#### Scenario: the five places agree
- **WHEN** `python scripts/check_license.py` runs from the repository root after this change
- **THEN** it prints `OK` and exits 0

#### Scenario: a manifest left on MIT is caught
- **WHEN** `clients/typescript/package.json` still says `"license": "MIT"` while the other four places say Apache-2.0
- **THEN** `python scripts/check_license.py` exits 1 and its output names `clients/typescript/package.json`

#### Scenario: a surviving classifier is caught
- **WHEN** `clients/python/pyproject.toml` declares `license = "Apache-2.0"` but keeps the classifier `License :: OSI Approved :: MIT License`
- **THEN** `python scripts/check_license.py` exits 1 naming the classifier, and `uv run pytest tests/unit/test_packaging.py` fails in `test_license_is_apache_2_expression`

### Requirement: LICENSE and NOTICE files ship with every artifact
The root `LICENSE` SHALL be the verbatim Apache-2.0 text from `https://www.apache.org/licenses/LICENSE-2.0.txt` (sha256 `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30`) and SHALL be copied byte-identical to `clients/python/LICENSE`, `clients/typescript/LICENSE` and `crates/rayd/LICENSE`. The root `NOTICE` SHALL name the product, `Copyright 2026 Rayito contributors`, and the vendored `e2b_charts` 1.0.0 (MIT, Copyright (c) 2025 FOUNDRYLABS, INC.) with its vendored path and a pointer to `VENDORED.md`, and SHALL be copied byte-identical to `clients/python/NOTICE` and `clients/typescript/NOTICE`. The vendored MIT `LICENSE` and `VENDORED.md` under `kernel-sidecar/src/rayito_kernel_sidecar/_vendor/` SHALL remain unchanged. `scripts/check_license.py` SHALL verify the root checksum, every copy and the `e2b_charts` mention, and `make lint` and the CI `check` job SHALL run it. A root `.gitattributes` SHALL force LF line endings on checkout (`* text=auto eol=lf`, with explicit `LICENSE text eol=lf` and `NOTICE text eol=lf`) so that a Windows clone with `core.autocrlf=true` keeps the files byte-identical to the apache.org text; when the root `LICENSE` fails the checksum and contains CRLF, the gate's message SHALL say so and point to `CONTRIBUTING.md`.

#### Scenario: a drifted copy is caught
- **WHEN** one byte of `crates/rayd/LICENSE` differs from the root `LICENSE`
- **THEN** `python scripts/check_license.py` exits 1 naming `crates/rayd/LICENSE`

#### Scenario: NOTICE credits the vendored code
- **WHEN** a reviewer opens `NOTICE` at the root, in `clients/python` and in `clients/typescript`
- **THEN** the three files are identical, mention `e2b_charts 1.0.0`, `MIT` and `FOUNDRYLABS, INC.`, and the `_vendor/e2b_charts/LICENSE` MIT file still sits beside the vendored code

#### Scenario: gate is wired
- **WHEN** a developer runs `make lint` or CI runs the `check` job with a manifest still on MIT
- **THEN** the run fails at the `check_license.py` step before any later step

#### Scenario: a CRLF checkout is named, not hidden
- **WHEN** the root `LICENSE` reaches the working tree with CRLF line endings (a clone made before `.gitattributes` existed, on a box with `core.autocrlf=true`)
- **THEN** `python scripts/check_license.py` exits 1, its `LICENSE` line says the file has CRLF endings and points to `CONTRIBUTING.md`, and a fresh checkout honouring `.gitattributes` makes the same command print `OK`

### Requirement: The wheel carries PEP 639 licence metadata and the licence files
`cd clients/python && uv build` SHALL produce a wheel whose `METADATA` contains `License-Expression: Apache-2.0`, `License-File: LICENSE` and `License-File: NOTICE` and no line starting with `Classifier: License ::`, and whose archive contains `rayito-<version>.dist-info/licenses/LICENSE` and `rayito-<version>.dist-info/licenses/NOTICE`. `scripts/check_wheel.py` SHALL assert all of the above in addition to its existing checks, and `uvx twine check` SHALL pass on the sdist and the wheel.

#### Scenario: wheel metadata
- **WHEN** CI runs `uv build`, `python scripts/check_wheel.py clients/python/dist/*.whl` and `uvx twine check clients/python/dist/*`
- **THEN** all three succeed and `check_wheel.py` prints `OK` for the wheel

#### Scenario: a wheel without NOTICE is rejected
- **WHEN** `check_wheel.py` inspects a wheel built from a `pyproject.toml` whose `license-files` is `["LICENSE"]` only
- **THEN** it prints `KO` with a line naming the missing `License-File: NOTICE` and the missing `.dist-info/licenses/NOTICE` entry and exits 1

### Requirement: The npm tarball carries LICENSE and NOTICE
`cd clients/typescript && pnpm pack` SHALL produce a tarball containing `package/LICENSE` (Apache-2.0 text) and `package/NOTICE` in addition to `package/package.json`, `package/README.md` and `package/dist/**`, and nothing else; `scripts/pack-check.mjs` SHALL require `package/NOTICE` in its `REQUIRED_ENTRIES`.

#### Scenario: pack check
- **WHEN** a developer runs `pnpm pack:check` in `clients/typescript`
- **THEN** the printed listing contains `package/LICENSE` and `package/NOTICE`, contains no entry outside `package/package.json`, `package/README.md`, `package/LICENSE`, `package/NOTICE` and `package/dist/`, and the command exits 0

#### Scenario: NOTICE dropped from files
- **WHEN** `package.json` `files` no longer lists `NOTICE`
- **THEN** `pnpm pack:check` exits non-zero with the message naming `package/NOTICE`

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
