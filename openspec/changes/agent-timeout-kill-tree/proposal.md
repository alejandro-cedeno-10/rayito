## Why

`sbx.agent` enforces `AgentLimits.timeout_seconds`/`timeoutMs` through the command's server timeout, and `rayd` delivers it with `killpg` to the run's process group. OpenCode and deepagents start every shell-tool command in its own session (`setsid`), and a command can also daemonise (a dev server, `nohup ... &` through a subshell that exits). Those processes are outside the group, so a timed-out run left them alive: they kept running, kept the run lock's descriptor and made the next run `busy`. The local agent e2e (`local-agent-e2e`) records this as a strict expected failure in both SDKs. `abort()` and the SDK limits work around it from the client with a `pgrep -P` walk run as a second command, which still misses a daemon re-parented away from the runtime.

## What Changes

- **proto**: `StartRequest.kill_tree` (field 6, default `false`). A `rayd` that predates it ignores it.
- **rayd**: a process started with `kill_tree` is made a child subreaper (`prctl(PR_SET_CHILD_SUBREAPER)` in its `pre_exec`, kept across `execve`), so every descendant stays reachable by `ppid` from it while it lives. Its timeout sends `SIGTERM` to every descendant (found before the group is signalled) and to its group, ends the grace early once nothing of the tree is left, and otherwise freezes (`SIGSTOP`) and kills (`SIGKILL`) every remaining descendant, also the ones re-parented away after the root died; the `EndEvent` of the timeout goes out only after that. A `SendSignal` delivers the signal to every descendant before the group, and a `SIGKILL` freezes the tree in bounded passes before killing it. Members are always identified by pid and kernel start time and checked again before each signal; nothing outside the tree is signalled and nothing is reaped (the PID 1 orphan reaper keeps that job). Domain logic in `rayd_core::process::tree`, adapters in `procfs_process_table` and `process_spawner`.
- **SDKs (Python sync and async, TypeScript)**: `commands.run(..., kill_tree=False)` / `killTree`. `sbx.agent` starts every run with it, and `abort()` and the SDK limits now only kill the handle: `stop_tree_command`/`stopTreeCommand` and `AGENT_STOP_TREE_TIMEOUT_SECONDS`/`AGENT_STOP_TREE_TIMEOUT_MS` are removed (private helpers, never exported).
- **Tests**: the expected failures become real assertions (`test_timeout_stops_the_agent_and_its_tools`, its TypeScript mirror); new model-free local tests drive `sbx.agent` with a stub runtime that daemonises a `sleep` and check that timeout and `abort()` leave it dead; `rayd` integration tests for timeout, escalation and `SIGKILL` of a daemonised grandchild, and domain tests for the tree walk.
- **Docs**: the agent guide states what timeout and `abort()` guarantee; the commands guide documents `kill_tree`.

## Impact

- Specs: `process-lifecycle` (ADDED), `ai-agent-runtime` (MODIFIED), `local-testing` (MODIFIED).
- Runtime change in `rayd`: needs the image rebuilt with the new `rayd`. With an older `rayd` the field is ignored and the timeout and `abort()` reach only the runtime's group.
- No AWS call, resource or cost.
