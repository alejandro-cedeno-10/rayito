//! Domain of the `rayd` agent: what a `MicroVM` boot is, how the six AWS
//! lifecycle hooks move it between phases, how the sandbox secret is
//! verified, what `Health` and `Metrics` report (and the 5 s metrics
//! history `MetricsHistory` serves), how processes are
//! planned, sequenced and retained, how files are read, written, listed
//! and watched, how an interactive terminal is planned, how code runs in a
//! kernel behind the sidecar, how deadlines exclude suspended time, the
//! logical sandbox deadline (`sandbox_timeout`), how
//! the user's home is checkpointed to and restored from an object store
//! (`persistence`), the guest egress policy and its local proxy
//! (`network`, ADR-012), how files move through presigned S3 URLs without
//! a credential in the VM (`transfer`, ADR-010), and what the guest's
//! capability mask allows (`capabilities`), and which filesystems a
//! `/suspend` syncs inside what deadline (`suspend_sync`). The tokens the SDKs parse in
//! the agent's messages, which are otherwise Spanish, live in
//! `wire_tokens`. No
//! transport types live here; the adapters in the `rayd` crate translate
//! gRPC and HTTP into these calls.
//!
//! M15 (Rayito 0.6) adds the shared domain of the optional features:
//! which zombies PID 1 may reap (`orphans`), the `ConfigureSandbox`
//! contract (`configure`) and the capability flags `Health` reports
//! (`features`, `root_egress`), and the credential-lease freshness rule
//! shared by every feature that needs the execution role inside the guest
//! (`credentials`). Each feature's own domain lives in its own module,
//! added by that feature: `s3_mount` (`m15-s3-mounts`) is the first one,
//! `telemetry` (m15-rayd-otlp, ADR-021) is rayd's own OTLP/HTTP metrics
//! exporter.
//!
//! The domain is safe Rust by construction: every `unsafe` block (FFI to
//! `libc`/`nix`, file descriptors, `fork`) belongs to an adapter in the
//! `rayd` crate, so `forbid(unsafe_code)` keeps it from creeping in here.

#![forbid(unsafe_code)]

pub mod auth;
pub mod capabilities;
pub mod clock;
pub mod code;
pub mod configure;
pub mod credentials;
pub mod features;
pub mod filesystem;
pub mod health;
pub mod hook_origin;
pub mod hooks;
pub mod lifecycle;
pub mod lifecycle_events;
pub mod listeners;
pub mod metrics;
pub mod metrics_history;
pub mod mount_path;
pub mod network;
pub mod orphans;
pub mod persistence;
pub mod process;
pub mod pty;
pub mod root_egress;
pub mod run_payload;
pub mod s3_mount;
pub mod sandbox_timeout;
pub mod secret_gateway;
pub mod session;
pub mod suspend_sync;
pub mod telemetry;
pub mod template;
pub mod transfer;
pub mod wire_tokens;
