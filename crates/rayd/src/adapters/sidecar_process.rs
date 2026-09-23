//! The kernel sidecar as a child process (design D8): spawned with the M2
//! `PreExecPlan` (own session, limits, identity switch), stdin fed by a
//! writer task from a bounded queue, stdout decoded line by line into
//! `SidecarEvent`s and awaited into the sink (real backpressure), stderr
//! parsed as the sidecar's JSON log and re-emitted with allowlisted fields
//! only, exit reported once. Off Linux the launcher answers `Unsupported`.

/// Fields of the sidecar's JSON log lines that `rayd` re-emits; anything
/// else is dropped so cell content never reaches `rayd`'s log
/// (design D16).
pub const SIDECAR_LOG_FIELDS: [&str; 29] = [
    "op",
    "id",
    "context_id",
    "execution_id",
    "execution_count",
    "events",
    "results",
    "mime_types",
    "timeout_ms",
    "outcome",
    "kernel_pid",
    "attempt",
    "backoff_ms",
    "exit_code",
    "warmup_ms",
    "restart_ms",
    "duration_ms",
    "bytes",
    "chunks",
    "contexts",
    "reseeded",
    "deferred",
    "failed",
    "code_len",
    "text_len",
    "reason",
    "state",
    "queued",
    "sidecar_restarts",
];

/// Longest stderr line parsed as a log record; longer ones only count.
pub const MAX_SIDECAR_LOG_LINE_BYTES: usize = 64 * 1024;

/// A sidecar stderr line reduced to what may be logged: the level, the
/// fixed `msg` literal and the allowlisted fields as compact JSON.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SidecarLogRecord {
    pub level: String,
    pub msg: String,
    pub fields: String,
}

/// `None` when the line is not the sidecar's log schema (plain text, a
/// traceback, a partial line): counted, never echoed.
#[must_use]
pub fn parse_sidecar_log_line(line: &str) -> Option<SidecarLogRecord> {
    let value: serde_json::Value = serde_json::from_str(line).ok()?;
    let object = value.as_object()?;
    let level = object.get("level")?.as_str()?.to_owned();
    let msg = object.get("msg")?.as_str()?.to_owned();
    let mut fields = serde_json::Map::new();
    for (key, value) in object {
        if SIDECAR_LOG_FIELDS.contains(&key.as_str()) {
            fields.insert(key.clone(), value.clone());
        }
    }
    Some(SidecarLogRecord {
        level,
        msg,
        fields: serde_json::Value::Object(fields).to_string(),
    })
}

pub fn emit_sidecar_log(record: &SidecarLogRecord) {
    match record.level.as_str() {
        "error" => tracing::error!(source = "sidecar", msg = %record.msg, fields = %record.fields),
        "warn" => tracing::warn!(source = "sidecar", msg = %record.msg, fields = %record.fields),
        "debug" => tracing::debug!(source = "sidecar", msg = %record.msg, fields = %record.fields),
        _ => tracing::info!(source = "sidecar", msg = %record.msg, fields = %record.fields),
    }
}

#[cfg(unix)]
pub use unix::{
    TokioSidecarLauncher, TokioSidecarLink, kill_process_group, prepare_socket_root,
    signal_process_group,
};
#[cfg(unix)]
pub type PlatformSidecarLauncher = unix::TokioSidecarLauncher;

#[cfg(not(unix))]
pub use unsupported::{
    UnsupportedSidecarLauncher, kill_process_group, prepare_socket_root, signal_process_group,
};
#[cfg(not(unix))]
pub type PlatformSidecarLauncher = unsupported::UnsupportedSidecarLauncher;

