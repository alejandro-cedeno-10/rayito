//! Real slot for `m15-s3-mounts` (ADR-017): a present `S3MountsConfig`
//! replaces the whole desired set of S3 mounts (mounts removed from it are
//! unmounted, new ones attached and started), rejected whole (no mount
//! touched) when a bucket is not on the image's own
//! `RAYITO_ALLOWED_MOUNT_BUCKETS` allowlist or the request repeats a mount
//! path. With `s3_mounts` absent from every `Configure` call, nothing in
//! this module runs: no `/dev/fuse` open, no `mount-s3` spawn, no new
//! environment variable read beyond what `build()` already did once at
//! startup (ADR-014 rule 4).

use std::collections::HashMap;
use std::sync::{Arc, Mutex, PoisonError};
use std::time::Duration;

use rayd_core::configure::{SectionCode, SectionOutcome};
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
use crate::adapters::{LinuxFuseDevice, TokioMountS3Daemon};
use crate::lifecycle::{LifecycleParticipant, ReadyVerdict};

/// Image-level bucket allowlist (`rayito image publish --env
/// RAYITO_ALLOWED_MOUNT_BUCKETS=team-data,runs-bucket`), read once at
/// agent startup — never a per-request activation switch (ADR-014 rule 4).
/// Absent or empty means no bucket is allowed, not "every bucket".
const ALLOWED_BUCKETS_ENV: &str = "RAYITO_ALLOWED_MOUNT_BUCKETS";
/// The platform's own region, read the same way `adapters::s3_store`
/// already reads it for ADR-009 persistence; `mount-s3`'s own `AWS_REGION`
/// comes from this, never a per-request value.
const AWS_REGION_ENV: &str = "AWS_REGION";
/// `/resume`'s liveness probe share (§7.2 of the M15 architecture):
/// "`/suspend` adds no extra step" for this feature, so its `/suspend`
/// demand is zero; this bound is `on_resume`'s own relaunch-detection
/// probe, not a `SuspendShares` allocation.
const RESUME_PROBE_TIMEOUT: Duration = Duration::from_secs(2);
const PARTICIPANT_NAME: &str = "s3_mounts";

struct MountEntry {
    spec: S3Mount,
    fd: std::os::fd::RawFd,
    pid: i32,
    state: MountState,
}

/// Shared between the `ConfigurableFeature` facade and the
/// `LifecycleParticipant` facade below, so `participant()` can hand out an
/// independent `Arc` without `Self` needing to be constructed behind one.
struct Inner {
    device: Arc<dyn FuseDevice>,
    daemon: Arc<dyn FuseDaemon>,
    allowed_buckets: Vec<String>,
    mounts: Mutex<HashMap<String, MountEntry>>,
}

impl Inner {
    fn lock(&self) -> std::sync::MutexGuard<'_, HashMap<String, MountEntry>> {
        self.mounts.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// Attaches FUSE and starts `mount-s3` for `mount`, replacing any
    /// existing entry at the same path; `mount` is assumed already
    /// validated (allowlist, no duplicate path within the request).
    fn mount_one(&self, mount: S3Mount) -> Result<(), MountErrorClass> {
        let fd = self.device.attach(&mount)?;
        let pid = match self.daemon.spawn(fd, &mount) {
            Ok(pid) => pid,
            Err(class) => {
                let _ = self.device.detach(&mount.mount_path, fd);
                self.lock().insert(
                    mount.mount_path.clone(),
                    MountEntry {
                        state: MountState::failed(mount.mount_path.clone(), class),
                        spec: mount,
                        fd,
                        pid: -1,
                    },
                );
                return Err(class);
            }
        };
        self.lock().insert(
            mount.mount_path.clone(),
            MountEntry {
                state: MountState::mounted(mount.mount_path.clone()),
                spec: mount,
                fd,
                pid,
            },
        );
        Ok(())
    }

