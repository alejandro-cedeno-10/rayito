## Why

A supply-chain review of the CI and release pipeline (2026-10, findings
SC-01 to SC-12) confirmed that the most sensitive artefact of the project,
the `rayd` binary that runs as PID 1 (root) in every customer MicroVM, was
built, signed and uploaded by a single job that also ran every crate's
build scripts and the cargo tools it installed, while holding both the
OIDC token cosign signs with and a `contents: write` token. That job also
restored GitHub Actions caches (`Swatinem/rust-cache`, `mlugg/setup-zig`),
and a tag run restores caches from the default-branch scope, which any job
on `main` can write. A foothold in any `main` job (a compromised locked
dependency, or an unverified tool download such as the binary
`cargo-deny-action` fetched without a checksum) could therefore end up in a
release signed with the official identity. Smaller gaps compounded it:
`python-build` hashed `dist/` only after twine's unpinned dependency graph
had run, the documented cosign identity was a regexp that accepted a
signature from any `rayd-v*` tag (downgrade or cross-version swap), nothing
required a published tag to point at `main`, Dependabot proposed releases
published hours earlier, the e2e jobs ran without their role configured,
and five TypeScript regexes over caller-supplied strings backtracked in
quadratic time (CodeQL `js/polynomial-redos` #10-#14).

## What Changes

- **`rayd` release split into three jobs** (`release.yml`): `rayd-build`
  (`contents: read`, checkout, build, SBOM, zip, unsigned `SHA256SUMS`),
  `rayd-sign` (`id-token: write`, no checkout, verifies the build output
  with `sha256sum -c`, runs only cosign) and `rayd-upload`
  (`contents: write`, `environment: release`, no checkout, re-verifies, and
  uploads without `--clobber`, refusing a release that already carries any
  of the assets).
- **No cache in any release job**: `Swatinem/rust-cache` and
  `mlugg/setup-zig` removed; zig 0.16.0 comes from a local composite action
  (`.github/actions/zig`) that checks a pinned sha256; the cargo tools are
  installed with `--locked --force` into a fresh `--root` and `CARGO_HOME`
  under `RUNNER_TEMP`; `setup-uv` runs with `enable-cache: false` and
  `setup-node` with `package-manager-cache: false`.
- **`python-build` hashes first**: `SHA256SUMS` right after `uv build`,
  then `check_wheel.py` and twine, then `sha256sum -c` again; twine from
  `.github/release/requirements-twine.txt` with `--require-hashes`; uv
  pinned by `version` and `checksum`.
- **Publishing needs the tagged commit on `main`** (`resolve`), and the
  workflow gets a `concurrency` group per ref.
- **Exact cosign identity**: the self-check uses
  `--certificate-identity https://github.com/${GITHUB_REPOSITORY}/.github/workflows/release.yml@${GITHUB_REF}`;
  `verify.md` and the README bind `refs/tags/rayd-v${RAYD_VERSION}`.
- **Verified tool downloads in every workflow**: `cargo-deny` 0.20.2 comes
  from a local composite action (`.github/actions/cargo-deny`) with a
  pinned sha256 instead of `EmbarkStudios/cargo-deny-action`; a sixth gate
  of `scripts/check_pins.py` requires `curl -o` + `sha256sum -c` + a pinned
  `<NAME>_SHA256` for every workflow or local-action step that downloads,
  and the pinning gate now covers `.github/actions/*/action.yml`.
- **Dependabot cooldown**: 7 days, 14 for a semver major.
- **e2e jobs skipped** while `vars.RAYITO_E2E_ROLE_ARN` is unset.
- **TypeScript SDK**: linear-time `stripTrailing`/`stripLeading`
  (`src/strings.ts`) replace `/\/+$/`, `/=+$/` and `/^\/+|\/+$/g`, and the
  http(s) URL split no longer backtracks, with unchanged semantics.
- Repository settings applied outside the tree (documented, not code):
  environment `release` (required reviewer, tag policy `rayd-v*`), branch
  policy `main` plus reviewer on `e2e`, a tag ruleset that forbids moving
  or deleting release tags, and fork-PR approval for all external
  contributors.

## Capabilities

### Modified Capabilities

- `ci-hardening`: credential-holding release jobs, no caches in releases,
  verified workflow downloads, Dependabot cooldown.
- `release-automation`: the `rayd` split, hash-first Python build, tag on
  `main`, exact cosign identity.
- `dependency-audit`: cargo-deny runs from a checksum-verified binary.
- `e2e-workflow`: jobs gated on the role variable.
- `typescript-sdk`: caller-supplied strings are parsed in linear time.

## Impact

- `.github/workflows/{release,ci,audit,e2e}.yml`, `.github/dependabot.yml`,
  `.github/actions/{zig,cargo-deny}/action.yml`,
  `.github/release/requirements-twine.txt`, `scripts/check_pins.py`,
  `scripts/tests/`, `clients/typescript/src/{strings,payload}.ts`,
  `clients/typescript/src/sandbox/{git-args,transfer}.ts`,
  `clients/typescript/src/templates/context.ts`, `SECURITY.md`,
  `docs/site/docs/{verify,security}.md`, `README.md`, `docs/RELEASING.md`,
  `infra/README.md`, the three package changelogs.
- Releases of `rayd` take a few more minutes (the cargo tools compile on
  every release) and wait for the maintainer's approval of the `release`
  environment, like PyPI and npm.
- No runtime change to `rayd` or the Python SDK; no AWS call.
