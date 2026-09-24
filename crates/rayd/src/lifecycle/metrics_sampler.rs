//! The periodic task that feeds `MetricsHistory` (design D2). It reads the
//! procfs probe only while the session's stream gate is open (`running` or
//! `resumed`): nothing recorded before `/run` can end up in the image-build
//! snapshot, and a suspension leaves a gap. It performs no network I/O, so
//! it never counts as endpoint activity and cannot postpone idle suspend.

use std::sync::Arc;
use std::time::Duration;

use rayd_core::metrics::{MetricsError, MetricsProbe};
use rayd_core::metrics_history::{MetricsHistory, MetricsSampler, SamplerTick};
use rayd_core::session::SandboxSession;
use tokio::task::JoinHandle;
use tokio::time::MissedTickBehavior;

/// Called once by whoever owns the runtime (`main`). `Skip` matters: the
/// monotonic clock jumps at resume, and the missed ticks must collapse into
/// one instead of firing a burst.
#[must_use]
pub fn spawn_metrics_sampler(
    session: Arc<SandboxSession>,
    probe: Arc<dyn MetricsProbe>,
    history: Arc<MetricsHistory>,
    interval: Duration,
) -> JoinHandle<()> {
    tokio::spawn(async move {
        let mut ticks = tokio::time::interval(interval);
        ticks.set_missed_tick_behavior(MissedTickBehavior::Skip);
        let mut sampler = MetricsSampler::default();
        loop {
            ticks.tick().await;
            sample_once(&session, probe.as_ref(), &history, &mut sampler);
        }
    })
}

/// One tick: a closed gate or a failing probe only drops the CPU baseline,
/// so the next good tick re-arms it instead of averaging across the hole.
fn sample_once(
    session: &SandboxSession,
    probe: &dyn MetricsProbe,
    history: &MetricsHistory,
    sampler: &mut MetricsSampler,
) {
    if session.stream_gate().is_err() {
        sampler.close_gate();
        return;
    }
    match read_tick(session, probe) {
        Ok(tick) => {
            if let Some(sample) = sampler.observe(&tick) {
                history.record(sample);
            }
        }
        Err(error) => {
            sampler.close_gate();
            tracing::debug!(reason = %error, "metrics sample skipped");
        }
    }
}

fn read_tick(
    session: &SandboxSession,
    probe: &dyn MetricsProbe,
) -> Result<SamplerTick, MetricsError> {
    Ok(SamplerTick {
        resume_generation: session.resume_generation(),
        cpu: probe.cpu_times()?,
        memory: probe.memory()?,
        disk: probe.disk_root()?,
        cpu_count: probe.cpu_count(),
        wall: session.clock().wall(),
    })
}

#[cfg(test)]
mod tests {
    use std::sync::Mutex;
    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
    use std::time::{SystemTime, UNIX_EPOCH};

    use rayd_core::clock::Clock;
    use rayd_core::metrics::{CpuTimes, DiskUsage, MemoryInfo};
    use rayd_core::metrics_history::{HISTORY_SAMPLE_INTERVAL, MetricsSample, RangeQuery};
    use rayd_core::session::RunHookInput;

    use super::*;

    /// Follows tokio's paused clock, so `sleep`/`advance` move the monotonic
    /// reading the way a suspended VM's clock jumps at resume; the wall clock
    /// is the epoch plus that reading, so sample timestamps are test seconds.
    struct TestClock {
        origin: tokio::time::Instant,
    }

    impl Clock for TestClock {
        fn monotonic(&self) -> Duration {
            self.origin.elapsed()
        }

        fn wall(&self) -> SystemTime {
            UNIX_EPOCH + self.monotonic()
        }
    }

    /// Every `cpu_times` read advances 10 busy and 30 idle jiffies (a 25 %
    /// window) plus whatever `add_busy` injected; `fail` makes `memory`
    /// answer an error.
    struct ScriptedProbe {
        cpu: Mutex<CpuTimes>,
        cpu_reads: AtomicUsize,
        failing: AtomicBool,
    }

    impl Default for ScriptedProbe {
        fn default() -> Self {
            Self {
                cpu: Mutex::new(CpuTimes { busy: 0, idle: 0 }),
                cpu_reads: AtomicUsize::new(0),
                failing: AtomicBool::new(false),
            }
        }
    }

    impl ScriptedProbe {
        fn add_busy(&self, jiffies: u64) {
            self.cpu.lock().unwrap().busy += jiffies;
        }

        fn fail(&self, failing: bool) {
            self.failing.store(failing, Ordering::SeqCst);
        }

        fn cpu_reads(&self) -> usize {
            self.cpu_reads.load(Ordering::SeqCst)
        }
    }

    impl MetricsProbe for ScriptedProbe {
        fn cpu_times(&self) -> Result<CpuTimes, MetricsError> {
            self.cpu_reads.fetch_add(1, Ordering::SeqCst);
            let mut cpu = self.cpu.lock().unwrap();
            cpu.busy += 10;
            cpu.idle += 30;
            Ok(*cpu)
        }

