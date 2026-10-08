## 1. proto and rayd

- [x] 1.1 `StartRequest.kill_tree` (field 6) and `make proto`
- [x] 1.2 `rayd_core::process::tree`: `KillScope`, `ProcessTree` (signal, freeze-and-kill, `is_gone`), `descendants`, `MemberSignaller`, named constants; domain tests
- [x] 1.3 `pre_exec` makes a `KillScope::Tree` child a child subreaper; `ProcfsProcessTable` implements `MemberSignaller`
- [x] 1.4 `ProcessManager`: tree target per process, `SendSignal` to the tree before the group, tree-aware timeout task, the `EndEvent` of a timeout after the tree settles
- [x] 1.5 Integration tests in `m2_process.rs`: timeout and escalation over a daemonised grandchild, `SIGKILL`, and the default group scope

## 2. SDKs

- [x] 2.1 Python sync and async `commands.run(kill_tree=)`, TypeScript `killTree`; unit tests
- [x] 2.2 `sbx.agent` runs with `kill_tree`; `stop_tree_command`/`stopTreeCommand` removed; unit tests
- [x] 2.3 Local e2e: the expected failures become assertions; model-free stub-runtime tests for timeout and `abort()` (Python and TypeScript)

## 3. Docs

- [x] 3.1 Agent guide: what timeout and `abort()` guarantee
- [x] 3.2 Commands guide: `kill_tree`/`killTree`
- [x] 3.3 CHANGELOGs (`[Unreleased]`, Fixed) of `rayd` and both SDKs
