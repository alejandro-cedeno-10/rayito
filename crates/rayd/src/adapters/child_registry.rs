//! Every child `rayd` spawns, and the gate that keeps the orphan reaper
//! from ever mistaking one of them for an orphan (`rayd-orphan-reaper`,
//! Q80).
//!
//! The kernel keeps one child list per process, so this registry is one
//! per process too ([`ChildRegistry::process`]): every launcher of a
//! child — commands and PTY shells, the kernel sidecar, `mount-s3`, the
//! `stat` mount probe, `ip`, a template's `ready_cmd` — spawns through
//! [`ChildRegistry::spawn`], and the workspace's `clippy.toml` forbids
//! calling `Command::spawn`/`status`/`output` anywhere else, so a new
//! launcher cannot silently bypass it.
//!
//! `spawn` holds the gate shared from before `fork` until the child is
//! recorded (pid plus kernel start time, `rayd_core::orphans`);
//! [`ChildRegistry::sweep_orphans`] holds it exclusively for its whole
//! scan-and-reap pass. Spawns never wait on each other, and a sweep never
//! sees a child that exists but is not recorded yet. Nothing unregisters:
//! a recorded child is forgotten once it has left the process table,
//! which is exactly when its owner's `wait` has consumed its status.

use std::io;
use std::sync::{Arc, LazyLock, Mutex, MutexGuard, PoisonError, RwLock};

use rayd_core::orphans::{OwnedChildren, ProcessTable, sweep};

use super::procfs_process_table::ProcfsProcessTable;

/// The one registry of this process, over the real `/proc`.
static PROCESS: LazyLock<Arc<ChildRegistry>> =
    LazyLock::new(|| Arc::new(ChildRegistry::new(Arc::new(ProcfsProcessTable))));

/// Something that starts one child process and can say its pid.
pub trait SpawnChild {
    type Child;

    /// Starts the child (`fork` + `exec`).
    fn spawn_child(&mut self) -> io::Result<Self::Child>;

    /// The child's pid; `None` when it is already reaped.
    fn child_pid(child: &Self::Child) -> Option<u32>;
}

#[allow(clippy::disallowed_methods)] // the one sanctioned spawn site
impl SpawnChild for tokio::process::Command {
    type Child = tokio::process::Child;

    fn spawn_child(&mut self) -> io::Result<Self::Child> {
        self.spawn()
    }

    fn child_pid(child: &Self::Child) -> Option<u32> {
        child.id()
    }
}

#[allow(clippy::disallowed_methods)] // the one sanctioned spawn site
impl SpawnChild for std::process::Command {
    type Child = std::process::Child;

    fn spawn_child(&mut self) -> io::Result<Self::Child> {
        self.spawn()
    }

    fn child_pid(child: &Self::Child) -> Option<u32> {
        Some(child.id())
    }
}

pub struct ChildRegistry {
    table: Arc<dyn ProcessTable>,
    gate: RwLock<()>,
    owned: Mutex<OwnedChildren>,
}

impl ChildRegistry {
    #[must_use]
    pub fn new(table: Arc<dyn ProcessTable>) -> Self {
        Self {
            table,
            gate: RwLock::new(()),
            owned: Mutex::new(OwnedChildren::default()),
        }
    }

    /// This process's registry: the one every launcher spawns through and
    /// the orphan reaper sweeps.
    #[must_use]
    pub fn process() -> Arc<Self> {
        Arc::clone(&PROCESS)
    }

    /// Spawns `command` and records the child before any sweep can run.
    /// A child already gone by the time it is read back (its owner's
    /// `wait` reaped it) needs no record.
    pub fn spawn<C: SpawnChild>(&self, command: &mut C) -> io::Result<C::Child> {
        let _shared = self.gate.read().unwrap_or_else(PoisonError::into_inner);
        let child = command.spawn_child()?;
        let entry = C::child_pid(&child)
            .and_then(|pid| i32::try_from(pid).ok())
            .and_then(|pid| self.table.entry(pid));
        if let Some(entry) = entry {
            self.owned().record(&entry);
        }
        Ok(child)
    }

    /// One reaping pass for `own_pid` (see `rayd_core::orphans::sweep`),
    /// with every spawn held off until it is done. Returns the pids reaped.
    pub fn sweep_orphans(&self, own_pid: i32) -> Vec<i32> {
        let _exclusive = self.gate.write().unwrap_or_else(PoisonError::into_inner);
        sweep(self.table.as_ref(), &mut self.owned(), own_pid)
    }

    /// How many recorded children have not left the process table yet.
    #[must_use]
    pub fn owned_count(&self) -> usize {
        self.owned().len()
    }

    fn owned(&self) -> MutexGuard<'_, OwnedChildren> {
        self.owned.lock().unwrap_or_else(PoisonError::into_inner)
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Barrier;
    use std::thread;
    use std::time::Duration;

    use rayd_core::orphans::ProcEntry;

    use super::*;

    const OWN_PID: i32 = 1;
    /// No process has this parent pid.
    const NOBODYS_PARENT: i32 = -1;
    const START_TICKS: u64 = 500;
    /// How long a spawn stays between `fork` and its record once the test
    /// knows it is there: long enough for a sweep to reach the gate.
    const SPAWN_STALL: Duration = Duration::from_millis(100);

