//! The pure half of step 4 of the `/suspend` checklist: dirty pages reach
//! the disk before the checkpoint, inside a bound. `sync(2)` has none: a
//! hung network or FUSE mount (or a slow disk) leaves it in `D` state, and a
//! `/suspend` that does not answer inside its window makes AWS terminate the
//! `MicroVM`. So `rayd` syncs each filesystem on its own with `syncfs(2)`,
//! waits at most the `SuspendBudget`'s deadline and answers 200 either way.
//!
//! This module decides which filesystems get a `syncfs` (`plan_sync`, over
//! the kernel's `mountinfo`), how long the hook waits (`SuspendBudget`) and
//! what the wait reports (`FlushReport`). The throwaway threads and the
//! syscalls live in the `rayd` adapter `adapters::bounded_sync`, behind the
//! `FilesystemSync` port.

use std::collections::HashSet;
use std::hash::BuildHasher;
use std::time::Duration;

use crate::hooks::{QUIESCE_TIMEOUT, STREAM_CLOSE_GRACE};

/// How long `/suspend` waits for the per-filesystem syncs by default. The
/// syncs that have not returned by then keep running in their own threads;
/// the hook answers without them.
pub const SUSPEND_SYNC_DEADLINE: Duration = Duration::from_secs(5);

/// Upper bound on the filesystems synced by one `/suspend` (one thread
/// each). A guest has a handful; the cap only guards a pathological table.
pub const MAX_SYNC_TARGETS: usize = 64;

/// The part of the `/suspend` hook budget the page-cache flush may spend.
/// The stream-close grace, the quiesce and the flush together never exceed
/// half the hook budget, so the 200 always goes out well inside it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SuspendBudget {
    sync_deadline: Duration,
}

impl SuspendBudget {
    /// `requested` clamped so that `STREAM_CLOSE_GRACE + QUIESCE_TIMEOUT +
    /// sync_deadline <= hook_budget / 2`. A hook budget too small for the
    /// other steps leaves a zero deadline: the syncs are started and the
    /// hook does not wait for them.
    #[must_use]
    pub fn new(requested: Duration, hook_budget: Duration) -> Self {
        let ceiling = (hook_budget / 2).saturating_sub(STREAM_CLOSE_GRACE + QUIESCE_TIMEOUT);
        Self {
            sync_deadline: requested.min(ceiling),
        }
    }

    /// The default deadline (`SUSPEND_SYNC_DEADLINE`) inside `hook_budget`.
    #[must_use]
    pub fn for_hook(hook_budget: Duration) -> Self {
        Self::new(SUSPEND_SYNC_DEADLINE, hook_budget)
    }

    #[must_use]
    pub fn sync_deadline(self) -> Duration {
        self.sync_deadline
    }
}

/// A filesystem's device number (`major:minor` in `mountinfo`): `syncfs`
/// acts on the whole filesystem, so bind mounts of one device share it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct DeviceId {
    pub major: u32,
    pub minor: u32,
}

/// One line of `/proc/self/mountinfo`, reduced to what the plan needs.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MountEntry {
    pub device: DeviceId,
    pub mount_point: String,
    pub fs_type: String,
    pub read_only: bool,
}

/// One `syncfs` to run: the filesystem and a path that opens it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SyncTarget {
    pub device: DeviceId,
    pub mount_point: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct SyncPlan {
    pub targets: Vec<SyncTarget>,
    /// Filesystems left out because the `syncfs` of an earlier `/suspend`
    /// has not returned yet: another thread would only pile up behind it.
    pub skipped_in_flight: usize,
}

/// What `/suspend` knows about the flush when it stops waiting. Counts
/// only: paths never reach the logs.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct FlushReport {
    pub synced: usize,
    pub failed: usize,
    /// Syncs still running when the deadline expired.
    pub pending: usize,
    pub skipped_in_flight: usize,
}

impl FlushReport {
    #[must_use]
    pub fn deadline_hit(&self) -> bool {
        self.pending > 0
    }
}

/// What the flush needs from the operating system. `syncfs` may block for
/// as long as the filesystem wants (forever on a hung hard NFS mount): the
/// adapter only ever calls it from a throwaway thread.
pub trait FilesystemSync: Send + Sync {
    /// The mounted filesystems; empty when the table cannot be read.
    fn mount_table(&self) -> Vec<MountEntry>;
    /// Flushes the filesystem that contains `mount_point`.
    fn syncfs(&self, mount_point: &str) -> std::io::Result<()>;
}

