# python-release Specification

## Purpose
TBD - created by archiving change m6-e2b-compat. Update Purpose after archive.
## Requirements
### Requirement: Package metadata for the first public release
`clients/python/pyproject.toml` and `rayito._version.__version__` SHALL both read `0.1.0`, and a unit test SHALL assert they match. The project SHALL declare the licence per PEP 639 as `license = "Apache-2.0"` with `license-files = ["LICENSE", "NOTICE"]` and SHALL declare **no** classifier starting with `License ::`; a unit test SHALL assert both. The project SHALL declare the classifiers `Development Status :: 3 - Alpha`, `Intended Audience :: Developers`, `Operating System :: OS Independent`, `Programming Language :: Python :: 3 :: Only`, `3.11`, `3.12`, `3.13`, `Framework :: AsyncIO`, `Topic :: Software Development :: Libraries :: Python Modules`, `Typing :: Typed`; `keywords`; `[project.urls]` with `Homepage`, `Repository`, `Documentation` and `Changelog`; and a `docs` dependency group with `mkdocs-material` and `mkdocstrings[python]`. Runtime dependencies SHALL stay `grpcio`, `protobuf` (floor equal to the `buf` plugin version) and `boto3`.

#### Scenario: version parity
- **WHEN** `tests/unit/test_packaging.py::test_version_matches_pyproject` reads `pyproject.toml` with `tomllib`
- **THEN** `project.version == rayito.__version__ == "0.1.0"`

#### Scenario: licence expression, no classifier
- **WHEN** `tests/unit/test_packaging.py::test_license_is_apache_2_expression` reads `pyproject.toml` with `tomllib`
- **THEN** `project.license == "Apache-2.0"`, `project["license-files"] == ["LICENSE", "NOTICE"]` and no entry of `project.classifiers` starts with `License ::`

### Requirement: Changelog and README document the released surface
`clients/python/CHANGELOG.md` SHALL follow Keep a Changelog with a `0.1.0` entry listing the surface by milestone, the image requirement for metadata, the known limitations (`set_timeout`, the 8 h cap, bandwidth) and the manual publication steps, with `0.0.1–0.0.5` collapsed as unpublished milestone builds. `clients/python/README.md` SHALL contain installation, credentials (`AWS_PROFILE`/`AWS_REGION`, `RAYITO_TEMPLATE`), and one runnable example for each of: create/kill, `commands` (foreground, background, `on_stdout`), `files` (`write`, `read`, `write_files`, `watch_dir`), `run_code` (text, png, error as data, contexts), `pty`, `pause`/`resume`, `get_host` with headers, `metadata` with the `list(metadata=)` O(n) note, the E2B shim (import swap and what raises `UnimplementedError`), and `AsyncSandbox`, plus a limits/costs table and links to the docs site. The root `README.md` SHALL carry the install line and the shim snippet.

#### Scenario: examples cover every surface
- **WHEN** a reviewer checks `clients/python/README.md` against the list above
- **THEN** each surface has a code block and the metadata block states the per-running-sandbox cost of `list(metadata=)`

### Requirement: Wheel build and content checks
`cd clients/python && uv build` SHALL produce an sdist and a wheel in `clients/python/dist`; `scripts/check_wheel.py <wheel>` SHALL assert the wheel contains `rayito/py.typed`, `rayito/e2b/__init__.py`, `rayito/v1/health_pb2.py` and `rayito/v1/health_pb2.pyi`, contains nothing under `tests/`, contains one entry ending in `.dist-info/licenses/LICENSE` and one ending in `.dist-info/licenses/NOTICE`, and that `METADATA` declares `Requires-Python: >=3.11`, the three runtime dependencies, `License-Expression: Apache-2.0`, `License-File: LICENSE` and `License-File: NOTICE` and no line starting with `Classifier: License ::`; `uvx twine check dist/*` SHALL pass. The CI `check` job SHALL run the build and both checks.

#### Scenario: wheel contents
- **WHEN** CI runs `uv build`, `python scripts/check_wheel.py clients/python/dist/*.whl` and `uvx twine check clients/python/dist/*`
- **THEN** all three succeed, the wheel lists no `tests/` entry, and `check_wheel.py` prints `OK`

#### Scenario: licence metadata missing
- **WHEN** `check_wheel.py` inspects a wheel whose `METADATA` lacks `License-Expression: Apache-2.0` or whose archive lacks `.dist-info/licenses/NOTICE`
- **THEN** it prints `KO` naming each missing item and exits 1

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

