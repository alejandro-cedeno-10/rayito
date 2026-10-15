## ADDED Requirements

### Requirement: The CLI surface is a shared golden that both SDKs reproduce
`testdata/cli/surface.json` SHALL list every command and group of the Python CLI with its path, whether it is a group, whether it is hidden and, per parameter, its name, kind, flags, type, `required`, `multiple`, default and, when it has them, choices, `is_flag` and `envvar`. `clients/python/tests/unit/cli/test_cli_surface.py` SHALL fail when the `typer` tree differs from the golden and SHALL regenerate it when `RAYITO_UPDATE_CLI_SURFACE` is set. The TypeScript CLI SHALL reproduce this surface and a vitest test SHALL compare its `defineCommand` tree with the same file.

#### Scenario: a changed option fails the Python test
- **WHEN** an option of any `rayito` command changes its flag, type or default without regenerating the golden
- **THEN** `test_cli_surface_matches_the_golden` fails and names the regeneration variable

#### Scenario: the golden carries no machine-specific data
- **WHEN** the golden is generated on two machines
- **THEN** the bytes are identical and contain no absolute local path

### Requirement: The parity checker reports the TypeScript CLI gap per phase
`python3 scripts/check_sdk_parity.py --cli` SHALL read `testdata/cli/surface.json` with the standard library only, print one line per leaf command (`PENDIENTE fase N rayito <path>` when `CLI_PHASES` assigns it a phase, `SIN FASE rayito <path>` otherwise) and SHALL NOT change the exit status of the library parity check while the TypeScript CLI is being delivered.

#### Scenario: every command is pending in phase 0
- **WHEN** `check_sdk_parity.py --cli` runs on the repository at phase 0
- **THEN** every line starts with `PENDIENTE` and the process exits 0

#### Scenario: a new command without a phase is visible
- **WHEN** the golden gains a command that no `CLI_PHASES` entry lists
- **THEN** its line starts with `SIN FASE`

### Requirement: The CLI exists in both SDKs
The npm package `rayito` SHALL declare a `rayito` executable and the Python package SHALL keep its `rayito` entry point; both SHALL accept the same global options (`--profile`, `--region`, `--json`, `--verbose`), print one JSON document on stdout with `--json`, use the same exit codes and report their runtime in `rayito --version`.

#### Scenario: the runtime is identifiable
- **WHEN** `rayito --version` runs from either package
- **THEN** the output names the runtime (`node` or `python`)