/// Filesystems with no page cache to write back (or none of their own).
const NOTHING_TO_SYNC: &[&str] = &[
    "autofs",
    "binfmt_misc",
    "bpf",
    "cgroup",
    "cgroup2",
    "configfs",
    "debugfs",
    "devpts",
    "devtmpfs",
    "efivarfs",
    "fusectl",
    "hugetlbfs",
    "mqueue",
    "nsfs",
    "proc",
    "pstore",
    "ramfs",
    "rpc_pipefs",
    "securityfs",
    "selinuxfs",
    "sysfs",
    "tmpfs",
    "tracefs",
];

/// The filesystems a `/suspend` syncs: writable, backed by something, one
/// per device, none whose previous `syncfs` is still running, at most
/// `MAX_SYNC_TARGETS`. An unreadable (empty) table falls back to `/`, so
/// the root filesystem is never left unsynced.
#[must_use]
pub fn plan_sync<S: BuildHasher>(
    mounts: &[MountEntry],
    in_flight: &HashSet<DeviceId, S>,
) -> SyncPlan {
    if mounts.is_empty() {
        let root = DeviceId { major: 0, minor: 0 };
        if in_flight.contains(&root) {
            return SyncPlan {
                targets: Vec::new(),
                skipped_in_flight: 1,
            };
        }
        return SyncPlan {
            targets: vec![SyncTarget {
                device: root,
                mount_point: "/".to_owned(),
            }],
            skipped_in_flight: 0,
        };
    }
    let mut seen = HashSet::new();
    let mut plan = SyncPlan::default();
    for mount in mounts {
        if mount.read_only
            || NOTHING_TO_SYNC.contains(&mount.fs_type.as_str())
            || !seen.insert(mount.device)
        {
            continue;
        }
        if in_flight.contains(&mount.device) {
            plan.skipped_in_flight += 1;
        } else if plan.targets.len() < MAX_SYNC_TARGETS {
            plan.targets.push(SyncTarget {
                device: mount.device,
                mount_point: mount.mount_point.clone(),
            });
        }
    }
    plan
}

/// Parses `/proc/self/mountinfo` (`proc(5)`): `id parent major:minor root
/// mount_point options [optional fields...] - fs_type source super_options`.
/// Malformed lines are skipped; `\040`-style escapes in the mount point
/// are decoded.
#[must_use]
pub fn parse_mountinfo(text: &str) -> Vec<MountEntry> {
    text.lines().filter_map(parse_mountinfo_line).collect()
}

fn parse_mountinfo_line(line: &str) -> Option<MountEntry> {
    let fields: Vec<&str> = line.split_whitespace().collect();
    let (major, minor) = fields.get(2)?.split_once(':')?;
    let device = DeviceId {
        major: major.parse().ok()?,
        minor: minor.parse().ok()?,
    };
    let mount_point = unescape_octal(fields.get(4)?);
    let read_only = fields.get(5)?.split(',').any(|option| option == "ro");
    let separator = fields.iter().skip(6).position(|field| *field == "-")? + 6;
    let fs_type = (*fields.get(separator + 1)?).to_owned();
    Some(MountEntry {
        device,
        mount_point,
        fs_type,
        read_only,
    })
}

