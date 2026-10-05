## ADDED Requirements

### Requirement: Signing waits for the release environment
Every job of `.github/workflows/release.yml` that holds `permissions.id-token: write` SHALL declare an `environment` with a required reviewer and SHALL run only when `needs.resolve.outputs.publish == 'true'`. `rayd-sign` SHALL run in the `release` environment, so GitHub issues no OIDC token, and Sigstore no certificate for `https://github.com/<repo>/.github/workflows/release.yml@refs/tags/rayd-v<version>`, before the maintainer approves the run. A `workflow_dispatch` dry run SHALL run the build jobs only and produce no signature. `scripts/tests/test_release_workflow.py` SHALL assert the rule for every `id-token: write` job.

#### Scenario: dry run from a release tag
- **WHEN** `release.yml` is dispatched from a `rayd-v*` tag with `dry_run: true`
- **THEN** `rayd-build` runs and uploads the unsigned assets, `rayd-sign` and `rayd-upload` are skipped, and no Fulcio certificate is issued

#### Scenario: a tag push before approval
- **WHEN** a `rayd-v*` tag is pushed and `release.yml` starts with `publish=true`
- **THEN** `rayd-sign` waits for the `release` environment's reviewer before it runs, and without approval nothing is signed

### Requirement: Published Python distributions are exactly the tag's wheel and sdist
`python-build`, before uploading its artifact, and `python-publish`, after its `sha256sum -c` checks and before `pypa/gh-action-pypi-publish`, SHALL each verify that `dist/` holds exactly `rayito-<version>-py3-none-any.whl` and `rayito-<version>.tar.gz` (with `<version>` from `needs.resolve.outputs.version`) and that `SHA256SUMS` lists exactly those two files, failing otherwise, because `sha256sum -c` ignores unlisted files and the publish action uploads every distribution in `packages-dir`.

#### Scenario: an extra wheel appears after hashing
- **WHEN** a process writes `rayito-9.9.9-py3-none-any.whl` into `dist/` after `SHA256SUMS` was computed
- **THEN** the file-set step fails in `python-build`, and again in `python-publish` if the artifact was altered, and nothing is published
