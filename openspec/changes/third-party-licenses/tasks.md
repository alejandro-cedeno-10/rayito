## 1. Generation

- [x] 1.1 `about.toml` (deny.toml allowlist, `notify` CC0 exception, musl
  target, no build/dev dependencies) and `about.hbs`.
- [x] 1.2 `THIRD_PARTY_LICENSES.md` generated with cargo-about 0.9.2
  `--frozen`; online and offline outputs compared (identical).
- [x] 1.3 `Makefile`: `require-cargo-about`, `licenses`, `licenses-check`,
  `image-licenses`; every `image-zip*` target stages the notices;
  `local-guest-context` and `clean` cover them; `.gitignore`.

## 2. Gates

- [x] 2.1 `scripts/check_third_party_licenses.py` (exact coverage of the
  `cargo tree` graph, listed crates recorded in `.dep-v0`, upstream
  `NOTICE` files; `.dep-v0` found to be a superset on the first CI run) with `scripts/tests/test_check_third_party_licenses.py`.
- [x] 2.2 `.github/actions/cargo-about` (binary pinned by sha256).
- [x] 2.3 `ci.yml` `build`: `make licenses-check`, coverage, staged
  notices, zip content check, notices in the artifact.

## 3. Shipping

- [x] 3.1 `_artifact.py` refuses a tree with `rayd` and without the notices;
  tests updated and added.
- [x] 3.2 `image/Dockerfile` copies `licenses/` to `/usr/share/doc/rayd/`.
- [x] 3.3 `release.yml`: freshness and coverage in `rayd-build`, notices in
  the zip and the assets, `SHA256SUMS` signed in `rayd-sign`, bundle digest
  checked in `rayd-upload`; `test_release_workflow.py` assertions.

## 4. Docs

- [x] 4.1 `docs/RELEASING.md`, `docs/site/docs/verify.md`,
  `docs/site/docs/cli.md`, `SECURITY.md`, `CONTRIBUTING.md`, gates
  reference.
- [x] 4.2 Audit §2.2 and §8 #1 marked done; `rayd` and Python changelogs.

## 5. Acceptance

- [x] 5.1 Gates green locally (Rust in the VM, Python, TypeScript, scripts,
  mkdocs, OpenSpec) and in CI, including `build` and `local-e2e`.
- [ ] 5.2 Archive after merge (no runtime change: gates are the acceptance).
