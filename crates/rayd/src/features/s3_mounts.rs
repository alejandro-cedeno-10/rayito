//! Real slot for `m15-s3-mounts` (ADR-017): a present `S3MountsConfig`
//! replaces the whole desired set of S3 mounts (mounts removed from it are
//! unmounted, new ones attached and started), rejected whole (no mount
//! touched) when a path's own shape is invalid, it overlaps another one in
//! the request, or a bucket is not on the image's own
//! `RAYITO_ALLOWED_MOUNT_BUCKETS` allowlist. With `s3_mounts` absent from
//! every `Configure` call, nothing in this module runs: no `/dev/fuse`
//! open, no `mount-s3` spawn, no new environment variable read beyond what
//! `build()` already did once at startup (ADR-014 rule 4).
//!
//! A newly-accepted mount is reported `Pending` the instant `apply()`
//! returns (the attach/spawn/readiness sequence runs in a background task,
//! `Inner::run_mount`); `ConfigureStatus` (or `sbx.mounts`) is how a caller
//! learns it reached `Mounted` or `Failed`. The same background machinery
//! also supervises every mount while `RUNNING`: `Inner::watch_tick`, polled
//! on a fixed interval, relaunches (as its own task, so one slow mount never
//! stalls supervision of the others) any settled mount whose daemon died,
//! with a backoff that grows per consecutive failure; a mount still
//! `Pending` belongs to the attempt in flight and is never relaunched on
//! top of it. `/resume`'s own probe
//! forces the same relaunch path for a mount whose daemon survived the
//! snapshot but whose FUSE connection did not answer afterwards. Every
//! relaunch re-attaches a fresh FUSE descriptor rather than reusing the
//! old one: a daemon that already completed `FUSE_INIT` and then died
//! leaves the kernel's end of that connection unusable by a new daemon,
//! the moment `rayd`'s own copy of the descriptor is closed.
//!
//! `Inner::claim`/`finish` are the fd/pid ownership rule that keeps a
//! racing `apply()` (a new spec, or the path dropped) and a racing
//! relaunch from ever closing, or leaking, the same descriptor: each
//! attempt is tagged with a `generation` when it starts, and only ever
//! writes its result into the shared table if that generation is still
//! the current one for that path — otherwise it tears its own result back
//! down instead of resurrecting a mount the caller has already moved past.

use std::collections::{HashMap, HashSet};
use std::os::fd::RawFd;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, PoisonError};
use std::time::{Duration, Instant};

use rayd_core::configure::{SectionCode, SectionOutcome};
use rayd_core::root_egress::RootEgressClass;
use rayd_core::s3_mount::{
    self, FuseDaemon, FuseDevice, MountErrorClass, MountPhase, MountState, S3Mount,
};
use rayd_core::suspend_sync::ParticipantDemand;
use rayito_proto::v1::{
    S3Mount as WireMount, S3MountPhase, S3MountState as WireMountState, S3MountsConfig,
    S3MountsStatus,
};

use super::FeatureContext;
use super::slot::ConfigurableFeature;
use crate::adapters::{
    LinuxFuseDevice, MOUNT_S3_BINARY, TokioMountS3Daemon, binary_on_path, detect_guest_capabilities,
};
use crate::hooks::PARTICIPANT_RESUME_TIMEOUT;
use crate::lifecycle::{LifecycleParticipant, ReadyVerdict};

/// Image-level bucket allowlist (`rayito image publish --env
/// RAYITO_ALLOWED_MOUNT_BUCKETS=team-data,runs-bucket`), read once at
/// agent startup — never a per-request activation switch (ADR-014 rule 4).
/// Absent or empty means no bucket is allowed, not "every bucket".
const ALLOWED_BUCKETS_ENV: &str = "RAYITO_ALLOWED_MOUNT_BUCKETS";
/// The platform's own region, read the same way `adapters::s3_store`
/// already reads it for ADR-009 persistence; `mount-s3`'s own `AWS_REGION`
/// comes from this, never a per-request value; `adapters::efs_mount`
/// hands the same variable to the `efs-utils` helper.
pub(crate) const AWS_REGION_ENV: &str = "AWS_REGION";
const FUSE_DEVICE_PATH: &str = "/dev/fuse";
/// The system account `image/Dockerfile` creates for the `mount-s3`
/// daemon. Like the binary, it exists in all four image variants (one
/// Dockerfile builds them all), so it only proves the image is new enough;
/// `detect_s3_mounts_supported` also requires `CAP_SYS_ADMIN`, which is
/// what actually tells `rayito-base-caps` apart.
const MOUNT_USER_NAME: &str = "rayito-mount";
/// Bound on one mount's attach-spawn-settle sequence before it is reported
/// `Failed`/`timeout` instead of staying `Pending`. Provisional — no
/// Q-measurement exists yet for `mount-s3`'s own IMDS-credential-plus-first-
/// S3-call latency; the AWS acceptance stage's S3M-1..S3M-4 campaign is
/// expected to tighten this.
const MOUNT_READY_TIMEOUT: Duration = Duration::from_secs(10);
/// Poll cadence of `Inner::await_ready`'s own retry loop while a mount
/// settles.
const MOUNT_PROBE_INTERVAL: Duration = Duration::from_millis(100);
/// How often the background watcher re-checks every current mount's
/// daemon for liveness while `RUNNING`.
const WATCHER_POLL_INTERVAL: Duration = Duration::from_secs(1);
/// Backoff before relaunching a mount again after a failed attempt: doubles
/// from this each time, capped at `RELAUNCH_BACKOFF_MAX`, reset to this the
/// moment a relaunch reaches `Mounted`. Provisional, chosen to keep a
/// bucket that is down for a while from being hammered with `mount-s3`
/// attempts without a real measurement to tune it against yet.
const RELAUNCH_BACKOFF_BASE: Duration = Duration::from_millis(500);
const RELAUNCH_BACKOFF_MAX: Duration = Duration::from_secs(30);
/// `/resume`'s own probe budget per mount (ADR-017): long enough for a
/// local FUSE round-trip, and strictly inside `hooks::PARTICIPANT_RESUME_TIMEOUT`,
/// the cap `hooks::mod` puts on this participant's whole `on_resume`. The
/// mounts are probed concurrently, so an unresponsive connection is
/// detected — and its relaunch started — before that cap cuts the call off.
const RESUME_PROBE_TIMEOUT: Duration = Duration::from_secs(1);
const _: () = assert!(
    RESUME_PROBE_TIMEOUT.as_nanos() < PARTICIPANT_RESUME_TIMEOUT.as_nanos(),
    "the s3-mounts probe must settle inside the participant's /resume cap"
);
pub(crate) const PARTICIPANT_NAME: &str = "s3_mounts";

