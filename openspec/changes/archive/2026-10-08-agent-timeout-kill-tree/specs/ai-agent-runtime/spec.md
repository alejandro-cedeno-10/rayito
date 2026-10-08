## MODIFIED Requirements

### Requirement: sbx.agent is lazy, enforces the SDK's own limits and aborts by stopping the process tree
`Sandbox.agent`/`AsyncSandbox.agent`/`Sandbox.agent` (TypeScript) SHALL be constructed with the sandbox and SHALL make no RPC until `run()`, `stream()` or `prepare()` is called. `run()` and `stream()` SHALL NOT accept an `attach` option. `run()` SHALL raise `AgentException`/`AgentError` when the run fails; `stream()` SHALL return an iterable (`AgentStream`) that never raises for an agent failure — its last event is `Done` or `AgentFailed` — and SHALL raise only for a sandbox or transport error. The SDK SHALL enforce `AgentLimits.max_steps`/`maxSteps` when a `StepStarted` index exceeds it and `AgentLimits.max_total_tokens`/`maxTotalTokens` after any `StepFinished` whose accumulated usage exceeds it, independent of what the runtime itself enforces. The runtime's configuration SHALL be written with `files.write_files`/`files.writeFiles` only when its sha256 differs from the last one applied to that `Agent`/`AsyncAgent` instance. Every run SHALL start its command with `kill_tree=True`/`killTree: true`, so `rayd` stops and kills every descendant of the runtime, including those that started their own session or were daemonised, on the run's timeout and on a `SIGKILL`. `AgentStream.abort()` SHALL kill the underlying command handle, with no other command run in the sandbox, and the stream's final event SHALL be `AgentFailed(reason="aborted")`. When the SDK ends a run for `max_steps` or `token_budget`, it SHALL kill the handle the same way and consume it until its end event before the stream ends, so the run lock is free for the next run. When the handle ends with the command's timeout end status (which `wait()` reports as `TimeoutException`/`TimeoutError`), the final event SHALL be `AgentFailed(reason="timeout")`.

#### Scenario: touching sbx.agent makes no call
- **WHEN** `sbx.agent` is read right after `Sandbox.create()`/`connect()`
- **THEN** no `commands.run`, `files.write_files` or boto3 call happens

#### Scenario: max_steps is enforced by the SDK, not only the runtime
- **WHEN** a runtime emits `StepStarted(index=2)` and `AgentLimits(max_steps=1)` was passed
- **THEN** the stream's next event is `AgentFailed(reason="max_steps")`, and `run()` raises `AgentException` with that reason

#### Scenario: the config is written once per sha
- **WHEN** two runs use the same `Agent`/`AsyncAgent` instance and the runtime's `build_config()` returns the same `config_sha256` both times
- **THEN** `files.write_files`/`files.writeFiles` is called only on the first run

#### Scenario: abort stops the process tree, then kills the handle
- **WHEN** `stream.abort()` is called
- **THEN** the run's command was started with `kill_tree`, the handle is killed, no other command is run in the sandbox, and `stream.result()` raises `AgentException`/`rejects` with `reason="aborted"`

#### Scenario: an SDK limit stops the runtime and its process tree
- **WHEN** the stream ends with `AgentFailed(reason="max_steps")` or `AgentFailed(reason="token_budget")`
- **THEN** the handle of the `kill_tree` run was killed and consumed to its end before the stream ended, and no other command was run

#### Scenario: a timeout reported only by the end event is a timeout
- **WHEN** the command handle ends without raising while iterating and its `wait()` raises `TimeoutException`/`TimeoutError`
- **THEN** the stream's final event is `AgentFailed(reason="timeout")`

#### Scenario: a timeout leaves nothing the runtime daemonised
- **WHEN** a run whose runtime daemonises a process (`setsid` and a parent that exits) reaches `AgentLimits.timeout_seconds`/`timeoutMs`
- **THEN** the stream's final event is `AgentFailed(reason="timeout")` and, when it arrives, the daemonised process is no longer alive
