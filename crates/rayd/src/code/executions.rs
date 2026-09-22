//! Executions as first-class runtime objects (design D9): one bounded
//! channel per subscriber, and the recorder task that owns an execution
//! from the sidecar's first event to its `End`, whatever happens to the
//! client streams: it runs the tracker, records every counted event in the
//! 4 MiB ring, fans it out without ever waiting on a client, applies the
//! server timeout on the running clock and marks the execution ended. A
//! subscriber whose queue is full is detached on the spot: the queue is
//! its whole slack, and the ring lets it `Reattach` where it lost track.

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::time::Duration;

use rayd_core::clock::Deadline;
use rayd_core::code::{
    ContextId, ExecuteOutput, ExecutionId, ExecutionRegistry, ExecutionTracker, SidecarEvent,
    SidecarOp, SyntheticError, TimeoutSchedule,
};
use rayd_core::process::SubscriberSlot;
use rayd_core::session::SandboxSession;
use tokio::sync::mpsc;
use tokio::sync::mpsc::error::{TryRecvError, TrySendError};

use super::execute::InFlightGuard;
use super::supervisor::{ExecutionHandle, SidecarSupervisor, lock};
use crate::lifecycle::running_sleep;
use crate::process::Delivery;

pub type SharedExecutions = Arc<std::sync::Mutex<ExecutionRegistry<ExecuteSink>>>;

#[derive(Debug, Default)]
struct SubscriberState {
    last_seq: AtomicU64,
    stalled: AtomicBool,
    detached: Arc<AtomicBool>,
}

/// The sending half stored in the execution registry; clones share the
/// subscriber's state. `origin` marks the `Execute` stream that started the
/// cell (the only one whose drop interrupts it).
#[derive(Clone)]
pub struct ExecuteSink {
    sender: mpsc::Sender<ExecuteOutput>,
    state: Arc<SubscriberState>,
    origin: bool,
}

/// The receiving half, consumed by exactly one `ExecutionSubscriberStream`.
pub struct ExecuteReceiver {
    receiver: mpsc::Receiver<ExecuteOutput>,
    state: Arc<SubscriberState>,
    capacity: usize,
}

impl ExecuteSink {
    #[must_use]
    pub fn open(capacity: usize, origin: bool) -> (Self, ExecuteReceiver) {
        let (sender, receiver) = mpsc::channel(capacity);
        let state = Arc::new(SubscriberState::default());
        (
            Self {
                sender,
                state: state.clone(),
                origin,
            },
            ExecuteReceiver {
                receiver,
                state,
                capacity,
            },
        )
    }

    /// Never waits: a full queue means this client has fallen a whole
    /// queue behind and is given up on the spot, so no subscriber can hold
    /// the recorder (and through it the sidecar's stdout) back.
    #[must_use]
    pub fn deliver(&self, event: ExecuteOutput) -> Delivery {
        let seq = event.seq();
        match self.sender.try_send(event) {
            Ok(()) => {
                self.state.last_seq.store(seq, Ordering::Relaxed);
                Delivery::Delivered
            }
            Err(TrySendError::Full(_)) => {
                self.state.stalled.store(true, Ordering::Relaxed);
                Delivery::Stalled
            }
            Err(TrySendError::Closed(_)) => Delivery::Closed,
        }
    }

    #[must_use]
    pub fn is_origin(&self) -> bool {
        self.origin
    }

    /// `/suspend`: the stream will be closed by the broadcast (or by the
    /// connection dying with the VM); its drop must not interrupt the cell.
    pub fn mark_detached(&self) {
        self.state.detached.store(true, Ordering::Relaxed);
    }

    #[must_use]
    pub fn detached_flag(&self) -> Arc<AtomicBool> {
        self.state.detached.clone()
    }
}

impl SubscriberSlot for ExecuteSink {
    fn is_open(&self) -> bool {
        !self.sender.is_closed()
    }
}

impl ExecuteReceiver {
    pub fn try_recv(&mut self) -> Result<ExecuteOutput, TryRecvError> {
        self.receiver.try_recv()
    }

    pub fn poll_recv(
        &mut self,
        cx: &mut std::task::Context<'_>,
    ) -> std::task::Poll<Option<ExecuteOutput>> {
        self.receiver.poll_recv(cx)
    }

    #[must_use]
    pub fn stalled(&self) -> bool {
        self.state.stalled.load(Ordering::Relaxed)
    }