struct MountEntry {
    spec: S3Mount,
    fd: Option<RawFd>,
    /// `-1` while nothing is live for this path (a failed attempt, or the
    /// brief window `claim` leaves between removing the old entry and
    /// `run_mount` writing its own result through `finish`).
    pid: i32,
    state: MountState,
    /// Which attempt last wrote this entry; `claim` hands out a fresh one
    /// per attempt and `finish` only applies a result whose generation
    /// still matches — see the module doc.
    generation: u64,
    backoff_attempt: u32,
    retry_at: Option<Instant>,
}

/// Shared between the `ConfigurableFeature` facade and the
/// `LifecycleParticipant` facade below, so `participant()` can hand out an
/// independent `Arc` without `Self` needing to be constructed behind one.
struct Inner {
    device: Arc<dyn FuseDevice>,
    daemon: Arc<dyn FuseDaemon>,
    allowed_buckets: Vec<String>,
    mounts: Mutex<HashMap<String, MountEntry>>,
    next_generation: AtomicU64,
    /// `MOUNT_READY_TIMEOUT` in `build()`; a test-only constructor shortens
    /// this so a "never becomes ready" test does not need the full
    /// production bound to prove the timeout path.
    ready_timeout: Duration,
}

impl Inner {
    fn lock(&self) -> std::sync::MutexGuard<'_, HashMap<String, MountEntry>> {
        self.mounts.lock().unwrap_or_else(PoisonError::into_inner)
    }

    fn new_generation(&self) -> u64 {
        self.next_generation.fetch_add(1, Ordering::Relaxed)
    }

    /// Claims `path` for `generation`: replaces whatever entry is there
    /// (if any) with a `Pending` placeholder under the new generation —
    /// visible to `status()` and to any other caller immediately — and
    /// hands back what the previous entry owned, for the caller to tear
    /// down itself, *outside* this lock.
    fn claim(&self, spec: S3Mount, generation: u64) -> (Option<RawFd>, i32) {
        let mut mounts = self.lock();
        let previous = mounts.remove(&spec.mount_path);
        let backoff_attempt = previous.as_ref().map_or(0, |entry| entry.backoff_attempt);
        mounts.insert(
            spec.mount_path.clone(),
            MountEntry {
                state: MountState::pending(spec.mount_path.clone()),
                spec,
                fd: None,
                pid: -1,
                generation,
                backoff_attempt,
                retry_at: None,
            },
        );
        previous.map_or((None, -1), |entry| (entry.fd, entry.pid))
    }

    /// Removes `path` entirely (no replacement claims it): used when a
    /// mount is dropped from a later `Configure` section. Returns what to
    /// tear down.
    fn release(&self, path: &str) -> (Option<RawFd>, i32) {
        self.lock()
            .remove(path)
            .map_or((None, -1), |entry| (entry.fd, entry.pid))
    }

    /// `kill` then `detach`, best-effort, for an fd/pid pair this `Inner`
    /// no longer has any entry pointing at — the only place either one is
    /// ever closed, so a given descriptor number is never passed here
    /// twice (`claim`/`release` each hand out a given entry's fd/pid
    /// exactly once, by removing the entry that held them).
    fn teardown(&self, path: &str, fd: Option<RawFd>, pid: i32) {
        if pid >= 0 {
            self.daemon.kill(pid);
        }
        if let Some(fd) = fd {
            let _ = self.device.detach(path, fd);
        }
    }

    fn unmount_one(&self, path: &str) {
        let (fd, pid) = self.release(path);
        self.teardown(path, fd, pid);
    }

    /// Writes one attempt's result into `path`'s entry, but only if
    /// `generation` is still current for it. Returns `true` when it was
    /// stale (a newer `claim` already replaced it): the caller must then
    /// tear `fd`/`pid` back down itself, since nothing else will.
    fn finish(
        &self,
        path: &str,
        generation: u64,
        fd: Option<RawFd>,
        pid: i32,
        new_state: MountState,
    ) -> bool {
        let stale = {
            let mut mounts = self.lock();
            match mounts.get_mut(path) {
                Some(entry) if entry.generation == generation => {
                    entry.fd = fd;
                    entry.pid = pid;
                    if new_state.phase == MountPhase::Mounted {
                        entry.backoff_attempt = 0;
                        entry.retry_at = None;
                    } else if new_state.phase == MountPhase::Failed {
                        entry.backoff_attempt = entry.backoff_attempt.saturating_add(1);
                        entry.retry_at =
                            Some(Instant::now() + relaunch_backoff(entry.backoff_attempt));
                    }
                    entry.state = new_state;
                    false
                }
                _ => true,
            }
        };
        if stale {
            self.teardown(path, fd, pid);
        }
        stale
    }

    /// Polls `is_alive`/`probe_ready` until `pid` answers filesystem
    /// requests at `mount_path`, it exits first, or `MOUNT_READY_TIMEOUT`
    /// runs out. `probe_ready` always runs on the blocking pool: it is a
    /// real subprocess with its own bounded wait, but calling it straight
    /// from this async context would still tie up a worker thread for
    /// that whole bound on every single poll.
    async fn await_ready(&self, mount_path: &str, pid: i32) -> Result<(), MountErrorClass> {
        let deadline = Instant::now() + self.ready_timeout;
        loop {
            if !self.daemon.is_alive(pid) {
                return Err(self.daemon.exit_class(pid));
            }
            if self.probe(mount_path).await {
                return Ok(());
            }
            if Instant::now() >= deadline {
                return Err(MountErrorClass::Timeout);
            }
            tokio::time::sleep(MOUNT_PROBE_INTERVAL).await;
        }
    }

    async fn probe(&self, mount_path: &str) -> bool {
        let device = Arc::clone(&self.device);
        let path = mount_path.to_owned();
        tokio::task::spawn_blocking(move || device.probe_ready(&path))
            .await
            .unwrap_or(false)
    }

    /// Attach, spawn and await readiness for `spec`, writing the result
    /// through `finish` under `generation`. The caller must have already
    /// `claim`ed `(spec.mount_path, generation)` — this never claims it
    /// itself, so a fresh mount (`apply`) and a relaunch (`watch_tick`,
    /// `force_relaunch`) share one path to the same effect.
    async fn run_mount(self: Arc<Self>, spec: S3Mount, generation: u64) {
        let path = spec.mount_path.clone();
        let fd = match self.device.attach(&spec) {
            Ok(fd) => fd,
            Err(class) => {
                self.finish(
                    &path,
                    generation,
                    None,
                    -1,
                    MountState::failed(path.clone(), class),
                );
                return;
            }
        };
        let pid = match self.daemon.spawn(fd, &spec) {
            Ok(pid) => pid,
            Err(class) => {
                let _ = self.device.detach(&path, fd);
                self.finish(
                    &path,
                    generation,
                    None,
                    -1,
                    MountState::failed(path.clone(), class),
                );
                return;
            }
        };
        // Published while still settling so a racing `apply()`/relaunch
        // can tear *this* attempt down (through `finish`'s own staleness
        // check) instead of orphaning it.
        if self.finish(
            &path,
            generation,
            Some(fd),
            pid,
            MountState::pending(path.clone()),
        ) {
            return;
        }
        match self.await_ready(&path, pid).await {
            Ok(()) => {
                self.finish(
                    &path.clone(),
                    generation,
                    Some(fd),
                    pid,
                    MountState::mounted(path),
                );
            }
            Err(class) => {
                if self.daemon.is_alive(pid) {
                    self.daemon.kill(pid);
                }
                let _ = self.device.detach(&path, fd);
                self.finish(
                    &path.clone(),
                    generation,
                    None,
                    -1,
                    MountState::failed(path, class),
                );
            }
        }
    }

    fn status(&self) -> S3MountsStatus {
        let mounts = self.lock();
        S3MountsStatus {
            mounts: mounts
                .values()
                .map(|entry| to_wire_state(&entry.state))
                .collect(),
        }
    }

    /// One pass of the continuous `RUNNING`-time supervisor: every settled
    /// mount whose daemon is not alive, whose backoff has elapsed, is
    /// relaunched with a fresh FUSE attach (never the old descriptor — see
    /// the module doc on why a dead daemon's old connection cannot be
    /// reused). `Pending` entries are skipped: their attempt is still in
    /// flight (`claim` leaves `pid == -1` until `run_mount` publishes one),
    /// and claiming them again would only make that attempt stale and run
    /// attach/spawn twice.
    fn watch_tick(self: &Arc<Self>) {
        let due: Vec<S3Mount> = {
            let mounts = self.lock();
            mounts
                .values()
                .filter(|entry| entry.state.phase != MountPhase::Pending)
                .filter(|entry| entry.pid < 0 || !self.daemon.is_alive(entry.pid))
                .filter(|entry| entry.retry_at.is_none_or(|at| Instant::now() >= at))
                .map(|entry| entry.spec.clone())
                .collect()
        };
        for spec in due {
            self.claim_and_spawn(spec);
        }
    }

    /// Claims a fresh generation for `spec.mount_path` (visible as
    /// `Pending` immediately), tears down whatever the previous entry
    /// owned, and spawns the attempt as its own task — the one sequence
    /// every (re)mount, first or relaunched, goes through. Synchronous up
    /// to the spawn, so the entry is already `Pending` (and skipped by
    /// `watch_tick`) before the caller's next look at the table.
    fn claim_and_spawn(self: &Arc<Self>, spec: S3Mount) {
        let generation = self.new_generation();
        let (old_fd, old_pid) = self.claim(spec.clone(), generation);
        self.teardown(&spec.mount_path, old_fd, old_pid);
        let inner = Arc::clone(self);
        tokio::spawn(async move {
            inner.run_mount(spec, generation).await;
        });
    }

    /// Forces a relaunch regardless of `is_alive` — `/resume`'s probe
    /// calls this for a mount whose daemon process is technically still
    /// running but whose FUSE connection did not survive the snapshot.
    fn force_relaunch(self: &Arc<Self>, path: &str) {
        let spec = { self.lock().get(path).map(|entry| entry.spec.clone()) };
        if let Some(spec) = spec {
            self.claim_and_spawn(spec);
        }
    }

    /// Paths whose attempt has settled (`Mounted` or `Failed`): the only
    /// ones `/resume`'s probe may force a relaunch of.
    fn settled_paths(&self) -> Vec<String> {
        self.lock()
            .iter()
            .filter(|(_, entry)| entry.state.phase != MountPhase::Pending)
            .map(|(path, _)| path.clone())
            .collect()
    }
}

