## Why

A second supply-chain review of CI and the release (2026-10, findings
SC-A01 to SC-A14) confirmed gaps that `sec-supply-chain-ci` left open. The
most serious: `rayd-sign` held `id-token: write` with no environment and no
publish gate, so a `workflow_dispatch` dry run started from a `rayd-v*` tag
obtained a Sigstore certificate with exactly the identity the documented
recipe accepts, and nobody had to approve anything; `verify.md` claimed
that a signature implied the maintainer's approval and a commit on `main`.
The same class of cache poisoning closed for `release.yml` (SC-01) was
still open for the Pages build and the e2e jobs, uv was unpinned outside
the release, `uv run` silently re-locked a stale `uv.lock`, `uvx` tools
pinned only the top-level package while their transitive graph resolved
fresh on every run, `python-publish` uploaded every file of `dist/` while
`sha256sum -c` checked only the listed ones, the image artifact builder
followed symlinks, the e2e OIDC trust could not spell the repository's
immutable `sub`, and the recommended image recipe made signature
verification optional.

## What Changes

- **Signing waits for approval**: every `release.yml` job with
  `id-token: write` declares an environment and runs only when
  `publish == 'true'`; `rayd-sign` joins the `release` environment. A dry
  run builds and stops.
- **No cache in privileged jobs**: every job of `release.yml`, `docs.yml`
  and `e2e.yml`, and every job holding `id-token`, `pages` or
  `contents: write` or an environment, restores no Actions cache.
- **uv pinned everywhere**: a local composite action
  `.github/actions/setup-uv` fixes the uv version and the sha256 of its
  tarball per runner architecture; no workflow calls `astral-sh/setup-uv`
  directly.
- **uv never re-locks**: `UV_LOCKED=1` in every workflow that runs uv, and
  `uv lock --check` for `clients/python` and `kernel-sidecar` in `ci.yml`.
- **Hash-pinned tools**: pip-audit and cfn-lint install from
  `.github/release/requirements-{pip-audit,cfn-lint}.txt` with
  `--require-hashes` (CI and `make infra-lint`), `make wheel` installs twine
  from the existing hashed file; gate 2 of `check_pins.py` admits `uvx` only
  for dependency-free tools (`ruff`); every tool file is audited by
  pip-audit and watched by a Dependabot `pip` entry; Dependabot also reads
  the local composite actions.
- **Exact `dist/`**: `python-build` and `python-publish` require `dist/` to
  hold exactly the tag's wheel and sdist, both listed in `SHA256SUMS`.
- **No symlinks in the image artifact**: `shipped_files` refuses any symlink
  outside the excluded directories, so `copy_tree` and `write_zip` inherit
  it.
- **OIDC trust with the immutable subject**: `infra/ci-oidc-role.yaml`
  takes `GitHubSubjectPrefix` (no default, read from the GitHub API) and
  keeps `StringEquals`.
- **Docs**: `verify.md` states what a signature proves and what it does
  not; the recommended `configurar-aws.md` recipe verifies the bundle and
  `SHA256SUMS` before `rayito image publish`; `SECURITY.md` T10,
  `security.md`, `docs/RELEASING.md` (PAT blast radius and rotation, the
  accepted GHCR tag of the PyPI publish action, pending repository
  settings), `infra/README.md`, CONTRIBUTING and the gates reference.

## Capabilities

### Modified Capabilities

- `ci-hardening`: privileged jobs restore no cache, uv pinned and locked,
  tools with dependencies never run through `uvx`, hashed tool files are
  audited and watched.
- `release-automation`: signing behind the `release` environment, exact
  `dist/` file set.
- `e2e-workflow`: trust on the exact subject prefix.
- `cli`: the image artifact builder refuses symlinks.
- `security-docs`: what a signature proves; verification before publish.

## Impact

- `.github/workflows/{release,ci,docs,e2e,audit}.yml`,
  `.github/actions/setup-uv/action.yml`, `.github/dependabot.yml`,
  `.github/release/requirements-{pip-audit,cfn-lint}.txt`, `Makefile`,
  `scripts/check_pins.py`, `scripts/tests/`, `infra/ci-oidc-role.yaml`,
  `clients/python/src/rayito/cli/_artifact.py` and its test, docs listed
  above, the `rayd` and Python changelogs.
- A `rayd` release asks for two approvals (sign, upload). Dry runs no
  longer produce signed bundles.
- Deploying `ci-oidc-role.yaml` now needs `GitHubSubjectPrefix`.
- No runtime change to `rayd` or the SDKs beyond the CLI builder refusing
  symlinks; no AWS call.
