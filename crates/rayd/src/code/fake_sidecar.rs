//! An in-memory `KernelSidecar` for the host tests of the supervisor and
//! the execute stream: each launch hands its event sink to the test, which
//! plays the sidecar's stdout by hand; the link records every request line
//! it is given and whether it was killed or terminated.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use rayd_core::clock::Clock;
use rayd_core::code::{
    ContextRegistry, EventFuture, KernelSidecar, SIDECAR_PROTOCOL_VERSION, SidecarEvent,
    SidecarEventSink, SidecarExitSink, SidecarIoError, SidecarLink, SidecarState,
};
use rayd_core::process::{Pid, ProcessIdentity, ResourceLimits, SpawnError, SpawnSpec, StdinMode};
use rayd_core::session::{RunHookInput, SandboxSession};
use tokio::sync::mpsc;

use super::supervisor::{KernelSignaller, SidecarSupervisor, SupervisorSettings, lock};

pub const FAKE_PID: u32 = 4242;

/// What the link recorded: shared between the test and the launch.
#[derive(Clone, Default)]
pub struct LaunchLog {
    lines: Arc<Mutex<Vec<String>>>,
    killed: Arc<AtomicBool>,
    terminated: Arc<AtomicBool>,
}

impl LaunchLog {
    /// The `op` of every request line sent so far, in order.
    pub fn ops(&self) -> Vec<String> {
        lock(&self.lines)
            .iter()
            .map(|line| {
                serde_json::from_str::<serde_json::Value>(line)
                    .ok()
                    .and_then(|value| value["op"].as_str().map(str::to_owned))
                    .unwrap_or_default()
            })
            .collect()
    }

    pub fn killed(&self) -> bool {
        self.killed.load(Ordering::Relaxed)
    }

    pub fn terminated(&self) -> bool {
        self.terminated.load(Ordering::Relaxed)
    }

    /// The request lines as JSON, in order.
    pub fn requests(&self) -> Vec<serde_json::Value> {
        lock(&self.lines)
            .iter()
            .filter_map(|line| serde_json::from_str(line).ok())
            .collect()
    }
}

/// One launch as the test sees it. The exit sink is held, not dropped:
/// dropping it is how the adapter reports that the process is gone.
pub struct Launched {
    events: SidecarEventSink,
    _exit: SidecarExitSink,
    pub log: LaunchLog,
}

impl Launched {
    /// One stdout line of the sidecar as the reader would hand it over:
    /// the future completes when the dispatcher accepted it.
    pub fn line(&mut self, event: SidecarEvent) -> EventFuture {
        (self.events)(event)
    }

    pub async fn emit(&mut self, event: SidecarEvent) {
        self.line(event).await;
    }

    pub async fn ready(&mut self) {
        self.ready_with_languages(vec!["python".to_owned()]).await;
    }

    /// `ready` announcing what the fake image ships (`["python"]` by
    /// default; an empty list plays an older sidecar without the field).
    pub async fn ready_with_languages(&mut self, languages: Vec<String>) {
        self.emit(SidecarEvent::Ready {
            v: SIDECAR_PROTOCOL_VERSION,
            default_context_id: "default".to_owned(),
            kernel_pid: Some(FAKE_PID + 1),
            warmup_ms: 0,
            languages,
        })
        .await;
    }

    pub fn ops(&self) -> Vec<String> {
        self.log.ops()
    }
}

pub struct FakeSidecar {
    launches: mpsc::UnboundedSender<Launched>,
}

impl KernelSidecar for FakeSidecar {
    fn launch(
        &self,
        _spec: &SpawnSpec,
        events: SidecarEventSink,
        exited: SidecarExitSink,
    ) -> Result<Box<dyn SidecarLink>, SpawnError> {
        let log = LaunchLog::default();
        let _ = self.launches.send(Launched {
            events,
            _exit: exited,
            log: log.clone(),
        });
        Ok(Box::new(FakeLink { log }))
    }
}

struct FakeLink {
    log: LaunchLog,
}

impl SidecarLink for FakeLink {
    fn pid(&self) -> Pid {
        Pid(FAKE_PID)
    }

    fn send(&self, line: &str) -> Result<(), SidecarIoError> {
        lock(&self.log.lines).push(line.to_owned());
        Ok(())
    }

    fn kill(&self) {
        self.log.killed.store(true, Ordering::Relaxed);
    }

    fn terminate(&self) {
        self.log.terminated.store(true, Ordering::Relaxed);
    }
}

fn spawn_spec() -> SpawnSpec {
    SpawnSpec {
        program: "fake-sidecar".to_owned(),
        args: Vec::new(),
        env: Vec::new(),
        cwd: "/home/user".to_owned(),
        identity: ProcessIdentity {
            uid: 1000,
            gid: 1000,
            groups: Vec::new(),
            username: "user".to_owned(),
            home: "/home/user".to_owned(),
            shell: "/bin/bash".to_owned(),
        },
        stdin: StdinMode::Pipe,
        limits: ResourceLimits::default(),
    }
}

pub struct ReadySupervisor {
    pub supervisor: Arc<SidecarSupervisor>,
    pub launched: Launched,
    /// Every later launch of the loop (a relaunch after an exit).
    pub launches: mpsc::UnboundedReceiver<Launched>,
    pub registry: Arc<Mutex<ContextRegistry>>,
    pub session: Arc<SandboxSession>,
}

/// Follows tokio's clock, so `start_paused` tests move the running clock
/// with `sleep`/`advance` the way a suspended VM's monotonic clock jumps.
pub struct TokioClock {
    origin: tokio::time::Instant,
}

impl TokioClock {
    pub fn new() -> Self {
        Self {
            origin: tokio::time::Instant::now(),
        }
    }
}

impl Clock for TokioClock {
    fn monotonic(&self) -> Duration {
        self.origin.elapsed()
    }

    fn wall(&self) -> SystemTime {
        UNIX_EPOCH + self.monotonic()
    }
}

/// A session past `/run`, so the stream gate is open and the running clock
/// follows the phase machine.
pub fn running_session() -> Arc<SandboxSession> {
    let session = Arc::new(SandboxSession::new(Arc::new(TokioClock::new()), "test"));
    let payload = "{\"v\":1,\"token_sha256\":\"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\"}";
    session.run(RunHookInput {
        sandbox_id: Some("mvm-test"),
        payload: Some(payload),
    });
    session
}

/// A supervisor whose loop already launched the fake and saw its `ready`.
pub async fn ready_supervisor(settings: SupervisorSettings) -> ReadySupervisor {
    ready_supervisor_with(settings, Arc::new(|_, _| {})).await
}

/// The same, with the kernel signaller the test observes.
pub async fn ready_supervisor_with(
    settings: SupervisorSettings,
    kernel_signaller: KernelSignaller,
) -> ReadySupervisor {
    let (sender, mut receiver) = mpsc::unbounded_channel();
    let registry = Arc::new(Mutex::new(ContextRegistry::default()));
    let session = running_session();
    let supervisor = SidecarSupervisor::new(
        Arc::new(FakeSidecar { launches: sender }),
        spawn_spec(),
        session.clone(),
        registry.clone(),
        settings,
        kernel_signaller,
    );
    drop(supervisor.spawn());
    let mut launched = receiver
        .recv()
        .await
        .expect("the supervisor loop launches the fake");
    launched.ready().await;
    let mut state = supervisor.watch_state();
    let _ = state
        .wait_for(|state| matches!(state, SidecarState::Ready))
        .await;
    ReadySupervisor {
        supervisor,
        launched,
        launches: receiver,
        registry,
        session,
    }
}
