//! Slot for `m15-efs-volumes` (ADR-018, experimental), wired to a
//! `VolumeMounter` (`rayd_core::volume`): `adapters::efs_mount::EfsUtilsMounter`
//! over `amazon-efs-utils`. `supported()` is exactly the mounter's own
//! `support()` — true only on an image that installs `amazon-efs-utils` and
//! runs with `CAP_SYS_ADMIN` (`detect_efs_supported`) — so
//! `Health.features.efs_volumes` comes from the adapter, never from a
//! hard-coded flag, and every image Rayito publishes today still answers
//! `SECTION_CODE_UNSUPPORTED` before looking at the section.
//!
//! Behind a supporting mounter, a present `EfsVolumesConfig` replaces the
//! whole desired set (`configure.proto` semantics): the section is
//! validated into a `VolumePlan` first (`Invalid`, nothing touched, on any
//! bad path, id or overlap), mounted paths the new plan drops or changes
//! are unmounted lazily (which also stops their `efs-proxy`), and each new
//! spec is mounted in plan order; the first failure answers `Failed` with
//! its closed class.
//!
//! The slot is also a lifecycle participant, with what the 2026-10-04
//! acceptance measured (`AWS_API_NOTES.md` §16 Q129/Q130):
//!
//! - `/suspend`: each mounted volume gets one bounded `syncfs`, all inside
//!   this participant's `SuspendShares` allocation. A volume whose flush
//!   does not finish turns `DEGRADED`/`flush_timeout`; if its mount target
//!   is unreachable the platform may terminate the `MicroVM` and those
//!   writes are lost (EFS-13) — nothing in the guest can prevent that, it
//!   is documented instead.
//! - `/resume`: `rayd_core::volume::resume` decides per volume: remount at
//!   once when the tunnel's credentials expired during the pause (EFS-12),
//!   otherwise a bounded probe and a remount only if it fails. Remounts run
//!   as their own tasks; the hook waits for them only up to `RESUME_WAIT`,
//!   and one that takes longer keeps going and reports through
//!   `ConfigureStatus` (`REMOUNTING`, then `MOUNTED` or `DEGRADED` with the
//!   failure class), like an S3 mount's relaunch does.
//! - `/terminate`: every live volume is unmounted and its proxy stopped,
//!   inside `TERMINATE_WAIT`.
//!
//! `Inner::ops` serializes `Configure` and remounts, and each entry carries
//! the `generation` of the `Configure` that created it: a remount only
//! writes its result into an entry whose generation is still the one it
//! started from, so it never resurrects a volume a later `Configure`
//! dropped or changed.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, PoisonError};
use std::time::{Duration, SystemTime};

use rayd_core::configure::{SectionCode, SectionOutcome};
use rayd_core::root_egress::RootEgressClass;
use rayd_core::suspend_sync::{ParticipantDemand, ParticipantReport, SUSPEND_SYNC_DEADLINE};
use rayd_core::volume::{
    AccessPointId, DegradeReason, FileSystemId, FlushOutcome, MountPath, MountReceipt, MountState,
    MountSupport, MountTargetIp, MountTransition, ResumeStep, UnmountMode, VolumeError,
    VolumeMounter, VolumePlan, VolumeSpec, after_probe, plan_resume,
};
use rayito_proto::v1::{
    EfsVolumeMount, EfsVolumeState, EfsVolumeStatus, EfsVolumesConfig, EfsVolumesStatus,
};

use super::FeatureContext;
use super::slot::ConfigurableFeature;
use crate::adapters::{EfsUtilsMounter, LeaseSource, LinuxEfsHost, detect_efs_supported};
use crate::hooks::{PARTICIPANT_RESUME_TIMEOUT, PARTICIPANT_TERMINATE_TIMEOUT};
use crate::lifecycle::LifecycleParticipant;