    /// A process table whose children are whatever the fake spawner put
    /// in it; `reap` removes the entry and remembers the pid.
    #[derive(Default)]
    struct FakeTable {
        entries: Mutex<Vec<ProcEntry>>,
        reaped: Mutex<Vec<i32>>,
    }

    impl FakeTable {
        fn add(&self, entry: ProcEntry) {
            self.entries.lock().unwrap().push(entry);
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
                .retain(|entry| entry.pid != pid);
        }
    }

    fn exited_child(pid: i32) -> ProcEntry {
        ProcEntry {
            pid,
            ppid: OWN_PID,
            zombie: true,
            start_ticks: START_TICKS,
        }
    }

    /// "Spawns" a child that has already exited by the time `spawn_child`
    /// returns (the worst case: a zombie before it is recorded); with
    /// `forked`, it then meets the test there and stalls before returning.
    struct FakeCommand {
        table: Arc<FakeTable>,
        pid: i32,
        forked: Option<Arc<Barrier>>,
        fails: bool,
    }

    impl SpawnChild for FakeCommand {
        type Child = i32;

        fn spawn_child(&mut self) -> io::Result<i32> {
            if self.fails {
                return Err(io::Error::from(io::ErrorKind::NotFound));
            }
            self.table.add(exited_child(self.pid));
            if let Some(forked) = &self.forked {
                forked.wait();
                thread::sleep(SPAWN_STALL);
            }
            Ok(self.pid)
        }

        fn child_pid(child: &i32) -> Option<u32> {
            u32::try_from(*child).ok()
        }
    }

    fn registry_over(table: &Arc<FakeTable>) -> ChildRegistry {
        ChildRegistry::new(Arc::clone(table) as Arc<dyn ProcessTable>)
    }

    fn command(table: &Arc<FakeTable>, pid: i32) -> FakeCommand {
        FakeCommand {
            table: Arc::clone(table),
            pid,
            forked: None,
            fails: false,
        }
    }

    #[test]
    fn a_spawned_child_is_owned_and_never_reaped_by_a_sweep() {
        let table = Arc::new(FakeTable::default());
        let registry = registry_over(&table);
        assert_eq!(registry.spawn(&mut command(&table, 50)).unwrap(), 50);
        assert!(registry.sweep_orphans(OWN_PID).is_empty());
        assert_eq!(registry.owned_count(), 1);
        assert!(table.reaped().is_empty());
    }

    #[test]
    fn an_orphan_nobody_spawned_through_the_registry_is_reaped() {
        let table = Arc::new(FakeTable::default());
        table.add(exited_child(77));
        let registry = registry_over(&table);
        assert_eq!(registry.sweep_orphans(OWN_PID), vec![77]);
        assert_eq!(table.reaped(), vec![77]);
    }

    #[test]
    fn a_child_its_owner_reaped_is_forgotten_on_the_next_sweep() {
        let table = Arc::new(FakeTable::default());
        let registry = registry_over(&table);
        registry.spawn(&mut command(&table, 50)).unwrap();
        table.entries.lock().unwrap().clear(); // tokio's `wait` reaped it
        assert!(registry.sweep_orphans(OWN_PID).is_empty());
        assert_eq!(registry.owned_count(), 0);
    }

    #[test]
    fn a_failed_spawn_records_nothing() {
        let table = Arc::new(FakeTable::default());
        let registry = registry_over(&table);
        let mut failing = FakeCommand {
            fails: true,
            ..command(&table, 50)
        };
        assert!(registry.spawn(&mut failing).is_err());
        assert_eq!(registry.owned_count(), 0);
    }

    #[test]
    fn a_sweep_waits_for_a_spawn_caught_between_fork_and_its_record() {
        let table = Arc::new(FakeTable::default());
        let registry = Arc::new(registry_over(&table));
        let forked = Arc::new(Barrier::new(2));
        let spawner = {
            let registry = Arc::clone(&registry);
            let mut slow = FakeCommand {
                forked: Some(Arc::clone(&forked)),
                ..command(&table, 50)
            };
            thread::spawn(move || registry.spawn(&mut slow).unwrap())
        };
        // The spawner is now inside `spawn`, its child already a zombie in
        // the table but not yet recorded; the sweep must block until the
        // record is in, and then leave the child alone.
        forked.wait();
        let reaped = registry.sweep_orphans(OWN_PID);
        assert_eq!(spawner.join().unwrap(), 50);
        assert!(reaped.is_empty(), "stole a child mid-spawn: {reaped:?}");
        assert!(table.reaped().is_empty());
        assert_eq!(registry.owned_count(), 1);
    }

    #[test]
    fn the_process_registry_is_one_shared_instance() {
        assert!(Arc::ptr_eq(
            &ChildRegistry::process(),
            &ChildRegistry::process()
        ));
    }

    #[cfg(target_os = "linux")]
    #[tokio::test]
    async fn a_real_child_is_recorded_until_its_wait_reaps_it() {
        let registry = ChildRegistry::new(Arc::new(ProcfsProcessTable));
        let mut child = registry
            .spawn(&mut tokio::process::Command::new("true"))
            .unwrap();
        assert_eq!(registry.owned_count(), 1);
        assert!(child.wait().await.unwrap().success());
        // Never the test binary's own pid: other tests' children are its
        // zombies too, and this sweep must only prune.
        assert!(registry.sweep_orphans(NOBODYS_PARENT).is_empty());
        assert_eq!(registry.owned_count(), 0);
    }
}