fn relaunch_backoff(attempt: u32) -> Duration {
    let factor = 1u32.checked_shl(attempt.min(16)).unwrap_or(u32::MAX);
    RELAUNCH_BACKOFF_BASE
        .saturating_mul(factor)
        .min(RELAUNCH_BACKOFF_MAX)
}

pub struct S3MountsFeature {
    inner: Arc<Inner>,
    supported: bool,
}

impl S3MountsFeature {
    fn new(
        device: Arc<dyn FuseDevice>,
        daemon: Arc<dyn FuseDaemon>,
        allowed_buckets: Vec<String>,
        supported: bool,
    ) -> Self {
        Self::with_ready_timeout(
            device,
            daemon,
            allowed_buckets,
            supported,
            MOUNT_READY_TIMEOUT,
        )
    }

    #[cfg_attr(not(test), allow(dead_code))]
    fn with_ready_timeout(
        device: Arc<dyn FuseDevice>,
        daemon: Arc<dyn FuseDaemon>,
        allowed_buckets: Vec<String>,
        supported: bool,
        ready_timeout: Duration,
    ) -> Self {
        let inner = Arc::new(Inner {
            device,
            daemon,
            allowed_buckets,
            mounts: Mutex::new(HashMap::new()),
            next_generation: AtomicU64::new(0),
            ready_timeout,
        });
        // A real `rayd` process is always under `#[tokio::main]` by the
        // time any feature's `build()` runs; the only context without a
        // runtime is the plain (non-async) smoke test in
        // `features::mod` that builds a whole `FeatureSet` outside one —
        // it never creates a mount either, so there is nothing to watch.
        if let Ok(handle) = tokio::runtime::Handle::try_current() {
            let watcher = Arc::clone(&inner);
            handle.spawn(async move {
                loop {
                    tokio::time::sleep(WATCHER_POLL_INTERVAL).await;
                    watcher.watch_tick();
                }
            });
        }
        Self { inner, supported }
    }
}

