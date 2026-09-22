//! Application service behind `PtyService` (design D4): turns a `PtyStart`
//! into a login shell on a fresh pseudo-terminal, pumps the master side in
//! 16 KiB chunks into the registry shared with plain processes, keeps the
//! writer and the resize handle per pid, applies the server timeout on the
//! running clock and records the end like a process. Every rule lives in
//! `rayd_core::pty` and `rayd_core::process`.

use std::collections::HashMap;
use std::path::Path;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::Duration;

use bytes::Bytes;
use rayd_core::process::{
    EndReason, OutputStream, Pid, ProcessEnd, ProcessError, ProcessEvent, ProcessKind,
    ProcessSummary, StdinMode, UserLookup, UserPolicy, end_from_wait,
};
use rayd_core::pty::{PTY_CHUNK_BYTES, PTY_DRAIN_GRACE, PtyChild, PtyError, PtySize, plan_pty};
use rayd_core::pty::{PtyPlan, PtySpawnInput};
use rayd_core::session::SandboxSession;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::task::AbortHandle;

use super::child::{PtyIo, PtyResizer, PtySpawner};
use crate::adapters::SpawnPlatform;
use crate::process::runtime::{
    GroupSignaller, ProcessControl, SharedRegistry, deliver_end, ensure_directory, fan_out,
    lock_registry, mark_ended, signal_error, spawn_timeout_task,
};
use crate::process::subscriber::{DEFAULT_STALL_TIMEOUT, SubscriberSink, SubscriberStream};
use crate::process::{ChildReader, ChildWriter};

pub const PTY_DEVICE_PATH: &str = "/dev/ptmx";
pub const SIGKILL: i32 = 9;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PtySettings {
    pub stall_timeout: Duration,
    pub drain_grace: Duration,
}

impl Default for PtySettings {
    fn default() -> Self {
        Self {
            stall_timeout: DEFAULT_STALL_TIMEOUT,
            drain_grace: PTY_DRAIN_GRACE,
        }
    }
}

type SharedWriter = Arc<tokio::sync::Mutex<Option<ChildWriter>>>;

struct PtyRuntime {
    writer: SharedWriter,
    resizer: Arc<dyn PtyResizer>,
    control: Arc<ProcessControl>,
    timeout_task: Option<AbortHandle>,
}

pub struct PtyManager<B: PtySpawner> {
    session: Arc<SandboxSession>,
    backend: B,
    lookup: Arc<dyn UserLookup>,
    policy: UserPolicy,
    registry: SharedRegistry,
    settings: PtySettings,
    pty_devices: bool,
    runtimes: Mutex<HashMap<Pid, PtyRuntime>>,
}

impl<B: PtySpawner> PtyManager<B> {
    pub fn new(
        session: Arc<SandboxSession>,
        backend: B,
        lookup: Arc<dyn UserLookup>,
        policy: UserPolicy,
        registry: SharedRegistry,
        settings: PtySettings,
    ) -> Arc<Self> {
        Arc::new(Self {
            session,
            backend,
            lookup,
            policy,
            registry,
            settings,
            pty_devices: pty_devices_present(),
            runtimes: Mutex::new(HashMap::new()),
        })
    }

    /// Whether the guest exposes `/dev/ptmx` (logged at boot, refused per
    /// request with `NoPtyDevices` otherwise).
    #[must_use]
    pub fn pty_devices(&self) -> bool {
        self.pty_devices
    }

    pub async fn create(
        self: &Arc<Self>,
        input: PtySpawnInput,
    ) -> Result<(Pid, SubscriberStream), PtyError> {
        self.session.accepts_new_streams()?;
        let defaults = self.session.spawn_defaults();
        let plan = plan_pty(&input, &defaults, self.policy, self.lookup.as_ref())?;
        ensure_directory(&plan.spec.cwd).await?;
        if !self.pty_devices {
            return Err(PtyError::NoPtyDevices);
        }
        let started_at = self.session.running_now();
        let size = plan.size;
        let (mut pty, pid, stream) = self.open_registered(plan)?;
        let control = Arc::new(ProcessControl::default());
        let writer = Arc::new(tokio::sync::Mutex::new(pty.take_writer()));
        let reader = pty.take_reader();
        let timeout_task = input.timeout.map(|timeout| {
            spawn_timeout_task(
                self.session.clone(),
                pid,
                timeout,
                control.clone(),
                self.group_signaller(),
            )
        });
        self.runtimes().insert(
            pid,
            PtyRuntime {
                writer: writer.clone(),
                resizer: pty.resize_handle(),
                control: control.clone(),
                timeout_task,
            },
        );
        tokio::spawn(
            self.clone()
                .supervise(pid, pty, reader, writer, control, started_at),
        );
        tracing::info!(
            pid = pid.0,
            cols = size.cols(),
            rows = size.rows(),
            live_processes = lock_registry(&self.registry).live_count(),
            "pty started"
        );
        Ok((pid, stream))
    }