        fn memory(&self) -> Result<MemoryInfo, MetricsError> {
            if self.failing.load(Ordering::SeqCst) {
                return Err(MetricsError::Io {
                    source_name: "/proc/meminfo",
                    reason: "scripted failure".to_owned(),
                });
            }
            Ok(MemoryInfo {
                total: 4_096,
                available: 1_024,
                cached: 512,
            })
        }

        fn disk_root(&self) -> Result<DiskUsage, MetricsError> {
            Ok(DiskUsage {
                total: 100,
                used: 10,
            })
        }

        fn cpu_count(&self) -> u32 {
            2
        }
    }

    fn booting_session() -> Arc<SandboxSession> {
        let clock = Arc::new(TestClock {
            origin: tokio::time::Instant::now(),
        });
        Arc::new(SandboxSession::new(clock, "test"))
    }

    fn run(session: &SandboxSession) {
        let payload = "{\"v\":1,\"token_sha256\":\"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\"}";
        session.run(RunHookInput {
            sandbox_id: Some("mvm-1"),
            payload: Some(payload),
        });
    }

    fn spawn(
        session: &Arc<SandboxSession>,
        probe: &Arc<ScriptedProbe>,
        history: &Arc<MetricsHistory>,
    ) -> JoinHandle<()> {
        let probe: Arc<dyn MetricsProbe> = probe.clone();
        spawn_metrics_sampler(
            session.clone(),
            probe,
            history.clone(),
            HISTORY_SAMPLE_INTERVAL,
        )
    }

    fn recorded(history: &MetricsHistory) -> Vec<MetricsSample> {
        history
            .query(&RangeQuery::from_wire(0, 0, 0).unwrap())
            .samples
    }

    fn stamps(history: &MetricsHistory) -> Vec<i64> {
        recorded(history)
            .iter()
            .map(|sample| sample.unix_ms)
            .collect()
    }

    fn assert_quarter_cpu(samples: &[MetricsSample]) {
        for sample in samples {
            assert!(
                (sample.cpu_used_pct - 25.0).abs() < 1e-9,
                "{samples:?} averages a window that is not the 5 s since the previous tick"
            );
        }
    }

    #[tokio::test(start_paused = true)]
    async fn no_samples_before_run_then_one_every_interval() {
        let session = booting_session();
        let probe = Arc::new(ScriptedProbe::default());
        let history = Arc::new(MetricsHistory::default());
        let sampler_task = spawn(&session, &probe, &history);
        tokio::time::sleep(Duration::from_secs(12)).await;
        assert_eq!(probe.cpu_reads(), 0, "the gate is closed before /run");
        assert!(recorded(&history).is_empty());
        run(&session);
        tokio::time::sleep(Duration::from_secs(20)).await;
        assert_eq!(stamps(&history), [20_000, 25_000, 30_000]);
        let samples = recorded(&history);
        assert_quarter_cpu(&samples);
        assert!(samples.iter().all(|sample| sample.mem_cache == 512
            && sample.mem_used == 3_072
            && sample.cpu_count == 2));
        sampler_task.abort();
    }

    #[tokio::test(start_paused = true)]
    async fn no_samples_while_suspending_and_a_gap_after_resume() {
        let session = booting_session();
        run(&session);
        let probe = Arc::new(ScriptedProbe::default());
        let history = Arc::new(MetricsHistory::default());
        let sampler_task = spawn(&session, &probe, &history);
        tokio::time::sleep(Duration::from_secs(17)).await;
        assert_eq!(stamps(&history), [5_000, 10_000, 15_000]);
        session.suspend().unwrap();
        let reads_before_the_freeze = probe.cpu_reads();
        probe.add_busy(10_000);
        tokio::time::advance(Duration::from_secs(300)).await;
        tokio::time::sleep(Duration::from_secs(1)).await;
        assert_eq!(
            probe.cpu_reads(),
            reads_before_the_freeze,
            "nothing is read while suspending"
        );
        session.resume().unwrap();
        tokio::time::sleep(Duration::from_secs(14)).await;
        assert_eq!(stamps(&history), [5_000, 10_000, 15_000, 325_000, 330_000]);
        assert_quarter_cpu(&recorded(&history));
        sampler_task.abort();
    }

    #[tokio::test(start_paused = true)]
    async fn probe_errors_skip_the_tick_and_rearm() {
        let session = booting_session();
        run(&session);
        let probe = Arc::new(ScriptedProbe::default());
        let history = Arc::new(MetricsHistory::default());
        let sampler_task = spawn(&session, &probe, &history);
        tokio::time::sleep(Duration::from_secs(7)).await;
        assert_eq!(stamps(&history), [5_000]);
        probe.fail(true);
        tokio::time::sleep(Duration::from_secs(5)).await;
        assert_eq!(stamps(&history), [5_000]);
        probe.fail(false);
        probe.add_busy(1_000);
        tokio::time::sleep(Duration::from_secs(10)).await;
        assert_eq!(stamps(&history), [5_000, 20_000]);
        assert_quarter_cpu(&recorded(&history));
        sampler_task.abort();
    }
}
