## Why

`docs/SECURITY_AUDIT.md` §8 lists two rows still open from the 2026-09-22
audit, both marked **M8** (this milestone is M10, the wave that finally
picks them up) and both about a compromised dependency trading a
publishing credential for a real one:

- **C-10**: `release.yml`'s `python` and `typescript` jobs are the only ones
  that hold `id-token: write`, and the same job installs dependencies and
  runs third-party build code next to that token — `pnpm install` runs
  dependency lifecycle scripts by default and `uv build` resolves the
  `uv_build` PEP 517 backend. A compromised `postinstall` or build backend
  can patch `dist/`/the packed tarball, or simply mint the OIDC token itself
  and trade it for a publish token, before the workflow ever reaches the
  `if: needs.resolve.outputs.publish == 'true'` step.
- **C-12**: `image/Dockerfile` installs `kernel-sidecar/requirements.txt` and
  `requirements-poly.txt` with exact `==` pins but no `--require-hashes`, so a
  file quietly added to an already-published release, or a wheel pulled for
  the wrong platform (falling back to an sdist that runs `setup.py` inside
  the build VM), is accepted silently.

Both rows are checkable with local gates and a `workflow_dispatch` dry run;
neither needs an AWS call, an image build or an e2e.

## What Changes

- **`release.yml` build/publish split (C-10, the residue of H-02).** The
  `python` and `typescript` jobs are each split into a `*-build` job (no
  `environment`, no `id-token`, the only job that checks out the repository
  and runs `pnpm`/`uv`) and a `*-publish` job (`environment` + `id-token:
  write`, no checkout, no install, no third-party tooling at all). The build
  job uploads its artefact and a job output with its sha256; the publish job
  downloads that artefact by a pinned `actions/download-artifact` commit,
  verifies the sha256 the build job produced with `sha256sum -c` and only
  then runs the actual publish action/CLI. `pnpm install --frozen-lockfile`
  gains `--ignore-scripts` (belt-and-suspenders with the new
  `clients/typescript/.npmrc`) and `npm publish` gains `--ignore-scripts`
  too. Trusted Publishing on both PyPI and npm binds the token to the
  workflow file and the `environment`, not the job name, so neither registry
  needs reconfiguring. `rayd` and `resolve` are untouched; `rayd`'s own
  residue (its cosign signing shares the same `id-token: write` job as its
  build) is named as a follow-up in `design.md`, not fixed here.
- **`clients/typescript/.npmrc` (C-10).** `ignore-scripts=true`, verified on
  a clean tree against the SDK's own lint/typecheck/build/test/pack:check
  before committing.
- **Hash-pinned, wheels-only sidecar installs (C-12).**
  `kernel-sidecar/requirements.txt` and `requirements-poly.txt` are
  regenerated with `uv pip compile --generate-hashes`, pinning no version
  differently than before (asserted by a `name==version` diff before
  committing; a new unit test asserts every pin carries a hash);
  `image/Dockerfile`'s two `pip install -r` layers gain
  `--require-hashes --no-deps --only-binary=:all:`. `scripts/check_pins.py`
  gains a fifth gate: every Dockerfile `pip install` (with `-r` or bare
  packages) needs those three flags, and every pin of a `requirements*.txt`
  needs a `--hash=sha256:`.
- **Docs.** `docs/SECURITY_AUDIT.md` marks C-10 and C-12 done in §8 and adds
  their rows to §9, dropping them from "Lo que sigue abierto" (C-06 and
  every other open row untouched); `SECURITY.md` T10 names the hash-pinned,
  wheels-only installs and the build/publish split; `docs/RELEASING.md`
  describes the new jobs and says dry runs skip publishing and that no
  Trusted Publisher reconfiguration is needed.

## Capabilities

### Modified Capabilities

- `release-automation`: the `release.yml publishes each component from its
  tag` requirement is rewritten around the `*-build`/`*-publish` split for
  Python and TypeScript, naming the new jobs, the artefact-plus-digest
  hand-off and the flags each build/publish step now carries.
- `ci-hardening`: the pinning gate gains a fifth check (Dockerfile pip
  installs and the hash coverage of the sidecar's requirements files).

### New Capabilities

None. Both rows land inside a capability that already owns the surface
they touch.

## Impact

- Edited: `.github/workflows/release.yml`, `scripts/check_pins.py`,
  `image/Dockerfile`, `kernel-sidecar/requirements.txt`,
  `kernel-sidecar/requirements-poly.txt`, `docs/SECURITY_AUDIT.md`,
  `SECURITY.md`, `docs/RELEASING.md`, `clients/typescript/CHANGELOG.md`,
  `clients/python/CHANGELOG.md`, `CHANGELOG.md` (crates/rayd, if the release
  workflow counts as its surface) or the repository-root changelog that
  covers CI/image, `docs/research/2026-09-m9-architecture-review.md` (mark
  C-10/C-12 fixed, if that document names them).
- New: `clients/typescript/.npmrc`, `scripts/tests/test_release_workflow.py`.
- Tests: `scripts/check_pins.py`'s own suite gains the fifth gate's cases;
  `scripts/tests/test_security_docs.py` gains the §9-table assertion;
  `kernel-sidecar`'s own suites are unaffected (same versions, only hashes
  added) and are re-run locally as evidence.
- Behaviour: none of the four plan items changes what any component does at
  runtime or what any published artefact contains; the only externally
  visible change is that a `workflow_dispatch` dry run now shows two more
  jobs (`*-build` alongside `*-publish`, the latter always skipped on a dry
  run) instead of one job per component.
- Cost: none. No AWS call, no image build and no e2e in this change; the
  `workflow_dispatch` dry runs used as evidence build and upload artefacts
  on GitHub-hosted runners only.

## Out of scope (named so nobody wonders)

- `.github/workflows/ci.yml`, `audit.yml` and `scorecard.yml` are not
  touched.
- The `rayd` job's own residue of C-10 (its cosign signing shares the same
  `id-token: write` job as its `cargo auditable zigbuild`) is not split
  here; `design.md` records it as a follow-up.
- `harden-runner`'s `egress-policy` stays `audit` everywhere; no pnpm, npm,
  Node, uv, twine or action version is bumped beyond the one new
  `actions/download-artifact` pin this change adds; no pinned Python
  package version changes (that is Dependabot's lane); no
  `onlyBuiltDependencies` is added; C-06 and every other `docs/SECURITY_AUDIT.md`
  row is untouched; `clients/python/pyproject.toml`'s build-backend range is
  untouched.
