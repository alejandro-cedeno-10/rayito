//! Domain of `m15-s3-mounts` (ADR-017): S3 buckets mounted into the guest
//! through `mount-s3`/FUSE, only on `rayito-base-caps` (or a size-suffixed
//! variant of it). Pure here: `error.rs` closes the set of reasons a mount
//! can fail (mirrors `S3MountState.error_class` of
//! `proto/rayito/v1/s3_mounts.proto`), `spec.rs` is the desired state of one
//! mount plus the allowlist check against the image's
//! `RAYITO_ALLOWED_MOUNT_BUCKETS`, `state.rs` is what a mount currently is,
//! and `ports.rs` is what the feature's slot (`rayd::features::s3_mounts`)
//! needs from the operating system — opening and attaching `/dev/fuse`
//! (`FuseDevice`) and running the `mount-s3` daemon against it
//! (`FuseDaemon`). The real adapters (`libc::mount(2)`, `tokio::process`)
//! live in `rayd::adapters::{fuse_device, mount_s3}`; nothing in this
//! module touches the filesystem, forks a process or reads an environment
//! variable.

pub mod error;
pub mod ports;
pub mod spec;
pub mod state;

pub use error::MountErrorClass;
pub use ports::{FuseDaemon, FuseDevice};
pub use spec::{S3Mount, parse_allowed_buckets, validate_mounts};
pub use state::{MountPhase, MountState};