fn unescape_octal(field: &str) -> String {
    let bytes = field.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        let escaped = bytes.get(index + 1..index + 4).and_then(|digits| {
            let text = std::str::from_utf8(digits).ok()?;
            u8::from_str_radix(text, 8).ok()
        });
        match (bytes[index], escaped) {
            (b'\\', Some(byte)) => {
                out.push(byte);
                index += 4;
            }
            (byte, _) => {
                out.push(byte);
                index += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

#[cfg(test)]
mod tests {
    use super::*;

    const HOOK_BUDGET: Duration = Duration::from_secs(24);

    fn mount(major: u32, minor: u32, mount_point: &str, fs_type: &str) -> MountEntry {
        MountEntry {
            device: DeviceId { major, minor },
            mount_point: mount_point.to_owned(),
            fs_type: fs_type.to_owned(),
            read_only: false,
        }
    }

    #[test]
    fn the_default_deadline_fits_well_inside_the_suspend_budget() {
        let budget = SuspendBudget::for_hook(HOOK_BUDGET);
        assert_eq!(budget.sync_deadline(), SUSPEND_SYNC_DEADLINE);
        assert!(STREAM_CLOSE_GRACE + QUIESCE_TIMEOUT + budget.sync_deadline() <= HOOK_BUDGET / 2);
    }

    #[test]
    fn a_requested_deadline_is_clamped_to_half_the_hook_budget() {
        let budget = SuspendBudget::new(Duration::from_secs(60), HOOK_BUDGET);
        assert_eq!(budget.sync_deadline(), Duration::from_secs(8));
        let short = SuspendBudget::new(Duration::from_millis(250), HOOK_BUDGET);
        assert_eq!(short.sync_deadline(), Duration::from_millis(250));
    }

    #[test]
    fn a_hook_budget_too_small_for_the_other_steps_waits_for_nothing() {
        // `suspendTimeoutInSeconds` can be as low as 1 s (80 %: 800 ms).
        let budget = SuspendBudget::for_hook(Duration::from_millis(800));
        assert_eq!(budget.sync_deadline(), Duration::ZERO);
    }

    #[test]
    fn mountinfo_lines_become_entries() {
        let text = "\
22 1 259:1 / / rw,relatime shared:1 - ext4 /dev/root rw
23 22 0:21 / /proc rw,nosuid - proc proc rw
40 22 0:45 / /mnt/my\\040data rw - fuse.mountpoint-s3 mountpoint-s3 rw
41 22 0:46 / /mnt/ro ro,relatime - nfs4 host:/ ro
garbage line
";
        let entries = parse_mountinfo(text);
        assert_eq!(entries.len(), 4);
        assert_eq!(entries[0], mount(259, 1, "/", "ext4"));
        assert_eq!(entries[1].fs_type, "proc");
        assert_eq!(entries[2].mount_point, "/mnt/my data");
        assert_eq!(entries[2].fs_type, "fuse.mountpoint-s3");
        assert!(entries[3].read_only);
        assert_eq!(entries[3].fs_type, "nfs4");
    }

    #[test]
    fn the_plan_keeps_one_writable_backed_filesystem_per_device() {
        let mut read_only = mount(8, 2, "/opt", "ext4");
        read_only.read_only = true;
        let mounts = [
            mount(259, 1, "/", "ext4"),
            mount(0, 21, "/proc", "proc"),
            mount(0, 22, "/tmp", "tmpfs"),
            mount(259, 1, "/home/user/bind", "ext4"),
            read_only,
            mount(0, 45, "/mnt/data", "fuse.mountpoint-s3"),
            mount(0, 46, "/mnt/efs", "nfs4"),
        ];
        let plan = plan_sync(&mounts, &HashSet::new());
        let points: Vec<&str> = plan
            .targets
            .iter()
            .map(|target| target.mount_point.as_str())
            .collect();
        assert_eq!(points, ["/", "/mnt/data", "/mnt/efs"]);
        assert_eq!(plan.skipped_in_flight, 0);
    }

    #[test]
    fn a_filesystem_whose_previous_sync_hangs_is_skipped() {
        let mounts = [mount(259, 1, "/", "ext4"), mount(0, 46, "/mnt/efs", "nfs4")];
        let in_flight = HashSet::from([DeviceId {
            major: 0,
            minor: 46,
        }]);
        let plan = plan_sync(&mounts, &in_flight);
        assert_eq!(plan.targets.len(), 1);
        assert_eq!(plan.targets[0].mount_point, "/");
        assert_eq!(plan.skipped_in_flight, 1);
    }

    #[test]
    fn an_unreadable_table_still_syncs_the_root() {
        let plan = plan_sync(&[], &HashSet::new());
        assert_eq!(plan.targets.len(), 1);
        assert_eq!(plan.targets[0].mount_point, "/");
        let root = plan.targets[0].device;
        let again = plan_sync(&[], &HashSet::from([root]));
        assert!(again.targets.is_empty());
        assert_eq!(again.skipped_in_flight, 1);
    }

    #[test]
    fn the_plan_is_capped() {
        let mounts: Vec<MountEntry> = (0..100)
            .map(|minor| mount(0, minor, &format!("/m{minor}"), "ext4"))
            .collect();
        assert_eq!(
            plan_sync(&mounts, &HashSet::new()).targets.len(),
            MAX_SYNC_TARGETS
        );
    }

    #[test]
    fn only_pending_syncs_mean_the_deadline_was_hit() {
        let mut report = FlushReport {
            synced: 2,
            failed: 1,
            ..FlushReport::default()
        };
        assert!(!report.deadline_hit());
        report.pending = 1;
        assert!(report.deadline_hit());
    }
}