    #[must_use]
    pub fn last_seq(&self) -> u64 {
        self.state.last_seq.load(Ordering::Relaxed)
    }

    /// How many events this subscriber may fall behind before it is
    /// truncated.
    #[must_use]
    pub fn capacity(&self) -> usize {
        self.capacity
    }

    #[must_use]
    pub fn detached_flag(&self) -> Arc<AtomicBool> {
        self.state.detached.clone()
    }
}

/// Interrupts a cell on the caller's behalf (an `Execute` origin dropped
/// before `End`).
#[derive(Clone)]
pub struct InterruptHandle {
    supervisor: Arc<SidecarSupervisor>,
    context_id: ContextId,
    execution_id: ExecutionId,
}

impl InterruptHandle {
    #[must_use]
    pub fn new(
        supervisor: Arc<SidecarSupervisor>,
        context_id: ContextId,
        execution_id: ExecutionId,
    ) -> Self {
        Self {
            supervisor,
            context_id,
            execution_id,
        }
    }

    pub fn interrupt(&self) {
        tracing::info!(
            execution_id = %self.execution_id,
            context_id = %self.context_id,
            "execute stream dropped before end; interrupting"
        );
        self.supervisor.send_detached(SidecarOp::Interrupt {
            context_id: self.context_id.as_str().to_owned(),
            execution_id: Some(self.execution_id.as_str().to_owned()),
        });
    }
}

/// Everything a recorder is born with.
pub struct RecorderParts {
    pub handle: ExecutionHandle,
    pub tracker: ExecutionTracker,
    pub schedule: Option<TimeoutSchedule>,
    pub supervisor: Arc<SidecarSupervisor>,
    pub session: Arc<SandboxSession>,
    pub executions: SharedExecutions,
    pub context_id: ContextId,
    pub envs: std::collections::BTreeMap<String, String>,
    pub guard: InFlightGuard,
}

struct Timers {
    interrupt_at: Option<Deadline>,
    restart_at: Option<Deadline>,
    timeout_ms: u64,
}

impl Timers {
    fn new(schedule: Option<TimeoutSchedule>, accepted_at: Duration) -> Self {
        match schedule {
            Some(schedule) => Self {
                interrupt_at: Some(Deadline::after(accepted_at, schedule.interrupt_at)),
                restart_at: Some(Deadline::after(accepted_at, schedule.restart_at)),
                timeout_ms: schedule.timeout_ms,
            },
            None => Self {
                interrupt_at: None,
                restart_at: None,
                timeout_ms: 0,
            },
        }
    }
}

pub struct ExecutionRecorder {
    handle: ExecutionHandle,
    tracker: ExecutionTracker,
    timers: Timers,
    supervisor: Arc<SidecarSupervisor>,
    session: Arc<SandboxSession>,
    executions: SharedExecutions,
    context_id: ContextId,
    envs: std::collections::BTreeMap<String, String>,
    done: bool,
    _guard: InFlightGuard,
}

impl ExecutionRecorder {
    #[must_use]
    pub fn new(parts: RecorderParts) -> Self {
        let RecorderParts {
            handle,
            tracker,
            schedule,
            supervisor,
            session,
            executions,
            context_id,
            envs,
            guard,
        } = parts;
        let timers = Timers::new(schedule, session.running_now());
        Self {
            handle,
            tracker,
            timers,
            supervisor,
            session,
            executions,
            context_id,
            envs,
            done: false,
            _guard: guard,
        }
    }

    /// Detached: the recorder outlives every client stream and exits by
    /// itself after the execution's `End`.
    pub fn spawn(self) {
        drop(tokio::spawn(self.run()));
    }

    /// Events the sidecar already delivered are consumed before the timers
    /// get a say: a cell that ended on time is never rewritten into a
    /// timeout, nor its context restarted, because a client read slowly.
    async fn run(mut self) {
        while !self.done {
            self.drain_queued();
            if self.done {
                break;
            }
            let session = self.session.clone();
            let interrupt_at = self.timers.interrupt_at;
            let restart_at = self.timers.restart_at;
            tokio::select! {
                biased;
                event = self.handle.receiver.recv() => match event {
                    Some(event) => self.on_event(&event),
                    None => self.channel_closed(),
                },
                () = deadline_sleep(&session, interrupt_at), if interrupt_at.is_some() => {
                    self.on_interrupt_deadline();
                }
                () = deadline_sleep(&session, restart_at), if restart_at.is_some() => {
                    self.on_restart_deadline();
                }
            }
        }
    }