pub(crate) const PARTICIPANT_NAME: &str = "efs_volumes";
/// This participant asks `/suspend` for the whole sync deadline: its
/// per-volume flushes run concurrently with each other and with the
/// per-filesystem `syncfs` calls, so asking for all of it never makes the
/// hook wait longer (`SuspendShares` caps every share at that deadline).
const SUSPEND_FLUSH_DEMAND: Duration = SUSPEND_SYNC_DEADLINE;
/// Each volume's flush deadline is the share minus this, so the
/// participant's own report (and the `DEGRADED` marks) land before
/// `hooks::mod` cuts the call off at exactly the share.
const SUSPEND_REPORT_SLACK: Duration = Duration::from_millis(100);
/// `/resume`'s probe of one volume (a `stat` in a child process): EFS-11
/// measured the first read correct ≤ 0.2 s after `resume()`, so a healthy
/// volume answers well inside this.
const RESUME_PROBE_BUDGET: Duration = Duration::from_millis(800);
/// How long `on_resume` waits for the probes and remounts it started; what
/// is still running then keeps going in the background. A remount is about
/// one mount (Q128: p95 589 ms), so a volume whose credentials expired is
/// usually `MOUNTED` again before `/resume` answers.
const RESUME_WAIT: Duration = Duration::from_millis(1_500);
/// How long `on_terminate` waits for every unmount (and proxy stop).
const TERMINATE_WAIT: Duration = Duration::from_millis(900);
const _: () = assert!(
    RESUME_PROBE_BUDGET.as_nanos() < RESUME_WAIT.as_nanos()
        && RESUME_WAIT.as_nanos() < PARTICIPANT_RESUME_TIMEOUT.as_nanos(),
    "the efs-volumes probe and wait must settle inside the participant's /resume cap"
);
const _: () = assert!(
    TERMINATE_WAIT.as_nanos() < PARTICIPANT_TERMINATE_TIMEOUT.as_nanos(),
    "the efs-volumes unmounts must settle inside the participant's /terminate cap"
);

#[must_use]
pub fn build(
    ctx: &FeatureContext,
) -> Arc<dyn ConfigurableFeature<EfsVolumesConfig, EfsVolumesStatus>> {
    let support = if detect_efs_supported() {
        MountSupport::Supported
    } else {
        MountSupport::Unsupported
    };
    let mounter = EfsUtilsMounter::new(
        Arc::new(LinuxEfsHost::new(ctx.region.clone())),
        Arc::clone(&ctx.credentials) as Arc<dyn LeaseSource>,
        support,
    );
    Arc::new(EfsVolumesSlot::new(Arc::new(mounter)))
}

/// One path of the last applied plan: what was asked and where it stands.
#[derive(Clone)]
struct Entry {
    spec: VolumeSpec,
    state: MountState,
    error_class: Option<&'static str>,
    credentials_expire_at: Option<SystemTime>,
    generation: u64,
}

impl Entry {
    /// Something is mounted at the path (possibly degraded or being
    /// remounted), so dropping it needs an unmount.
    fn is_live(&self) -> bool {
        matches!(
            self.state,
            MountState::Mounted | MountState::Degraded | MountState::Remounting
        )
    }

    fn status(&self) -> EfsVolumeStatus {
        EfsVolumeStatus {
            mount_path: self.spec.mount_path.as_str().to_owned(),
            state: wire_state(self.state) as i32,
            last_error_class: self.error_class.unwrap_or_default().to_owned(),
        }
    }
}

/// Shared between the `ConfigurableFeature` facade and the
/// `LifecycleParticipant` facade, so `participant()` can hand out an `Arc`
/// of it from `&self`.
struct Inner {
    mounter: Arc<dyn VolumeMounter>,
    /// The last applied plan, in plan order.
    entries: Mutex<Vec<Entry>>,
    ops: tokio::sync::Mutex<()>,
    next_generation: AtomicU64,
}

impl Inner {
    fn snapshot(&self) -> Vec<Entry> {
        self.entries
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
    }

    fn replace(&self, entries: Vec<Entry>) {
        *self.entries.lock().unwrap_or_else(PoisonError::into_inner) = entries;
    }

    fn new_generation(&self) -> u64 {
        self.next_generation.fetch_add(1, Ordering::Relaxed)
    }

    /// Applies `event` to the entry for `path` created by `generation`,
    /// when the state machine allows it; `receipt` (a successful remount)
    /// replaces the credentials' expiry. Returns whether it applied.
    fn transition(
        &self,
        path: &MountPath,
        generation: u64,
        event: MountTransition,
        error_class: Option<&'static str>,
        receipt: Option<MountReceipt>,
    ) -> bool {
        let mut entries = self.entries.lock().unwrap_or_else(PoisonError::into_inner);
        let Some(entry) = entries
            .iter_mut()
            .find(|entry| entry.spec.mount_path == *path && entry.generation == generation)
        else {
            return false;
        };
        let Some(next) = entry.state.apply(event) else {
            return false;
        };
        entry.state = next;
        entry.error_class = error_class;
        if let Some(receipt) = receipt {
            entry.credentials_expire_at = receipt.credentials_expire_at;
        }
        true
    }

    fn spec_of(&self, path: &MountPath, generation: u64, state: MountState) -> Option<VolumeSpec> {
        self.snapshot()
            .into_iter()
            .find(|entry| {
                entry.spec.mount_path == *path
                    && entry.generation == generation
                    && entry.state == state
            })
            .map(|entry| entry.spec)
    }

