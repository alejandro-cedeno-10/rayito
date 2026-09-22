//! Adapters of the `rayd` agent: the gRPC services (tonic), the AWS
//! lifecycle hooks (axum), the process and PTY managers with their pumps,
//! the filesystem manager with its read, write and watch drivers, the code
//! manager with its sidecar supervisor and execution recorders, the
//! persistence manager over the S3 and tar adapters (ADR-009), the
//! suspend broadcast and running-clock sleep, and the operating-system
//! adapters, all over the `rayd-core` domain. `main.rs` binds them to
//! sockets; integration tests drive them in-process.

pub mod adapters;
pub mod code;
pub mod filesystem;
pub mod grpc;
pub mod hooks;
pub mod lifecycle;
pub mod logging;
pub mod persistence;
pub mod process;
pub mod pty;

pub const AGENT_VERSION: &str = env!("CARGO_PKG_VERSION");
