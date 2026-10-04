## ADDED Requirements

### Requirement: Kernel pids reported by the sidecar are verified before rayd signals them
`rayd` SHALL treat every `kernel_pid` the sidecar reports (`ready`, and the
replies to create, restart and rotate a kernel) as untrusted input, because
any sandbox process can write into the sidecar's protocol pipe. A reported
pid SHALL be recorded only when the process table shows it is not `0` or
`1`, is a direct child of the sidecar currently running, leads its own
process group and belongs to the sidecar's own uid; it SHALL be recorded
as a `KernelProcess` pinned to its start time, and a pid that fails SHALL
be dropped with one `kernel_pid_rejected` line naming the reason. Before
each signal (`SIGKILL` of an orphan on sidecar exit, `SIGTERM`/`SIGKILL`
when the agent ends) `rayd` SHALL re-read the table and SHALL NOT signal
the group when the pid now names another process. The process-group
signaller SHALL refuse group `0`, `1` and `rayd`'s own group whatever asks
for it. `SECURITY.md` T12 SHALL state that sandbox processes can write to
the sidecar's pipe and how kernel pids are verified.

#### Scenario: a forged ready names rayd's own group
- **WHEN** a `ready` line reports `kernel_pid` 0, 1, a root process that
  is not a child of the sidecar, or a pid no process has
- **THEN** no kernel is recorded and neither the sidecar's exit nor the
  agent's exit signals any process group for it

#### Scenario: a kernel pid recycled after admission
- **WHEN** an admitted kernel's pid names a process with a later start
  time by the time the agent ends
- **THEN** that group is not signalled

#### Scenario: a real kernel is still killed
- **WHEN** the sidecar exits while a kernel it started (a child leading its
  own group) is alive
- **THEN** `rayd` signals that kernel's group as before
