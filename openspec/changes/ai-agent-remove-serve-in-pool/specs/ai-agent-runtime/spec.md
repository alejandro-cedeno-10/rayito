## MODIFIED Requirements

### Requirement: The AgentRuntime port resolves by name or by object
Both SDKs SHALL define a pure `AgentRuntime` port (`build_config`/`buildConfig`, `command`, `new_state`/`newState`, `parse_line`/`parseLine`, `finish`, `template_steps`/`templateSteps`, `warmup_steps`/`warmupSteps`) and a `nombre -> AgentRuntime` registry. `warmup_steps()`/`warmupSteps()` SHALL take no argument. `runtime=`/`runtime` SHALL accept either a registered name or any object that implements the port. An unregistered name SHALL raise `UnimplementedError`/`UnimplementedError` naming the runtime, never a raw `KeyError`/`undefined` access; any other value SHALL raise `InvalidArgumentException`/`InvalidArgumentError`.

#### Scenario: an unregistered runtime name
- **WHEN** `sbx.agent.run(prompt, spec=spec, runtime="opencode")` is called before any adapter registers `"opencode"`
- **THEN** it raises `UnimplementedError` naming `runtime="opencode"`, before any RPC

#### Scenario: a caller's own runtime object
- **WHEN** `runtime=` is an object that implements every method of `AgentRuntime`
- **THEN** `sbx.agent.run()` uses it directly, with no registry lookup


## ADDED Requirements

### Requirement: sbx.agent is lazy, enforces the SDK's own limits and aborts by stopping the process tree
`Sandbox.agent`/`AsyncSandbox.agent`/`Sandbox.agent` (TypeScript) SHALL be constructed with the sandbox and SHALL make no RPC until `run()`, `stream()` or `prepare()` is called. `run()` and `stream()` SHALL NOT accept an `attach` option. `run()` SHALL raise `AgentException`/`AgentError` when the run fails; `stream()` SHALL return an iterable (`AgentStream`) that never raises for an agent failure — its last event is `Done` or `AgentFailed` — and SHALL raise only for a sandbox or transport error. The SDK SHALL enforce `AgentLimits.max_steps`/`maxSteps` when a `StepStarted` index exceeds it and `AgentLimits.max_total_tokens`/`maxTotalTokens` after any `StepFinished` whose accumulated usage exceeds it, independent of what the runtime itself enforces. The runtime's configuration SHALL be written with `files.write_files`/`files.writeFiles` only when its sha256 differs from the last one applied to that `Agent`/`AsyncAgent` instance. `AgentStream.abort()` SHALL run `stop_tree_command(pid)`/`stopTreeCommand(pid)` (which stops the runtime process and stops and kills every descendant of it, including those that started their own session), before killing the underlying command handle, and the stream's final event SHALL be `AgentFailed(reason="aborted")`. When the SDK ends a run for `max_steps` or `token_budget`, it SHALL stop the runtime the same way and consume the handle until its end event before the stream ends, so the run lock is free for the next run. When the handle ends with the command's timeout end status (which `wait()` reports as `TimeoutException`/`TimeoutError`), the final event SHALL be `AgentFailed(reason="timeout")`.

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
- **THEN** `stop_tree_command(pid)` is the only command run in the sandbox before the handle is killed, and `stream.result()` raises `AgentException`/`rejects` with `reason="aborted"`

#### Scenario: an SDK limit stops the runtime and its process tree
- **WHEN** the stream ends with `AgentFailed(reason="max_steps")` or `AgentFailed(reason="token_budget")`
- **THEN** `stop_tree_command(pid)` ran and the handle was killed and consumed to its end before the stream ended

#### Scenario: a timeout reported only by the end event is a timeout
- **WHEN** the command handle ends without raising while iterating and its `wait()` raises `TimeoutException`/`TimeoutError`
- **THEN** the stream's final event is `AgentFailed(reason="timeout")`

### Requirement: The OpenCode adapter execs opencode run directly, headless and byte-identical across SDKs

The `opencode` runtime SHALL write an `opencode.json` that only carries the
model credential placeholder, SHALL pass the prompt on stdin and never in
argv, SHALL always pass `--title` and SHALL hold a per-sandbox run lock.
After taking the lock and checking that `opencode` is on the `PATH`, the run
script SHALL `exec` `opencode run --format json` directly: it SHALL NOT
probe, start or attach to an `opencode serve`, and SHALL NOT print any
`rayito.` line other than `rayito.busy` and `rayito.runtime_missing`. A
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

#### Scenario: The run script execs OpenCode

- **WHEN** a unit test builds the run command for a spec in either SDK
- **THEN** the script equals `testdata/agent/opencode-run-commands.json`,
  its last line is `exec 'opencode' 'run' …`, and it contains neither
  `--attach` nor `curl`

## REMOVED Requirements

### Requirement: sbx.agent is a lazy property that enforces the SDK's own limits
**Reason**: The `attach` option and the runtime abort command existed only for the resident `opencode serve` of option D, removed in 0.9.0.
**Migration**: Drop `attach=` from `run()`/`stream()`; `abort()` keeps stopping the process tree.

### Requirement: The OpenCode adapter is headless, credential-free and byte-identical across SDKs
**Reason**: Attaching to a resident `opencode serve` and re-reading the turn from it existed only for option D, removed in 0.9.0.
**Migration**: None for callers: every run now starts `opencode run` itself, as runs without a server already did.
