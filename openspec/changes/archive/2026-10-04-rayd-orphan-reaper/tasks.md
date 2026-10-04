## 1. Domain

- [x] 1.1 `rayd_core::orphans`: `ProcEntry`, `ProcessTable`, `OwnedChildren`
  (record, owns by pid + start time, `forget_departed`), `orphans_to_reap`,
  `sweep`, `adopts_orphans`; unit tests over a fake table.

## 2. Adapters

- [x] 2.1 `adapters::procfs_process_table::ProcfsProcessTable` (`/proc/[pid]/stat`
  parsing with start time, `waitpid(pid, WNOHANG)`), unit-tested.
- [x] 2.2 `adapters::child_registry::ChildRegistry`: process-wide instance,
  spawn gate, `spawn` for `std`/`tokio` commands (`SpawnChild`),
  `sweep_orphans`; tests for the gate, pruning and a real child.
- [x] 2.3 Every launcher spawns through `ChildRegistry::spawn`
  (`process_spawner`, `pty_backend`, `sidecar_process`, `mount_s3`,
  `fuse_device`, `ip_command`, `shell_ready_probe`);
  `FeatureContext.child_registry` removed; `clippy.toml`
  `disallowed-methods` for `Command::spawn`/`status`/`output`.
- [x] 2.4 `adapters::orphan_reaper::OrphanReaper` over the registry, active
  only for PID 1 or a child subreaper.

## 3. Activation

- [x] 3.1 `lifecycle::spawn_child_reaper` (`SIGCHLD` + interval, blocking
  pool), tested with a counting reaper.
- [x] 3.2 `main.rs` starts it when `rayd` adopts orphans and logs
  `orphan_reaper`.

## 4. Verification

- [x] 4.1 `crates/rayd/tests/m15_orphan_reaper.rs`: double-forked daemon
  zombie appears without the reaper and is cleared with it; 48 concurrent
  commands keep their exit codes with a 1 ms sweep; a naive reaper (no
  ownership check) fails it.
- [x] 4.2 Docs: `ARCHITECTURE.md`, `MILESTONES.md`, `crates/rayd/CHANGELOG.md`.
- [x] 4.3 Real AWS: an S3 mount leaves no `<defunct>` and command exit
  codes stay correct (serialized acceptance stage). (Verified in the 0.6.1
  acceptance on 2026-10-03: 0 `<defunct>` after a mount, after a
  double-forked daemon exits and after 40 concurrent commands, none with a
  wrong exit code; PTY, kernel and background statuses also correct.)
