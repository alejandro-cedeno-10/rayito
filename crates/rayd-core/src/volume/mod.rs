//! Domain of EFS-backed volumes (`m15-efs-volumes`, ADR-018, experimental):
//! what a mount request is, how a sandbox's volumes are planned without
//! overlapping, the states a mount moves through, and the error table a
//! client sees. Pure rules over the `VolumeMounter` port; `mount(2)`, the
//! `efs-utils` helper and its watchdog live in the `rayd` adapter
//! (`adapters::efs_mount`).
//!
//! This module ships ahead of the EFS measurement campaign
//! (`docs/research/2026-10-efs-persistence.md`, questions EFS-1..EFS-20):
//! `rayd`'s only adapter today is `UnavailableEfsMounter`
//! (`support()` always `Unsupported`), so every mount request fails closed
//! regardless of how well-formed it is. The domain and the port are real
//! and tested so that the first adapter that *does* mount EFS, once the
//! campaign clears the stop criteria (EFS-2, EFS-3, EFS-8, EFS-11, EFS-13),
//! drops in without reshaping any of this.

pub mod error;
pub mod plan;
pub mod ports;
pub mod spec;
pub mod state;

pub use error::VolumeError;
pub use plan::{VolumePlan, VolumePlanError};
pub use ports::{
    BoxFuture, MountFailure, MountFailureClass, MountSupport, ProbeOutcome, UnmountMode,
    VolumeMounter,
};
pub use spec::{AccessPointId, FileSystemId, MountPath, MountTargetIp, VolumeSpec};
pub use state::{MountState, MountTransition};