#[tonic::async_trait]
impl ConfigurableFeature<S3MountsConfig, S3MountsStatus> for S3MountsFeature {
    fn supported(&self) -> bool {
        self.supported
    }

    async fn apply(&self, cfg: S3MountsConfig) -> SectionOutcome {
        let desired: Vec<S3Mount> = cfg.mounts.into_iter().map(from_wire_mount).collect();
        if let Err(validation) = s3_mount::validate_mounts(&desired, &self.inner.allowed_buckets) {
            return SectionOutcome {
                code: SectionCode::Invalid,
                error_class: Some(validation.class.as_str().to_owned()),
            };
        }
        let desired_paths: HashSet<String> = desired
            .iter()
            .map(|mount| mount.mount_path.clone())
            .collect();
        let to_remove: Vec<String> = {
            let mounts = self.inner.lock();
            mounts
                .keys()
                .filter(|path| !desired_paths.contains(path.as_str()))
                .cloned()
                .collect()
        };
        for path in to_remove {
            self.inner.unmount_one(&path);
        }
        let mut any_pending = false;
        for mount in desired {
            let unchanged = {
                let mounts = self.inner.lock();
                mounts.get(&mount.mount_path).is_some_and(|entry| {
                    entry.spec == mount
                        && entry.state.phase == MountPhase::Mounted
                        && entry.pid >= 0
                        && self.inner.daemon.is_alive(entry.pid)
                })
            };
            if unchanged {
                continue;
            }
            any_pending = true;
            self.inner.claim_and_spawn(mount);
        }
        if any_pending {
            SectionOutcome {
                code: SectionCode::Pending,
                error_class: None,
            }
        } else {
            SectionOutcome::applied()
        }
    }

    async fn status(&self) -> S3MountsStatus {
        self.inner.status()
    }

    fn participant(&self) -> Option<Arc<dyn LifecycleParticipant>> {
        Some(Arc::new(S3MountsParticipant {
            inner: Arc::clone(&self.inner),
        }))
    }

    /// The `mount-s3` daemon (uid 990, below the M6 `uidrange 1000-65535`
    /// blackhole) reaches IMDS and S3 directly with the execution role.
    fn root_egress_class(&self) -> Option<RootEgressClass> {
        Some(RootEgressClass::S3)
    }
}

