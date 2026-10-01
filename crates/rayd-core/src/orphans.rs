//! Which zombies PID 1 should reap (M15 foundations, Q80 finding:
//! "`rayd` (PID 1) doesn't reap orphan zombies",
//! `docs/research/2026-10-e2b-out-of-scope.md`).
//!
//! Inside the `MicroVM` `rayd` runs as PID 1: a double-forked or daemonized
//! process started by the sandbox's own code (a user `bash` session, for
//! example) re-parents to PID 1 once its direct parent exits, exactly like
//! an orphan in any container's init. Nobody else ever calls `waitpid` on
//! it, so it stays a zombie (`Z` in `/proc/[pid]/stat`) for the life of the
//! sandbox: harmless on its own, but each one is an open slot in the
//! kernel's process table and a `commands.list`/`ps` entry nobody asked
//! for.
//!
//! `rayd` already reaps its own direct children through tokio's
//! `Child::wait` (`adapters::process_spawner`, `pty_backend`,
//! `sidecar_process`). This module decides which *other* zombies — the
//! re-parented ones — PID 1 may safely `waitpid` on: exactly those whose
//! `ppid` is PID 1's own and whose pid is not one `rayd` is already
//! waiting on itself (`is_owned`). Calling `waitpid(-1, WNOHANG)` instead
//! would race tokio's own reaper and could steal the exit status of a
//! child `rayd` spawned a moment ago, before tokio got to read it — so the
//! adapter (`adapters::orphan_reaper`) only ever waits on the exact pids
//! this module names.

/// One zombie line read from `/proc/[pid]/stat`: its pid and its parent's.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ZombieEntry {
    pub pid: i32,
    pub ppid: i32,
}

/// The zombies PID 1 should `waitpid(pid, WNOHANG)` on, in the order they
/// were observed: re-parented to `own_pid` (never a zombie's own children,
/// which have a different `ppid`) and not a pid `rayd` already owns
/// (`is_owned`, backed by `adapters::child_registry::ChildRegistry`).
#[must_use]
pub fn orphans_to_reap(
    zombies: &[ZombieEntry],
    own_pid: i32,
    is_owned: impl Fn(i32) -> bool,
) -> Vec<i32> {
    zombies
        .iter()
        .filter(|zombie| zombie.ppid == own_pid && !is_owned(zombie.pid))
        .map(|zombie| zombie.pid)
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn zombie(pid: i32, parent_pid: i32) -> ZombieEntry {
        ZombieEntry {
            pid,
            ppid: parent_pid,
        }
    }

    #[test]
    fn a_reparented_zombie_not_owned_by_rayd_is_reaped() {
        let zombies = [zombie(50, 1)];
        let reaped = orphans_to_reap(&zombies, 1, |_| false);
        assert_eq!(reaped, vec![50]);
    }

    #[test]
    fn a_zombie_still_owned_by_rayds_own_child_registry_is_left_alone() {
        // rayd spawned this child directly; tokio's own `Child::wait` has
        // not yet consumed its exit status. Stealing it here would make
        // that `wait()` hang forever.
        let zombies = [zombie(50, 1)];
        let reaped = orphans_to_reap(&zombies, 1, |pid| pid == 50);
        assert!(reaped.is_empty());
    }

    #[test]
    fn a_zombie_whose_parent_is_not_rayd_is_left_alone() {
        // Not PID 1's to reap: it belongs to whatever process is its real
        // parent (e.g. a shell that is itself still running).
        let zombies = [zombie(50, 42)];
        let reaped = orphans_to_reap(&zombies, 1, |_| false);
        assert!(reaped.is_empty());
    }

    #[test]
    fn only_rayds_own_zombies_are_reaped_from_a_mixed_table() {
        let zombies = [zombie(10, 1), zombie(11, 42), zombie(12, 1), zombie(13, 1)];
        let reaped = orphans_to_reap(&zombies, 1, |pid| pid == 13);
        assert_eq!(reaped, vec![10, 12]);
    }

    #[test]
    fn an_empty_table_reaps_nothing() {
        assert!(orphans_to_reap(&[], 1, |_| false).is_empty());
    }

    #[test]
    fn when_rayd_is_not_pid_1_nothing_can_be_its_reparented_orphan() {
        // `own_pid` is always the caller's own pid; a non-init rayd (tests,
        // or a future non-PID-1 deployment) correctly reaps nothing because
        // no zombie reparents to a non-init process.
        let zombies = [zombie(50, 1)];
        let reaped = orphans_to_reap(&zombies, 4321, |_| false);
        assert!(reaped.is_empty());
    }
}
