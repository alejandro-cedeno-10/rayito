//! What `/resume` does with each volume (research doc §4.7, measured in
//! `AWS_API_NOTES.md` §16 Q129). Short pauses need nothing: after 60 s and
//! 10 min the first read succeeded ≤ 0.2 s after `resume()` (EFS-11). A
//! pause that crosses the expiry of the execution-role credentials the
//! mount helper signed the tunnel with does: AWS drops the tunnel's TCP
//! connection on restore, `efs-proxy` reconnects with the expired
//! credentials and every read answers `Permission denied` until the volume
//! is remounted (EFS-12, 70 min). So the decision is, per volume:
//!
//! 1. Credentials known to be expired, or inside
//!    `credentials::REFRESH_MARGIN` of expiring: remount straight away —
//!    probing first would only spend the participant's `/resume` budget on
//!    a reconnection already known to fail.
//! 2. Otherwise, a cheap probe (a bounded `stat` in a child process) and a
//!    remount only if it does not come back healthy — this also covers a
//!    volume whose `/suspend` flush timed out, and the expiry the adapter
//!    could not learn.
//!
//! Every reason a volume is degraded maps to one closed
//! `EfsVolumeStatus.last_error_class` string (`DegradeReason::as_str`).

use std::time::SystemTime;

use super::ports::{MountFailureClass, ProbeOutcome};
use super::state::MountState;
use crate::credentials::{CredentialKind, CredentialLease};

/// Why a mounted volume left `Mounted`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum DegradeReason {
    /// The suspension crossed (or came within the refresh margin of) the
    /// expiry of the credentials the tunnel was signed with (EFS-12).
    CredentialsExpired,
    /// `/suspend`'s bounded flush did not finish (EFS-13): the volume may
    /// have lost writes, and is probed again at `/resume`.
    FlushTimedOut,
    /// The probe failed (for example `EACCES`/`ESTALE`).
    Stale,
    /// The probe did not answer inside its budget (mount target
    /// unreachable).
    Unreachable,
    /// The mount point itself is gone.
    Gone,
    /// The remount that tried to recover the volume failed with this
    /// class; the volume stays degraded until the next `/resume` or
    /// `Configure`.
    RemountFailed(MountFailureClass),
}

impl DegradeReason {
    /// The closed `last_error_class` value `efs_volumes.proto` documents
    /// for a `DEGRADED`/`REMOUNTING` volume.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::CredentialsExpired => "credentials_expired",
            Self::FlushTimedOut => "flush_timeout",
            Self::Stale => "stale",
            Self::Unreachable => "unreachable",
            Self::Gone => "gone",
            Self::RemountFailed(class) => class.as_str(),
        }
    }
}

/// The first thing `/resume` does for one volume.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ResumeStep {
    /// Not mounted (never mounted, failed, unmounted, or already being
    /// remounted): nothing to recover.
    Nothing,
    /// Probe it within the participant budget, then `after_probe`.
    Probe,
    /// Remount without probing.
    Remount(DegradeReason),
}

/// Step 1 of the module doc. `credentials_expire_at` is the
/// `MountReceipt` of the volume's last (re)mount; `now` is wall-clock time
/// right after the resume (AWS corrects the guest's wall clock on restore,
/// `AWS_API_NOTES.md` §15).
#[must_use]
pub fn plan_resume(
    state: MountState,
    credentials_expire_at: Option<SystemTime>,
    now: SystemTime,
) -> ResumeStep {
    if !matches!(state, MountState::Mounted | MountState::Degraded) {
        return ResumeStep::Nothing;
    }
    let lease_spent = credentials_expire_at.is_some_and(|expires_at| {
        CredentialLease {
            kind: CredentialKind::ExecutionRole,
            expires_at,
        }
        .needs_refresh(now)
    });
    if lease_spent {
        ResumeStep::Remount(DegradeReason::CredentialsExpired)
    } else {
        ResumeStep::Probe
    }
}

/// Step 2 of the module doc: `None` for a healthy volume, otherwise why it
/// must be remounted.
#[must_use]
pub fn after_probe(outcome: ProbeOutcome) -> Option<DegradeReason> {
    match outcome {
        ProbeOutcome::Healthy => None,
        ProbeOutcome::Stale => Some(DegradeReason::Stale),
        ProbeOutcome::Hung => Some(DegradeReason::Unreachable),
        ProbeOutcome::Gone => Some(DegradeReason::Gone),
    }
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use super::*;
    use crate::credentials::REFRESH_MARGIN;

    fn now() -> SystemTime {
        SystemTime::UNIX_EPOCH + Duration::from_secs(1_700_000_000)
    }

    #[test]
    fn a_volume_that_is_not_mounted_needs_nothing() {
        for state in [
            MountState::Requested,
            MountState::Mounting,
            MountState::Remounting,
            MountState::Unmounted,
            MountState::Failed,
        ] {
            assert_eq!(plan_resume(state, None, now()), ResumeStep::Nothing);
        }
    }

    #[test]
    fn credentials_expired_during_the_pause_remount_without_probing() {
        let expired = now() - Duration::from_secs(60);
        assert_eq!(
            plan_resume(MountState::Mounted, Some(expired), now()),
            ResumeStep::Remount(DegradeReason::CredentialsExpired)
        );
    }

    #[test]
    fn credentials_inside_the_refresh_margin_also_remount() {
        let almost = now() + REFRESH_MARGIN;
        assert_eq!(
            plan_resume(MountState::Mounted, Some(almost), now()),
            ResumeStep::Remount(DegradeReason::CredentialsExpired)
        );
    }

    #[test]
    fn fresh_credentials_only_probe() {
        let fresh = now() + REFRESH_MARGIN + Duration::from_secs(1);
        assert_eq!(
            plan_resume(MountState::Mounted, Some(fresh), now()),
            ResumeStep::Probe
        );
    }

    #[test]
    fn an_unknown_expiry_falls_back_to_the_probe() {
        assert_eq!(
            plan_resume(MountState::Mounted, None, now()),
            ResumeStep::Probe
        );
    }

    #[test]
    fn a_degraded_volume_is_planned_like_a_mounted_one() {
        assert_eq!(
            plan_resume(MountState::Degraded, None, now()),
            ResumeStep::Probe
        );
    }

    #[test]
    fn only_a_healthy_probe_keeps_the_mount() {
        assert_eq!(after_probe(ProbeOutcome::Healthy), None);
        assert_eq!(after_probe(ProbeOutcome::Stale), Some(DegradeReason::Stale));
        assert_eq!(
            after_probe(ProbeOutcome::Hung),
            Some(DegradeReason::Unreachable)
        );
        assert_eq!(after_probe(ProbeOutcome::Gone), Some(DegradeReason::Gone));
    }

    #[test]
    fn a_failed_remount_reports_the_mount_failure_class() {
        assert_eq!(
            DegradeReason::RemountFailed(MountFailureClass::IamDenied).as_str(),
            "iam_denied"
        );
        assert_eq!(
            DegradeReason::CredentialsExpired.as_str(),
            "credentials_expired"
        );
        assert_eq!(DegradeReason::FlushTimedOut.as_str(), "flush_timeout");
    }
}
