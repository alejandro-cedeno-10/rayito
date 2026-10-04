## ADDED Requirements

### Requirement: PID 1 reaps orphan zombies without stealing its own children's exit status
When `rayd` is the process orphans re-parent to (PID 1, or a child subreaper), it SHALL reap every zombie re-parented to it that it did not spawn itself, on every `SIGCHLD` and at least every 5 s, by calling `waitpid` on that exact pid and never on `-1`. Every child `rayd` spawns (processes, PTY shells, the kernel sidecar, `mount-s3`, internal probes and helpers) SHALL be recorded by pid and kernel start time before any reaping pass can observe it, and SHALL never be reaped by that pass, so its own `wait` always receives its real exit status. A recorded child SHALL be forgotten once it has left the process table.

#### Scenario: a double-forked daemon leaves no zombie
- **WHEN** a command starts a background daemon through a subshell that exits at once, exits with code 7, and the daemon exits later
- **THEN** the command reports exit code 7 and no zombie whose parent is `rayd` remains

#### Scenario: concurrent commands keep their exit codes while the reaper sweeps
- **WHEN** 48 commands, half of them starting double-forked daemons, run concurrently while the reaper sweeps every millisecond and on every `SIGCHLD`
- **THEN** every command reports exactly the exit code it returned and no zombie whose parent is `rayd` remains

#### Scenario: a recycled pid is not mistaken for an owned child
- **WHEN** a pid `rayd` once spawned is reused by an orphan that dies re-parented to `rayd`
- **THEN** the orphan is reaped, because its start time differs from the recorded one

#### Scenario: outside PID 1 nothing is reaped
- **WHEN** `rayd` is neither PID 1 nor a child subreaper
- **THEN** the orphan reaper is not started
