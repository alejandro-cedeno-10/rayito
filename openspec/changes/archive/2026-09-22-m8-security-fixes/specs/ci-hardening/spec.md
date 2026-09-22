## MODIFIED Requirements

### Requirement: Every action is pinned to a full commit SHA with a version comment
Every `uses:` reference in every file under `.github/workflows/` SHALL name a 40-character commit SHA followed by a comment with the release tag it was resolved from (`owner/repo@<sha> # vX.Y.Z`), including path-scoped actions such as `github/codeql-action/upload-sarif`. No workflow SHALL reference an action by tag, major alias, branch or truncated SHA. The gate SHALL be an allowlist, not a denylist of spellings: `scripts/check_pins.py` SHALL report every `uses:` line of `.github/workflows/*.yml` and `*.yaml` whose reference does not match `[^@\s]+@[0-9a-f]{40}( +#.*)?` in full, skipping only commented lines and local actions whose reference starts with `./`, and SHALL exit 1 when it reports anything. The CI `check` job SHALL run it, and SHALL run `actionlint` 1.7.12 (downloaded from its release with its checksum verified) over every workflow. `make lint` SHALL run `python scripts/check_pins.py` and `actionlint -no-color` when the binary is on `PATH`, printing the install hint for `actionlint` otherwise.

#### Scenario: a tag reference is rejected
- **WHEN** a workflow contains `uses: actions/checkout@v7`
- **THEN** the pinning gate of the `check` job fails naming the file and line

#### Scenario: the spellings the old denylist missed
- **WHEN** the gate runs over `uses: owner/action@1.2.3`, `uses: owner/action@latest`, `uses: owner/action@release-v2` and `uses: owner/action@ab12cd34`
- **THEN** all four are reported and the gate exits 1

#### Scenario: a full SHA and a local action pass
- **WHEN** the gate runs over `uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1`, the same line without the comment, and `uses: ./.github/actions/local`
- **THEN** nothing is reported and the gate exits 0

#### Scenario: every workflow is actionlint clean
- **WHEN** `actionlint -no-color .github/workflows/*.yml` runs locally or in CI
- **THEN** it exits 0

## ADDED Requirements

### Requirement: Every uvx invocation names an exact version
Every `uvx` invocation under `.github/workflows/` and in the `Makefile` SHALL name its tool with an exact version (`uvx twine==7.0.0 check …`, `uvx ruff==0.16.7 check .`, `uvx cfn-lint==1.56.3 -- …`, `uvx pip-audit==2.10.1 -r …`), including the `--from <package>==<version>` form, because those invocations resolve and execute third-party code from PyPI inside jobs that hold publishing credentials (`release.yml`'s Python job declares `environment: pypi` and `id-token: write`). `scripts/check_pins.py` SHALL report every `uvx` invocation in those files whose tool carries no `==` and exit 1, the CI `check` job and `make lint` SHALL run it, and the maintainer recipes in `docs/RELEASING.md` and `CONTRIBUTING.md` SHALL quote the same pinned commands. User-facing install recipes under `docs/site/` (`uvx --from "rayito[mcp]" rayito-mcp`) SHALL NOT be part of the gate's scope, since they install the published Rayito rather than a tool of this build.

#### Scenario: an unpinned tool fails the gate
- **WHEN** the gate runs over `uvx twine check clients/python/dist/*` and `uvx cfn-lint --version`
- **THEN** both lines are reported with their file and line number and the gate exits 1

#### Scenario: pinned invocations pass
- **WHEN** the gate runs over `uvx pip-audit==2.10.1 -r kernel-sidecar/requirements.txt --no-deps --strict`, `uvx ruff==0.16.7 format --check .` and `uvx --from pkg==1.0 tool`
- **THEN** nothing is reported and the gate exits 0

#### Scenario: the repository is clean
- **WHEN** `python3 scripts/check_pins.py` runs over the real `.github/workflows/` and `Makefile`
- **THEN** it exits 0, and `grep -rn "uvx " .github/workflows Makefile` shows a version on every line