    fn drain_queued(&mut self) {
        while !self.done {
            match self.handle.receiver.try_recv() {
                Ok(event) => self.on_event(&event),
                Err(TryRecvError::Empty) => return,
                Err(TryRecvError::Disconnected) => self.channel_closed(),
            }
        }
    }

    fn on_event(&mut self, event: &SidecarEvent) {
        let outputs = self.tracker.on_event(event);
        self.record(outputs);
    }

    fn channel_closed(&mut self) {
        let outputs = if self.handle.stalled.load(Ordering::Relaxed) {
            self.tracker.synthetic_end(
                SyntheticError::OutputTruncated,
                "the recorder did not consume output in time",
            )
        } else {
            self.tracker
                .synthetic_end(SyntheticError::KernelDied, "the kernel sidecar exited")
        };
        self.record(outputs);
        self.done = true;
    }

    fn on_interrupt_deadline(&mut self) {
        self.timers.interrupt_at = None;
        self.tracker.mark_timed_out(self.timers.timeout_ms);
        tracing::info!(
            execution_id = %self.tracker.execution_id(),
            context_id = %self.context_id,
            timeout_ms = self.timers.timeout_ms,
            "execution timed out; interrupting"
        );
        self.supervisor.send_detached(SidecarOp::Interrupt {
            context_id: self.context_id.as_str().to_owned(),
            execution_id: Some(self.tracker.execution_id().as_str().to_owned()),
        });
    }

    fn on_restart_deadline(&mut self) {
        self.timers.restart_at = None;
        tracing::warn!(
            execution_id = %self.tracker.execution_id(),
            context_id = %self.context_id,
            "kernel ignored the interrupt; restarting the context"
        );
        self.supervisor.send_detached(SidecarOp::RestartContext {
            context_id: self.context_id.as_str().to_owned(),
            envs: self.envs.clone(),
        });
    }

    /// Ring first, then every subscriber in `seq` order; the `End` closes
    /// the execution once it has been delivered.
    fn record(&mut self, outputs: Vec<ExecuteOutput>) {
        for output in outputs {
            let is_end = output.is_end();
            let execution_id = self.tracker.execution_id().clone();
            let sinks = lock(&self.executions).push(&execution_id, output.clone());
            for (id, sink) in sinks {
                match sink.deliver(output.clone()) {
                    Delivery::Delivered => {}
                    Delivery::Stalled => {
                        tracing::warn!(
                            execution_id = %execution_id,
                            seq = output.seq(),
                            "execute subscriber fell a full queue behind; stream truncated"
                        );
                        lock(&self.executions).detach(&execution_id, id);
                    }
                    Delivery::Closed => lock(&self.executions).detach(&execution_id, id),
                }
            }
            if is_end {
                self.finish(&execution_id);
            }
        }
    }

    fn finish(&mut self, execution_id: &ExecutionId) {
        self.done = true;
        self.timers.interrupt_at = None;
        self.timers.restart_at = None;
        let running_now = self.session.running_now();
        let subscribers = lock(&self.executions)
            .mark_ended(execution_id, running_now)
            .len();
        self.supervisor.unregister_execution(self.handle.request_id);
        tracing::info!(
            execution_id = %execution_id,
            context_id = %self.context_id,
            subscribers,
            "execution ended"
        );
    }
}

