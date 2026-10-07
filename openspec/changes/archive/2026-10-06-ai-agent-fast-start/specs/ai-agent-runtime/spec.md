## MODIFIED Requirements

### Requirement: The OpenCode adapter is headless, credential-free and byte-identical across SDKs

The `opencode` runtime SHALL write an `opencode.json` that only carries the
model credential placeholder, SHALL pass the prompt on stdin and never in
argv, SHALL always pass `--title`, SHALL hold a per-sandbox run lock and
SHALL attach to a resident `opencode serve` only when it answers its health
check. When it attaches, the run script SHALL create the session on the
server (title `OPENCODE_SESSION_TITLE` and the non-interactive rules that
`opencode run` applies: `question`, `plan_enter` and `plan_exit` denied)
unless a `session_id` was given, SHALL announce it with a
`rayito.attached` line that carries the session id, the start time and
whether reasoning was asked, and, after `opencode run` exits, SHALL re-read
that turn's messages from `GET /session/<id>/message` one page of one
message at a time, newest first, until the prompt's `user` message or
`OPENCODE_RECONCILE_MAX_MESSAGES`, and print them oldest first as
`rayito.message` lines. The adapter SHALL emit from those lines only the
parts the stream did not already emit (by part id), with the filters of
`opencode run --format json` (finished text, finished reasoning only when
asked, finished tools, step start and finish), SHALL ignore messages older
than the run's start time, and SHALL turn an assistant message `error` into
`AgentFailed(reason="model_error")` when the stream carried none. A
`session_id` that is not an OpenCode id SHALL raise
`InvalidArgumentException` / `InvalidArgumentError`. Python and TypeScript
SHALL produce the same configuration bytes, sha256 and run script for the
same spec (`testdata/agent/`).

#### Scenario: Error events decide failure, not the exit code

- **WHEN** OpenCode emits an `error` event and then exits with code 0
- **THEN** the run ends with `AgentFailed(reason="model_error")`, whose
  `detail_code` is the error name and never its message

#### Scenario: A second run while one holds the lock

- **WHEN** a run starts while another holds the run lock
- **THEN** it ends with `AgentFailed(reason="busy")` without starting OpenCode

#### Scenario: An attached run that the CLI cut short

- **WHEN** a unit test feeds the captured output of `opencode run --attach`
  1.18.34 (`testdata/agent/opencode-attach/`), whose stream stops after the
  first `step_start` and is followed by the re-read messages
- **THEN** both SDKs emit every step, tool call and text of the turn once,
  in order, end with `Done` and the summed usage, and match `expected.json`

#### Scenario: A model error that only the server saw

- **WHEN** the re-read assistant message carries `error` and the stream had
  no `error` event
- **THEN** the run ends with `AgentFailed(reason="model_error")` whose
  `detail_code` is the error name
