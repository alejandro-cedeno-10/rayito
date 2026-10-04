//! Which `efs-proxy` process belongs to which volume, and when one is gone
//! (`AWS_API_NOTES.md` §16 Q128). `mount -t efs -o tls` starts one
//! `efs-proxy` per mount (the local end of the TLS tunnel the kernel's NFS
//! client talks to) and `umount` never stops it: without `systemd` (PID 1 is
//! `rayd`) nothing else does either, so every mount/unmount cycle used to
//! leak one process. The adapter therefore records the proxies a mount
//! started — the ones present after the mount helper returned that were not
//! there before it ran, with mounts serialized so no other mount can start
//! one in between — and stops exactly those on unmount.
//!
//! `efs-proxy` is not `rayd`'s child: `mount.efs` starts it and exits, so it
//! re-parents to PID 1. A process is pinned by its pid *and* its kernel
//! start time (`orphans::ProcEntry::start_ticks`), so a recycled pid is
//! never mistaken for the proxy, and never signalled.

use crate::orphans::ProcEntry;

/// One `efs-proxy` process, identified for its whole life.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct ProxyProcess {
    pub pid: i32,
    pub start_ticks: u64,
}

/// The proxies present in `after` that `before` did not have: the ones the
/// mount that ran between the two snapshots started.
#[must_use]
pub fn spawned_between(before: &[ProxyProcess], after: &[ProxyProcess]) -> Vec<ProxyProcess> {
    after
        .iter()
        .filter(|proxy| !before.contains(proxy))
        .copied()
        .collect()
}

/// Whether `observed` (the process table's current entry for
/// `expected.pid`, `None` once it left the table) is still the same,
/// running process: a zombie has already exited, and a different start time
/// means the pid now names another process.
#[must_use]
pub fn is_still_running(expected: ProxyProcess, observed: Option<ProcEntry>) -> bool {
    observed.is_some_and(|entry| {
        entry.pid == expected.pid && entry.start_ticks == expected.start_ticks && !entry.zombie
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    const START: u64 = 4_200;

    fn proxy(pid: i32) -> ProxyProcess {
        ProxyProcess {
            pid,
            start_ticks: START,
        }
    }

    fn entry(pid: i32, start_ticks: u64, zombie: bool) -> ProcEntry {
        ProcEntry {
            pid,
            ppid: 1,
            zombie,
            start_ticks,
        }
    }

    #[test]
    fn only_the_proxies_a_mount_started_are_attributed_to_it() {
        let before = [proxy(10), proxy(11)];
        let after = [proxy(10), proxy(11), proxy(12)];
        assert_eq!(spawned_between(&before, &after), [proxy(12)]);
    }

    #[test]
    fn a_proxy_that_exited_during_the_mount_is_not_attributed() {
        let before = [proxy(10)];
        let after = [proxy(12)];
        assert_eq!(spawned_between(&before, &after), [proxy(12)]);
    }

    #[test]
    fn a_recycled_pid_counts_as_a_new_process() {
        let before = [proxy(10)];
        let recycled = ProxyProcess {
            pid: 10,
            start_ticks: START + 1,
        };
        assert_eq!(spawned_between(&before, &[recycled]), [recycled]);
    }

    #[test]
    fn a_mount_that_started_nothing_attributes_nothing() {
        assert!(spawned_between(&[proxy(10)], &[proxy(10)]).is_empty());
    }

    #[test]
    fn the_same_live_process_is_still_running() {
        assert!(is_still_running(proxy(12), Some(entry(12, START, false))));
    }

    #[test]
    fn a_zombie_or_a_missing_entry_has_stopped() {
        assert!(!is_still_running(proxy(12), Some(entry(12, START, true))));
        assert!(!is_still_running(proxy(12), None));
    }

    #[test]
    fn a_pid_reused_by_another_process_has_stopped() {
        assert!(!is_still_running(
            proxy(12),
            Some(entry(12, START + 9, false))
        ));
    }
}
