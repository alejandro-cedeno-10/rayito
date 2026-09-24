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
//! capability mask allows (`capabilities`). No
//! transport types live here; the adapters in the `rayd` crate translate
//! gRPC and HTTP into these calls.

pub mod auth;
pub mod capabilities;
pub mod clock;
pub mod code;
pub mod filesystem;
pub mod health;
pub mod hooks;
pub mod lifecycle;
pub mod metrics;
pub mod metrics_history;
pub mod network;
pub mod persistence;
pub mod process;
pub mod pty;
pub mod run_payload;
pub mod sandbox_timeout;
pub mod session;
pub mod transfer;
