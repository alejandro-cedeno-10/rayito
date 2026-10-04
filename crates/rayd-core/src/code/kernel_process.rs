//! Which kernel process groups `rayd` may signal. `rayd` (root) kills a
//! kernel's whole group when the sidecar dies or the agent ends, by a pid
//! the sidecar reported over its protocol pipe (`ready.kernel_pid`, the
//! `kernel_pid` of a create or restart reply). The sidecar runs as the
//! sandbox user, and so does every other sandbox process, which can reach
//! that pipe through `/proc` (`SECURITY.md` T12): the number is untrusted
//! input, never a handle.
//!
//! So a reported pid is admitted only when the kernel table confirms it
//! names a kernel of the sidecar that reported it: not `0` or `1`, a live
//! process whose parent is that sidecar, the leader of its own process
//! group (kernels start in a new session), owned by the sidecar's own
//! user. What is admitted is pinned by its start time ([`KernelProcess`]),
//! the same identity `orphans::OwnedChildren` uses against pid reuse, and
//! re-checked right before each signal ([`KernelProcess::may_signal`]).

/// The lowest pid that can be a kernel: `0` names the caller's own process
/// group in `kill(2)`/`killpg(3)` and `1` is `init` (`rayd` itself in the
/// `MicroVM`, `orphans::INIT_PID`).
pub const LOWEST_KERNEL_PID: u32 = 2;

/// What the kernel's process table says about one process: its parent,
/// its process group, its start time in clock ticks after boot
/// (`proc(5)` `stat` fields 4, 5 and 22) and its real uid (`status`,
/// `Uid:`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ProcessFacts {
    pub ppid: u32,
    pub pgrp: u32,
    pub start_ticks: u64,
    pub uid: u32,
}

/// Why a reported kernel pid was not admitted (`kernel_pid_rejected`
/// lines carry `as_str`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum KernelPidRejection {
    /// `0` or `1`.
    Reserved,
    /// No process has that pid (or the table cannot be read).
    Gone,
    /// The reporting sidecar is no longer in the table.
    SidecarGone,
    /// The process is not a child of the sidecar that reported it.
    NotSidecarChild,
    /// The process does not lead its own process group.
    NotGroupLeader,
    /// The process belongs to another user than the sidecar.
    ForeignOwner,
}

impl KernelPidRejection {
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Reserved => "reserved",
            Self::Gone => "gone",
            Self::SidecarGone => "sidecar_gone",
            Self::NotSidecarChild => "not_sidecar_child",
            Self::NotGroupLeader => "not_group_leader",
            Self::ForeignOwner => "foreign_owner",
        }
    }
}

/// A kernel the table vouched for when it was reported: its pid (which is
/// also its process group), pinned to its start time and user.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct KernelProcess {
    pid: u32,
    start_ticks: u64,
    uid: u32,
}

impl KernelProcess {
    /// Admits `pid`, reported by the sidecar `sidecar_pid`, given what the
    /// table says about both.
    pub fn admit(
        pid: u32,
        sidecar_pid: u32,
        kernel: Option<ProcessFacts>,
        sidecar: Option<ProcessFacts>,
    ) -> Result<Self, KernelPidRejection> {
        if pid < LOWEST_KERNEL_PID {
            return Err(KernelPidRejection::Reserved);
        }
        let kernel = kernel.ok_or(KernelPidRejection::Gone)?;
        let sidecar = sidecar.ok_or(KernelPidRejection::SidecarGone)?;
        if kernel.ppid != sidecar_pid {
            return Err(KernelPidRejection::NotSidecarChild);
        }
        if kernel.pgrp != pid {
            return Err(KernelPidRejection::NotGroupLeader);
        }
        if kernel.uid != sidecar.uid {
            return Err(KernelPidRejection::ForeignOwner);
        }
        Ok(Self {
            pid,
            start_ticks: kernel.start_ticks,
            uid: kernel.uid,
        })
    }

    #[must_use]
    pub fn pid(&self) -> u32 {
        self.pid
    }

    /// Whether the group named by this kernel's pid may be signalled now,
    /// given what the table says about that pid at this moment.
    ///
    /// The same process (start time, user, still its group's leader):
    /// yes. Another process with that pid: no, the number was recycled. No
    /// process at all: yes, because Linux does not hand out a pid while a
    /// process group of that number still exists, so the number can only
    /// name the kernel's own group (members it left behind) or nothing.
    #[must_use]
    pub fn may_signal(&self, now: Option<ProcessFacts>) -> bool {
        now.is_none_or(|facts| {
            facts.start_ticks == self.start_ticks && facts.uid == self.uid && facts.pgrp == self.pid
        })
    }
}