    fn unmount_one(&self, mount_path: &str) {
        let entry = self.lock().remove(mount_path);
        if let Some(entry) = entry {
            if entry.pid >= 0 {
                self.daemon.kill(entry.pid);
            }
            let _ = self.device.detach(mount_path, entry.fd);
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

    /// `/resume`'s probe (§7.2): a dead daemon is relaunched from the
    /// entry's own stored spec; the FUSE attach is assumed to have
    /// survived the pause (the mount and its descriptor are ordinary
    /// kernel state, checkpointed with the rest of the guest), so only
    /// the daemon process is restarted, never re-attached.
    fn relaunch_dead_daemons(&self) {
        let dead_paths: Vec<String> = {
            let mounts = self.lock();
            mounts
                .iter()
                .filter(|(_, entry)| entry.pid >= 0 && !self.daemon.is_alive(entry.pid))
                .map(|(path, _)| path.clone())
                .collect()
        };
        for path in dead_paths {
            let (fd, spec) = {
                let mounts = self.lock();
                match mounts.get(&path) {
                    Some(entry) => (entry.fd, entry.spec.clone()),
                    None => continue,
                }
            };
            match self.daemon.spawn(fd, &spec) {
                Ok(pid) => {
                    let mut mounts = self.lock();
                    if let Some(entry) = mounts.get_mut(&path) {
                        entry.pid = pid;
                        entry.state = MountState::mounted(path.clone());
                    }
                }
                Err(class) => {
                    let mut mounts = self.lock();
                    if let Some(entry) = mounts.get_mut(&path) {
                        entry.pid = -1;
                        entry.state = MountState::failed(path.clone(), class);
                    }
                }
            }
        }
    }
}

pub struct S3MountsFeature {
    inner: Arc<Inner>,
}

impl S3MountsFeature {
    fn new(
        device: Arc<dyn FuseDevice>,
        daemon: Arc<dyn FuseDaemon>,
        allowed_buckets: Vec<String>,
    ) -> Self {
        Self {
            inner: Arc::new(Inner {
                device,
                daemon,
                allowed_buckets,
                mounts: Mutex::new(HashMap::new()),
            }),
        }
    }
}

#[tonic::async_trait]
impl ConfigurableFeature<S3MountsConfig, S3MountsStatus> for S3MountsFeature {
    fn supported(&self) -> bool {
        true
    }

    async fn apply(&self, cfg: S3MountsConfig) -> SectionOutcome {
        let desired: Vec<S3Mount> = cfg.mounts.into_iter().map(from_wire_mount).collect();
        if let Err(validation) = s3_mount::validate_mounts(&desired, &self.inner.allowed_buckets) {
            return SectionOutcome {
                code: SectionCode::Invalid,
                error_class: Some(validation.class.as_str().to_owned()),
            };
        }
        let desired_paths: std::collections::HashSet<String> = desired
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
        let mut first_failure: Option<MountErrorClass> = None;
        for mount in desired {
            let already_applied = self
                .inner
                .lock()
                .get(&mount.mount_path)
                .is_some_and(|entry| entry.spec == mount && entry.pid >= 0);
            if already_applied {
                continue;
            }
            if let Err(class) = self.inner.mount_one(mount) {
                first_failure.get_or_insert(class);
            }
        }
        match first_failure {
            Some(class) => SectionOutcome {
                code: SectionCode::Failed,
                error_class: Some(class.as_str().to_owned()),
            },
            None => SectionOutcome::applied(),
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
            // "`/suspend` adds no extra step" (§7.2): the bounded
            // per-filesystem `syncfs` already covers a mounted FUSE
            // filesystem, so this participant never asks for a share of
            // its own (the default `on_suspend` is a no-op).
            max: Duration::ZERO,
        }
    }

    async fn on_resume(&self) {
        let mount_paths: Vec<String> = { self.inner.lock().keys().cloned().collect() };
        for mount_path in mount_paths {
            let probe = tokio::process::Command::new("stat")
                .arg(&mount_path)
                .status();
            let responsive = tokio::time::timeout(RESUME_PROBE_TIMEOUT, probe)
                .await
                .is_ok_and(|result| result.is_ok_and(|status| status.success()));
            if !responsive {
                self.inner.relaunch_dead_daemons();
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

#[must_use]
pub fn build(
    _ctx: &FeatureContext,
) -> Arc<dyn ConfigurableFeature<S3MountsConfig, S3MountsStatus>> {
    let allowed_buckets =
        s3_mount::parse_allowed_buckets(&std::env::var(ALLOWED_BUCKETS_ENV).unwrap_or_default());
    let region = std::env::var(AWS_REGION_ENV).unwrap_or_default();
    Arc::new(S3MountsFeature::new(
        Arc::new(LinuxFuseDevice),
        Arc::new(TokioMountS3Daemon::new(region)),
        allowed_buckets,
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
    }

    #[derive(Default)]
    struct FakeDaemon {
        fail_with: StdMutex<Option<MountErrorClass>>,
        next_pid: StdMutex<i32>,
        alive: StdMutex<Vec<i32>>,
        killed: StdMutex<Vec<i32>>,
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

        fn kill(&self, pid: i32) {
            self.alive
                .lock()
                .unwrap()
                .retain(|candidate| *candidate != pid);
            self.killed.lock().unwrap().push(pid);
        }
    }

    fn feature(allowed: &[&str]) -> (S3MountsFeature, Arc<FakeDevice>, Arc<FakeDaemon>) {
        let device = Arc::new(FakeDevice::default());
        let daemon = Arc::new(FakeDaemon::default());
        let allowed_buckets = allowed.iter().map(|bucket| (*bucket).to_owned()).collect();
        let feature = S3MountsFeature::new(
            device.clone() as Arc<dyn FuseDevice>,
            daemon.clone() as Arc<dyn FuseDaemon>,
            allowed_buckets,
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

    #[test]
    fn supported_is_always_true_once_a_real_slot_exists() {
        let (feature, ..) = feature(&["team-data"]);
        assert!(feature.supported());
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
    async fn an_allowed_mount_is_attached_spawned_and_reported_mounted() {
        let (feature, _device, daemon) = feature(&["team-data"]);
        let outcome = feature
            .apply(S3MountsConfig {
                mounts: vec![wire_mount("/mnt/data", "team-data", true)],
            })
            .await;
        assert_eq!(outcome.code, SectionCode::Applied);
        assert_eq!(daemon.alive.lock().unwrap().len(), 1);
        let status = feature.status().await;
        assert_eq!(status.mounts.len(), 1);
        assert_eq!(status.mounts[0].mount_path, "/mnt/data");
        assert_eq!(status.mounts[0].phase, i32::from(S3MountPhase::Mounted));
    }

    #[tokio::test]
    async fn a_daemon_spawn_failure_reports_failed_with_its_class() {
        let (feature, _device, daemon) = feature(&["team-data"]);
        *daemon.fail_with.lock().unwrap() = Some(MountErrorClass::HelperMissing);
        let outcome = feature
            .apply(S3MountsConfig {
                mounts: vec![wire_mount("/mnt/data", "team-data", true)],
            })
            .await;
        assert_eq!(outcome.code, SectionCode::Failed);
        assert_eq!(outcome.error_class.as_deref(), Some("helper_missing"));
        let status = feature.status().await;
        assert_eq!(status.mounts[0].phase, i32::from(S3MountPhase::Failed));
        assert_eq!(status.mounts[0].error_class, "helper_missing");
    }

    #[tokio::test]
    async fn a_mount_dropped_from_a_later_request_is_unmounted() {
        let (feature, device, daemon) = feature(&["team-data"]);
        feature
            .apply(S3MountsConfig {
                mounts: vec![wire_mount("/mnt/data", "team-data", true)],
            })
            .await;
        let outcome = feature.apply(S3MountsConfig { mounts: vec![] }).await;
        assert_eq!(outcome.code, SectionCode::Applied);
        assert_eq!(
            *device.detached.lock().unwrap(),
            vec!["/mnt/data".to_owned()]
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
        feature.apply(request).await;
        // Only one attach/spawn pair happened across both calls.
        assert_eq!(*device.next_fd.lock().unwrap(), 1);
        assert_eq!(daemon.alive.lock().unwrap().len(), 1);
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
        feature.apply(S3MountsConfig { mounts: vec![] }).await;
        assert_eq!(device.detached.lock().unwrap().len(), 2);
        assert!(feature.status().await.mounts.is_empty());
    }

    #[test]
    fn the_participant_demands_no_suspend_share() {
        let (feature, ..) = feature(&["team-data"]);
        let participant = feature
            .participant()
            .expect("s3-mounts always joins /suspend+/ready");
        assert_eq!(participant.demand().max, Duration::ZERO);
        assert_eq!(participant.ready_gate(), ReadyVerdict::Ok);
    }
}