    async fn apply(&self, plan: &VolumePlan) -> SectionOutcome {
        let _serialized = self.ops.lock().await;
        let previous = self.snapshot();
        self.unmount_dropped(&previous, plan).await;
        self.mount_all(&previous, plan).await
    }

    /// Lazily unmounts every live entry whose exact spec the new plan no
    /// longer asks for (dropped, or changed and about to be remounted); a
    /// failure here never blocks the new plan.
    async fn unmount_dropped(&self, previous: &[Entry], plan: &VolumePlan) {
        for entry in previous.iter().filter(|entry| entry.is_live()) {
            if !plan.specs().contains(&entry.spec) {
                let _best_effort = self
                    .mounter
                    .unmount(&entry.spec.mount_path, UnmountMode::Lazy)
                    .await;
            }
        }
    }

    /// Mounts each spec in plan order, keeping an identical live entry as
    /// it is (state, credentials and generation included); stops at the
    /// first failure.
    async fn mount_all(&self, previous: &[Entry], plan: &VolumePlan) -> SectionOutcome {
        let generation = self.new_generation();
        let mut entries: Vec<Entry> = plan
            .specs()
            .iter()
            .map(|spec| {
                previous
                    .iter()
                    .find(|entry| entry.is_live() && entry.spec == *spec)
                    .cloned()
                    .unwrap_or_else(|| Entry {
                        spec: spec.clone(),
                        state: MountState::Requested,
                        error_class: None,
                        credentials_expire_at: None,
                        generation,
                    })
            })
            .collect();
        for entry in &mut entries {
            if entry.state != MountState::Requested {
                continue;
            }
            match self.mounter.mount(&entry.spec).await {
                Ok(receipt) => {
                    entry.state = MountState::Mounted;
                    entry.credentials_expire_at = receipt.credentials_expire_at;
                }
                Err(failure) => {
                    let class = failure.class.as_str();
                    entry.state = MountState::Failed;
                    entry.error_class = Some(class);
                    self.replace(entries);
                    return outcome(SectionCode::Failed, Some(class));
                }
            }
        }
        self.replace(entries);
        SectionOutcome::applied()
    }

    /// One volume's `/resume` (see the module doc): probe if asked, then
    /// remount if needed.
    async fn recover(self: Arc<Self>, path: MountPath, generation: u64, step: ResumeStep) {
        let reason = match step {
            ResumeStep::Nothing => return,
            ResumeStep::Remount(reason) => reason,
            ResumeStep::Probe => {
                let probed = self.mounter.probe(&path, RESUME_PROBE_BUDGET).await;
                let Some(reason) = after_probe(probed) else {
                    self.transition(
                        &path,
                        generation,
                        MountTransition::ProbeFoundHealthy,
                        None,
                        None,
                    );
                    return;
                };
                reason
            }
        };
        self.remount(&path, generation, reason).await;
    }

    async fn remount(&self, path: &MountPath, generation: u64, reason: DegradeReason) {
        let degrade = if reason == DegradeReason::CredentialsExpired {
            MountTransition::CredentialsExpired
        } else {
            MountTransition::ProbeFoundDegraded
        };
        self.transition(path, generation, degrade, Some(reason.as_str()), None);
        let _serialized = self.ops.lock().await;
        let Some(spec) = self.spec_of(path, generation, MountState::Degraded) else {
            return;
        };
        self.transition(
            path,
            generation,
            MountTransition::RemountStarted,
            Some(reason.as_str()),
            None,
        );
        let _best_effort = self.mounter.unmount(path, UnmountMode::Lazy).await;
        match self.mounter.mount(&spec).await {
            Ok(receipt) => {
                self.transition(
                    path,
                    generation,
                    MountTransition::RemountSucceeded,
                    None,
                    Some(receipt),
                );
                tracing::info!(reason = reason.as_str(), "efs volume remounted");
            }
            Err(failure) => {
                let failed = DegradeReason::RemountFailed(failure.class);
                self.transition(
                    path,
                    generation,
                    MountTransition::RemountFailed,
                    Some(failed.as_str()),
                    None,
                );
                tracing::warn!(
                    reason = reason.as_str(),
                    class = failed.as_str(),
                    "efs volume remount failed"
                );
            }
        }
    }

    fn status(&self) -> EfsVolumesStatus {
        EfsVolumesStatus {
            volumes: self.snapshot().iter().map(Entry::status).collect(),
        }
    }
}

/// The `efs_volumes` slot over any `VolumeMounter`.
pub struct EfsVolumesSlot {
    inner: Arc<Inner>,
}

