//! Which zombies PID 1 may reap (Q80 finding: "`rayd` (PID 1) doesn't reap
//! orphan zombies", `docs/research/2026-10-e2b-out-of-scope.md`), and the
//! ledger that keeps it from ever stealing an exit status `rayd` itself is
//! waiting on (`rayd-orphan-reaper`).
//!
//! Inside the `MicroVM` `rayd` runs as PID 1: a double-forked or daemonized
//! process started by the sandbox's own code re-parents to PID 1 once its
//! direct parent exits, exactly like an orphan in any container's init.
//! Nobody else ever calls `waitpid` on it, so without this module it stays
//! a zombie (`Z` in `/proc/[pid]/stat`, `<defunct>` in `ps`) for the life
//! of the sandbox.
//!
//! `rayd` reaps its *own* direct children through tokio's `Child::wait`
//! (or `std`'s, for the one synchronous probe), which always waits on one
//! exact pid. An orphan and one of those children look identical in
//! `/proc` once they exit (both are zombies whose `ppid` is `rayd`'s), so
//! a plain `waitpid(-1, WNOHANG)` would race tokio and could take the exit
//! status of a command, a PTY shell, the kernel sidecar or `mount-s3`
//! before tokio reads it. Instead every child `rayd` spawns is recorded in
//! an [`OwnedChildren`] ledger, stamped with its kernel start time so a
//! recycled pid can never be mistaken for it, and [`sweep`] only reaps
//! zombies re-parented to `rayd` that the ledger does not own.
//!
//! The adapter side (`rayd::adapters::child_registry`) guarantees the
//! ledger is complete whenever [`sweep`] runs: every spawn records its pid
//! under a shared gate the sweep takes exclusively, so no child can exist
//! between `fork` and being recorded while a sweep looks at the table.

use std::collections::HashMap;

/// The pid the kernel gives `init`; orphans re-parent to it unless a
/// closer ancestor is a child subreaper (`prctl(PR_SET_CHILD_SUBREAPER)`).
pub const INIT_PID: i32 = 1;

/// One process as `/proc/[pid]/stat` (`proc(5)`) describes it: its pid,
/// its parent's, whether it is a zombie (state `Z`) and its start time in
/// clock ticks since boot (field 22), which together with the pid
/// identifies one process for its whole life, zombie included.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ProcEntry {
    pub pid: i32,
    pub ppid: i32,
    pub zombie: bool,
    pub start_ticks: u64,
}

/// Port over the kernel's process table: what `rayd` can see about other
/// processes and the one call that reaps a zombie child.
pub trait ProcessTable: Send + Sync {
    /// Every process currently visible, in any order.
    fn snapshot(&self) -> Vec<ProcEntry>;

    /// One process by pid; `None` once it is gone (reaped, or never was).
    fn entry(&self, pid: i32) -> Option<ProcEntry>;

    /// `waitpid(pid, WNOHANG)` on exactly this pid; never blocks and never
    /// touches any other pid. A pid that is no longer a child is a no-op.
    fn reap(&self, pid: i32);
}

/// Whether this process can be the parent orphans re-parent to: `init`
/// itself, or a child subreaper (a test harness standing in for PID 1).
#[must_use]
pub fn adopts_orphans(own_pid: i32, child_subreaper: bool) -> bool {
    own_pid == INIT_PID || child_subreaper
}

/// The children `rayd` spawned itself and has not seen leave the process
/// table yet, each pinned to its start time.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct OwnedChildren {
    start_ticks_by_pid: HashMap<i32, u64>,
}

impl OwnedChildren {
    /// Records a child `rayd` just spawned (its `entry` read right after
    /// `fork`).
    pub fn record(&mut self, child: &ProcEntry) {
        self.start_ticks_by_pid.insert(child.pid, child.start_ticks);
    }

    /// Whether `entry` is one of `rayd`'s own children: same pid *and*
    /// same start time, so a recycled pid is never owned by accident.
    #[must_use]
    pub fn owns(&self, entry: &ProcEntry) -> bool {
        self.start_ticks_by_pid.get(&entry.pid) == Some(&entry.start_ticks)
    }