### Requirement: Docs site skeleton builds strictly
`docs/site/mkdocs.yml` SHALL configure `mkdocs-material` with the `mkdocstrings` python handler pointed at `clients/python/src` and a nav of twelve pages under `docs/site/docs`, in this order: `index.md`, `quickstart.md`, `cli.md` (`m7-cli`), `concepts.md`, `api.md` (mkdocstrings for `rayito.Sandbox`, `rayito.AsyncSandbox`, the sub-clients, `rayito.SandboxPool`, `rayito.AsyncSandboxPool`, `rayito.PoolConfig`, the pool stats and backends, the models and `rayito.e2b`), `security.md`, `limits.md`, `e2b-compat.md` (what works unchanged, what maps with a note, what raises `UnimplementedError` and why, the `timeout`/`set_timeout` difference, the metadata cost), `mcp.md` (`m7-mcp-server`), `cost.md` (the measured numbers with their source file: $0.126/h at 2 GB/1 vCPU, ≈ $0.0049 per suspend/resume cycle, $0.037/week per image version, ≈ $0.03 per e2e run, the measured create/resume/first-cell timings, 0.65 MB/s write, 6.71 MB/s read, the measured `list(metadata=)` cost per running sandbox, and one row for an idle pool slot linking `pool.md`), `pool.md` ("Pool de sandboxes": the suspended-slot pool, its API, semantics, backends, custody of the secret, the measured `T_take`/`T_create` and the per-slot cost table) and `verify.md` (release verification). `make docs` and a CI `docs` job SHALL build it with `mkdocs build --strict` and no deploy.

#### Scenario: strict build
- **WHEN** CI runs `cd clients/python && uv run --group docs mkdocs build -f ../../docs/site/mkdocs.yml --strict`
- **THEN** the build succeeds with no warnings, and the API page renders `rayito.Sandbox.create`, `rayito.SandboxPool.take` and `rayito.e2b.Sandbox` signatures

#### Scenario: pool page in the nav
- **WHEN** a reader opens the built site's navigation
- **THEN** "Pool de sandboxes" appears right after "Modelo de costes" and before "Verificar una release", and `cost.md` links to `pool.md` from its pool row

### Requirement: mypy over tests is a lint gate
`[tool.mypy]` SHALL check `src` and `tests` with `strict = true` and `warn_unreachable = true`; the only relaxation SHALL be `disallow_subclassing_any = false` scoped to the five test modules that subclass the untyped generated gRPC servicers (`tests.unit.conftest`, `tests.unit.fake_process`, `tests.unit.fake_filesystem`, `tests.unit.fake_code`, `tests.unit.fake_pty`). The 34 pre-existing errors SHALL be fixed without adding blanket `# type: ignore` comments (an error-coded ignore is allowed only where a test deliberately passes an invalid type to exercise runtime validation), test behaviour SHALL be unchanged, and `make lint` and `ci.yml` SHALL run `uv run mypy src tests` and `uv run ruff format --check .` alongside `uv run ruff check .`.

#### Scenario: zero errors
- **WHEN** `cd clients/python && uv run mypy src tests` runs
- **THEN** it reports `Success: no issues found` and `uv run pytest tests/unit` still passes every existing test

#### Scenario: gate wired
- **WHEN** a test file introduces a type error
- **THEN** both `make lint` and the CI `check` job fail

### Requirement: Optional extra `mcp` and console script `rayito-mcp`
`clients/python/pyproject.toml` SHALL declare `[project.optional-dependencies] mcp = ["mcp>=2.2,<3"]`, `[project.scripts] rayito-mcp = "rayito.mcp.__main__:main"`, and SHALL include `"rayito[mcp]"` in the `dev` dependency group so that `uv run` installs the extra for every gate without new flags. The base runtime dependencies SHALL remain exactly `grpcio`, `protobuf` and `boto3`. `scripts/check_wheel.py` SHALL additionally assert that the wheel contains `rayito/mcp/__init__.py` and `rayito/mcp/__main__.py`, that `entry_points.txt` contains `rayito-mcp = rayito.mcp.__main__:main`, and that `METADATA` contains `Provides-Extra: mcp` and a `Requires-Dist` line for `mcp>=2.2,<3` conditioned on `extra == 'mcp'`. A unit test SHALL assert the three `pyproject.toml` declarations, and another SHALL assert in a subprocess that `import rayito` does not import `mcp`.

#### Scenario: pyproject declarations
- **WHEN** `tests/unit/test_packaging.py::test_mcp_extra_and_script` reads `pyproject.toml` with `tomllib`
- **THEN** `project["optional-dependencies"]["mcp"] == ["mcp>=2.2,<3"]`, `project["scripts"]["rayito-mcp"] == "rayito.mcp.__main__:main"` and `"rayito[mcp]"` is in `dependency-groups.dev`

#### Scenario: wheel carries the subpackage and the entry point
- **WHEN** `cd clients/python && uv build && python ../../scripts/check_wheel.py dist/*.whl` runs
- **THEN** it prints `OK`, and a wheel lacking `rayito/mcp/__main__.py` or the `rayito-mcp` entry point makes it print `KO` naming the missing item and exit 1

#### Scenario: base import stays lean
- **WHEN** `python -c "import rayito, sys; raise SystemExit('mcp' in sys.modules)"` runs in the project environment
- **THEN** it exits 0

