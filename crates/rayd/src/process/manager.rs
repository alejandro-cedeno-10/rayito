//! Application service behind `ProcessService`: turns a request into a
//! spawned child, runs the output pump, stdin, timeout and reaping for each
//! process, and keeps the shared registry consistent. Everything that
//! needs tokio lives here or in `runtime`; every rule lives in
//! `rayd_core::process`.

use std::collections::HashMap;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::Duration;

use bytes::Bytes;
use rayd_core::process::{
    EndReason, Pid, ProcessEnd, ProcessError, ProcessEvent, ProcessKind, ProcessRegistry,
    ProcessSummary, RegistryLimits, SpawnInput, SpawnSpec, UserLookup, UserPolicy, end_from_wait,
    plan_spawn, validate_signal,
};
use rayd_core::process::{OutputBudget, OutputStream, SpawnedChild, StdinMode};
use rayd_core::session::SandboxSession;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::task::{AbortHandle, JoinHandle};

use super::child::{ChildIo, ChildReader, ChildWriter, Spawner};
use super::runtime::{
    GroupSignaller, ProcessControl, SharedRegistry, deliver_end, ensure_directory, fan_out,
    lock_registry, mark_ended, signal_error, spawn_timeout_task,
};
use super::subscriber::{DEFAULT_STALL_TIMEOUT, SubscriberSink, SubscriberStream};
use crate::adapters::SpawnPlatform;
use crate::lifecycle::{DEFAULT_REAPER_INTERVAL, Reaper, spawn_reaper};

pub const OUTPUT_CHUNK_BYTES: usize = 32 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ManagerSettings {
    pub stall_timeout: Duration,
    pub reaper_interval: Duration,
}

impl Default for ManagerSettings {
    fn default() -> Self {
        Self {
            stall_timeout: DEFAULT_STALL_TIMEOUT,
            reaper_interval: DEFAULT_REAPER_INTERVAL,
        }
    }
}

/// The registry processes and PTYs share, created once per boot, with
/// rings bounded only by their own capacity.
#[must_use]
pub fn shared_registry(limits: RegistryLimits) -> SharedRegistry {
    Arc::new(Mutex::new(ProcessRegistry::new(limits)))
}

/// The shared registry whose rings charge the sandbox-wide output budget
/// (`main` hands the same budget to the code manager).
#[must_use]
pub fn shared_registry_with_budget(limits: RegistryLimits, budget: OutputBudget) -> SharedRegistry {
    Arc::new(Mutex::new(ProcessRegistry::with_budget(limits, budget)))
}

type SharedStdin = Arc<tokio::sync::Mutex<Option<ChildWriter>>>;

struct ProcessRuntime {
    stdin: SharedStdin,
    control: Arc<ProcessControl>,
    timeout_task: Option<AbortHandle>,
}

pub struct ProcessManager<S: Spawner> {
    session: Arc<SandboxSession>,
    spawner: S,
    lookup: Arc<dyn UserLookup>,
    policy: UserPolicy,
    settings: ManagerSettings,
    registry: SharedRegistry,
    runtimes: Mutex<HashMap<Pid, ProcessRuntime>>,
}

impl<S: Spawner> ProcessManager<S> {
    pub fn new(
        session: Arc<SandboxSession>,
        spawner: S,
        lookup: Arc<dyn UserLookup>,
        policy: UserPolicy,
        registry: SharedRegistry,
        settings: ManagerSettings,
    ) -> Arc<Self> {
        Arc::new(Self {
            session,
            spawner,
            lookup,
            policy,
            settings,
            registry,
            runtimes: Mutex::new(HashMap::new()),
        })
    }

    /// The reaper tick for the shared registry alone; `main` adds the
    /// execution registry through `lifecycle::spawn_reaper`.
    pub fn spawn_reaper(self: &Arc<Self>) -> JoinHandle<()> {
        let reaper: Arc<dyn Reaper> = self.clone();
        spawn_reaper(self.settings.reaper_interval, vec![reaper])
    }

    #[must_use]
    pub fn registry(&self) -> SharedRegistry {
        self.registry.clone()
    }

