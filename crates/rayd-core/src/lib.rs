//! Domain of the `rayd` agent: what a `MicroVM` boot is, how the six AWS
//! lifecycle hooks move it between phases, how the sandbox secret is
//! verified, what `Health` and `Metrics` report, how processes are
//! planned, sequenced and retained, how files are read, written, listed
//! and watched, how an interactive terminal is planned, how code runs in a
//! kernel behind the sidecar, how deadlines exclude suspended time, how
//! the user's home is checkpointed to and restored from an object store
//! (`persistence`), and what the guest's capability mask allows
//! (`capabilities`). No
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
pub mod persistence;
pub mod process;
pub mod pty;
pub mod run_payload;
pub mod session;
