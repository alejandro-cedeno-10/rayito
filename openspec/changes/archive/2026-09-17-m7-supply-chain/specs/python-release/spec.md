## MODIFIED Requirements

### Requirement: Release workflow with Trusted Publishing, dry-run only
`.github/workflows/release.yml` SHALL trigger on tags `python-v*`, `typescript-v*` and `rayd-v*` and on `workflow_dispatch` (inputs `tag`, optional, and `dry_run`, default `true`); a `resolve` job SHALL map the tag to one component and version. The Python job SHALL run only for a `python-v*` tag (or a dispatched `python-v*` `tag`), SHALL run `uv build`, `scripts/check_wheel.py`, `twine check`, assert that the tag version equals `pyproject.toml`'s version, upload the `dist` artifact, and SHALL publish only when the run is a tag push or a dispatch with `tag` set and `dry_run: false`, in the GitHub environment `pypi`, with `permissions: id-token: write` and `contents: read`, using `pypa/gh-action-pypi-publish` pinned to a commit SHA with `packages-dir: clients/python/dist`, no API token and attestations enabled (the action's default). The workflow SHALL declare `permissions: contents: read` at the top level, pin every action by commit SHA with a version comment, and start every job with `step-security/harden-runner`. The tags are created by release-please (`release-automation`); registering the PyPI Trusted Publisher on `release.yml`/`pypi` and creating the environment remain manual steps documented in `docs/RELEASING.md`.

#### Scenario: dispatch builds without publishing
- **WHEN** the workflow runs via `workflow_dispatch` with `tag: python-v0.2.0` and `dry_run: true`
- **THEN** the Python job builds, checks and uploads the `dist` artifact and the publish step is skipped

#### Scenario: tag mismatch fails early
- **WHEN** the workflow runs on a tag `python-v0.3.0` while `pyproject.toml` says `0.2.0`
- **THEN** the Python job fails before uploading any artifact

#### Scenario: other components do not run the Python job
- **WHEN** the workflow runs on `rayd-v0.2.0`
- **THEN** the Python job is skipped and only the `rayd` job runs
