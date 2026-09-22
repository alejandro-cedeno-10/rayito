//! What processes and PTYs share at runtime (design D4): the registry
//! behind one mutex, the fan-out of an output chunk to its subscribers
//! (ring first, then every sink, stalled ones detached), the terminal event
//! delivery, the end-reason record and the server timeout task measured on
//! the running clock (design D6).

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::Duration;

use bytes::Bytes;
use rayd_core::process::{
    CwdRejection, EndReason, OutputStream, Pid, ProcessEnd, ProcessError, ProcessEvent,
    ProcessRegistry, SignalError, SubscriberId, TimeoutPlan, TimeoutStep,
};
use rayd_core::session::SandboxSession;
use tokio::task::AbortHandle;

use super::subscriber::{Delivery, SubscriberSink};
use crate::lifecycle::running_sleep;

pub type SharedRegistry = Arc<Mutex<ProcessRegistry<SubscriberSink>>>;
/// `killpg` for one kind of child, injected so the timeout task is the
/// same for processes and PTYs.
pub type GroupSignaller = Arc<dyn Fn(Pid, i32) -> Result<(), SignalError> + Send + Sync>;

pub fn lock_registry(registry: &SharedRegistry) -> MutexGuard<'_, ProcessRegistry<SubscriberSink>> {
    registry.lock().unwrap_or_else(PoisonError::into_inner)
}

/// Shared between the supervisor, the timeout task and `SendSignal`/`Kill`:
/// why the process is ending and whether it has already been reaped.
#[derive(Debug, Default)]
pub struct ProcessControl {
    reason: Mutex<EndReason>,
    reaped: AtomicBool,
}

impl ProcessControl {
    #[must_use]
    pub fn reason(&self) -> EndReason {
        *self.reason.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// A timeout outranks a client signal: once the deadline fired the end is
    /// reported as `timeout` whatever else hits the process.
    pub fn record(&self, reason: EndReason) {
        let mut current = self.reason.lock().unwrap_or_else(PoisonError::into_inner);
        if *current != EndReason::Timeout {
            *current = reason;
        }
    }

    #[must_use]
    pub fn reaped(&self) -> bool {
        self.reaped.load(Ordering::SeqCst)
    }

    pub fn mark_reaped(&self) {
        self.reaped.store(true, Ordering::SeqCst);
    }
}

/// Pushes the chunk into the pid's ring and delivers it to every subscriber
/// in `seq` order; a stalled or closed subscriber is detached.
pub async fn fan_out(registry: &SharedRegistry, pid: Pid, stream: OutputStream, bytes: Bytes) {
    let (event, sinks) = {
        let mut registry = lock_registry(registry);
        match registry.push_output(pid, stream, bytes) {
            Ok(event) => (event, registry.subscribers(pid)),
            Err(_) => return,
        }
    };
    for (id, sink) in sinks {
        match sink.deliver(ProcessEvent::Output(event.clone())).await {
            Delivery::Delivered => {}
            Delivery::Stalled => {
                tracing::warn!(
                    pid = pid.0,
                    seq = event.seq,
                    "subscriber stalled; stream truncated"
                );
                lock_registry(registry).detach(pid, id);
            }
            Delivery::Closed => lock_registry(registry).detach(pid, id),
        }
    }
}

/// Records the end on the running clock and hands back the subscribers to
/// deliver it to.
pub fn mark_ended(
    registry: &SharedRegistry,
    pid: Pid,
    end: ProcessEnd,
    running_now: Duration,
) -> Vec<(SubscriberId, SubscriberSink)> {
    lock_registry(registry)
        .mark_ended(pid, end, running_now)
        .unwrap_or_default()
}

pub async fn deliver_end(sinks: Vec<(SubscriberId, SubscriberSink)>, end: &ProcessEnd) {
    for (_, sink) in sinks {
        let _ = sink.deliver(ProcessEvent::Ended(end.clone())).await;
    }
}

/// SIGTERM to the group when the running-clock deadline is due, SIGKILL
/// after the grace if the child is still there. A pause between arming and
/// firing leaves the remaining budget untouched.
pub fn spawn_timeout_task(
    session: Arc<SandboxSession>,
    pid: Pid,
    timeout: Duration,
    control: Arc<ProcessControl>,
    signal: GroupSignaller,
) -> AbortHandle {
    let plan = TimeoutPlan::new(session.running_now(), timeout);
    tokio::spawn(async move {
        running_sleep(&session, plan.term_at).await;
        if control.reaped() {
            return;
        }
        control.record(EndReason::Timeout);
        deliver_timeout_signal(&signal, pid, TimeoutStep::Term);
        running_sleep(&session, plan.kill_at).await;
        if !control.reaped() {
            deliver_timeout_signal(&signal, pid, TimeoutStep::Kill);
        }
    })
    .abort_handle()
}

fn deliver_timeout_signal(signal: &GroupSignaller, pid: Pid, step: TimeoutStep) {
    match signal(pid, step.signal()) {
        Ok(()) => tracing::info!(pid = pid.0, step = step.as_str(), "timeout signal sent"),
        Err(error) => tracing::warn!(
            pid = pid.0,
            step = step.as_str(),
            reason = %error,
            "timeout signal failed"
        ),
    }
}

pub async fn ensure_directory(path: &str) -> Result<(), ProcessError> {
    match tokio::fs::metadata(path).await {
        Ok(metadata) if metadata.is_dir() => Ok(()),
        _ => Err(ProcessError::InvalidCwd(CwdRejection::NotADirectory)),
    }
}

#[must_use]
pub fn signal_error(pid: Pid, error: SignalError) -> ProcessError {
    match error {
        SignalError::NoSuchProcess => ProcessError::NotFound { pid },
        SignalError::Failed(reason) => ProcessError::Internal {
            operation: "signal",
            reason,
        },
    }
}
