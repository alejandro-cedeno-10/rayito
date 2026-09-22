//! Code-execution application layer of `rayd`: the sidecar supervisor, the
//! manager behind `CodeService` and the hooks, the execution recorders
//! with their subscriber streams and the `/validate` runner, all over the
//! `rayd-core` code domain.

pub mod execute;
pub mod executions;
#[cfg(test)]
mod fake_sidecar;
pub mod manager;
pub mod supervisor;
pub mod validate;

pub use execute::{ExecutionSubscriberStream, InFlightGuard};
pub use executions::{ExecuteReceiver, ExecuteSink, ExecutionRecorder, InterruptHandle};
pub use manager::{
    CodeManager, CodeSettings, ExecuteInput, platform_code_manager, sidecar_identity,
};
pub use supervisor::{
    ExecutionHandle, KernelKiller, OpTimeouts, SidecarSupervisor, SupervisorSettings,
};
pub use validate::{VALIDATE_TIMEOUT, run_validation};
