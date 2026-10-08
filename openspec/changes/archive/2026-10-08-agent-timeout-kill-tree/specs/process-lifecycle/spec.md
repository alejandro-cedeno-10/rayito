## ADDED Requirements

### Requirement: A kill_tree process's timeout and signals reach its whole tree
When `StartRequest.kill_tree` is `true`, `rayd` SHALL make the spawned process a child subreaper before `exec`, so that a descendant whose parent exits re-parents to it. Its timeout SHALL send `SIGTERM` to every live descendant before its process group, and SHALL then wait up to the 5 s grace, ending it early once the process was reaped and no descendant it signalled is alive; otherwise it SHALL stop (`SIGSTOP`) every live descendant, also those re-parented away after the process died, in passes until a pass finds none or 16 passes ran, kill (`SIGKILL`) every stopped one, and `SIGKILL` the group if the process is still alive. The `EndEvent` of such a timeout SHALL be sent only after that. `SendSignal` SHALL deliver the signal to every live descendant before the group, and a `SIGKILL` SHALL first stop the process and its descendants and kill the descendants. A descendant SHALL be identified by pid and kernel start time and signalled only if its pid still names that same, non-zombie process; no process outside the tree SHALL be signalled, and nothing SHALL be reaped there (the orphan reaper keeps that job). With `kill_tree` `false` (the default) the timeout and `SendSignal` SHALL reach only the process group. The SDKs SHALL expose it as `commands.run(..., kill_tree=False)` (Python, sync and async) and `killTree` (TypeScript).

#### Scenario: a timeout reaches a daemon outside the group
- **WHEN** a `kill_tree` process with `timeout_ms: 300` starts a `sleep` through `setsid sh -c '... &'`, whose shell exits at once, and then sleeps
- **THEN** the stream ends with `status:"timeout"` within 4 s, and the daemonised `sleep` is no longer alive

#### Scenario: a daemon that ignores SIGTERM is killed after the grace
- **WHEN** the same daemon ignores `SIGTERM`
- **THEN** the `EndEvent` arrives no sooner than 5 s after the deadline and the daemon is dead by then

#### Scenario: SIGKILL freezes and kills the tree
- **WHEN** `SendSignal{pid, 9}` targets a `kill_tree` process with such a daemon
- **THEN** the stream ends with `status:"signaled"` and the daemon is dead

#### Scenario: the default scope is the group
- **WHEN** the same script runs without `kill_tree` and times out
- **THEN** the daemon is still alive

#### Scenario: a recycled pid is never signalled
- **WHEN** a remembered member exits and its pid is reused by an unrelated process before the kill pass
- **THEN** the unrelated process receives no signal