impl EfsVolumesSlot {
    #[must_use]
    pub fn new(mounter: Arc<dyn VolumeMounter>) -> Self {
        Self {
            inner: Arc::new(Inner {
                mounter,
                entries: Mutex::new(Vec::new()),
                ops: tokio::sync::Mutex::new(()),
                next_generation: AtomicU64::new(0),
            }),
        }
    }
}

#[tonic::async_trait]
impl ConfigurableFeature<EfsVolumesConfig, EfsVolumesStatus> for EfsVolumesSlot {
    fn supported(&self) -> bool {
        self.inner.mounter.support() == MountSupport::Supported
    }

    async fn apply(&self, cfg: EfsVolumesConfig) -> SectionOutcome {
        if !self.supported() {
            return SectionOutcome::unsupported();
        }
        match plan_from_wire(&cfg.mounts) {
            Ok(plan) => self.inner.apply(&plan).await,
            Err(error) => outcome(SectionCode::Invalid, Some(invalid_class(error))),
        }
    }

    async fn status(&self) -> EfsVolumesStatus {
        self.inner.status()
    }

    /// Only a supporting build joins the hooks: with no mounter there is
    /// never a volume to flush, probe or unmount, and `/suspend`/`/resume`/
    /// `/terminate` stay exactly as they were.
    fn participant(&self) -> Option<Arc<dyn LifecycleParticipant>> {
        self.supported().then(|| {
            Arc::new(EfsVolumesParticipant {
                inner: Arc::clone(&self.inner),
            }) as Arc<dyn LifecycleParticipant>
        })
    }

    /// `mount.efs` and `efs-proxy` (root) read the execution role from
    /// IMDS and open the TLS tunnel to the mount target.
    fn root_egress_class(&self) -> Option<RootEgressClass> {
        Some(RootEgressClass::Efs)
    }
}

struct EfsVolumesParticipant {
    inner: Arc<Inner>,
}

impl EfsVolumesParticipant {
    fn live_entries(&self, states: &[MountState]) -> Vec<Entry> {
        self.inner
            .snapshot()
            .into_iter()
            .filter(|entry| states.contains(&entry.state))
            .collect()
    }
}

#[tonic::async_trait]
impl LifecycleParticipant for EfsVolumesParticipant {
    fn demand(&self) -> ParticipantDemand {
        ParticipantDemand {
            name: PARTICIPANT_NAME,
            max: SUSPEND_FLUSH_DEMAND,
        }
    }

    async fn on_suspend(&self, share: Duration) -> ParticipantReport {
        let deadline = share.saturating_sub(SUSPEND_REPORT_SLACK);
        let mut volume_flushes = tokio::task::JoinSet::new();
        for entry in self.live_entries(&[MountState::Mounted, MountState::Degraded]) {
            let mounter = Arc::clone(&self.inner.mounter);
            volume_flushes.spawn(async move {
                let result = mounter.flush(&entry.spec.mount_path, deadline).await;
                (entry, result)
            });
        }
        let mut unflushed = 0_usize;
        let mut timed_out = false;
        while let Some(joined) = volume_flushes.join_next().await {
            let Ok((entry, result)) = joined else {
                continue;
            };
            if result.left_unflushed() {
                unflushed += 1;
                timed_out |= matches!(result, FlushOutcome::TimedOut | FlushOutcome::InFlight);
                self.inner.transition(
                    &entry.spec.mount_path,
                    entry.generation,
                    MountTransition::FlushTimedOut,
                    Some(DegradeReason::FlushTimedOut.as_str()),
                    None,
                );
            }
        }
        if unflushed > 0 {
            tracing::warn!(
                unflushed,
                deadline_ms = u64::try_from(deadline.as_millis()).unwrap_or(u64::MAX),
                "efs volume flush incomplete at suspend"
            );
        }
        ParticipantReport {
            completed: unflushed == 0,
            timed_out,
        }
    }

    async fn on_resume(&self) {
        let now = SystemTime::now();
        let mut recoveries = Vec::new();
        for entry in self.inner.snapshot() {
            let step = plan_resume(entry.state, entry.credentials_expire_at, now);
            if step == ResumeStep::Nothing {
                continue;
            }
            let inner = Arc::clone(&self.inner);
            recoveries.push(tokio::spawn(inner.recover(
                entry.spec.mount_path,
                entry.generation,
                step,
            )));
        }
        let waited = tokio::time::timeout(RESUME_WAIT, async {
            for recovery in recoveries {
                let _ = recovery.await;
            }
        })
        .await;
        if waited.is_err() {
            tracing::info!("efs volume recovery continues after resume");
        }
    }