/// The `/suspend`/`/ready` facet of the same shared state. A distinct type
/// (rather than `S3MountsFeature` implementing both traits itself) so
/// `participant()` can clone an `Arc` out of a `&self` call without
/// `S3MountsFeature` needing to already live behind one.
struct S3MountsParticipant {
    inner: Arc<Inner>,
}

#[tonic::async_trait]
impl LifecycleParticipant for S3MountsParticipant {
    fn demand(&self) -> ParticipantDemand {
        ParticipantDemand {
            name: PARTICIPANT_NAME,
            // "`/suspend` adds no extra step" (ADR-017): the bounded
            // per-filesystem `syncfs` already covers a mounted FUSE
            // filesystem, so this participant never asks for a share of
            // its own (the default `on_suspend` is a no-op).
            max: Duration::ZERO,
        }
    }

    async fn on_resume(&self) {
        let mut probes = tokio::task::JoinSet::new();
        for path in self.inner.settled_paths() {
            let device = Arc::clone(&self.inner.device);
            probes.spawn(async move {
                let probe_path = path.clone();
                let responsive = tokio::time::timeout(
                    RESUME_PROBE_TIMEOUT,
                    tokio::task::spawn_blocking(move || device.probe_ready(&probe_path)),
                )
                .await
                .is_ok_and(|joined| joined.unwrap_or(false));
                (path, responsive)
            });
        }
        while let Some(joined) = probes.join_next().await {
            if let Ok((path, false)) = joined {
                self.inner.force_relaunch(&path);
            }
        }
    }

    fn ready_gate(&self) -> ReadyVerdict {
        // A failed or still-settling mount never holds back `/ready`: the
        // sandbox is otherwise usable, and `sbx.mounts`/`ConfigureStatus`
        // already surface the failure to the caller.
        ReadyVerdict::Ok
    }
}

fn from_wire_mount(wire: WireMount) -> S3Mount {
    S3Mount {
        mount_path: wire.mount_path,
        bucket: wire.bucket,
        prefix: wire.prefix,
        read_only: wire.read_only,
        allow_overwrite: wire.allow_overwrite,
        allow_delete: wire.allow_delete,
    }
}

fn to_wire_state(state: &MountState) -> WireMountState {
    let phase = match state.phase {
        MountPhase::Pending => S3MountPhase::Pending,
        MountPhase::Mounted => S3MountPhase::Mounted,
        MountPhase::Failed => S3MountPhase::Failed,
    };
    WireMountState {
        mount_path: state.mount_path.clone(),
        phase: i32::from(phase),
        error_class: state
            .error_class
            .map(|class| class.as_str().to_owned())
            .unwrap_or_default(),
    }
}

/// Real preconditions for this boot to actually offer `mounts=`. The
/// binary and the `rayito-mount` account ship in all four image variants
/// (one `image/Dockerfile` builds them all; only the published
/// `additionalOsCapabilities` differ), so they only prove the image is new
/// enough. What tells `rayito-base-caps` apart is the capability itself:
/// `mount(2)` needs `CAP_SYS_ADMIN` in rayd's effective set (VOL-1/Q79:
/// root without it gets `EPERM` even for a tmpfs), and `/dev/fuse` only
/// exists with it. Without this check a `rayito-base` agent would report
/// `features.s3_mounts = true`, the SDK's after-boot capability gate would
/// never fire for a custom image name, and the mount would fail later as
/// `helper_missing` instead of `UnimplementedError` before `create()`
/// returns. Computed once at `build()` time: none of these changes for the
/// life of the process.
fn detect_s3_mounts_supported() -> bool {
    detect_guest_capabilities().sys_admin()
        && binary_on_path(MOUNT_S3_BINARY)
        && std::path::Path::new(FUSE_DEVICE_PATH).exists()
        && system_user_exists(MOUNT_USER_NAME)
}

fn system_user_exists(name: &str) -> bool {
    std::fs::read_to_string("/etc/passwd").is_ok_and(|passwd| {
        passwd
            .lines()
            .any(|line| line.split(':').next() == Some(name))
    })
}

#[must_use]
pub fn build(
    _ctx: &FeatureContext,
) -> Arc<dyn ConfigurableFeature<S3MountsConfig, S3MountsStatus>> {
    let allowed_buckets =
        s3_mount::parse_allowed_buckets(&std::env::var(ALLOWED_BUCKETS_ENV).unwrap_or_default());
    let region = std::env::var(AWS_REGION_ENV).unwrap_or_default();
    let supported = detect_s3_mounts_supported();
    Arc::new(S3MountsFeature::new(
        Arc::new(LinuxFuseDevice),
        Arc::new(TokioMountS3Daemon::new(region)),
        allowed_buckets,
        supported,
    ))
}

#[cfg(test)]
mod tests {
    use std::sync::Mutex as StdMutex;

    use super::*;

    #[derive(Default)]
    struct FakeDevice {
        fail_with: StdMutex<Option<MountErrorClass>>,
        next_fd: StdMutex<i32>,
        detached: StdMutex<Vec<String>>,
        /// `false` by default would make every mount hang until
        /// `MOUNT_READY_TIMEOUT`; tests that want `Pending`/timeout
        /// behaviour set this explicitly instead.
        ready: StdMutex<bool>,
    }

    impl FakeDevice {
        fn new_ready() -> Self {
            Self {
                ready: StdMutex::new(true),
                ..Self::default()
            }
        }
    }

