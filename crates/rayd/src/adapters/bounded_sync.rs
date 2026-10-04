//! Step 4 of the `/suspend` checklist with a bound: one `syncfs(2)` per
//! filesystem, each in a throwaway thread, and a wait of at most the
//! `SuspendBudget`'s deadline. The plan and the budget are the domain's
//! (`rayd_core::suspend_sync`); this adapter only runs the syscalls.
//!
//! Plain `std::thread`s rather than `spawn_blocking`: a `syncfs` stuck on a
//! hung mount never returns, and the runtime waits for its blocking pool on
//! shutdown, so a stuck blocking task would also hang `/terminate`. A
//! detached thread just stays in `D` state until the VM goes away; the
//! next `/suspend` skips its filesystem instead of stacking another thread
//! behind it.

use std::collections::HashSet;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

use rayd_core::suspend_sync::{
    DeviceId, FilesystemSync, FlushReport, MountEntry, SuspendBudget, plan_sync,
};

const SYNC_THREAD_NAME: &str = "rayd-syncfs";

/// `FilesystemSync` over `/proc/self/mountinfo` and `syncfs(2)` on Linux.
/// Elsewhere the table is empty and `syncfs` answers `Unsupported`.
#[derive(Debug, Default, Clone, Copy)]
pub struct PlatformFilesystemSync;

#[cfg(target_os = "linux")]
impl FilesystemSync for PlatformFilesystemSync {
    fn mount_table(&self) -> Vec<MountEntry> {
        match std::fs::read_to_string("/proc/self/mountinfo") {
            Ok(text) => rayd_core::suspend_sync::parse_mountinfo(&text),
            Err(error) => {
                tracing::warn!(reason = %error.kind(), "mount table unreadable");
                Vec::new()
            }
        }
    }

    fn syncfs(&self, mount_point: &str) -> std::io::Result<()> {
        let directory = std::fs::File::open(mount_point)?;
        nix::unistd::syncfs(&directory).map_err(std::io::Error::from)
    }
}

#[cfg(not(target_os = "linux"))]
impl FilesystemSync for PlatformFilesystemSync {
    fn mount_table(&self) -> Vec<MountEntry> {
        Vec::new()
    }

    fn syncfs(&self, _mount_point: &str) -> std::io::Result<()> {
        Err(std::io::ErrorKind::Unsupported.into())
    }
}

/// Runs the domain's sync plan against a `FilesystemSync` and stops
/// waiting at the budget's deadline. Remembers the filesystems whose
/// `syncfs` has not returned across calls.
pub struct BoundedFlush {
    sync: Arc<dyn FilesystemSync>,
    budget: SuspendBudget,
    in_flight: Arc<Mutex<HashSet<DeviceId>>>,
}

impl BoundedFlush {
    #[must_use]
    pub fn new(sync: Arc<dyn FilesystemSync>, budget: SuspendBudget) -> Self {
        Self {
            sync,
            budget,
            in_flight: Arc::default(),
        }
    }

    #[must_use]
    pub fn budget(&self) -> SuspendBudget {
        self.budget
    }

    /// Starts one thread per planned filesystem and waits for them until
    /// the deadline. Never fails and never waits longer than the deadline
    /// (plus reading the mount table).
    pub async fn flush(&self) -> FlushReport {
        let mounts = self.sync.mount_table();
        let plan = plan_sync(&mounts, &lock(&self.in_flight));
        let (done_tx, mut done_rx) = tokio::sync::mpsc::unbounded_channel::<bool>();
        let mut report = FlushReport {
            skipped_in_flight: plan.skipped_in_flight,
            ..FlushReport::default()
        };
        let mut launched = 0_usize;
        for target in plan.targets {
            let device = target.device;
            lock(&self.in_flight).insert(device);
            let in_flight = self.in_flight.clone();
            let done_tx = done_tx.clone();
            let spawned = spawn_syncfs_thread(
                self.sync.clone(),
                target.mount_point.clone(),
                move |synced| {
                    lock(&in_flight).remove(&target.device);
                    // The receiver is gone once the hook stopped waiting.
                    let _ = done_tx.send(synced);
                },
            );
            if spawned.is_ok() {
                launched += 1;
            } else {
                lock(&self.in_flight).remove(&device);
                report.failed += 1;
            }
        }
        drop(done_tx);
        let deadline = tokio::time::Instant::now() + self.budget.sync_deadline();
        let mut finished = 0_usize;
        while finished < launched {
            match tokio::time::timeout_at(deadline, done_rx.recv()).await {
                Ok(Some(true)) => report.synced += 1,
                Ok(Some(false)) => report.failed += 1,
                Ok(None) | Err(_) => break,
            }
            finished += 1;
        }
        report.pending = launched - finished;
        report
    }
}