    /// Drops ended entries whose retention window closed, then, while the
    /// shared output budget sits above its high-water mark, the oldest
    /// ended ones regardless of age.
    pub fn reap_expired(&self) -> Vec<Pid> {
        let running_now = self.session.running_now();
        let mut registry = lock_registry(&self.registry);
        let mut expired = registry.reap_expired(running_now);
        let dropped = registry.drop_ended_over_budget();
        let level = registry.budget().level();
        drop(registry);
        if !dropped.is_empty() {
            tracing::warn!(
                output_budget_bytes = level,
                entries_dropped = dropped.len(),
                "retained processes dropped for the output budget"
            );
        }
        if !expired.is_empty() {
            tracing::debug!(reaped = expired.len(), "retained processes expired");
        }
        expired.extend(dropped);
        expired
    }

    pub async fn start(
        self: &Arc<Self>,
        input: SpawnInput,
    ) -> Result<(Pid, SubscriberStream), ProcessError> {
        self.session.accepts_new_streams()?;
        let defaults = self.session.spawn_defaults();
        let spec = plan_spawn(&input, &defaults, self.policy, self.lookup.as_ref())?;
        ensure_directory(&spec.cwd).await?;
        let started_at = self.session.running_now();
        let (mut child, pid, stream) = self.spawn_registered(&spec, input.config, input.tag)?;
        let control = Arc::new(ProcessControl::default());
        let stdin = Arc::new(tokio::sync::Mutex::new(child.take_stdin()));
        let stdout = child.take_stdout();
        let stderr = child.take_stderr();
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
            ProcessRuntime {
                stdin,
                control: control.clone(),
                timeout_task,
            },
        );
        tokio::spawn(
            self.clone()
                .supervise(pid, child, stdout, stderr, control, started_at),
        );
        tracing::info!(
            pid = pid.0,
            live_processes = lock_registry(&self.registry).live_count(),
            "process started"
        );
        Ok((pid, stream))
    }

    pub fn connect(&self, pid: Pid, from_seq: u64) -> Result<SubscriberStream, ProcessError> {
        self.session.accepts_new_streams()?;
        let (sink, receiver) = SubscriberSink::open(self.settings.stall_timeout);
        let attachment = {
            let mut registry = lock_registry(&self.registry);
            registry.ensure_kind(pid, ProcessKind::Process)?;
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

    /// Returns once the bytes are written and flushed, which is the stdin
    /// backpressure a client sees.
    pub async fn send_input(&self, pid: Pid, data: Bytes) -> Result<(), ProcessError> {
        let stdin = self.stdin_handle(pid)?;
        let mut guard = stdin.lock().await;
        let writer = guard.as_mut().ok_or(ProcessError::StdinClosed { pid })?;
        let written = async {
            writer.write_all(&data).await?;
            writer.flush().await
        }
        .await;
        written.map_err(|error| stdin_error(pid, &error))
    }

    /// Idempotent: closing an already closed pipe is fine.
    pub async fn close_stdin(&self, pid: Pid) -> Result<(), ProcessError> {
        let stdin = self.stdin_handle(pid)?;
        let mut guard = stdin.lock().await;
        drop(guard.take());
        Ok(())
    }

    /// Works on both kinds (E2B's `commands.kill(pid)` also kills a PTY);
    /// the end reason is recorded on the entry that owns the pid.
    pub fn send_signal(&self, pid: Pid, signal: i32) -> Result<(), ProcessError> {
        let signal = validate_signal(signal)?;
        if !lock_registry(&self.registry).is_live(pid) {
            return Err(ProcessError::NotFound { pid });
        }
        if let Some(runtime) = self.runtimes().get(&pid) {
            runtime.control.record(EndReason::Signal);
        }
        self.spawner
            .signal_group(pid, signal)
            .map_err(|error| signal_error(pid, error))
    }

    #[must_use]
    pub fn list(&self) -> Vec<ProcessSummary> {
        lock_registry(&self.registry).live()
    }

    fn group_signaller(self: &Arc<Self>) -> GroupSignaller {
        let manager = self.clone();
        Arc::new(move |pid, signal| manager.spawner.signal_group(pid, signal))
    }

    /// Capacity check, `fork`/`exec`, registration and the first subscriber
    /// happen under one lock so a refused `Start` never spawns and no event
    /// can slip between the spawn and its subscriber.
    fn spawn_registered(
        &self,
        spec: &SpawnSpec,
        config: rayd_core::process::ProcessConfigInfo,
        tag: Option<String>,
    ) -> Result<(S::Child, Pid, SubscriberStream), ProcessError> {
        let mut registry = lock_registry(&self.registry);
        registry.ensure_capacity()?;
        let child = self.spawner.spawn(spec)?;
        let pid = child.pid();
        registry.register(pid, ProcessKind::Process, config, tag, spec.stdin)?;
        let (sink, receiver) = SubscriberSink::open(self.settings.stall_timeout);
        registry.attach(pid, 0, sink)?;
        let stream = SubscriberStream::new(vec![ProcessEvent::Started { pid }], Some(receiver));
        Ok((child, pid, stream))
    }

    async fn supervise(
        self: Arc<Self>,
        pid: Pid,
        mut child: S::Child,
        stdout: Option<ChildReader>,
        stderr: Option<ChildReader>,
        control: Arc<ProcessControl>,
        started_at: Duration,
    ) {
        self.pump_output(pid, stdout, stderr).await;
        let outcome = child.wait().await;
        control.mark_reaped();
        let end = match outcome {
            Ok(outcome) => end_from_wait(outcome, control.reason()),
            Err(error) => {
                tracing::warn!(pid = pid.0, reason = %error.kind(), "wait failed");
                ProcessEnd::wait_failed()
            }
        };
        self.finish(pid, end, started_at).await;
    }

    /// One pump for both pipes so `seq` order is also delivery order: each
    /// chunk is pushed into the ring and fanned out before the next read.
    async fn pump_output(
        &self,
        pid: Pid,
        mut stdout: Option<ChildReader>,
        mut stderr: Option<ChildReader>,
    ) {
        let mut stdout_buffer = vec![0u8; OUTPUT_CHUNK_BYTES];
        let mut stderr_buffer = vec![0u8; OUTPUT_CHUNK_BYTES];
        loop {
            let (stream, read) = tokio::select! {
                read = read_chunk(&mut stdout, &mut stdout_buffer), if stdout.is_some() => {
                    (OutputStream::Stdout, read)
                }
                read = read_chunk(&mut stderr, &mut stderr_buffer), if stderr.is_some() => {
                    (OutputStream::Stderr, read)
                }
                else => break,
            };
            let buffer = match stream {
                OutputStream::Stdout => &stdout_buffer,
                OutputStream::Stderr => &stderr_buffer,
            };
            match read {
                Ok(length) if length > 0 => {
                    let bytes = Bytes::copy_from_slice(&buffer[..length]);
                    fan_out(&self.registry, pid, stream, bytes).await;
                }
                _ => match stream {
                    OutputStream::Stdout => stdout = None,
                    OutputStream::Stderr => stderr = None,
                },
            }
        }
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
            "process ended"
        );
        deliver_end(sinks, &end).await;
    }

    fn stdin_handle(&self, pid: Pid) -> Result<SharedStdin, ProcessError> {
        let mode = {
            let registry = lock_registry(&self.registry);
            registry.ensure_kind(pid, ProcessKind::Process)?;
            registry.stdin_mode(pid)?
        };
        match mode {
            StdinMode::Null => Err(ProcessError::StdinNotOpen { pid }),
            StdinMode::Pipe => self
                .runtimes()
                .get(&pid)
                .map(|runtime| runtime.stdin.clone())
                .ok_or(ProcessError::NotFound { pid }),
        }
    }

    fn runtimes(&self) -> MutexGuard<'_, HashMap<Pid, ProcessRuntime>> {
        self.runtimes.lock().unwrap_or_else(PoisonError::into_inner)
    }
}

impl<S: Spawner> Reaper for ProcessManager<S> {
    fn reap_expired(&self) {
        ProcessManager::reap_expired(self);
    }
}

/// Builds the manager for the host `rayd` runs on (`detect_spawn_platform`)
/// over a registry the PTY manager shares.
pub fn platform_manager(
    session: Arc<SandboxSession>,
    platform: SpawnPlatform,
    policy: UserPolicy,
    registry: SharedRegistry,
    settings: ManagerSettings,
) -> Arc<ProcessManager<crate::adapters::PlatformSpawner>> {
    ProcessManager::new(
        session,
        platform.spawner,
        platform.lookup,
        policy,
        registry,
        settings,
    )
}

async fn read_chunk(reader: &mut Option<ChildReader>, buffer: &mut [u8]) -> std::io::Result<usize> {
    match reader {
        Some(reader) => reader.read(buffer).await,
        None => Ok(0),
    }
}

fn stdin_error(pid: Pid, error: &std::io::Error) -> ProcessError {
    if error.kind() == std::io::ErrorKind::BrokenPipe {
        ProcessError::StdinClosed { pid }
    } else {
        ProcessError::Internal {
            operation: "stdin write",
            reason: error.kind().to_string(),
        }
    }
}
