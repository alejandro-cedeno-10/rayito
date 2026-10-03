//! PID 1's orphan reaping (`rayd-orphan-reaper`, Q80): a `Reaper` that runs
//! one `ChildRegistry::sweep_orphans` pass for `rayd`'s own pid, so a
//! double-forked daemon that exits inside the sandbox never stays
//! `<defunct>` under `rayd`, while every child `rayd` spawned itself keeps
//! its exit status for its own `wait` (`adapters::child_registry`).
//!
//! Active only where orphans actually re-parent to `rayd`: as PID 1 (the
//! `MicroVM`) or as a child subreaper (an integration test standing in for
//! it). Anywhere else the process that adopts orphans is someone else, so
//! `reap_expired` does nothing. `lifecycle::spawn_child_reaper` runs it on
//! every `SIGCHLD` and on a periodic sweep.

use std::sync::Arc;

use rayd_core::orphans::adopts_orphans;

use crate::lifecycle::Reaper;

use super::child_registry::ChildRegistry;

pub struct OrphanReaper {
    registry: Arc<ChildRegistry>,
    own_pid: i32,
    active: bool,
}

impl OrphanReaper {
    /// Over this process's own pid and child-subreaper attribute, read
    /// once: neither changes for the life of `rayd`.
    #[must_use]
    pub fn new(registry: Arc<ChildRegistry>) -> Self {
        let own_pid = own_pid();
        Self::for_pid(
            registry,
            own_pid,
            adopts_orphans(own_pid, is_child_subreaper()),
        )
    }

    fn for_pid(registry: Arc<ChildRegistry>, own_pid: i32, active: bool) -> Self {
        Self {
            registry,
            own_pid,
            active,
        }
    }

    /// Whether orphans re-parent to this process, i.e. whether reaping
    /// does anything here.
    #[must_use]
    pub fn is_active(&self) -> bool {
        self.active
    }

    /// One pass; the pids it reaped (always empty when inactive).
    #[must_use]
    pub fn sweep(&self) -> Vec<i32> {
        if !self.active {
            return Vec::new();
        }
        self.registry.sweep_orphans(self.own_pid)
    }
}

impl Reaper for OrphanReaper {
    fn reap_expired(&self) {
        let reaped = self.sweep();
        if !reaped.is_empty() {
            tracing::debug!(orphans_reaped = reaped.len(), "orphan zombies reaped");
        }
    }
}

fn own_pid() -> i32 {
    // A real pid always fits i32 (Linux caps pid_max well under 2^31); a
    // value that somehow didn't would never equal any `ppid`, which is the
    // safe direction to fail in.
    i32::try_from(std::process::id()).unwrap_or(-1)
}

#[cfg(target_os = "linux")]
fn is_child_subreaper() -> bool {
    nix::sys::prctl::get_child_subreaper().unwrap_or(false)
}

#[cfg(not(target_os = "linux"))]
fn is_child_subreaper() -> bool {
    false
}

#[cfg(test)]
mod tests {
    use std::sync::Mutex;

    use rayd_core::orphans::{ProcEntry, ProcessTable};

    use super::*;

    const OWN_PID: i32 = 4_321;

    struct OneOrphan {
        reaped: Mutex<Vec<i32>>,
    }

    impl ProcessTable for OneOrphan {
        fn snapshot(&self) -> Vec<ProcEntry> {
            vec![ProcEntry {
                pid: 50,
                ppid: OWN_PID,
                zombie: true,
                start_ticks: 1,
            }]
        }

        fn entry(&self, _pid: i32) -> Option<ProcEntry> {
            None
        }

        fn reap(&self, pid: i32) {
            self.reaped.lock().unwrap().push(pid);
        }
    }

    fn reaper(active: bool) -> (OrphanReaper, Arc<OneOrphan>) {
        let table = Arc::new(OneOrphan {
            reaped: Mutex::default(),
        });
        let registry = Arc::new(ChildRegistry::new(
            Arc::clone(&table) as Arc<dyn ProcessTable>
        ));
        (OrphanReaper::for_pid(registry, OWN_PID, active), table)
    }

    #[test]
    fn an_active_reaper_reaps_the_orphans_of_its_own_pid() {
        let (reaper, table) = reaper(true);
        reaper.reap_expired();
        assert_eq!(*table.reaped.lock().unwrap(), vec![50]);
    }

    #[test]
    fn an_inactive_reaper_never_touches_the_table() {
        let (reaper, table) = reaper(false);
        assert!(!reaper.is_active());
        assert!(reaper.sweep().is_empty());
        assert!(table.reaped.lock().unwrap().is_empty());
    }

    #[test]
    fn a_test_runner_that_is_neither_init_nor_a_subreaper_is_inactive() {
        // Every test runner's own pid is never 1, and this unit-test binary
        // never sets PR_SET_CHILD_SUBREAPER.
        assert!(!OrphanReaper::new(ChildRegistry::process()).is_active());
    }
}
