## ADDED Requirements

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