/// Port over the kernel processes `rayd` may admit and signal: the
/// `/proc` table and `killpg(2)` in production, a recording fake in tests.
pub trait KernelProcesses: Send + Sync {
    /// Checks a pid the sidecar `sidecar_pid` reported against the table.
    fn admit(&self, pid: u32, sidecar_pid: u32) -> Result<KernelProcess, KernelPidRejection>;

    /// Sends `signal` to the kernel's process group unless its pid now
    /// names another process; `true` when a signal was sent.
    fn signal(&self, kernel: &KernelProcess, signal: i32) -> bool;
}

#[cfg(test)]
mod tests {
    use super::*;

    const SIDECAR: u32 = 400;
    const KERNEL: u32 = 401;
    const SANDBOX_UID: u32 = 1000;
    const STARTED: u64 = 7731;

    fn sidecar() -> ProcessFacts {
        ProcessFacts {
            ppid: 1,
            pgrp: SIDECAR,
            start_ticks: 7000,
            uid: SANDBOX_UID,
        }
    }

    fn kernel() -> ProcessFacts {
        ProcessFacts {
            ppid: SIDECAR,
            pgrp: KERNEL,
            start_ticks: STARTED,
            uid: SANDBOX_UID,
        }
    }

    fn admitted() -> KernelProcess {
        KernelProcess::admit(KERNEL, SIDECAR, Some(kernel()), Some(sidecar())).unwrap()
    }

    #[test]
    fn a_kernel_of_the_reporting_sidecar_is_admitted_with_its_start_time() {
        let admitted = admitted();
        assert_eq!(admitted.pid(), KERNEL);
        assert!(admitted.may_signal(Some(kernel())));
    }

    #[test]
    fn pid_zero_and_init_are_never_admitted_whatever_the_table_says() {
        for forged in [0, 1] {
            let facts = ProcessFacts {
                pgrp: forged,
                ..kernel()
            };
            assert_eq!(
                KernelProcess::admit(forged, SIDECAR, Some(facts), Some(sidecar())),
                Err(KernelPidRejection::Reserved)
            );
        }
    }

    #[test]
    fn a_pid_that_is_not_a_child_of_the_sidecar_is_rejected() {
        let rayd_child = ProcessFacts {
            ppid: 1,
            ..kernel()
        };
        assert_eq!(
            KernelProcess::admit(KERNEL, SIDECAR, Some(rayd_child), Some(sidecar())),
            Err(KernelPidRejection::NotSidecarChild)
        );
    }

    #[test]
    fn a_child_that_does_not_lead_its_group_is_rejected() {
        let member = ProcessFacts {
            pgrp: SIDECAR,
            ..kernel()
        };
        assert_eq!(
            KernelProcess::admit(KERNEL, SIDECAR, Some(member), Some(sidecar())),
            Err(KernelPidRejection::NotGroupLeader)
        );
    }

    #[test]
    fn a_process_of_another_user_is_rejected() {
        let root_owned = ProcessFacts { uid: 0, ..kernel() };
        assert_eq!(
            KernelProcess::admit(KERNEL, SIDECAR, Some(root_owned), Some(sidecar())),
            Err(KernelPidRejection::ForeignOwner)
        );
    }

    #[test]
    fn a_missing_kernel_or_sidecar_is_rejected() {
        assert_eq!(
            KernelProcess::admit(KERNEL, SIDECAR, None, Some(sidecar())),
            Err(KernelPidRejection::Gone)
        );
        assert_eq!(
            KernelProcess::admit(KERNEL, SIDECAR, Some(kernel()), None),
            Err(KernelPidRejection::SidecarGone)
        );
    }

    #[test]
    fn a_recycled_pid_is_never_signalled() {
        let admitted = admitted();
        let recycled = ProcessFacts {
            start_ticks: STARTED + 1,
            ..kernel()
        };
        assert!(!admitted.may_signal(Some(recycled)));
        let other_user = ProcessFacts { uid: 0, ..kernel() };
        assert!(!admitted.may_signal(Some(other_user)));
        let not_leader = ProcessFacts {
            pgrp: SIDECAR,
            ..kernel()
        };
        assert!(!admitted.may_signal(Some(not_leader)));
    }

    #[test]
    fn a_gone_leader_still_lets_its_group_be_signalled() {
        assert!(admitted().may_signal(None));
    }

    #[test]
    fn every_rejection_has_a_distinct_log_name() {
        let names = [
            KernelPidRejection::Reserved,
            KernelPidRejection::Gone,
            KernelPidRejection::SidecarGone,
            KernelPidRejection::NotSidecarChild,
            KernelPidRejection::NotGroupLeader,
            KernelPidRejection::ForeignOwner,
        ]
        .map(KernelPidRejection::as_str);
        let unique: std::collections::BTreeSet<_> = names.iter().collect();
        assert_eq!(unique.len(), names.len());
    }
}
