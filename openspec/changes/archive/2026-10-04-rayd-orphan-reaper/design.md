## Context

`rayd` is PID 1 in the `MicroVM`. Its own children are reaped by tokio
(`Child::wait`, one exact pid via `waitpid(pid)`, driven by `SIGCHLD` or a
pidfd) or, for the synchronous `stat` mount probe, by `std`'s
`Child::try_wait`. Orphans re-parent to PID 1 and were never reaped (Q80).
Once they exit, an orphan and one of `rayd`'s own children are
indistinguishable in `/proc`: both are zombies whose `ppid` is 1.

## Goals / Non-Goals

- Goal: no `<defunct>` orphan survives under `rayd`; no exit status of a
  child `rayd` spawned is ever taken from its owner.
- Non-goal: changing how any launcher waits on its own child, or reaping
  zombies whose parent is not `rayd` (their parent owns them).

## Decisions

### D1. Reap exact pids from a `/proc` scan, never `waitpid(-1)`

`waitid(P_ALL, WNOWAIT)` can peek, but it only ever reports one child and
cannot skip past an owned zombie to look at the next, so it cannot tell
which zombies are safe without the same ownership data a `/proc` scan
needs. The sweep reads every `/proc/[pid]/stat`, selects zombies whose
`ppid` is `rayd`'s own pid and which the ledger does not own, and calls
`waitpid(pid, WNOHANG)` on each. `ECHILD` (someone else got it first) is
harmless.

### D2. Ownership is (pid, start time), recorded under a spawn gate

The previous registry recorded the pid after `spawn()` returned; a child
that exits immediately can be a zombie before that line runs, and a sweep
in that window would take it. `ChildRegistry::spawn` now holds an
`RwLock` read guard across `fork`/`exec` and the record; `sweep_orphans`
takes the write guard for the whole scan-and-reap. Spawns stay concurrent
with each other; a sweep (a few ms of `/proc` reads) briefly holds them
off. The record is the child's start time (`/proc/[pid]/stat` field 22),
so a recycled pid is never treated as owned; a child already reaped by its
owner when it is read back simply is not recorded.

### D3. Nothing unregisters; departed children are pruned by the sweep

Each sweep first forgets every recorded child that is no longer in the
table *as itself* (pid gone, or its start time changed). This needs no
cooperation from the launchers' wait paths, handles a child whose handle
was dropped without waiting (tokio's orphan queue reaps it; it stays owned
until then, so tokio's own `waitpid(pid)` can never race us onto a
recycled pid), and keeps the ledger bounded by the live children.

### D4. One registry per process, enforced by clippy

The kernel's child list is per process, and a forgotten wiring would
silently reintroduce the race, so the registry is a process-wide instance
(`ChildRegistry::process()`), and `clippy.toml`'s `disallowed-methods`
rejects `std`/`tokio` `Command::spawn`/`status`/`output` outside
`ChildRegistry::spawn` (the only `#[allow]`). The reaper itself still takes
the registry by injection, and the domain is tested over a fake
`ProcessTable`.

### D5. Triggered by `SIGCHLD` plus the shared 5 s sweep

`lifecycle::spawn_child_reaper` wakes on every `SIGCHLD` (tokio supports a
second listener next to its own process driver) and on
`DEFAULT_REAPER_INTERVAL`, which catches anything a coalesced signal hid;
each pass runs on the blocking pool. It is only started when
`adopts_orphans(pid, child_subreaper)` holds: PID 1 in the `MicroVM`, or a
child subreaper — which is how the integration test stands in for PID 1
without a PID namespace.

## Risks / Trade-offs

- A spawn can wait a few ms behind a sweep. Sweeps are bounded by the
  number of processes and only run on `SIGCHLD` or every 5 s.
- Recording reads `/proc/[pid]/stat` right after `fork`; if that pid were
  recycled by an unrelated process within those microseconds (a full pid
  wrap-around), that process's eventual zombie would be left alone rather
  than stolen — the safe direction.
