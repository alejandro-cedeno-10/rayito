## MODIFIED Requirements

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
