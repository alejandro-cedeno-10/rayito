## MODIFIED Requirements

### Requirement: /suspend always answers 200 and never destroys state
`POST /suspend` SHALL always answer HTTP 200 (a non-200 terminates the MicroVM, measured 2026-09-15), inside 80 % of the declared `suspendTimeoutInSeconds` (24 s) with a design target below 5 s, and SHALL be idempotent: the first call of a cycle bumps `suspend_generation`, moves the phase to `Suspending` and runs the checklist; a repeated call answers `unchanged` and runs nothing but the bounded filesystem sync. `rayd` SHALL NOT kill, signal or interrupt any process, PTY, execution, kernel or the sidecar, SHALL NOT wait for executions or processes to finish, and SHALL respond 200 even when a step fails (the failure is logged).

#### Scenario: idempotent suspend
- **WHEN** an integration test posts `/suspend` twice
- **THEN** both answer 200, the first with `outcome: "changed"` and `streams_closed` equal to the number of open streams, the second with `outcome: "unchanged"` and `streams_closed: 0`, and `suspend_generation` is 1

#### Scenario: suspend before run
- **WHEN** `/suspend` arrives before `/run`
- **THEN** it answers 200 with `outcome: "illegal"` and the phase does not change

### Requirement: /suspend closes every client stream with a form the client can recognise
On the first `/suspend` of a cycle `rayd` SHALL stop accepting new streams (`UNAVAILABLE` with details `suspending` for `Start`, `Connect`, `Create`, `WatchDir`, `Read`, `Write`, `Execute`, `Reattach`) and SHALL close every live client stream before answering: `Process.Start`/`Connect` with an in-stream `EndEvent{exited:false, status:"suspending", exit_code:0, error:{code:"suspending"}}` followed by a clean end; `Pty.Create`/`Connect` with `PtyExited{exited:false, status:"suspending", error:{code:"suspending"}}`; `WatchDir`, `Read`, `Execute` and `Reattach` with the gRPC status `UNAVAILABLE` and details `suspending` (an `Execute`/`Reattach` closed this way SHALL NOT emit `ExecutionEnd` and SHALL NOT interrupt the execution); an in-flight `Write` SHALL be aborted (temporary file removed, destination untouched, at most 200 ms of body drained) with `UNAVAILABLE suspending`. `rayd` SHALL wait at most 2 s for the wrapped streams to drop, send `quiesce` to the sidecar with a 2 s timeout, run the bounded filesystem sync, and log `streams_closed`, `streams_pending` and `suspend_ms`.

#### Scenario: eight streams closed with their forms
- **WHEN** an integration test holds a `Start` and a `Connect` on `sleep 30`, a PTY stream, a `WatchDir`, a half-consumed `Read`, an `Execute` of `sleep 3`, a `Reattach` on it and a `Write` mid-body, and posts `/suspend`
- **THEN** within 2 s the process streams end with `EndEvent{status:"suspending"}`, the PTY stream with `PtyExited{status:"suspending"}`, the other five with `UNAVAILABLE` and details `suspending`, the hook body reports `streams_closed: 8`, the fake sidecar received `quiesce` and no `interrupt`, the `Write` temporary is gone, and `sleep 30` and the shell are still listed

#### Scenario: new streams refused while suspending
- **WHEN** `/suspend` has been acknowledged and no `/resume` yet
- **THEN** `Start`, `Create`, `WatchDir` and `Execute` fail with `UNAVAILABLE` and details `suspending`

## ADDED Requirements

### Requirement: /suspend syncs each filesystem inside a bounded deadline
`/suspend` SHALL flush dirty pages with one `syncfs(2)` per filesystem, never with an unbounded `sync(2)`. The filesystems SHALL come from `/proc/self/mountinfo`: writable mounts only, one per device (`major:minor`), none of the pseudo filesystems without a page cache of their own (`proc`, `sysfs`, `tmpfs`, `cgroup2`, `devpts` and the like), at most 64; an unreadable table SHALL fall back to `/`. Each `syncfs` SHALL run in its own throwaway operating-system thread (not the async runtime's blocking pool), and the hook SHALL wait for them at most the sync deadline of the `SuspendBudget`, a pure domain rule in `rayd_core::suspend_sync`: 5 s by default, clamped so that the stream-close grace, the quiesce timeout and the sync deadline together never exceed half the hook budget (zero when the budget is too small for the other steps). A `syncfs` still running at the deadline SHALL keep running off the hook's path and SHALL NOT delay the 200; its filesystem SHALL be skipped by later `/suspend` calls until it returns. A hit deadline, failed syncs and skipped filesystems SHALL be logged with counts and the deadline only, never a path or file content. The syscalls SHALL live in a `rayd` adapter behind the `FilesystemSync` port, with a fake for tests.

#### Scenario: a hung mount does not hold the 200
- **WHEN** a unit test builds the hooks router with a fake `FilesystemSync` whose only filesystem never returns from `syncfs`, accepts `/run` and posts `/suspend` twice
- **THEN** the first `/suspend` answers 200 with `outcome: "changed"` after at least the 5 s sync deadline and within half the hook budget, the second answers 200 with `outcome: "unchanged"` without waiting for the deadline, and the hung filesystem never gets a second thread

#### Scenario: healthy filesystems are all synced
- **WHEN** the bounded flush runs over two filesystems that sync and one that fails
- **THEN** it reports two synced, one failed and none pending, and does not log a hit deadline

#### Scenario: the budget stays inside the hook
- **WHEN** `SuspendBudget` is built for the 24 s `/suspend` budget, for a 60 s request, and for an 800 ms budget
- **THEN** the sync deadline is 5 s, 8 s and zero respectively