async fn deadline_sleep(session: &Arc<SandboxSession>, deadline: Option<Deadline>) {
    match deadline {
        Some(deadline) => running_sleep(session, deadline).await,
        None => std::future::pending().await,
    }
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;
    use std::sync::Arc;
    use std::time::Duration;

    use rayd_core::code::{CreateContextInput, ExecuteOutput, ExecutionLimits, SidecarEvent};
    use tokio_stream::StreamExt;

    use super::super::fake_sidecar::{ReadySupervisor, ready_supervisor};
    use super::super::manager::{CodeManager, CodeSettings, ExecuteInput};
    use super::super::supervisor::{OpTimeouts, SupervisorSettings};
    use crate::adapters::OsRandomSource;
    use crate::code::ExecutionSubscriberStream;

    struct Fixture {
        manager: Arc<CodeManager>,
        fake: ReadySupervisor,
    }

    async fn fixture(stall: Duration, retention: Duration) -> Fixture {
        let fake = ready_supervisor(SupervisorSettings {
            stall_timeout: stall,
            queue_capacity: 4,
            op_timeouts: OpTimeouts {
                interrupt: Duration::from_millis(200),
                ..OpTimeouts::default()
            },
            ..SupervisorSettings::default()
        })
        .await;
        let settings = CodeSettings {
            stall_timeout: stall,
            queue_capacity: 4,
            interrupt_grace: Duration::from_millis(500),
            executions: ExecutionLimits {
                max_subscribers: 8,
                retention,
                max_retained: 32,
            },
            ..CodeSettings::default()
        };
        let manager = CodeManager::new(
            fake.session.clone(),
            Some(fake.supervisor.clone()),
            fake.registry.clone(),
            Arc::new(OsRandomSource),
            settings,
            "/home/user".to_owned(),
        );
        Fixture { manager, fake }
    }

    impl Fixture {
        async fn execute(&self, timeout_ms: u64) -> (ExecutionSubscriberStream, u64, String) {
            let stream = self
                .manager
                .execute(ExecuteInput {
                    context_id: None,
                    language: None,
                    code: "x".to_owned(),
                    timeout_ms,
                    envs: BTreeMap::new(),
                })
                .await
                .unwrap();
            let request = self.fake.launched.log.requests().pop().unwrap();
            let id = request["id"].as_u64().unwrap();
            let execution_id = request["execution_id"].as_str().unwrap().to_owned();
            (stream, id, execution_id)
        }

        async fn emit(&mut self, event: SidecarEvent) {
            self.fake.launched.emit(event).await;
        }

        fn ops(&self) -> Vec<String> {
            self.fake.launched.ops()
        }
    }

    fn started(id: u64, execution_id: &str) -> SidecarEvent {
        SidecarEvent::Started {
            id,
            execution_id: execution_id.to_owned(),
            execution_count: 1,
        }
    }

    fn stdout(id: u64, execution_id: &str, text: &str) -> SidecarEvent {
        SidecarEvent::Stdout {
            id,
            execution_id: execution_id.to_owned(),
            text: text.to_owned(),
            timestamp_unix_ns: 1,
        }
    }

    fn end(id: u64, execution_id: &str) -> SidecarEvent {
        SidecarEvent::End {
            id,
            execution_id: execution_id.to_owned(),
            execution_count: 1,
        }
    }

    fn names(outputs: &[ExecuteOutput]) -> Vec<String> {
        outputs
            .iter()
            .map(|output| match output {
                ExecuteOutput::Started { .. } => "started".to_owned(),
                ExecuteOutput::Stdout { text, .. } => format!("stdout:{}", text.trim()),
                ExecuteOutput::Stderr { .. } => "stderr".to_owned(),
                ExecuteOutput::Result { .. } => "result".to_owned(),
                ExecuteOutput::Error { error, .. } => format!("error:{}", error.name),
                ExecuteOutput::End { .. } => "end".to_owned(),
            })
            .collect()
    }

    async fn settle() {
        tokio::time::sleep(Duration::from_millis(20)).await;
    }

    #[tokio::test(start_paused = true)]
    async fn the_ring_serves_a_second_subscriber_and_both_see_the_end() {
        let mut fixture = fixture(Duration::from_secs(30), Duration::from_secs(30)).await;
        let (mut origin, id, execution_id) = fixture.execute(0).await;
        fixture.emit(started(id, &execution_id)).await;
        fixture.emit(stdout(id, &execution_id, "a\n")).await;
        settle().await;
        assert_eq!(names(&[origin.next().await.unwrap()]), vec!["started"]);
        let mut late = fixture.manager.reattach(None, &execution_id, 1).unwrap();
        fixture.emit(stdout(id, &execution_id, "b\n")).await;
        fixture.emit(end(id, &execution_id)).await;
        let origin_rest: Vec<ExecuteOutput> = (&mut origin).collect().await;
        assert_eq!(names(&origin_rest), vec!["stdout:a", "stdout:b", "end"]);
        let late_all: Vec<ExecuteOutput> = (&mut late).collect().await;
        assert_eq!(
            names(&late_all),
            vec!["started", "stdout:a", "stdout:b", "end"]
        );
        settle().await;
        assert_eq!(fixture.ops(), vec!["execute"], "nothing interrupted");
        let replayed = fixture.manager.reattach(None, &execution_id, 3).unwrap();
        let replayed: Vec<ExecuteOutput> = replayed.collect().await;
        assert_eq!(names(&replayed), vec!["stdout:b", "end"]);
        assert!(matches!(
            fixture.manager.reattach(None, &execution_id, 9),
            Err(rayd_core::code::CodeError::ReplayOutOfRange { .. })
        ));
    }

    /// Design D9: the recorder never waits on a client. One subscriber
    /// stuck while the sidecar emits many queues' worth of events costs
    /// the others nothing, an op issued meanwhile is answered, the
    /// dispatcher never parks and no time is spent waiting.
    #[tokio::test(start_paused = true)]
    async fn a_stuck_subscriber_never_holds_the_recorder_or_the_sidecar() {
        let mut fixture = fixture(Duration::from_secs(30), Duration::from_secs(30)).await;
        let (mut origin, id, execution_id) = fixture.execute(0).await;
        fixture.emit(started(id, &execution_id)).await;
        settle().await;
        assert_eq!(names(&[origin.next().await.unwrap()]), vec!["started"]);
        let late = fixture.manager.reattach(None, &execution_id, 0).unwrap();
        let reader = tokio::spawn(async move {
            let all: Vec<ExecuteOutput> = late.collect().await;
            all
        });
        let manager = fixture.manager.clone();
        let creating = tokio::spawn(async move {
            manager
                .create_context(CreateContextInput {
                    language: String::new(),
                    cwd: None,
                    envs: BTreeMap::new(),
                })
                .await
        });
        let started_at = tokio::time::Instant::now();
        for index in 0..300 {
            fixture
                .emit(stdout(id, &execution_id, &format!("{index}\n")))
                .await;
            assert!(!fixture.manager.dispatch_blocked());
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
        let create = fixture
            .fake
            .launched
            .log
            .requests()
            .into_iter()
            .find(|request| request["op"] == "create_context")
            .expect("the op left while the origin was stuck");
        fixture
            .emit(SidecarEvent::Reply {
                id: create["id"].as_u64().unwrap(),
                ok: true,
                payload: Some(serde_json::json!({ "kernel_pid": 7 })),
                error: None,
            })
            .await;
        assert!(creating.await.unwrap().is_ok());
        fixture.emit(end(id, &execution_id)).await;
        assert!(
            started_at.elapsed() < Duration::from_secs(5),
            "the recorder waited: {:?}",
            started_at.elapsed()
        );
        let late_all = names(&reader.await.unwrap());
        assert_eq!(late_all.len(), 301, "{late_all:?}");
        assert_eq!(late_all.first().map(String::as_str), Some("stdout:0"));
        assert_eq!(late_all.last().map(String::as_str), Some("end"));
        let origin_rest = names(&(&mut origin).collect::<Vec<ExecuteOutput>>().await);
        assert!(
            origin_rest.contains(&"error:OutputTruncated".to_owned()),
            "{origin_rest:?}"
        );
        assert_eq!(origin_rest.last().map(String::as_str), Some("end"));
        assert!(origin_rest.len() <= 6, "{origin_rest:?}");
        settle().await;
        assert_eq!(fixture.ops(), vec!["execute", "create_context"]);
    }

    #[tokio::test(start_paused = true)]
    async fn a_stalled_subscriber_is_truncated_alone() {
        let mut fixture = fixture(Duration::from_millis(100), Duration::from_secs(30)).await;
        let (mut origin, id, execution_id) = fixture.execute(0).await;
        fixture.emit(started(id, &execution_id)).await;
        settle().await;
        assert_eq!(names(&[origin.next().await.unwrap()]), vec!["started"]);
        let late = fixture.manager.reattach(None, &execution_id, 0).unwrap();
        let reader = tokio::spawn(async move {
            let all: Vec<ExecuteOutput> = late.collect().await;
            all
        });
        for index in 0..8 {
            fixture
                .emit(stdout(id, &execution_id, &format!("{index}\n")))
                .await;
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
        tokio::time::sleep(Duration::from_millis(300)).await;
        fixture.emit(end(id, &execution_id)).await;
        let late_all = reader.await.unwrap();
        assert_eq!(
            names(&late_all),
            vec![
                "stdout:0", "stdout:1", "stdout:2", "stdout:3", "stdout:4", "stdout:5", "stdout:6",
                "stdout:7", "end"
            ]
        );
        let origin_rest: Vec<ExecuteOutput> = (&mut origin).collect().await;
        let last = names(&origin_rest);
        assert_eq!(last.last().map(String::as_str), Some("end"));
        assert!(
            last.contains(&"error:OutputTruncated".to_owned()),
            "{last:?}"
        );
        assert!(last.len() < 10, "{last:?}");
        settle().await;
        assert_eq!(
            fixture.ops(),
            vec!["execute"],
            "a truncated origin never interrupts"
        );
    }

    #[tokio::test(start_paused = true)]
    async fn dropping_the_origin_interrupts_but_detached_and_reattached_do_not() {
        let mut fixture = fixture(Duration::from_secs(30), Duration::from_secs(30)).await;
        let (origin, id, execution_id) = fixture.execute(0).await;
        fixture.emit(started(id, &execution_id)).await;
        settle().await;
        let late = fixture.manager.reattach(None, &execution_id, 0).unwrap();
        drop(late);
        settle().await;
        assert_eq!(fixture.ops(), vec!["execute"]);
        drop(origin);
        settle().await;
        assert_eq!(fixture.ops(), vec!["execute", "interrupt"]);
        fixture.emit(end(id, &execution_id)).await;
        settle().await;
        let (origin, id, execution_id) = fixture.execute(0).await;
        fixture.emit(started(id, &execution_id)).await;
        settle().await;
        assert_eq!(fixture.manager.detach_for_suspend(), 1);
        drop(origin);
        settle().await;
        assert_eq!(
            fixture.ops(),
            vec!["execute", "interrupt", "execute"],
            "a detached origin is silent"
        );
    }

    #[tokio::test(start_paused = true)]
    async fn the_timeout_runs_on_the_running_clock_and_survives_a_pause() {
        let mut fixture = fixture(Duration::from_secs(30), Duration::from_secs(30)).await;
        let (mut origin, id, execution_id) = fixture.execute(1_000).await;
        fixture.emit(started(id, &execution_id)).await;
        settle().await;
        assert_eq!(names(&[origin.next().await.unwrap()]), vec!["started"]);
        tokio::time::sleep(Duration::from_millis(500)).await;
        fixture.fake.session.suspend().unwrap();
        tokio::time::sleep(Duration::from_secs(300)).await;
        assert_eq!(
            fixture.ops(),
            vec!["execute"],
            "no interrupt while suspending"
        );
        fixture.fake.session.resume().unwrap();
        tokio::time::sleep(Duration::from_millis(400)).await;
        assert_eq!(
            fixture.ops(),
            vec!["execute"],
            "the remaining budget is intact"
        );
        tokio::time::sleep(Duration::from_millis(200)).await;
        assert_eq!(fixture.ops(), vec!["execute", "interrupt"]);
        tokio::time::sleep(Duration::from_millis(600)).await;
        assert_eq!(
            fixture.ops(),
            vec!["execute", "interrupt", "restart_context"]
        );
        fixture.emit(end(id, &execution_id)).await;
        let rest: Vec<ExecuteOutput> = (&mut origin).collect().await;
        assert_eq!(names(&rest), vec!["error:ExecutionTimeout", "end"]);
    }

    #[tokio::test(start_paused = true)]
    async fn an_ended_execution_is_retained_on_the_running_clock_then_reaped() {
        let mut fixture = fixture(Duration::from_secs(30), Duration::from_secs(30)).await;
        let (mut origin, id, execution_id) = fixture.execute(0).await;
        fixture.emit(started(id, &execution_id)).await;
        fixture.emit(end(id, &execution_id)).await;
        let all: Vec<ExecuteOutput> = (&mut origin).collect().await;
        assert_eq!(names(&all), vec!["started", "end"]);
        assert_eq!(fixture.manager.execution_count(), 1);
        assert!(fixture.manager.reattach(None, &execution_id, 1).is_ok());
        tokio::time::sleep(Duration::from_secs(20)).await;
        fixture.fake.session.suspend().unwrap();
        tokio::time::sleep(Duration::from_secs(600)).await;
        fixture.fake.session.resume().unwrap();
        assert!(
            fixture.manager.reap_expired().is_empty(),
            "paused time does not count"
        );
        tokio::time::sleep(Duration::from_secs(11)).await;
        assert_eq!(fixture.manager.reap_expired().len(), 1);
        assert!(matches!(
            fixture.manager.reattach(None, &execution_id, 1),
            Err(rayd_core::code::CodeError::ExecutionNotFound)
        ));
        assert_eq!(fixture.manager.execution_count(), 0);
    }
}