    /// Drops every recorded child that is no longer in `table` as itself:
    /// its owner already reaped it (or its pid now names someone else).
    /// Nothing ever needs to unregister a child by hand, and a child whose
    /// handle was dropped without waiting (tokio's own orphan queue reaps
    /// it later) stays owned until it is really gone.
    pub fn forget_departed(&mut self, table: &[ProcEntry]) {
        let alive: HashMap<i32, u64> = table
            .iter()
            .map(|entry| (entry.pid, entry.start_ticks))
            .collect();
        self.start_ticks_by_pid
            .retain(|pid, start_ticks| alive.get(pid) == Some(start_ticks));
    }

    #[must_use]
    pub fn len(&self) -> usize {
        self.start_ticks_by_pid.len()
    }

    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.start_ticks_by_pid.is_empty()
    }
}

/// The zombies `own_pid` should reap, in the order observed: re-parented
/// to `own_pid` and not one of its own children.
#[must_use]
pub fn orphans_to_reap(table: &[ProcEntry], own_pid: i32, owned: &OwnedChildren) -> Vec<i32> {
    table
        .iter()
        .filter(|entry| entry.zombie && entry.ppid == own_pid && !owned.owns(entry))
        .map(|entry| entry.pid)
        .collect()
}

/// One pass: forget the children that already left, then reap every
/// orphan zombie of `own_pid`. Returns the pids it reaped. The caller must
/// hold the spawn gate exclusively for the whole call (see the module
/// doc), or a child spawned mid-sweep could be taken for an orphan.
pub fn sweep(table: &dyn ProcessTable, owned: &mut OwnedChildren, own_pid: i32) -> Vec<i32> {
    let snapshot = table.snapshot();
    owned.forget_departed(&snapshot);
    let orphans = orphans_to_reap(&snapshot, own_pid, owned);
    for pid in &orphans {
        table.reap(*pid);
    }
    orphans
}

#[cfg(test)]
mod tests {
    use std::sync::Mutex;

    use super::*;

    const RAYD: i32 = INIT_PID;
    const SHELL: i32 = 42;

    fn running(pid: i32, parent: i32, start_ticks: u64) -> ProcEntry {
        ProcEntry {
            pid,
            ppid: parent,
            zombie: false,
            start_ticks,
        }
    }

    fn zombie(pid: i32, parent: i32, start_ticks: u64) -> ProcEntry {
        ProcEntry {
            zombie: true,
            ..running(pid, parent, start_ticks)
        }
    }

    /// An in-memory process table: `reap` removes a zombie child of
    /// `RAYD`, exactly like `waitpid`, and remembers the call.
    #[derive(Default)]
    struct FakeTable {
        entries: Mutex<Vec<ProcEntry>>,
        reaped: Mutex<Vec<i32>>,
    }

    impl FakeTable {
        fn with(entries: &[ProcEntry]) -> Self {
            Self {
                entries: Mutex::new(entries.to_vec()),
                reaped: Mutex::default(),
            }
        }

        fn reaped(&self) -> Vec<i32> {
            self.reaped.lock().unwrap().clone()
        }
    }

    impl ProcessTable for FakeTable {
        fn snapshot(&self) -> Vec<ProcEntry> {
            self.entries.lock().unwrap().clone()
        }

        fn entry(&self, pid: i32) -> Option<ProcEntry> {
            self.snapshot().into_iter().find(|entry| entry.pid == pid)
        }

        fn reap(&self, pid: i32) {
            self.reaped.lock().unwrap().push(pid);
            self.entries
                .lock()
                .unwrap()
                .retain(|entry| !(entry.pid == pid && entry.zombie && entry.ppid == RAYD));
        }
    }

    fn owning(children: &[ProcEntry]) -> OwnedChildren {
        let mut owned = OwnedChildren::default();
        for child in children {
            owned.record(child);
        }
        owned
    }