    pub fn connect(&self, pid: Pid, from_seq: u64) -> Result<SubscriberStream, PtyError> {
        self.session.accepts_new_streams()?;
        let (sink, receiver) = SubscriberSink::open(self.settings.stall_timeout);
        let attachment = {
            let mut registry = lock_registry(&self.registry);
            registry.ensure_kind(pid, ProcessKind::Pty)?;
            registry.attach(pid, from_seq, sink)?
        };
        let mut head = vec![ProcessEvent::Started { pid }];
        head.extend(attachment.replay.into_iter().map(ProcessEvent::Output));
        if let Some(end) = attachment.end {
            head.push(ProcessEvent::Ended(end));
        }
        let live = attachment.subscriber.map(|_| receiver);
        Ok(SubscriberStream::new(head, live))
    }

    /// Returns once the bytes reached the master side; a PTY whose shell
    /// exited answers `NotFound`.
    pub async fn send_input(&self, pid: Pid, data: Bytes) -> Result<(), PtyError> {
        let writer = self.runtime_of(pid, |runtime| runtime.writer.clone())?;
        let mut guard = writer.lock().await;
        let writer = guard
            .as_mut()
            .ok_or(PtyError::Process(ProcessError::NotFound { pid }))?;
        let written = async {
            writer.write_all(&data).await?;
            writer.flush().await
        }
        .await;
        written.map_err(|error| io_failure(pid, "pty write", &error))
    }

    pub fn resize(&self, pid: Pid, cols: u32, rows: u32) -> Result<(), PtyError> {
        let resizer = self.runtime_of(pid, |runtime| runtime.resizer.clone())?;
        let size = PtySize::new(cols, rows)?;
        resizer
            .resize(size)
            .map_err(|error| io_failure(pid, "resize", &error))?;
        tracing::info!(
            pid = pid.0,
            cols = size.cols(),
            rows = size.rows(),
            "pty resized"
        );
        Ok(())
    }

    /// `SIGKILL` to the shell's session; the end is reported as `signaled`.
    pub fn kill(&self, pid: Pid) -> Result<(), PtyError> {
        let control = self.runtime_of(pid, |runtime| runtime.control.clone())?;
        control.record(EndReason::Signal);
        self.backend
            .signal_group(pid, SIGKILL)
            .map_err(|error| PtyError::Process(signal_error(pid, error)))
    }

    #[must_use]
    pub fn list(&self) -> Vec<ProcessSummary> {
        lock_registry(&self.registry)
            .live()
            .into_iter()
            .filter(|summary| summary.kind == ProcessKind::Pty)
            .collect()
    }

    fn group_signaller(self: &Arc<Self>) -> GroupSignaller {
        let manager = self.clone();
        Arc::new(move |pid, signal| manager.backend.signal_group(pid, signal))
    }

    /// Capacity check, `openpty` + spawn, registration and the first
    /// subscriber under one lock, as for processes.
    fn open_registered(&self, plan: PtyPlan) -> Result<(B::Pty, Pid, SubscriberStream), PtyError> {
        let PtyPlan { spec, size, config } = plan;
        let mut registry = lock_registry(&self.registry);
        registry.ensure_capacity()?;
        let pty = self
            .backend
            .open(&spec, size)
            .map_err(ProcessError::Spawn)?;
        let pid = pty.pid();
        registry.register(pid, ProcessKind::Pty, config, None, StdinMode::Pipe)?;
        let (sink, receiver) = SubscriberSink::open(self.settings.stall_timeout);
        registry.attach(pid, 0, sink)?;
        let stream = SubscriberStream::new(vec![ProcessEvent::Started { pid }], Some(receiver));
        Ok((pty, pid, stream))
    }

    /// Pumps the master until every slave closed (EOF/EIO) or the shell
    /// exited; a background child still holding the slave gets the drain
    /// grace, then the master is closed and the end recorded.
    async fn supervise(
        self: Arc<Self>,
        pid: Pid,
        mut pty: B::Pty,
        reader: Option<ChildReader>,
        writer: SharedWriter,
        control: Arc<ProcessControl>,
        started_at: Duration,
    ) {
        let registry = self.registry.clone();
        let outcome = {
            let mut pump = std::pin::pin!(pump_master(&registry, pid, reader));
            let mut wait = std::pin::pin!(pty.wait());
            tokio::select! {
                () = &mut pump => wait.await,
                outcome = &mut wait => {
                    let _ = tokio::time::timeout(self.settings.drain_grace, &mut pump).await;
                    outcome
                }
            }
        };
        control.mark_reaped();
        drop(writer.lock().await.take());
        let end = match outcome {
            Ok(outcome) => end_from_wait(outcome, control.reason()),
            Err(error) => {
                tracing::warn!(pid = pid.0, reason = %error.kind(), "pty wait failed");
                ProcessEnd::wait_failed()
            }
        };
        drop(pty);
        self.finish(pid, end, started_at).await;
    }

