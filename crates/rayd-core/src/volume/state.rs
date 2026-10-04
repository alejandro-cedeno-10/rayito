//! The lifecycle of one mounted volume (research doc §4.2 `MountState`) as
//! a small state machine: every move is a pure function from the current
//! state and an event to the next state (or `None` for an event that
//! cannot happen there), so the adapter's hooks (`/run`, `/resume`,
//! `/terminate`) never have to re-derive what is legal.

/// Where one volume's mount currently stands.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum MountState {
    /// Accepted by `ConfigureSandbox`, not yet attempted.
    Requested,
    /// The adapter is running the mount helper.
    Mounting,
    /// `stat` on the mount point succeeds.
    Mounted,
    /// Was `Mounted`; a probe found it `Stale`/`Hung`/`Gone`, its
    /// `/suspend` flush timed out, or the credentials of its tunnel expired
    /// during a pause (`super::resume::DegradeReason` says which).
    Degraded,
    /// Recovering a `Degraded` mount: `umount -l` followed by a fresh
    /// mount attempt.
    Remounting,
    /// Cleanly unmounted (`/terminate`, best effort).
    Unmounted,
    /// The adapter gave up: `VolumeError` names why.
    Failed,
}

/// What moves a volume from one `MountState` to the next.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum MountTransition {
    MountStarted,
    MountSucceeded,
    MountFailed,
    ProbeFoundDegraded,
    /// A probe of a `Degraded` volume came back healthy (a flush that
    /// timed out while the mount target was briefly unreachable).
    ProbeFoundHealthy,
    /// `/suspend`'s bounded flush left the volume with unwritten pages.
    FlushTimedOut,
    /// `/resume` found the tunnel's credentials expired.
    CredentialsExpired,
    RemountStarted,
    RemountSucceeded,
    RemountFailed,
    Unmounted,
}

impl MountState {
    /// The next state for `transition`, or `None` when that transition
    /// cannot happen from `self` (the caller's bug, not a runtime error:
    /// `rayd`'s adapter never offers an event the state machine refuses).
    #[must_use]
    pub fn apply(self, transition: MountTransition) -> Option<Self> {
        use MountState::{Degraded, Failed, Mounted, Mounting, Remounting, Requested, Unmounted};
        use MountTransition::{
            CredentialsExpired, FlushTimedOut, MountFailed, MountStarted, MountSucceeded,
            ProbeFoundDegraded, ProbeFoundHealthy, RemountFailed, RemountStarted, RemountSucceeded,
            Unmounted as UnmountedEvent,
        };
        match (self, transition) {
            (Requested, MountStarted) => Some(Mounting),
            (Mounting, MountSucceeded)
            | (Remounting, RemountSucceeded)
            | (Degraded, ProbeFoundHealthy) => Some(Mounted),
            (Mounting, MountFailed) => Some(Failed),
            (Mounted | Degraded, ProbeFoundDegraded | FlushTimedOut | CredentialsExpired)
            | (Remounting, RemountFailed) => Some(Degraded),
            (Degraded, RemountStarted) => Some(Remounting),
            (Mounted | Degraded | Remounting | Failed, UnmountedEvent) => Some(Unmounted),
            _unreachable_from_this_state => None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use MountState::{Degraded, Failed, Mounted, Mounting, Remounting, Requested, Unmounted};
    use MountTransition::{
        CredentialsExpired, FlushTimedOut, MountFailed, MountStarted, MountSucceeded,
        ProbeFoundDegraded, ProbeFoundHealthy, RemountFailed, RemountStarted, RemountSucceeded,
        Unmounted as UnmountedEvent,
    };

    #[test]
    fn a_mount_attempt_moves_requested_to_mounting() {
        assert_eq!(Requested.apply(MountStarted), Some(Mounting));
    }

    #[test]
    fn a_successful_mount_moves_mounting_to_mounted() {
        assert_eq!(Mounting.apply(MountSucceeded), Some(Mounted));
    }

    #[test]
    fn a_failed_mount_moves_mounting_to_failed() {
        assert_eq!(Mounting.apply(MountFailed), Some(Failed));
    }

    #[test]
    fn a_degraded_probe_moves_mounted_to_degraded() {
        assert_eq!(Mounted.apply(ProbeFoundDegraded), Some(Degraded));
    }

    #[test]
    fn a_timed_out_flush_or_expired_credentials_degrade_a_mounted_volume() {
        assert_eq!(Mounted.apply(FlushTimedOut), Some(Degraded));
        assert_eq!(Mounted.apply(CredentialsExpired), Some(Degraded));
        assert_eq!(Degraded.apply(FlushTimedOut), Some(Degraded));
        assert_eq!(Degraded.apply(ProbeFoundDegraded), Some(Degraded));
    }

    #[test]
    fn a_healthy_probe_restores_a_degraded_volume() {
        assert_eq!(Degraded.apply(ProbeFoundHealthy), Some(Mounted));
        assert_eq!(Mounted.apply(ProbeFoundHealthy), None);
    }

    #[test]
    fn a_volume_being_remounted_is_not_degraded_again() {
        assert_eq!(Remounting.apply(FlushTimedOut), None);
        assert_eq!(Remounting.apply(CredentialsExpired), None);
    }

    #[test]
    fn a_remount_round_trip_returns_to_mounted() {
        assert_eq!(Degraded.apply(RemountStarted), Some(Remounting));
        assert_eq!(Remounting.apply(RemountSucceeded), Some(Mounted));
    }

    #[test]
    fn a_failed_remount_falls_back_to_degraded_not_failed() {
        assert_eq!(Remounting.apply(RemountFailed), Some(Degraded));
    }

    #[test]
    fn terminate_unmounts_from_every_live_state() {
        assert_eq!(Mounted.apply(UnmountedEvent), Some(Unmounted));
        assert_eq!(Degraded.apply(UnmountedEvent), Some(Unmounted));
        assert_eq!(Remounting.apply(UnmountedEvent), Some(Unmounted));
        assert_eq!(Failed.apply(UnmountedEvent), Some(Unmounted));
    }

    #[test]
    fn requested_cannot_be_probed_or_unmounted_directly() {
        assert_eq!(Requested.apply(ProbeFoundDegraded), None);
        assert_eq!(Requested.apply(UnmountedEvent), None);
    }

    #[test]
    fn unmounted_is_terminal() {
        for transition in [
            MountStarted,
            MountSucceeded,
            MountFailed,
            ProbeFoundDegraded,
            ProbeFoundHealthy,
            FlushTimedOut,
            CredentialsExpired,
            RemountStarted,
            RemountSucceeded,
            RemountFailed,
            UnmountedEvent,
        ] {
            assert_eq!(Unmounted.apply(transition), None);
        }
    }
}