    #[test]
    fn a_reparented_zombie_rayd_did_not_spawn_is_reaped() {
        let table = FakeTable::with(&[zombie(50, RAYD, 900)]);
        let reaped = sweep(&table, &mut OwnedChildren::default(), RAYD);
        assert_eq!(reaped, vec![50]);
        assert!(table.snapshot().is_empty());
    }

    #[test]
    fn a_zombie_child_rayd_spawned_itself_is_left_to_its_own_wait() {
        // tokio's `Child::wait` has not consumed this exit status yet;
        // taking it here would make that `wait()` fail with `ECHILD`.
        let child = zombie(50, RAYD, 900);
        let table = FakeTable::with(&[child]);
        let mut owned = owning(&[child]);
        assert!(sweep(&table, &mut owned, RAYD).is_empty());
        assert!(table.reaped().is_empty());
        assert!(owned.owns(&child), "still waiting for its owner");
    }

    #[test]
    fn a_recycled_pid_with_a_new_start_time_is_not_owned() {
        // rayd's child 50 was reaped by tokio long ago; pid 50 now names
        // an orphan that started later and died re-parented to rayd.
        let table = FakeTable::with(&[zombie(50, RAYD, 7_000)]);
        let mut owned = owning(&[running(50, RAYD, 900)]);
        assert_eq!(sweep(&table, &mut owned, RAYD), vec![50]);
        assert!(owned.is_empty());
    }

    #[test]
    fn a_child_that_left_the_table_is_forgotten_without_unregistering() {
        let table = FakeTable::with(&[running(60, RAYD, 1_000)]);
        let mut owned = owning(&[running(50, RAYD, 900), running(60, RAYD, 1_000)]);
        assert!(sweep(&table, &mut owned, RAYD).is_empty());
        assert_eq!(owned.len(), 1);
        assert!(owned.owns(&running(60, RAYD, 1_000)));
    }

    #[test]
    fn a_zombie_whose_parent_is_not_rayd_is_left_to_its_parent() {
        let table = FakeTable::with(&[running(SHELL, RAYD, 10), zombie(50, SHELL, 900)]);
        assert!(sweep(&table, &mut OwnedChildren::default(), RAYD).is_empty());
    }

    #[test]
    fn a_running_orphan_is_not_touched_until_it_dies() {
        let table = FakeTable::with(&[running(50, RAYD, 900)]);
        assert!(sweep(&table, &mut OwnedChildren::default(), RAYD).is_empty());
        assert!(table.reaped().is_empty());
    }

    #[test]
    fn only_unowned_orphans_are_reaped_from_a_mixed_table() {
        let own_command = zombie(13, RAYD, 300);
        let table = FakeTable::with(&[
            zombie(10, RAYD, 100),
            zombie(11, SHELL, 110),
            zombie(12, RAYD, 120),
            own_command,
            running(14, RAYD, 140),
        ]);
        let mut owned = owning(&[own_command]);
        assert_eq!(sweep(&table, &mut owned, RAYD), vec![10, 12]);
        assert_eq!(table.reaped(), vec![10, 12]);
    }

    #[test]
    fn an_empty_table_reaps_nothing_and_forgets_everything() {
        let mut owned = owning(&[running(50, RAYD, 900)]);
        assert!(sweep(&FakeTable::default(), &mut owned, RAYD).is_empty());
        assert!(owned.is_empty());
    }

    #[test]
    fn a_subreaper_reaps_the_orphans_re_parented_to_its_own_pid() {
        const SUBREAPER: i32 = 4_321;
        let table = FakeTable::with(&[zombie(50, SUBREAPER, 900), zombie(51, RAYD, 901)]);
        assert_eq!(
            orphans_to_reap(&table.snapshot(), SUBREAPER, &OwnedChildren::default()),
            vec![50]
        );
    }

    #[test]
    fn only_init_or_a_child_subreaper_adopts_orphans() {
        assert!(adopts_orphans(INIT_PID, false));
        assert!(adopts_orphans(4_321, true));
        assert!(!adopts_orphans(4_321, false));
    }
}