    async fn finish(&self, pid: Pid, end: ProcessEnd, started_at: Duration) {
        let now = self.session.running_now();
        let sinks = mark_ended(&self.registry, pid, end.clone(), now);
        if let Some(timeout_task) = self
            .runtimes()
            .remove(&pid)
            .and_then(|runtime| runtime.timeout_task)
        {
            timeout_task.abort();
        }
        tracing::info!(
            pid = pid.0,
            status = end.status.as_str(),
            exit_code = end.exit_code,
            signal = end.signal,
            duration_ms = now.saturating_sub(started_at).as_millis(),
            subscribers = sinks.len(),
            live_processes = lock_registry(&self.registry).live_count(),
            "pty ended"
        );
        deliver_end(sinks, &end).await;
    }

    /// Kind check first, so a plain process answers `FAILED_PRECONDITION`
    /// and an ended or unknown PTY `NOT_FOUND`.
    fn runtime_of<T>(&self, pid: Pid, pick: impl FnOnce(&PtyRuntime) -> T) -> Result<T, PtyError> {
        {
            let registry = lock_registry(&self.registry);
            registry.ensure_kind(pid, ProcessKind::Pty)?;
            if !registry.is_live(pid) {
                return Err(PtyError::Process(ProcessError::NotFound { pid }));
            }
        }
        self.runtimes()
            .get(&pid)
            .map(pick)
            .ok_or(PtyError::Process(ProcessError::NotFound { pid }))
    }

    fn runtimes(&self) -> MutexGuard<'_, HashMap<Pid, PtyRuntime>> {
        self.runtimes.lock().unwrap_or_else(PoisonError::into_inner)
    }
}

/// Builds the manager for the host `rayd` runs on, sharing the lookup and
/// identity switch of the process platform and the process registry.
pub fn platform_pty_manager(
    session: Arc<SandboxSession>,
    platform: &SpawnPlatform,
    policy: UserPolicy,
    registry: SharedRegistry,
    settings: PtySettings,
) -> Arc<PtyManager<crate::adapters::PlatformPtyBackend>> {
    PtyManager::new(
        session,
        crate::adapters::PlatformPtyBackend::new(platform.identity_switch),
        platform.lookup.clone(),
        policy,
        registry,
        settings,
    )
}

#[must_use]
pub fn pty_devices_present() -> bool {
    Path::new(PTY_DEVICE_PATH).exists()
}

/// One read at a time, each at most `PTY_CHUNK_BYTES`, pushed and fanned
/// out before the next; ends at EOF (every slave closed).
async fn pump_master(registry: &SharedRegistry, pid: Pid, reader: Option<ChildReader>) {
    let Some(mut reader) = reader else {
        return;
    };
    let mut buffer = vec![0u8; PTY_CHUNK_BYTES];
    loop {
        match reader.read(&mut buffer).await {
            Ok(length) if length > 0 => {
                let bytes = Bytes::copy_from_slice(&buffer[..length]);
                fan_out(registry, pid, OutputStream::Stdout, bytes).await;
            }
            _ => return,
        }
    }
}

fn io_failure(pid: Pid, operation: &'static str, error: &std::io::Error) -> PtyError {
    match error.kind() {
        std::io::ErrorKind::BrokenPipe | std::io::ErrorKind::NotFound => {
            PtyError::Process(ProcessError::NotFound { pid })
        }
        kind => PtyError::Process(ProcessError::Internal {
            operation,
            reason: kind.to_string(),
        }),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn settings_default_to_the_contract_values() {
        let settings = PtySettings::default();
        assert_eq!(settings.stall_timeout, Duration::from_secs(30));
        assert_eq!(settings.drain_grace, Duration::from_millis(500));
    }

    #[test]
    fn write_failures_map_to_not_found_or_internal() {
        let gone = io_failure(
            Pid(1),
            "pty write",
            &std::io::Error::from(std::io::ErrorKind::BrokenPipe),
        );
        assert_eq!(
            gone,
            PtyError::Process(ProcessError::NotFound { pid: Pid(1) })
        );
        let other = io_failure(Pid(1), "resize", &std::io::Error::other("boom"));
        assert!(matches!(
            other,
            PtyError::Process(ProcessError::Internal {
                operation: "resize",
                ..
            })
        ));
    }
}
