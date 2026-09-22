## ADDED Requirements

### Requirement: Package metadata for the first public release
`clients/python/pyproject.toml` and `rayito._version.__version__` SHALL both read `0.1.0`, and a unit test SHALL assert they match. The project SHALL declare the classifiers `Development Status :: 3 - Alpha`, `Intended Audience :: Developers`, `License :: OSI Approved :: MIT License`, `Operating System :: OS Independent`, `Programming Language :: Python :: 3 :: Only`, `3.11`, `3.12`, `3.13`, `Framework :: AsyncIO`, `Topic :: Software Development :: Libraries :: Python Modules`, `Typing :: Typed`; `keywords`; `[project.urls]` with `Homepage`, `Repository`, `Documentation` and `Changelog`; and a `docs` dependency group with `mkdocs-material` and `mkdocstrings[python]`. Runtime dependencies SHALL stay `grpcio`, `protobuf` (floor equal to the `buf` plugin version) and `boto3`.

#### Scenario: version parity
- **WHEN** `tests/unit/test_packaging.py::test_version_matches_pyproject` reads `pyproject.toml` with `tomllib`
- **THEN** `project.version == rayito.__version__ == "0.1.0"`

### Requirement: Changelog and README document the released surface
`clients/python/CHANGELOG.md` SHALL follow Keep a Changelog with a `0.1.0` entry listing the surface by milestone, the image requirement for metadata, the known limitations (`set_timeout`, the 8 h cap, bandwidth) and the manual publication steps, with `0.0.1–0.0.5` collapsed as unpublished milestone builds. `clients/python/README.md` SHALL contain installation, credentials (`AWS_PROFILE`/`AWS_REGION`, `RAYITO_TEMPLATE`), and one runnable example for each of: create/kill, `commands` (foreground, background, `on_stdout`), `files` (`write`, `read`, `write_files`, `watch_dir`), `run_code` (text, png, error as data, contexts), `pty`, `pause`/`resume`, `get_host` with headers, `metadata` with the `list(metadata=)` O(n) note, the E2B shim (import swap and what raises `UnimplementedError`), and `AsyncSandbox`, plus a limits/costs table and links to the docs site. The root `README.md` SHALL carry the install line and the shim snippet.

#### Scenario: examples cover every surface
- **WHEN** a reviewer checks `clients/python/README.md` against the list above
- **THEN** each surface has a code block and the metadata block states the per-running-sandbox cost of `list(metadata=)`

### Requirement: Wheel build and content checks
`cd clients/python && uv build` SHALL produce an sdist and a wheel in `clients/python/dist`; `scripts/check_wheel.py <wheel>` SHALL assert the wheel contains `rayito/py.typed`, `rayito/e2b/__init__.py`, `rayito/v1/health_pb2.py` and `rayito/v1/health_pb2.pyi`, contains nothing under `tests/`, and that `METADATA` declares `Requires-Python: >=3.11` and the three runtime dependencies; `uvx twine check dist/*` SHALL pass. The CI `check` job SHALL run the build and both checks.

#### Scenario: wheel contents
- **WHEN** CI runs `uv build`, `python scripts/check_wheel.py clients/python/dist/*.whl` and `uvx twine check clients/python/dist/*`
- **THEN** all three succeed and the wheel lists no `tests/` entry

### Requirement: Release workflow with Trusted Publishing, dry-run only
`.github/workflows/release.yml` SHALL trigger on tags `python-v*` and on `workflow_dispatch`; its `build` job SHALL run `uv build`, `scripts/check_wheel.py`, `twine check`, assert on a tag that the tag version equals `pyproject.toml`'s version, and upload the `dist` artifact; its `publish` job SHALL run only on a tag push, `needs: build`, in the GitHub environment `pypi`, with `permissions: id-token: write` and `contents: read`, using `pypa/gh-action-pypi-publish@release/v1` with `packages-dir: clients/python/dist` and no API token. This change SHALL NOT push a tag nor publish; registering the PyPI Trusted Publisher and creating the `pypi` environment SHALL be documented as manual steps.

#### Scenario: dispatch builds without publishing
- **WHEN** the workflow runs via `workflow_dispatch`
- **THEN** the `build` job succeeds and the `publish` job is skipped

#### Scenario: tag mismatch fails early
- **WHEN** the workflow runs on a tag `python-v0.2.0` while `pyproject.toml` says `0.1.0`
- **THEN** the `build` job fails before uploading any artifact

### Requirement: Docs site skeleton builds strictly
`docs/site/mkdocs.yml` SHALL configure `mkdocs-material` with the `mkdocstrings` python handler pointed at `clients/python/src` and a nav of eight pages under `docs/site/docs`: `index.md`, `quickstart.md`, `concepts.md`, `api.md` (mkdocstrings for `rayito.Sandbox`, `rayito.AsyncSandbox`, the sub-clients, the models and `rayito.e2b`), `security.md`, `limits.md`, `e2b-compat.md` (what works unchanged, what maps with a note, what raises `UnimplementedError` and why, the `timeout`/`set_timeout` difference, the metadata cost) and `cost.md` (the measured numbers with their source file: $0.126/h at 2 GB/1 vCPU, ≈ $0.011 per suspend/resume cycle, $0.037/week per image version, ≈ $0.03 per e2e run, 2.36 s to `kernel_ready`, ≈ 1.4 s `pause()`, 0.7–1.6 s auto-resume, 0.65 MB/s write, 6.71 MB/s read, the measured `list(metadata=)` cost per running sandbox). `make docs` and a CI `docs` job SHALL build it with `mkdocs build --strict` and no deploy.

#### Scenario: strict build
- **WHEN** CI runs `cd clients/python && uv run --group docs mkdocs build -f ../../docs/site/mkdocs.yml --strict`
- **THEN** the build succeeds with no warnings, and the API page renders `rayito.Sandbox.create` and `rayito.e2b.Sandbox` signatures

### Requirement: mypy over tests is a lint gate
`[tool.mypy]` SHALL check `src` and `tests` with `strict = true` and `warn_unreachable = true`; the only relaxation SHALL be `disallow_subclassing_any = false` scoped to the five test modules that subclass the untyped generated gRPC servicers (`tests.unit.conftest`, `tests.unit.fake_process`, `tests.unit.fake_filesystem`, `tests.unit.fake_code`, `tests.unit.fake_pty`). The 34 pre-existing errors SHALL be fixed without adding blanket `# type: ignore` comments (an error-coded ignore is allowed only where a test deliberately passes an invalid type to exercise runtime validation), test behaviour SHALL be unchanged, and `make lint` and `ci.yml` SHALL run `uv run mypy src tests` and `uv run ruff format --check .` alongside `uv run ruff check .`.

#### Scenario: zero errors
- **WHEN** `cd clients/python && uv run mypy src tests` runs
- **THEN** it reports `Success: no issues found` and `uv run pytest tests/unit` still passes every existing test

#### Scenario: gate wired
- **WHEN** a test file introduces a type error
- **THEN** both `make lint` and the CI `check` job fail