    async fn on_terminate(&self) {
        let mut unmounts = Vec::new();
        for entry in self.inner.snapshot().into_iter().filter(Entry::is_live) {
            let inner = Arc::clone(&self.inner);
            unmounts.push(tokio::spawn(async move {
                let path = entry.spec.mount_path;
                let _best_effort = inner.mounter.unmount(&path, UnmountMode::Lazy).await;
                inner.transition(
                    &path,
                    entry.generation,
                    MountTransition::Unmounted,
                    None,
                    None,
                );
            }));
        }
        let _ = tokio::time::timeout(TERMINATE_WAIT, async {
            for unmount in unmounts {
                let _ = unmount.await;
            }
        })
        .await;
    }
}

/// The wire section as a validated plan: every field parsed by the domain
/// (`rayd_core::volume::spec`), then the set checked as a whole.
fn plan_from_wire(mounts: &[EfsVolumeMount]) -> Result<VolumePlan, VolumeError> {
    let specs = mounts
        .iter()
        .map(spec_from_wire)
        .collect::<Result<Vec<_>, _>>()?;
    VolumePlan::build(specs).map_err(|error| VolumeError::from(&error))
}

fn spec_from_wire(mount: &EfsVolumeMount) -> Result<VolumeSpec, VolumeError> {
    let mount_target_ip = if mount.mount_target_ip.is_empty() {
        None
    } else {
        Some(MountTargetIp::parse(&mount.mount_target_ip)?)
    };
    Ok(VolumeSpec {
        file_system_id: FileSystemId::parse(&mount.file_system_id)?,
        access_point_id: AccessPointId::parse(&mount.access_point_id)?,
        mount_path: MountPath::parse(&mount.mount_path)?,
        read_only: mount.read_only,
        mount_target_ip,
    })
}

fn outcome(code: SectionCode, error_class: Option<&str>) -> SectionOutcome {
    SectionOutcome {
        code,
        error_class: error_class.map(str::to_owned),
    }
}

fn wire_state(state: MountState) -> EfsVolumeState {
    match state {
        MountState::Requested => EfsVolumeState::Requested,
        MountState::Mounting => EfsVolumeState::Mounting,
        MountState::Mounted => EfsVolumeState::Mounted,
        MountState::Degraded => EfsVolumeState::Degraded,
        MountState::Remounting => EfsVolumeState::Remounting,
        MountState::Unmounted => EfsVolumeState::Unmounted,
        MountState::Failed => EfsVolumeState::Failed,
    }
}

/// `error_class` for an `Invalid` section: a closed snake string, never the
/// offending value.
fn invalid_class(error: VolumeError) -> &'static str {
    match error {
        VolumeError::InvalidPath => "invalid_path",
        VolumeError::Overlap => "overlap",
        VolumeError::TooMany => "too_many",
        VolumeError::InvalidIdentifier => "invalid_identifier",
        VolumeError::NotAllowed => "not_allowed",
        VolumeError::Unsupported => "unsupported",
    }
}

#[cfg(test)]
mod tests {
    use rayd_core::volume::{BoxFuture, MountFailure, MountFailureClass, ProbeOutcome};

    use super::*;

    /// A mounter that claims support, records every call, fails the mount
    /// of `fail_path` (if any), answers probes with `probe` and flushes with
    /// `flush`; each successful mount reports `lease` as its credentials'
    /// expiry.
    struct FakeMounter {
        fail_path: Mutex<Option<&'static str>>,
        probe: Mutex<ProbeOutcome>,
        flush: Mutex<FlushOutcome>,
        lease: Mutex<Option<SystemTime>>,
        mount_delay: Mutex<Duration>,
        calls: Mutex<Vec<String>>,
    }

    impl Default for FakeMounter {
        fn default() -> Self {
            Self {
                fail_path: Mutex::new(None),
                probe: Mutex::new(ProbeOutcome::Healthy),
                flush: Mutex::new(FlushOutcome::Flushed),
                lease: Mutex::new(None),
                mount_delay: Mutex::new(Duration::ZERO),
                calls: Mutex::new(Vec::new()),
            }
        }
    }

    impl FakeMounter {
        fn calls(&self) -> Vec<String> {
            self.calls.lock().unwrap().clone()
        }

        fn record(&self, call: String) {
            self.calls.lock().unwrap().push(call);
        }
    }

    impl VolumeMounter for FakeMounter {
        fn support(&self) -> MountSupport {
            MountSupport::Supported
        }