/// Runs `syncfs(mount_point)` on its own throwaway, detached thread (see
/// the module doc for why never on the blocking pool) and hands whether it
/// succeeded to `done` once it returns, which may be never. Shared by
/// `BoundedFlush` and the EFS volumes' own per-volume flush
/// (`adapters::efs_mount`).
pub fn spawn_syncfs_thread(
    sync: Arc<dyn FilesystemSync>,
    mount_point: String,
    done: impl FnOnce(bool) + Send + 'static,
) -> std::io::Result<()> {
    std::thread::Builder::new()
        .name(SYNC_THREAD_NAME.to_owned())
        .spawn(move || done(sync.syncfs(&mount_point).is_ok()))
        .map(drop)
}

fn lock(in_flight: &Mutex<HashSet<DeviceId>>) -> MutexGuard<'_, HashSet<DeviceId>> {
    in_flight.lock().unwrap_or_else(PoisonError::into_inner)
}

/// Test doubles of the port: filesystems that sync at once, fail, or never
/// return.
#[cfg(test)]
pub(crate) mod fake {
    use std::sync::atomic::{AtomicUsize, Ordering};

    use rayd_core::suspend_sync::{DeviceId, FilesystemSync, MountEntry};

    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    pub(crate) enum Behaviour {
        Syncs,
        Fails,
        BlocksForever,
    }

    pub(crate) struct FakeFilesystemSync {
        mounts: Vec<(MountEntry, Behaviour)>,
        calls: AtomicUsize,
    }

    impl FakeFilesystemSync {
        pub(crate) fn new(mounts: &[(&str, Behaviour)]) -> Self {
            let mounts = mounts
                .iter()
                .zip(1_u32..)
                .map(|((mount_point, behaviour), minor)| {
                    let entry = MountEntry {
                        device: DeviceId { major: 0, minor },
                        mount_point: (*mount_point).to_owned(),
                        fs_type: "ext4".to_owned(),
                        read_only: false,
                    };
                    (entry, *behaviour)
                })
                .collect();
            Self {
                mounts,
                calls: AtomicUsize::new(0),
            }
        }

        pub(crate) fn calls(&self) -> usize {
            self.calls.load(Ordering::SeqCst)
        }
    }

    impl FilesystemSync for FakeFilesystemSync {
        fn mount_table(&self) -> Vec<MountEntry> {
            self.mounts.iter().map(|(entry, _)| entry.clone()).collect()
        }

        fn syncfs(&self, mount_point: &str) -> std::io::Result<()> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            let behaviour = self
                .mounts
                .iter()
                .find(|(entry, _)| entry.mount_point == mount_point)
                .map_or(Behaviour::Fails, |(_, behaviour)| *behaviour);
            match behaviour {
                Behaviour::Syncs => Ok(()),
                Behaviour::Fails => Err(std::io::ErrorKind::Other.into()),
                Behaviour::BlocksForever => loop {
                    std::thread::park();
                },
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use std::time::{Duration, Instant};

    use super::fake::{Behaviour, FakeFilesystemSync};
    use super::*;

    fn flush_with(fake: &Arc<FakeFilesystemSync>, deadline: Duration) -> BoundedFlush {
        BoundedFlush::new(
            fake.clone(),
            SuspendBudget::new(deadline, Duration::from_secs(24)),
        )
    }

    #[tokio::test]
    async fn every_filesystem_is_synced_when_none_hangs() {
        let fake = Arc::new(FakeFilesystemSync::new(&[
            ("/", Behaviour::Syncs),
            ("/data", Behaviour::Syncs),
            ("/broken", Behaviour::Fails),
        ]));
        let report = flush_with(&fake, Duration::from_secs(5)).flush().await;
        assert_eq!(
            report,
            FlushReport {
                synced: 2,
                failed: 1,
                pending: 0,
                skipped_in_flight: 0
            }
        );
        assert!(!report.deadline_hit());
    }

    #[tokio::test]
    async fn a_hung_filesystem_costs_the_deadline_and_no_more() {
        let fake = Arc::new(FakeFilesystemSync::new(&[
            ("/", Behaviour::Syncs),
            ("/mnt/hung", Behaviour::BlocksForever),
        ]));
        let flush = flush_with(&fake, Duration::from_millis(200));
        let started = Instant::now();
        let report = flush.flush().await;
        let elapsed = started.elapsed();
        assert!(elapsed >= Duration::from_millis(200), "{elapsed:?}");
        assert!(elapsed < Duration::from_secs(2), "{elapsed:?}");
        assert_eq!(report.synced, 1);
        assert_eq!(report.pending, 1);
        assert!(report.deadline_hit());
    }

    #[tokio::test]
    async fn the_next_flush_skips_a_filesystem_still_hung() {
        let fake = Arc::new(FakeFilesystemSync::new(&[
            ("/", Behaviour::Syncs),
            ("/mnt/hung", Behaviour::BlocksForever),
        ]));
        let flush = flush_with(&fake, Duration::from_millis(100));
        assert_eq!(flush.flush().await.pending, 1);
        let again = flush.flush().await;
        assert_eq!(again.synced, 1);
        assert_eq!(again.pending, 0);
        assert_eq!(again.skipped_in_flight, 1);
        assert!(fake.calls() <= 3, "no second thread behind the hung sync");
    }
}