    impl FuseDevice for FakeDevice {
        fn attach(&self, _mount: &S3Mount) -> Result<std::os::fd::RawFd, MountErrorClass> {
            if let Some(class) = *self.fail_with.lock().unwrap() {
                return Err(class);
            }
            let mut next = self.next_fd.lock().unwrap();
            *next += 1;
            Ok(*next)
        }

        fn detach(&self, mount_path: &str, _fd: std::os::fd::RawFd) -> Result<(), MountErrorClass> {
            self.detached.lock().unwrap().push(mount_path.to_owned());
            Ok(())
        }

        fn probe_ready(&self, _mount_path: &str) -> bool {
            *self.ready.lock().unwrap()
        }
    }

    #[derive(Default)]
    struct FakeDaemon {
        fail_with: StdMutex<Option<MountErrorClass>>,
        next_pid: StdMutex<i32>,
        alive: StdMutex<Vec<i32>>,
        killed: StdMutex<Vec<i32>>,
        exit_classes: StdMutex<HashMap<i32, MountErrorClass>>,
    }

    impl FuseDaemon for FakeDaemon {
        fn spawn(&self, _fd: std::os::fd::RawFd, _mount: &S3Mount) -> Result<i32, MountErrorClass> {
            if let Some(class) = *self.fail_with.lock().unwrap() {
                return Err(class);
            }
            let mut next = self.next_pid.lock().unwrap();
            *next += 1;
            let pid = *next;
            self.alive.lock().unwrap().push(pid);
            Ok(pid)
        }

        fn is_alive(&self, pid: i32) -> bool {
            self.alive.lock().unwrap().contains(&pid)
        }

        fn exit_class(&self, pid: i32) -> MountErrorClass {
            self.exit_classes
                .lock()
                .unwrap()
                .remove(&pid)
                .unwrap_or(MountErrorClass::Network)
        }

        fn kill(&self, pid: i32) {
            self.alive
                .lock()
                .unwrap()
                .retain(|candidate| *candidate != pid);
            self.killed.lock().unwrap().push(pid);
        }
    }

    impl FakeDaemon {
        /// Simulates the daemon dying on its own (a crash, or
        /// `mount-s3` exiting for a reason `classify_exit` maps to
        /// `class`), independent of `kill`.
        fn die(&self, pid: i32, class: MountErrorClass) {
            self.alive
                .lock()
                .unwrap()
                .retain(|candidate| *candidate != pid);
            self.exit_classes.lock().unwrap().insert(pid, class);
        }
    }

    fn feature(allowed: &[&str]) -> (S3MountsFeature, Arc<FakeDevice>, Arc<FakeDaemon>) {
        feature_with_support(allowed, true)
    }

    fn feature_with_support(
        allowed: &[&str],
        supported: bool,
    ) -> (S3MountsFeature, Arc<FakeDevice>, Arc<FakeDaemon>) {
        let device = Arc::new(FakeDevice::new_ready());
        let daemon = Arc::new(FakeDaemon::default());
        let allowed_buckets = allowed.iter().map(|bucket| (*bucket).to_owned()).collect();
        let feature = S3MountsFeature::new(
            device.clone() as Arc<dyn FuseDevice>,
            daemon.clone() as Arc<dyn FuseDaemon>,
            allowed_buckets,
            supported,
        );
        (feature, device, daemon)
    }

    fn wire_mount(path: &str, bucket: &str, read_only: bool) -> WireMount {
        WireMount {
            mount_path: path.to_owned(),
            bucket: bucket.to_owned(),
            prefix: String::new(),
            read_only,
            allow_overwrite: false,
            allow_delete: false,
        }
    }