        fn mount(&self, spec: &VolumeSpec) -> BoxFuture<'_, Result<MountReceipt, MountFailure>> {
            let path = spec.mount_path.as_str().to_owned();
            self.record(format!("mount {path}"));
            let fails = *self.fail_path.lock().unwrap() == Some(path.as_str());
            let lease = *self.lease.lock().unwrap();
            let delay = *self.mount_delay.lock().unwrap();
            Box::pin(async move {
                tokio::time::sleep(delay).await;
                if fails {
                    Err(MountFailure {
                        class: MountFailureClass::IamDenied,
                    })
                } else {
                    Ok(MountReceipt {
                        credentials_expire_at: lease,
                    })
                }
            })
        }

        fn unmount(
            &self,
            path: &MountPath,
            _mode: UnmountMode,
        ) -> BoxFuture<'_, Result<(), MountFailure>> {
            self.record(format!("unmount {}", path.as_str()));
            Box::pin(async { Ok(()) })
        }

        fn probe(&self, path: &MountPath, _budget: Duration) -> BoxFuture<'_, ProbeOutcome> {
            self.record(format!("probe {}", path.as_str()));
            let outcome = *self.probe.lock().unwrap();
            Box::pin(async move { outcome })
        }

        fn flush(&self, path: &MountPath, _deadline: Duration) -> BoxFuture<'_, FlushOutcome> {
            self.record(format!("flush {}", path.as_str()));
            let outcome = *self.flush.lock().unwrap();
            Box::pin(async move { outcome })
        }
    }

    fn mount(path: &str) -> EfsVolumeMount {
        EfsVolumeMount {
            mount_path: path.to_owned(),
            file_system_id: "fs-0123abcd".to_owned(),
            access_point_id: "fsap-0123abcd".to_owned(),
            read_only: false,
            mount_target_ip: String::new(),
        }
    }

    fn config(paths: &[&str]) -> EfsVolumesConfig {
        EfsVolumesConfig {
            mounts: paths.iter().map(|path| mount(path)).collect(),
        }
    }

    fn slot_over(mounter: &Arc<FakeMounter>) -> EfsVolumesSlot {
        EfsVolumesSlot::new(Arc::clone(mounter) as Arc<dyn VolumeMounter>)
    }

    async fn states(slot: &EfsVolumesSlot) -> Vec<(String, i32, String)> {
        slot.status()
            .await
            .volumes
            .into_iter()
            .map(|v| (v.mount_path, v.state, v.last_error_class))
            .collect()
    }

    fn participant(slot: &EfsVolumesSlot) -> Arc<dyn LifecycleParticipant> {
        slot.participant()
            .expect("a supporting slot joins the hooks")
    }

    #[tokio::test]
    async fn the_shipped_slot_is_unsupported_without_efs_utils() {
        let slot = build(&FeatureContext::default());
        assert!(!slot.supported());
        let outcome = slot.apply(config(&["/mnt/data"])).await;
        assert_eq!(outcome.code, SectionCode::Unsupported);
        assert_eq!(slot.status().await, EfsVolumesStatus::default());
        assert!(slot.participant().is_none());
    }

    #[tokio::test]
    async fn a_supported_mounter_mounts_the_plan_in_path_order() {
        let mounter = Arc::new(FakeMounter::default());
        let slot = slot_over(&mounter);
        assert!(slot.supported());
        assert_eq!(slot.root_egress_class(), Some(RootEgressClass::Efs));
        let outcome = slot.apply(config(&["/mnt/b", "/mnt/a"])).await;
        assert_eq!(outcome, SectionOutcome::applied());
        assert_eq!(mounter.calls(), ["mount /mnt/a", "mount /mnt/b"]);
        let ready = EfsVolumeState::Mounted as i32;
        assert_eq!(
            states(&slot).await,
            [
                ("/mnt/a".to_owned(), ready, String::new()),
                ("/mnt/b".to_owned(), ready, String::new()),
            ]
        );
    }

    #[tokio::test]
    async fn an_invalid_section_touches_nothing() {
        let mounter = Arc::new(FakeMounter::default());
        let slot = slot_over(&mounter);
        let outcome = slot.apply(config(&["/mnt/a", "/mnt/a/nested"])).await;
        assert_eq!(outcome.code, SectionCode::Invalid);
        assert_eq!(outcome.error_class.as_deref(), Some("overlap"));
        let outcome = slot.apply(config(&["relative"])).await;
        assert_eq!(outcome.error_class.as_deref(), Some("invalid_path"));
        assert!(mounter.calls().is_empty());
    }

    #[tokio::test]
    async fn a_failed_mount_answers_failed_with_its_class() {
        let mounter = Arc::new(FakeMounter::default());
        *mounter.fail_path.lock().unwrap() = Some("/mnt/b");
        let slot = slot_over(&mounter);
        let outcome = slot.apply(config(&["/mnt/a", "/mnt/b"])).await;
        assert_eq!(outcome.code, SectionCode::Failed);
        assert_eq!(outcome.error_class.as_deref(), Some("iam_denied"));
        let failed = &slot.status().await.volumes[1];
        assert_eq!(failed.state, EfsVolumeState::Failed as i32);
        assert_eq!(failed.last_error_class, "iam_denied");
    }

    #[tokio::test]
    async fn a_changed_spec_is_unmounted_then_mounted_again() {
        let mounter = Arc::new(FakeMounter::default());
        let slot = slot_over(&mounter);
        slot.apply(config(&["/mnt/a"])).await;
        let mut changed = config(&["/mnt/a"]);
        changed.mounts[0].read_only = true;
        slot.apply(changed).await;
        assert_eq!(
            mounter.calls(),
            ["mount /mnt/a", "unmount /mnt/a", "mount /mnt/a"]
        );
    }

    #[tokio::test]
    async fn a_new_plan_unmounts_only_the_paths_it_drops() {
        let mounter = Arc::new(FakeMounter::default());
        let slot = slot_over(&mounter);
        slot.apply(config(&["/mnt/a", "/mnt/b"])).await;
        slot.apply(config(&["/mnt/b"])).await;
        assert_eq!(
            mounter.calls(),
            ["mount /mnt/a", "mount /mnt/b", "unmount /mnt/a"]
        );
    }

    #[tokio::test]
    async fn the_participant_asks_for_the_whole_sync_deadline() {
        let mounter = Arc::new(FakeMounter::default());
        let slot = slot_over(&mounter);
        let demand = participant(&slot).demand();
        assert_eq!(demand.name, PARTICIPANT_NAME);
        assert_eq!(demand.max, SUSPEND_SYNC_DEADLINE);
    }

    #[tokio::test]
    async fn suspend_flushes_every_mounted_volume() {
        let mounter = Arc::new(FakeMounter::default());
        let slot = slot_over(&mounter);
        slot.apply(config(&["/mnt/a", "/mnt/b"])).await;
        let report = participant(&slot).on_suspend(SUSPEND_SYNC_DEADLINE).await;
        assert!(report.completed);
        assert!(!report.timed_out);
        let mut calls = mounter.calls();
        calls.sort();
        assert!(calls.contains(&"flush /mnt/a".to_owned()));
        assert!(calls.contains(&"flush /mnt/b".to_owned()));
    }

    #[tokio::test]
    async fn an_unflushed_volume_is_degraded_with_flush_timeout() {
        let mounter = Arc::new(FakeMounter::default());
        let slot = slot_over(&mounter);
        slot.apply(config(&["/mnt/a"])).await;
        *mounter.flush.lock().unwrap() = FlushOutcome::TimedOut;
        let report = participant(&slot).on_suspend(SUSPEND_SYNC_DEADLINE).await;
        assert!(!report.completed);
        assert!(report.timed_out);
        assert_eq!(
            states(&slot).await,
            [(
                "/mnt/a".to_owned(),
                EfsVolumeState::Degraded as i32,
                "flush_timeout".to_owned()
            )]
        );
    }

    #[tokio::test]
    async fn a_short_pause_only_probes_and_keeps_the_mount() {
        let mounter = Arc::new(FakeMounter::default());
        *mounter.lease.lock().unwrap() = Some(SystemTime::now() + Duration::from_secs(3_600));
        let slot = slot_over(&mounter);
        slot.apply(config(&["/mnt/a"])).await;
        participant(&slot).on_resume().await;
        assert_eq!(mounter.calls(), ["mount /mnt/a", "probe /mnt/a"]);
        assert_eq!(states(&slot).await[0].1, EfsVolumeState::Mounted as i32);
    }

    #[tokio::test]
    async fn a_pause_past_the_credentials_expiry_remounts_without_probing() {
        let mounter = Arc::new(FakeMounter::default());
        *mounter.lease.lock().unwrap() = Some(SystemTime::now() - Duration::from_secs(60));
        let slot = slot_over(&mounter);
        slot.apply(config(&["/mnt/a"])).await;
        *mounter.lease.lock().unwrap() = Some(SystemTime::now() + Duration::from_secs(3_600));
        participant(&slot).on_resume().await;
        assert_eq!(
            mounter.calls(),
            ["mount /mnt/a", "unmount /mnt/a", "mount /mnt/a"]
        );
        assert_eq!(
            states(&slot).await,
            [(
                "/mnt/a".to_owned(),
                EfsVolumeState::Mounted as i32,
                String::new()
            )]
        );
        // The new lease is kept: the next resume only probes.
        participant(&slot).on_resume().await;
        assert_eq!(mounter.calls().last().unwrap(), "probe /mnt/a");
    }

    #[tokio::test]
    async fn a_failed_probe_remounts() {
        let mounter = Arc::new(FakeMounter::default());
        let slot = slot_over(&mounter);
        slot.apply(config(&["/mnt/a"])).await;
        *mounter.probe.lock().unwrap() = ProbeOutcome::Stale;
        participant(&slot).on_resume().await;
        assert_eq!(
            mounter.calls(),
            [
                "mount /mnt/a",
                "probe /mnt/a",
                "unmount /mnt/a",
                "mount /mnt/a"
            ]
        );
        assert_eq!(states(&slot).await[0].1, EfsVolumeState::Mounted as i32);
    }

    #[tokio::test]
    async fn a_remount_that_fails_leaves_the_volume_degraded_with_its_class() {
        let mounter = Arc::new(FakeMounter::default());
        *mounter.lease.lock().unwrap() = Some(SystemTime::now() - Duration::from_secs(60));
        let slot = slot_over(&mounter);
        slot.apply(config(&["/mnt/a"])).await;
        *mounter.fail_path.lock().unwrap() = Some("/mnt/a");
        participant(&slot).on_resume().await;
        assert_eq!(
            states(&slot).await,
            [(
                "/mnt/a".to_owned(),
                EfsVolumeState::Degraded as i32,
                "iam_denied".to_owned()
            )]
        );
    }

    #[tokio::test]
    async fn a_flush_timeout_volume_that_probes_healthy_is_mounted_again() {
        let mounter = Arc::new(FakeMounter::default());
        let slot = slot_over(&mounter);
        slot.apply(config(&["/mnt/a"])).await;
        *mounter.flush.lock().unwrap() = FlushOutcome::TimedOut;
        participant(&slot).on_suspend(SUSPEND_SYNC_DEADLINE).await;
        participant(&slot).on_resume().await;
        assert_eq!(
            states(&slot).await,
            [(
                "/mnt/a".to_owned(),
                EfsVolumeState::Mounted as i32,
                String::new()
            )]
        );
    }

    #[tokio::test]
    async fn a_remount_slower_than_the_resume_wait_finishes_in_the_background() {
        let mounter = Arc::new(FakeMounter::default());
        *mounter.lease.lock().unwrap() = Some(SystemTime::now() - Duration::from_secs(60));
        let slot = slot_over(&mounter);
        slot.apply(config(&["/mnt/a"])).await;
        *mounter.mount_delay.lock().unwrap() = RESUME_WAIT + Duration::from_millis(300);
        let started = tokio::time::Instant::now();
        participant(&slot).on_resume().await;
        assert!(started.elapsed() < PARTICIPANT_RESUME_TIMEOUT);
        assert_eq!(states(&slot).await[0].1, EfsVolumeState::Remounting as i32);
        tokio::time::sleep(Duration::from_millis(600)).await;
        assert_eq!(states(&slot).await[0].1, EfsVolumeState::Mounted as i32);
    }

    #[tokio::test]
    async fn a_remount_never_resurrects_a_volume_a_later_configure_dropped() {
        let mounter = Arc::new(FakeMounter::default());
        *mounter.lease.lock().unwrap() = Some(SystemTime::now() - Duration::from_secs(60));
        let slot = slot_over(&mounter);
        slot.apply(config(&["/mnt/a"])).await;
        *mounter.mount_delay.lock().unwrap() = RESUME_WAIT + Duration::from_millis(300);
        participant(&slot).on_resume().await;
        *mounter.mount_delay.lock().unwrap() = Duration::ZERO;
        slot.apply(config(&[])).await;
        assert!(slot.status().await.volumes.is_empty());
    }

    #[tokio::test]
    async fn terminate_unmounts_every_live_volume() {
        let mounter = Arc::new(FakeMounter::default());
        let slot = slot_over(&mounter);
        slot.apply(config(&["/mnt/a", "/mnt/b"])).await;
        participant(&slot).on_terminate().await;
        let calls = mounter.calls();
        assert!(calls.contains(&"unmount /mnt/a".to_owned()));
        assert!(calls.contains(&"unmount /mnt/b".to_owned()));
        let unmounted = EfsVolumeState::Unmounted as i32;
        assert!(
            states(&slot)
                .await
                .iter()
                .all(|(_, state, _)| *state == unmounted)
        );
    }
}
