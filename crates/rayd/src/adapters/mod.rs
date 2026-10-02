//! Operating-system adapters behind the `rayd-core` ports: process
//! spawning, pseudo-terminals, the kernel sidecar process, the metrics
//! probe, the filesystem with its per-thread identity, the inotify
//! watcher, the user database for names, the OS random source, the guest's
//! capability mask, the policy route that blocks IMDS for the sandbox user,
//! the egress policy routes over the shared `ip` runner (ADR-012),
//! the tar/gzip home archiver, the S3 object store (ADR-009) and the
//! credential-free HTTPS client of presigned transfers (ADR-010), the
//! bounded per-filesystem `syncfs` of `/suspend`, the boot-time reader of
//! `/etc/rayito/template.json` and the shell runner behind a template's
//! `ready_cmd` (ADR-022, `features::template_start`).
//! Each has a Linux implementation and an `Unsupported` stand-in so the
//! crate builds and its domain tests run on any host.

pub mod bounded_sync;
pub mod capabilities;
pub mod child_registry;
pub mod credential_broker;
pub mod egress_routes;
pub mod fs_identity;
pub mod fs_template_spec;
pub mod imds_block;
pub mod ip_command;
pub mod name_resolver;
pub mod notify_watcher;
pub mod orphan_reaper;
pub mod process_spawner;
pub mod procfs_metrics;
pub mod pty_backend;
pub mod random;
pub mod s3_store;
pub mod shell_ready_probe;
pub mod sidecar_process;
pub mod signed_http;
pub mod std_filesystem;
pub mod tar_archiver;

pub use bounded_sync::{BoundedFlush, PlatformFilesystemSync};
pub use capabilities::{GuestCapabilities, detect_guest_capabilities};
pub use child_registry::ChildRegistry;
pub use credential_broker::{
    CredentialBrokerError, GuestCredentials, ImdsCredentialBroker, PushedCredentials,
};
pub use fs_identity::FsIdentityGuard;
pub use fs_template_spec::{FsTemplateSpecSource, TemplateSpecSource};
pub use imds_block::{
    IMDS_ADDRESS, IMDS_VERIFY_BUDGET, ImdsBlock, ImdsProbe, ImdsState, USER_PROBE_CODE,
    USER_PROBE_PROGRAM, UserConnectProbe, install_imds_block, probe_root, rule_present,
    verify_imds_block,
};
#[cfg(unix)]
pub use name_resolver::NixNameResolver;
pub use name_resolver::{NumericNameResolver, PlatformNameResolver};
#[cfg(unix)]
pub use notify_watcher::NotifyWatcher;
pub use notify_watcher::PlatformWatcher;
pub use orphan_reaper::OrphanReaper;
pub use process_spawner::{
    IdentitySwitch, PlatformSpawner, SpawnPlatform, detect_spawn_platform, inherited_nofile_limits,
};
pub use procfs_metrics::PlatformMetricsProbe;
#[cfg(unix)]
pub use pty_backend::NixPtyBackend;
pub use pty_backend::PlatformPtyBackend;
pub use random::OsRandomSource;
pub use s3_store::{ABORT_BUDGET, CredentialsSource, EXECUTION_ROLE_PROFILE, S3ObjectStore};
pub use shell_ready_probe::{ReadyProbe, ShellReadyProbe};
#[cfg(unix)]
pub use sidecar_process::TokioSidecarLauncher;
pub use sidecar_process::{
    PlatformSidecarLauncher, kill_process_group, prepare_socket_root, signal_process_group,
};
pub use signed_http::{FilteringResolver, HyperSignedHttp, SignedHttpInitError};
pub use std_filesystem::PlatformFileSystem;
#[cfg(unix)]
pub use std_filesystem::StdFileSystem;
pub use tar_archiver::PlatformHomeArchiver;
#[cfg(unix)]
pub use tar_archiver::TarHomeArchiver;

/// The errno name of an I/O error (`EIO`), or its kind when the error
/// carries no errno; the only thing about a failed read that is logged.
#[must_use]
pub fn io_error_name(error: &std::io::Error) -> String {
    errno_name(error).unwrap_or_else(|| error.kind().to_string())
}

#[cfg(unix)]
fn errno_name(error: &std::io::Error) -> Option<String> {
    error
        .raw_os_error()
        .map(|code| format!("{:?}", nix::errno::Errno::from_raw(code)))
}

#[cfg(not(unix))]
fn errno_name(_error: &std::io::Error) -> Option<String> {
    None
}
