# sdk-limits Specification

## Purpose
TBD - created by archiving change m6-typescript-sdk. Update Purpose after archive.
## Requirements
### Requirement: One JSON source of API limits for every SDK
The repository SHALL hold every client-validated Lambda MicroVMs limit in `limits.json` at the repository root, with camelCase keys and the values documented in `AWS_API_NOTES.md` §2, §3, §6 and §11 (`maxDurationSeconds 28800`, `minDurationSeconds 1`, `idleMaxIdleMinSeconds 60`, `idleSuspendedMinSeconds 0`, `tokenTtlMinutes 60`, `tokenTtlMinMinutes 1`, `tokenRefreshAfterMinutes 45`, `tokenRefreshRetrySeconds 60`, `runHookPayloadMaxChars 4096`, `clientTokenMax 128`, `microvmIdMinLength 1`, `microvmIdMaxLength 256`, `listMaxResults 50`, `networkConnectorsMax 10`, `defaultPort 8080`, `hooksPort 9000`, `portMin 1`, `portMax 65535`, `endpointTlsPort 443`, `hookPathPrefix`, `maxConcurrentConnections1Vcpu 8`, `microvmStates`, `terminalStates`, `suspendedStates`, `managedNetworkConnectors`, `supportedRegions`, `apiTps`). `scripts/gen_limits.py` (standard library only) SHALL render `clients/python/src/rayito/_limits.py` and `clients/typescript/src/limits.ts` from it deterministically, keeping the existing Python constant names (`UPPER_SNAKE`, `Final`, tuples, `frozenset`s, `dict[str, int]`) and the same names in TypeScript (`export const`, `as const` arrays, `ReadonlySet<string>`, `Readonly<Record<string, number>>`), each file starting with a header that says it is generated. `--check` SHALL exit non-zero with a diff when either rendered file differs from the JSON. Neither rendered module SHALL be user-configurable.

#### Scenario: regeneration is idempotent
- **WHEN** `python scripts/gen_limits.py` runs twice in a row
- **THEN** the second run changes no bytes in `_limits.py` or `limits.ts`, and `python scripts/gen_limits.py --check` exits 0

#### Scenario: hand edit is caught
- **WHEN** a developer changes `DEFAULT_PORT` in `_limits.py` to `8081` without touching `limits.json`
- **THEN** `python scripts/gen_limits.py --check` exits 1 and prints a diff naming `_limits.py`

#### Scenario: values are unchanged for Python
- **WHEN** `_limits.py` is regenerated from `limits.json` in this change
- **THEN** every constant has the value it had in `rayito` 0.0.5 (`MAX_DURATION_SECONDS == 28800`, `API_TPS["SuspendMicrovm"] == 2`, `TERMINAL_STATES == frozenset({"TERMINATING", "TERMINATED"})`, …) and the 429 existing unit tests still pass

### Requirement: Each SDK carries a drift test against limits.json
The Python unit suite SHALL include a test that loads `limits.json` and asserts every constant of `rayito._limits` equals its JSON value (skipped only when the JSON is absent, i.e. the package was installed from a wheel), and the TypeScript unit suite SHALL include the same assertion for `limits.ts` importing the JSON directly. `make lint` SHALL run `python scripts/gen_limits.py --check`.

#### Scenario: Python drift test
- **WHEN** `limits.json` sets `listMaxResults` to `49` and `_limits.py` is not regenerated
- **THEN** `uv run pytest tests/unit/test_limits.py` fails naming `LIST_MAX_RESULTS`

#### Scenario: TypeScript drift test
- **WHEN** `limits.ts` is edited so `TOKEN_REFRESH_AFTER_MINUTES` is `50`
- **THEN** `pnpm test` fails in `limits.test.ts` naming `TOKEN_REFRESH_AFTER_MINUTES`

