## MODIFIED Requirements

### Requirement: The CLI is an optional extra with a console entry point
The Python SDK SHALL declare `[project.optional-dependencies] cli = ["typer>=0.15,<1"]` and `[project.scripts] rayito = "rayito.cli.__main__:main"`, and the `dev` dependency group SHALL contain the identical `typer` pin (asserted by `tests/unit/test_packaging.py`). `rayito.cli.__main__.main()` SHALL import the typer application lazily and, when `typer` is not installed, print a Spanish line telling the user to install `rayito[cli]` to stderr and return 2. `rayito/cli/__init__.py` SHALL contain no imports, and only `rayito/cli/app.py`, `image.py`, `sandbox.py` and `doctor.py` SHALL import `typer`, so `rayito.cli._artifact`, `_publish`, `_prune`, `_checks`, `_compat`, `_session` and `_console` import without the extra. `rayito.cli._artifact` SHALL import only standard-library modules (asserted by an `ast` test against `sys.stdlib_module_names`). The wheel SHALL contain `rayito/cli/__init__.py` and its `METADATA` SHALL declare `Provides-Extra: cli` and `Requires-Dist: typer>=0.15,<1; extra == "cli"`, both asserted by `scripts/check_wheel.py`.

#### Scenario: entry point without the extra
- **WHEN** `rayito` runs in an environment where `rayito` is installed without the `cli` extra
- **THEN** stderr contains `rayito[cli]` and the process exits 2

#### Scenario: library modules need no typer
- **WHEN** a test removes `typer` from `sys.modules` and blocks its import, then imports `rayito.cli._publish`, `rayito.cli._prune`, `rayito.cli._artifact`, `rayito.cli._checks` and `rayito.cli._compat`
- **THEN** every import succeeds

#### Scenario: wheel metadata
- **WHEN** `cd clients/python && uv build && python ../../scripts/check_wheel.py dist/*.whl` runs
- **THEN** it prints `OK`, the wheel lists `rayito/cli/__init__.py`, and `METADATA` contains `Provides-Extra: cli` and `Requires-Dist: typer>=0.15,<1; extra == "cli"`