#[cfg(unix)]
mod unix {
    use std::io;
    use std::os::unix::fs::{PermissionsExt, chown};
    use std::path::Path;
    use std::process::Stdio;
    use std::sync::Arc;
    use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};

    use nix::sys::signal::{Signal, killpg};
    use rayd_core::code::{
        KernelSidecar, MAX_SIDECAR_LINE_BYTES, ProtocolError, SidecarEventSink, SidecarExitSink,
        SidecarIoError, SidecarLink, decode_event,
    };
    use rayd_core::process::{Pid, ProcessIdentity, SpawnError, SpawnSpec};
    use tokio::io::AsyncWriteExt;
    use tokio::process::Command;
    use tokio::sync::mpsc;
    use tokio_stream::StreamExt;
    use tokio_util::codec::{FramedRead, LinesCodec, LinesCodecError};

    use super::{MAX_SIDECAR_LOG_LINE_BYTES, emit_sidecar_log, parse_sidecar_log_line};
    use crate::adapters::process_spawner::PreExecPlan;
    use crate::adapters::{IdentitySwitch, io_error_name};

    pub const REQUEST_QUEUE_CAPACITY: usize = 1024;

    /// `/run` may be a tmpfs, so the socket root is prepared at every boot:
    /// created, owned by the sidecar identity and `0700`.
    pub fn prepare_socket_root(path: &Path, identity: &ProcessIdentity) -> io::Result<()> {
        std::fs::create_dir_all(path)?;
        chown(path, Some(identity.uid), Some(identity.gid))?;
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700))
    }

    /// `SIGKILL` to a kernel's own session: kernels are group leaders, so
    /// the pid names the group.
    pub fn kill_process_group(pid: u32) {
        signal_process_group(pid, Signal::SIGKILL as i32);
    }

    /// Any signal to a group led by `pid` (a kernel or the sidecar); an
    /// unknown signal number or a gone group is ignored.
    pub fn signal_process_group(pid: u32, signal: i32) {
        let (Ok(raw), Ok(signal)) = (i32::try_from(pid), Signal::try_from(signal)) else {
            return;
        };
        let _ = killpg(nix::unistd::Pid::from_raw(raw), signal);
    }

    pub struct TokioSidecarLauncher {
        identity_switch: IdentitySwitch,
    }

    impl TokioSidecarLauncher {
        #[must_use]
        pub fn new(identity_switch: IdentitySwitch) -> Self {
            Self { identity_switch }
        }
    }

    impl KernelSidecar for TokioSidecarLauncher {
        fn launch(
            &self,
            spec: &SpawnSpec,
            events: SidecarEventSink,
            exited: SidecarExitSink,
        ) -> Result<Box<dyn SidecarLink>, SpawnError> {
            let plan = PreExecPlan::new(spec, self.identity_switch)
                .map_err(|error| SpawnError::Failed(io_error_name(&error)))?;
            let mut command = Command::new(&spec.program);
            command
                .args(&spec.args)
                .env_clear()
                .envs(spec.env.iter().map(|(key, value)| (key, value)))
                .current_dir(&spec.cwd)
                .process_group(0)
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .kill_on_drop(false);
            // SAFETY: `apply` only issues setrlimit/setgroups/setgid/setuid on
            // values computed before the fork; it allocates nothing and takes
            // no locks, which is what async-signal-safety requires here.
            unsafe { command.pre_exec(move || plan.apply()) };
            let mut child = command.spawn().map_err(|error| spawn_error(&error))?;
            let pid = child
                .id()
                .ok_or_else(|| SpawnError::Failed("child has no pid".to_owned()))?;
            let stdin = child
                .stdin
                .take()
                .ok_or_else(|| SpawnError::Failed("stdin not piped".to_owned()))?;
            let stdout = child
                .stdout
                .take()
                .ok_or_else(|| SpawnError::Failed("stdout not piped".to_owned()))?;
            let stderr = child
                .stderr
                .take()
                .ok_or_else(|| SpawnError::Failed("stderr not piped".to_owned()))?;
            let (sender, receiver) = mpsc::channel::<String>(REQUEST_QUEUE_CAPACITY);
            let closed = Arc::new(AtomicBool::new(false));
            let link = TokioSidecarLink {
                pid: Pid(pid),
                sender,
                closed: closed.clone(),
            };
            tokio::spawn(write_requests(stdin, receiver, closed));
            let fault = Arc::new(AtomicBool::new(false));
            tokio::spawn(read_events(pid, stdout, events, fault.clone()));
            let stderr_lines = Arc::new(AtomicU64::new(0));
            tokio::spawn(read_logs(pid, stderr, stderr_lines.clone()));
            tokio::spawn(async move {
                let status = child.wait().await;
                let code = status.ok().and_then(|status| status.code());
                tracing::info!(
                    pid,
                    exit_code = code,
                    sidecar_stderr_lines = stderr_lines.load(Ordering::Relaxed),
                    protocol_fault = fault.load(Ordering::Relaxed),
                    "sidecar exited"
                );
                exited(code);
            });
            Ok(Box::new(link))
        }
    }

    pub struct TokioSidecarLink {
        pid: Pid,
        sender: mpsc::Sender<String>,
        closed: Arc<AtomicBool>,
    }

    impl SidecarLink for TokioSidecarLink {
        fn pid(&self) -> Pid {
            self.pid
        }

        fn send(&self, line: &str) -> Result<(), SidecarIoError> {
            if self.closed.load(Ordering::Relaxed) {
                return Err(SidecarIoError::Closed);
            }
            self.sender
                .try_send(line.to_owned())
                .map_err(|error| match error {
                    mpsc::error::TrySendError::Full(_) => SidecarIoError::QueueFull,
                    mpsc::error::TrySendError::Closed(_) => SidecarIoError::Closed,
                })
        }

        fn kill(&self) {
            kill_process_group(self.pid.0);
        }

        fn terminate(&self) {
            signal_process_group(self.pid.0, Signal::SIGTERM as i32);
        }
    }

    async fn write_requests(
        mut stdin: tokio::process::ChildStdin,
        mut receiver: mpsc::Receiver<String>,
        closed: Arc<AtomicBool>,
    ) {
        while let Some(line) = receiver.recv().await {
            let written = async {
                stdin.write_all(line.as_bytes()).await?;
                stdin.write_all(b"\n").await?;
                stdin.flush().await
            }
            .await;
            if let Err(error) = written {
                tracing::warn!(reason = %io_error_name(&error), "sidecar stdin closed");
                break;
            }
        }
        closed.store(true, Ordering::Relaxed);
    }

    /// One protocol fault (unknown event, malformed or oversized line) is
    /// fatal: the sidecar is killed and the supervisor relaunches it.
    async fn read_events(
        pid: u32,
        stdout: tokio::process::ChildStdout,
        mut events: SidecarEventSink,
        fault: Arc<AtomicBool>,
    ) {
        let mut lines = FramedRead::new(
            stdout,
            LinesCodec::new_with_max_length(MAX_SIDECAR_LINE_BYTES),
        );
        while let Some(item) = lines.next().await {
            let line = match item {
                Ok(line) => line,
                Err(LinesCodecError::MaxLineLengthExceeded) => {
                    fatal(
                        pid,
                        &fault,
                        &ProtocolError::LineTooLong {
                            bytes: MAX_SIDECAR_LINE_BYTES,
                        },
                        0,
                    );
                    return;
                }
                Err(LinesCodecError::Io(error)) => {
                    tracing::debug!(pid, reason = %io_error_name(&error), "sidecar stdout closed");
                    return;
                }
            };
            match decode_event(&line) {
                Ok(event) => events(event).await,
                Err(error) => {
                    fatal(pid, &fault, &error, line.len());
                    return;
                }
            }
        }
    }

    fn fatal(pid: u32, fault: &AtomicBool, error: &ProtocolError, line_bytes: usize) {
        fault.store(true, Ordering::Relaxed);
        tracing::error!(pid, reason = %error, bytes = line_bytes, "sidecar protocol fault; killing it");
        kill_process_group(pid);
    }

    async fn read_logs(pid: u32, stderr: tokio::process::ChildStderr, counter: Arc<AtomicU64>) {
        let mut lines = FramedRead::new(
            stderr,
            LinesCodec::new_with_max_length(MAX_SIDECAR_LOG_LINE_BYTES),
        );
        while let Some(item) = lines.next().await {
            match item {
                Ok(line) => match parse_sidecar_log_line(&line) {
                    Some(record) => emit_sidecar_log(&record),
                    None => {
                        counter.fetch_add(1, Ordering::Relaxed);
                    }
                },
                Err(LinesCodecError::MaxLineLengthExceeded) => {
                    counter.fetch_add(1, Ordering::Relaxed);
                }
                Err(LinesCodecError::Io(_)) => break,
            }
        }
        tracing::debug!(
            pid,
            sidecar_stderr_lines = counter.load(Ordering::Relaxed),
            "sidecar stderr closed"
        );
    }

    fn spawn_error(error: &io::Error) -> SpawnError {
        match error.kind() {
            io::ErrorKind::NotFound | io::ErrorKind::PermissionDenied => {
                SpawnError::CannotExecute(io_error_name(error))
            }
            _ => SpawnError::Failed(io_error_name(error)),
        }
    }
}

