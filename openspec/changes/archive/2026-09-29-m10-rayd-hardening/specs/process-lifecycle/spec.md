## ADDED Requirements

### Requirement: Signal dispositions are reset before exec
Every child `rayd` spawns (a process, a PTY shell, or the kernel sidecar — the three launchers sharing `PreExecPlan::apply`) SHALL, between `fork` and `exec`, have every signal's disposition reset to `SIG_DFL` and its blocked-signal set cleared, so a signal `rayd` itself (or whatever launched it, `nohup` included) has set to ignored or blocked never reaches user code. `SIGKILL` and `SIGSTOP`, which refuse the reset, SHALL be treated as already correct, never as a failure.

#### Scenario: an ignored SIGHUP in the parent does not reach the child
- **WHEN** the parent process has `SIGHUP` set to `SIG_IGN` (as `nohup` leaves it) and starts a child that sends itself `SIGHUP`
- **THEN** the child dies from the kernel's default action for `SIGHUP`, rather than surviving it and continuing to run