    /// Polls `status()` until `path` leaves `Pending` (or a bounded number
    /// of yields passes): the attach/spawn/readiness sequence runs in a
    /// background task since the M15-s3-mounts `Pending`-on-accept change,
    /// so a test must let the runtime make progress before asserting on
    /// the settled state.
    async fn wait_settled(feature: &S3MountsFeature, path: &str) -> WireMountState {
        for _ in 0..500 {
            let status = feature.status().await;
            if let Some(state) = status.mounts.iter().find(|m| m.mount_path == path)
                && state.phase != i32::from(S3MountPhase::Pending)
            {
                return state.clone();
            }
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
        panic!("{path} never left Pending");
    }

    #[tokio::test]
    async fn an_unsupported_build_never_claims_support() {
        let (feature, ..) = feature_with_support(&["team-data"], false);
        assert!(!feature.supported());
    }

    #[tokio::test]
    async fn a_supported_build_claims_support() {
        let (feature, ..) = feature(&["team-data"]);
        assert!(feature.supported());
        assert_eq!(feature.root_egress_class(), Some(RootEgressClass::S3));
    }

    #[tokio::test]
    async fn a_bucket_outside_the_allowlist_is_rejected_before_mounting_anything() {
        let (feature, device, daemon) = feature(&["team-data"]);
        let outcome = feature
            .apply(S3MountsConfig {
                mounts: vec![wire_mount("/mnt/data", "other-bucket", true)],
            })
            .await;
        assert_eq!(outcome.code, SectionCode::Invalid);
        assert_eq!(outcome.error_class.as_deref(), Some("not_allowed"));
        assert!(device.detached.lock().unwrap().is_empty());
        assert!(daemon.alive.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn an_invalid_path_shape_is_rejected_even_on_an_allowed_bucket() {
        let (feature, ..) = feature(&["team-data"]);
        let outcome = feature
            .apply(S3MountsConfig {
                mounts: vec![wire_mount("/etc/cron.d", "team-data", true)],
            })
            .await;
        assert_eq!(outcome.code, SectionCode::Invalid);
        assert_eq!(outcome.error_class.as_deref(), Some("invalid_path"));
    }

    #[tokio::test]
    async fn an_allowed_mount_is_pending_then_settles_to_mounted() {
        let (feature, _device, daemon) = feature(&["team-data"]);
        let outcome = feature
            .apply(S3MountsConfig {
                mounts: vec![wire_mount("/mnt/data", "team-data", true)],
            })
            .await;
        assert_eq!(outcome.code, SectionCode::Pending);
        let state = wait_settled(&feature, "/mnt/data").await;
        assert_eq!(state.phase, i32::from(S3MountPhase::Mounted));
        assert_eq!(daemon.alive.lock().unwrap().len(), 1);
    }

    #[tokio::test]
    async fn a_daemon_spawn_failure_settles_to_failed_with_its_class() {
        let (feature, _device, daemon) = feature(&["team-data"]);
        *daemon.fail_with.lock().unwrap() = Some(MountErrorClass::HelperMissing);
        feature
            .apply(S3MountsConfig {
                mounts: vec![wire_mount("/mnt/data", "team-data", true)],
            })
            .await;
        let state = wait_settled(&feature, "/mnt/data").await;
        assert_eq!(state.phase, i32::from(S3MountPhase::Failed));
        assert_eq!(state.error_class, "helper_missing");
    }

    #[tokio::test]
    async fn a_mount_that_never_becomes_ready_times_out_and_is_torn_down() {
        // A real (short) timeout rather than `MOUNT_READY_TIMEOUT` (10s)
        // sped up with virtual time: `await_ready`'s own probe hops
        // through `spawn_blocking`, and mixing that with
        // `tokio::time::pause`/`advance` is its own source of flakiness.
        let device = Arc::new(FakeDevice::default()); // `ready` defaults to `false`
        let daemon = Arc::new(FakeDaemon::default());
        let feature = S3MountsFeature::with_ready_timeout(
            device.clone() as Arc<dyn FuseDevice>,
            daemon.clone() as Arc<dyn FuseDaemon>,
            vec!["team-data".to_owned()],
            true,
            Duration::from_millis(50),
        );
        feature
            .apply(S3MountsConfig {
                mounts: vec![wire_mount("/mnt/data", "team-data", true)],
            })
            .await;
        let state = wait_settled(&feature, "/mnt/data").await;
        assert_eq!(state.phase, i32::from(S3MountPhase::Failed));
        assert_eq!(state.error_class, "timeout");
        assert!(daemon.killed.lock().unwrap().contains(&1));
        assert!(
            device
                .detached
                .lock()
                .unwrap()
                .contains(&"/mnt/data".to_owned())
        );
    }

    #[tokio::test]
    async fn a_mount_dropped_from_a_later_request_is_unmounted() {
        let (feature, device, daemon) = feature(&["team-data"]);
        feature
            .apply(S3MountsConfig {
                mounts: vec![wire_mount("/mnt/data", "team-data", true)],
            })
            .await;
        wait_settled(&feature, "/mnt/data").await;
        let outcome = feature.apply(S3MountsConfig { mounts: vec![] }).await;
        assert_eq!(outcome.code, SectionCode::Applied);
        assert!(
            device
                .detached
                .lock()
                .unwrap()
                .contains(&"/mnt/data".to_owned())
        );
        assert!(daemon.killed.lock().unwrap().contains(&1));
        assert!(feature.status().await.mounts.is_empty());
    }

    #[tokio::test]
    async fn reapplying_the_same_mount_is_a_no_op() {
        let (feature, device, daemon) = feature(&["team-data"]);
        let request = S3MountsConfig {
            mounts: vec![wire_mount("/mnt/data", "team-data", true)],
        };
        feature.apply(request.clone()).await;
        wait_settled(&feature, "/mnt/data").await;
        let outcome = feature.apply(request).await;
        assert_eq!(outcome.code, SectionCode::Applied);
        // Only one attach/spawn pair happened across both calls.
        assert_eq!(*device.next_fd.lock().unwrap(), 1);
        assert_eq!(daemon.alive.lock().unwrap().len(), 1);
    }

    #[tokio::test]
    async fn changing_read_only_on_the_same_path_kills_the_old_daemon_before_the_new_attach() {
        let (feature, device, daemon) = feature(&["team-data"]);
        feature
            .apply(S3MountsConfig {
                mounts: vec![wire_mount("/mnt/data", "team-data", true)],
            })
            .await;
        wait_settled(&feature, "/mnt/data").await;
        feature
            .apply(S3MountsConfig {
                mounts: vec![wire_mount("/mnt/data", "team-data", false)],
            })
            .await;
        wait_settled(&feature, "/mnt/data").await;
        assert!(daemon.killed.lock().unwrap().contains(&1));
        assert!(
            device
                .detached
                .lock()
                .unwrap()
                .contains(&"/mnt/data".to_owned())
        );
        assert_eq!(*daemon.alive.lock().unwrap(), vec![2]);
    }

    #[tokio::test]
    async fn an_empty_section_unmounts_every_current_mount() {
        let (feature, device, _daemon) = feature(&["a", "b"]);
        feature
            .apply(S3MountsConfig {
                mounts: vec![
                    wire_mount("/mnt/a", "a", true),
                    wire_mount("/mnt/b", "b", true),
                ],
            })
            .await;
        wait_settled(&feature, "/mnt/a").await;
        wait_settled(&feature, "/mnt/b").await;
        feature.apply(S3MountsConfig { mounts: vec![] }).await;
        assert_eq!(device.detached.lock().unwrap().len(), 2);
        assert!(feature.status().await.mounts.is_empty());
    }

    #[tokio::test]
    async fn a_spawn_failure_followed_by_an_empty_section_detaches_exactly_once() {
        // Force the spawn (not the attach) to fail, so `run_mount` itself
        // detaches the fd it just attached and stores `fd: None`.
        let device = Arc::new(FakeDevice::new_ready());
        let daemon = Arc::new(FakeDaemon::default());
        *daemon.fail_with.lock().unwrap() = Some(MountErrorClass::HelperMissing);
        let feature = S3MountsFeature::new(
            device.clone() as Arc<dyn FuseDevice>,
            daemon as Arc<dyn FuseDaemon>,
            vec!["team-data".to_owned()],
            true,
        );
        feature
            .apply(S3MountsConfig {
                mounts: vec![wire_mount("/mnt/data", "team-data", true)],
            })
            .await;
        wait_settled(&feature, "/mnt/data").await;
        assert_eq!(device.detached.lock().unwrap().len(), 1);
        feature.apply(S3MountsConfig { mounts: vec![] }).await;
        // Still exactly one detach: the failed entry had `fd: None`, so
        // `unmount_one` had nothing left to close.
        assert_eq!(device.detached.lock().unwrap().len(), 1);
    }

    #[tokio::test]
    async fn a_dead_daemon_is_relaunched_by_the_background_watcher() {
        let (feature, device, daemon) = feature(&["team-data"]);
        feature
            .apply(S3MountsConfig {
                mounts: vec![wire_mount("/mnt/data", "team-data", true)],
            })
            .await;
        wait_settled(&feature, "/mnt/data").await;
        daemon.die(1, MountErrorClass::Network);
        // Real time, not `pause`/`advance`: the background watcher only
        // notices on its own `WATCHER_POLL_INTERVAL` tick, and mixing a
        // virtual clock with the `spawn_blocking` hop inside `probe()` is
        // its own source of flakiness. Waits for *both* `Mounted` and the
        // new pid, since the stale `Mounted` state from the first mount
        // (pid 1, now dead) is still what `status()` reports until the
        // watcher actually relaunches it.
        let deadline = tokio::time::Instant::now() + WATCHER_POLL_INTERVAL + Duration::from_secs(3);
        let mut relaunched = None;
        while tokio::time::Instant::now() < deadline {
            let status = feature.status().await;
            let found = status
                .mounts
                .iter()
                .find(|m| m.mount_path == "/mnt/data")
                .cloned();
            if let Some(state) = &found
                && state.phase == i32::from(S3MountPhase::Mounted)
                && daemon.alive.lock().unwrap().contains(&2)
            {
                relaunched = found;
                break;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        let state = relaunched.expect("mount relaunched to Mounted under a new pid");
        assert_eq!(state.phase, i32::from(S3MountPhase::Mounted));
        assert!(
            device
                .detached
                .lock()
                .unwrap()
                .contains(&"/mnt/data".to_owned())
        );
    }

    #[tokio::test]
    async fn a_pending_mount_is_never_relaunched_on_top_of_its_own_attempt() {
        // Never ready, with a bound far beyond the test: the entry stays
        // `Pending` for the whole test, first with `pid == -1` (the window
        // right after `claim`) and then with the spawned pid published.
        let device = Arc::new(FakeDevice::default());
        let daemon = Arc::new(FakeDaemon::default());
        let feature = S3MountsFeature::with_ready_timeout(
            device.clone() as Arc<dyn FuseDevice>,
            daemon.clone() as Arc<dyn FuseDaemon>,
            vec!["team-data".to_owned()],
            true,
            Duration::from_secs(60),
        );
        feature
            .apply(S3MountsConfig {
                mounts: vec![wire_mount("/mnt/data", "team-data", true)],
            })
            .await;
        feature.inner.watch_tick();
        for _ in 0..200 {
            if !daemon.alive.lock().unwrap().is_empty() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
        feature.inner.watch_tick();
        tokio::time::sleep(Duration::from_millis(50)).await;
        assert_eq!(*device.next_fd.lock().unwrap(), 1);
        assert_eq!(*daemon.alive.lock().unwrap(), vec![1]);
        assert!(daemon.killed.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn the_participant_demands_no_suspend_share() {
        let (feature, ..) = feature(&["team-data"]);
        let participant = feature
            .participant()
            .expect("s3-mounts always joins /suspend+/ready");
        assert_eq!(participant.demand().max, Duration::ZERO);
        assert_eq!(participant.ready_gate(), ReadyVerdict::Ok);
    }

    #[test]
    fn relaunch_backoff_doubles_and_caps() {
        assert_eq!(relaunch_backoff(0), RELAUNCH_BACKOFF_BASE);
        assert_eq!(relaunch_backoff(1), RELAUNCH_BACKOFF_BASE * 2);
        assert_eq!(relaunch_backoff(10), RELAUNCH_BACKOFF_MAX);
    }

    #[test]
    fn system_user_exists_reads_etc_passwd_by_login_name() {
        assert!(system_user_exists("root"));
        assert!(!system_user_exists("not-a-real-rayito-user"));
    }
}