#[cfg(not(unix))]
mod unsupported {
    use std::path::Path;

    use rayd_core::code::{KernelSidecar, SidecarEventSink, SidecarExitSink, SidecarLink};
    use rayd_core::process::{ProcessIdentity, SpawnError, SpawnSpec};

    pub fn prepare_socket_root(path: &Path, _identity: &ProcessIdentity) -> std::io::Result<()> {
        std::fs::create_dir_all(path)
    }

    pub fn kill_process_group(_pid: u32) {}

    pub fn signal_process_group(_pid: u32, _signal: i32) {}

    #[derive(Debug, Default, Clone, Copy)]
    pub struct UnsupportedSidecarLauncher;

    impl UnsupportedSidecarLauncher {
        #[must_use]
        pub fn new(_identity_switch: crate::adapters::IdentitySwitch) -> Self {
            Self
        }
    }

    impl KernelSidecar for UnsupportedSidecarLauncher {
        fn launch(
            &self,
            _spec: &SpawnSpec,
            _events: SidecarEventSink,
            _exited: SidecarExitSink,
        ) -> Result<Box<dyn SidecarLink>, SpawnError> {
            Err(SpawnError::Unsupported)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn log_lines_keep_only_allowlisted_fields() {
        let record = parse_sidecar_log_line(
            "{\"level\":\"info\",\"msg\":\"kernel warm\",\"context_id\":\"default\",\"warmup_ms\":1500,\"code\":\"print(1)\",\"envs\":{\"A\":\"1\"}}",
        )
        .unwrap();
        assert_eq!(record.level, "info");
        assert_eq!(record.msg, "kernel warm");
        assert_eq!(
            record.fields,
            "{\"context_id\":\"default\",\"warmup_ms\":1500}"
        );
    }

    #[test]
    fn non_schema_lines_are_not_records() {
        assert_eq!(
            parse_sidecar_log_line("Traceback (most recent call last):"),
            None
        );
        assert_eq!(parse_sidecar_log_line("{\"msg\":\"no level\"}"), None);
        assert_eq!(parse_sidecar_log_line("{\"level\":\"info\"}"), None);
        assert_eq!(parse_sidecar_log_line("[1, 2]"), None);
    }
}
