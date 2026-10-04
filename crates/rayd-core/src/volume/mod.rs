//! Domain of EFS-backed volumes (`m15-efs-volumes`, ADR-018, experimental):
//! what a mount request is, how a sandbox's volumes are planned without
//! overlapping, the states a mount moves through, which `efs-proxy` belongs
//! to which mount, what `/resume` does with each volume, and the error
//! table a client sees. Pure rules over the `VolumeMounter` port;
//! `mount(2)`, the `efs-utils` helper and the processes it leaves behind
//! live in the `rayd` adapter (`adapters::efs_mount::EfsUtilsMounter`).
//!
//! The adapter only reports support where the image carries
//! `amazon-efs-utils` and `CAP_SYS_ADMIN` (research doc §4.1 rule 1); the
//! rules here encode what the 2026-10-04 acceptance measured
//! (`AWS_API_NOTES.md` §16 Q127–Q134): `efs-proxy` outlives `umount`
//! (`proxy`), a pause that crosses the credentials' expiry needs a remount
//! (`resume`), and an unreachable mount target with unflushed writes costs
//! the `MicroVM` at `/suspend` (`ports::FlushOutcome`).

pub mod error;
pub mod plan;
pub mod ports;
pub mod proxy;
pub mod resume;
pub mod spec;
pub mod state;

pub use error::VolumeError;
pub use plan::{VolumePlan, VolumePlanError};
pub use ports::{
    BoxFuture, FlushOutcome, MountFailure, MountFailureClass, MountReceipt, MountSupport,
    ProbeOutcome, UnmountMode, VolumeMounter,
};
pub use proxy::{ProxyProcess, is_still_running, spawned_between};
pub use resume::{DegradeReason, ResumeStep, after_probe, plan_resume};
pub use spec::{AccessPointId, FileSystemId, MountPath, MountTargetIp, VolumeSpec};
pub use state::{MountState, MountTransition};
