## MODIFIED Requirements

### Requirement: Package metadata for the first public release
`clients/python/pyproject.toml` and `rayito._version.__version__` SHALL both read `0.1.0`, and a unit test SHALL assert they match. The project SHALL declare the licence per PEP 639 as `license = "Apache-2.0"` with `license-files = ["LICENSE", "NOTICE"]` and SHALL declare **no** classifier starting with `License ::`; a unit test SHALL assert both. The project SHALL declare the classifiers `Development Status :: 3 - Alpha`, `Intended Audience :: Developers`, `Operating System :: OS Independent`, `Programming Language :: Python :: 3 :: Only`, `3.11`, `3.12`, `3.13`, `Framework :: AsyncIO`, `Topic :: Software Development :: Libraries :: Python Modules`, `Typing :: Typed`; `keywords`; `[project.urls]` with `Homepage`, `Repository`, `Documentation` and `Changelog`; and a `docs` dependency group with `mkdocs-material` and `mkdocstrings[python]`. Runtime dependencies SHALL stay `grpcio`, `protobuf` (floor equal to the `buf` plugin version) and `boto3`.

#### Scenario: version parity
- **WHEN** `tests/unit/test_packaging.py::test_version_matches_pyproject` reads `pyproject.toml` with `tomllib`
- **THEN** `project.version == rayito.__version__ == "0.1.0"`

#### Scenario: licence expression, no classifier
- **WHEN** `tests/unit/test_packaging.py::test_license_is_apache_2_expression` reads `pyproject.toml` with `tomllib`
- **THEN** `project.license == "Apache-2.0"`, `project["license-files"] == ["LICENSE", "NOTICE"]` and no entry of `project.classifiers` starts with `License ::`

### Requirement: Wheel build and content checks
`cd clients/python && uv build` SHALL produce an sdist and a wheel in `clients/python/dist`; `scripts/check_wheel.py <wheel>` SHALL assert the wheel contains `rayito/py.typed`, `rayito/e2b/__init__.py`, `rayito/v1/health_pb2.py` and `rayito/v1/health_pb2.pyi`, contains nothing under `tests/`, contains one entry ending in `.dist-info/licenses/LICENSE` and one ending in `.dist-info/licenses/NOTICE`, and that `METADATA` declares `Requires-Python: >=3.11`, the three runtime dependencies, `License-Expression: Apache-2.0`, `License-File: LICENSE` and `License-File: NOTICE` and no line starting with `Classifier: License ::`; `uvx twine check dist/*` SHALL pass. The CI `check` job SHALL run the build and both checks.

#### Scenario: wheel contents
- **WHEN** CI runs `uv build`, `python scripts/check_wheel.py clients/python/dist/*.whl` and `uvx twine check clients/python/dist/*`
- **THEN** all three succeed, the wheel lists no `tests/` entry, and `check_wheel.py` prints `OK`

#### Scenario: licence metadata missing
- **WHEN** `check_wheel.py` inspects a wheel whose `METADATA` lacks `License-Expression: Apache-2.0` or whose archive lacks `.dist-info/licenses/NOTICE`
- **THEN** it prints `KO` naming each missing item and exits 1
