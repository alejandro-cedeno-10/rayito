## ADDED Requirements

### Requirement: A failed startup says why, bounded and without secrets
When the host cannot start, it SHALL write the failed marker with the failure code on the first line (the `detail_code` of an `AgentFailure` such as `app_exited` or `app_not_ready`, or the class name of any other exception) and, if there is one, the diagnostics on the following lines. For an `http-app` the diagnostics SHALL be the end of the app's stderr; for any other exception, its message. The host SHALL remove control characters, every environment value of at least 8 characters except those of structural variables (`PATH`, `PYTHONPATH`, `HOME` and the like), credential patterns and long token-like runs, and SHALL bound the result to `AGENT_STARTUP_DIAGNOSTICS_MAX_BYTES` (2048) before writing it. `deploy()` SHALL raise `SandboxException` whose message is the code and, when present, the diagnostics, bounded again by the SDK. The host SHALL NOT log the diagnostics. A marker with only a code (an older host) SHALL read as before.

#### Scenario: the app exits at startup
- **WHEN** the app prints a traceback to stderr and exits
- **THEN** the marker's first line is `app_exited`, the rest is the end of the traceback and `deploy()` raises `SandboxException` containing it

#### Scenario: a secret in the output
- **WHEN** the app prints the value of an environment variable named like a key, or `token=<value>`, to stderr before failing
- **THEN** neither value appears in the marker nor in the exception

#### Scenario: a noisy app
- **WHEN** the app writes 400 lines to stderr before failing
- **THEN** the diagnostics are at most 2048 bytes and end with the last line

### Requirement: Usage the agent does not report is unknown, not zero
`Done`, `AgentResult` and the `POST /invocations` JSON SHALL carry `usage_reported` (`usageReported` in TypeScript). The host SHALL set it to `false` when no `step_finished` event of the run carried a `usage` object, and the decoders SHALL read an absent field as `true`. The `http-app` driver SHALL populate `usage` from the app's response when it carries `usage` with `input`/`output`, `input_tokens`/`output_tokens` or `prompt_tokens`/`completion_tokens` (and the cache and reasoning spellings), and SHALL emit `step_finished` without a `usage` object otherwise; the same applies to a `callable` that returns no usage and to a `command` with `protocol="text"`. `usage` SHALL stay a `TokenUsage` of zeros when unreported. The CLI SHALL print `tokens=desconocido` and, with `--json`, `usageReported: false`.

#### Scenario: an app that reports usage
- **WHEN** the app answers `{"text": "hola", "usage": {"input_tokens": 12, "output_tokens": 5}}`
- **THEN** the result has `usage.input == 12`, `usage.output == 5` and `usage_reported` true

#### Scenario: an app that reports nothing
- **WHEN** the app answers `{"text": "hola"}`
- **THEN** the result has zero usage and `usage_reported` false

#### Scenario: an older service
- **WHEN** a client reads a response without `usage_reported`
- **THEN** `usage_reported` is true
