## MODIFIED Requirements

### Requirement: Every uvx invocation names an exact version
Every `uvx` invocation under `.github/workflows/` and in the `Makefile` SHALL name its tool with an exact version (`uvx ruff==0.16.7 check .`), including the `--from <package>==<version>` form, because those invocations resolve and execute third-party code from PyPI inside jobs on `main` and on the maintainer's workstation. Because `==` pins only the top-level tool and its transitive graph resolves fresh on every run (no hash, no cooldown), `uvx` SHALL run only tools listed in `scripts/check_pins.py`'s `DEPENDENCY_FREE_UVX_TOOLS` (today `ruff`, a binary with no Python dependencies); every other tool (twine, pip-audit, cfn-lint) SHALL be installed with `uv pip install --require-hashes --no-deps --only-binary :all:` from a hash-pinned file under `.github/release/requirements-*.txt` into a fresh virtual environment. `scripts/check_pins.py` SHALL report every `uvx` invocation in those files whose tool carries no `==`, and every pinned `uvx` of a tool outside that list, and exit 1; the CI `check` job and `make lint` SHALL run it, and the maintainer recipes in `docs/RELEASING.md` and `CONTRIBUTING.md` SHALL install the same tools from the same hashed files. User-facing install recipes under `docs/site/` (`uvx --from "rayito[mcp]" rayito-mcp`) SHALL NOT be part of the gate's scope, since they install the published Rayito rather than a tool of this build.

#### Scenario: an unpinned tool fails the gate
- **WHEN** the gate runs over `uvx twine check clients/python/dist/*` and `uvx cfn-lint --version`
- **THEN** both lines are reported with their file and line number and the gate exits 1

#### Scenario: a pinned tool with dependencies fails the gate
- **WHEN** the gate runs over `uvx pip-audit==2.10.1 -r req.txt`, `uvx twine==7.0.0 check dist/*`, `uvx cfn-lint==1.56.3 -- infra/iam.yaml` and `uvx --from pkg==1.0 tool`
- **THEN** each line is reported with the reason that `uvx` resolves the tool's graph on the fly, and the gate exits 1

#### Scenario: pinned invocations pass
- **WHEN** the gate runs over `uvx ruff==0.16.7 format --check .` and `uvx --from ruff==0.16.7 ruff check .`
- **THEN** nothing is reported and the gate exits 0

#### Scenario: the repository is clean
- **WHEN** `python3 scripts/check_pins.py` runs over the real `.github/workflows/`, the local actions and the `Makefile`
- **THEN** it exits 0, and the only `uvx` left in those files runs `ruff`

## ADDED Requirements

### Requirement: Privileged jobs restore no cache
No job of `release.yml`, `docs.yml` (its build becomes the GitHub Pages site, deployed without a reviewer) or `e2e.yml` (AWS credentials), and no job in any workflow that holds `id-token: write`, `pages: write` or `contents: write` or declares an `environment`, SHALL restore a GitHub Actions cache: no `actions/cache`, `Swatinem/rust-cache` or `mlugg/setup-zig`, `setup-uv` with its cache off, `setup-node` without `cache` and with `package-manager-cache: false`, and no `pnpm/action-setup` cache. Unprivileged jobs of `ci.yml` MAY keep their caches. `scripts/tests/test_workflow_hardening.py` SHALL derive the privileged set from the workflow files, so a new privileged job is covered without editing the test.

#### Scenario: the Pages build restores a poisoned cache
- **WHEN** a `ci.yml` job on a push to `main` has written a main-scope uv cache entry with the key `docs.yml`'s build would compute
- **THEN** `docs.yml`'s build does not restore it, because its `setup-uv` runs with the cache off, and the test fails if the cache is turned back on

#### Scenario: a new job with a token
- **WHEN** a job with `id-token: write` is added to any workflow with `cache: pnpm` on `setup-node`
- **THEN** `test_privileged_jobs_restore_no_cache` fails naming the workflow, the job and the cache

### Requirement: uv is pinned in every workflow and never re-locks
Every workflow job SHALL install uv through the local composite action `.github/actions/setup-uv`, which SHALL hold the uv version and the sha256 of its x86_64 and aarch64 Linux tarballs in one place, select the checksum from `runner.arch` (failing on any other architecture), pass both to a SHA-pinned `astral-sh/setup-uv`, and default its `enable-cache` input to `false`. No workflow SHALL call `astral-sh/setup-uv` directly. Every workflow that runs `uv` SHALL set `UV_LOCKED: "1"` at the workflow level, and `ci.yml`'s `check` job SHALL run `uv lock --check` in `clients/python` and in `kernel-sidecar`. Dependabot's `github-actions` entry SHALL read `/.github/actions/*` as well as `/`.

#### Scenario: a stale lock
- **WHEN** a pull request changes a dependency range in `clients/python/pyproject.toml` without regenerating `uv.lock`
- **THEN** `uv lock --check` fails in the `check` job, and any `uv run` in a workflow refuses to re-lock instead of installing newer versions

#### Scenario: uv is installed on an arm runner
- **WHEN** the `arm` job of `ci.yml` uses `./.github/actions/setup-uv`
- **THEN** setup-uv downloads the pinned version and verifies it against the aarch64 checksum

### Requirement: Hash-pinned tool requirements are audited and watched
Every `.github/release/requirements-*.txt` SHALL carry `==` and `--hash=sha256:` on every pin (gate 5 of `scripts/check_pins.py`), SHALL state in its header how to regenerate it with `uv pip compile --generate-hashes --exclude-newer <today - 7 days>`, SHALL be audited by pip-audit (itself installed from `requirements-pip-audit.txt` with `--require-hashes`) in `ci.yml`'s `audit` job and in the weekly `audit.yml`, and SHALL be watched by a Dependabot `pip` entry for `/.github/release` with the same cooldown as the other ecosystems.

#### Scenario: a CVE in a pin of twine's graph
- **WHEN** an advisory is published for a package pinned in `requirements-twine.txt`
- **THEN** the weekly audit fails and Dependabot proposes the patched pin

#### Scenario: a new tool file
- **WHEN** a `requirements-<tool>.txt` is added under `.github/release/`
- **THEN** the audit jobs' loop covers it without a workflow change and the gate requires hashes on its pins
