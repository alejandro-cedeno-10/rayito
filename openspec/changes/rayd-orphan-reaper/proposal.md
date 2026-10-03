## Why

Q80 (`AWS_API_NOTES.md`, `docs/research/2026-10-e2b-out-of-scope.md`)
measured that a daemonized process (there, `mount-s3` in daemon mode) left
a `<defunct>` entry under `rayd` for the life of the sandbox: `rayd` runs
as PID 1 inside the `MicroVM`, every orphan re-parents to it, and nothing
ever reaped them. `v06-foundations` (design E2) shipped the pure domain
(`rayd_core::orphans`) and the adapters (`ChildRegistry`, `OrphanReaper`)
but deliberately left them dormant: only `mount-s3` registered its pid,
registration happened *after* `fork` (a child that exited at once could be
swept as an orphan first), and a reaper calling `waitpid` on a pid tokio is
still waiting on steals that child's exit status — a command, a PTY shell,
the kernel sidecar or `mount-s3` would report a wrong or missing exit
code.

## What Changes

- **Ledger stamped with start time** (`rayd_core::orphans`): `ProcEntry`
  (pid, ppid, zombie, `/proc/[pid]/stat` start time), the `ProcessTable`
  port, `OwnedChildren` (pid → start time, so a recycled pid is never
  mistaken for an owned child; forgotten automatically once the child
  leaves the process table, so nothing unregisters by hand) and `sweep`
  (forget departed children, reap unowned zombies re-parented to us, one
  `waitpid(pid, WNOHANG)` per pid, never `waitpid(-1, _)`).
  `adopts_orphans` decides where reaping is meaningful: PID 1, or a child
  subreaper.
- **One process-wide `ChildRegistry` with a spawn gate**
  (`adapters::child_registry`): every launcher (commands, PTY shells, the
  kernel sidecar, `mount-s3`, the `stat` mount probe, `ip`, a template's
  `ready_cmd`) spawns through `ChildRegistry::spawn`, which holds a shared
  gate from before `fork` until the child is recorded; `sweep_orphans`
  holds it exclusively. The workspace `clippy.toml` forbids
  `Command::spawn`/`status`/`output` everywhere else, so a new launcher
  cannot bypass it. `FeatureContext.child_registry` and the `mount-s3`
  constructor argument go away (one registry per process, like the
  kernel's child list).
- **`/proc` adapter** (`adapters::procfs_process_table`): `ProcfsProcessTable`
  parses `/proc/[pid]/stat` and reaps one exact pid.
- **Activation** (`main.rs`, `lifecycle::spawn_child_reaper`): `OrphanReaper`
  runs on every `SIGCHLD` and on the shared 5 s sweep, on the blocking
  pool; it is only started when `rayd` adopts orphans (PID 1 or child
  subreaper), and logs `orphan_reaper` at boot.
- **Tests**: core unit tests with a fake process table (owned zombie left
  alone, recycled pid reaped, departed child forgotten, mixed table), the
  registry's gate test (a sweep caught between a spawn's `fork` and its
  record waits and steals nothing), the SIGCHLD wake-up, and an integration
  test (`crates/rayd/tests/m15_orphan_reaper.rs`) where the test process is
  a child subreaper: a double-forked daemon leaves a zombie without the
  reaper, none with it, and 48 concurrent commands keep their exact exit
  codes while the sweep runs every millisecond.

## Capabilities

### Modified Capabilities

- `process-lifecycle`: adds the PID-1 orphan reaping requirement.

## Impact

- `crates/rayd-core/src/orphans.rs`; `crates/rayd/src/adapters/{child_registry,
  orphan_reaper,procfs_process_table,process_spawner,pty_backend,
  sidecar_process,mount_s3,fuse_device,ip_command,shell_ready_probe}.rs`;
  `crates/rayd/src/lifecycle/reaper.rs`; `crates/rayd/src/features/{mod,
  s3_mounts}.rs`; `crates/rayd/src/main.rs`; `clippy.toml`.
- No proto, SDK or image change; no new dependency. Without orphans the
  only cost is one `/proc` scan per `SIGCHLD` and per 5 s.
