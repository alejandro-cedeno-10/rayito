//! EFS mount adapter (`m15-efs-volumes`, ADR-018, experimental):
//! `EfsUtilsMounter` mounts one access point with `amazon-efs-utils`
//! (`mount -t efs -o tls,iam,accesspoint=<fsap>[,mounttargetip=<ip>][,ro]
//! <fs-id>:/ <dir>`, the invocation `AWS_API_NOTES.md` §16 Q128 measured
//! without `systemd`: 20 of 20 mounts, p50 313 ms, p95 589 ms) and owns
//! everything that invocation leaves behind:
//!
//! - **`efs-proxy`**: every mount starts its own proxy (the local end of the
//!   TLS tunnel) and `umount` does not stop it (Q128: 20 processes after 20
//!   cycles). The mount runs under `mount_lock`, the proxies present after
//!   the helper returned that were not there before are this mount's
//!   (`rayd_core::volume::proxy`), and `unmount` stops exactly those:
//!   `SIGTERM`, then `SIGKILL` after `PROXY_TERM_GRACE`. The helper itself
//!   (`mount`, then `mount.efs`) is `rayd`'s child and is spawned through
//!   `ChildRegistry`, so the orphan reaper never takes its exit status.
//!   `efs-proxy` is not: `mount.efs` starts it and exits, so it re-parents
//!   to PID 1; `rayd` never `waitpid`s it, tells it apart by pid *and*
//!   kernel start time, and leaves its zombie to the orphan reaper.
//! - **The mountpoint**: the helper mounts on a root-only staging directory
//!   (`STAGING_DIR`, under `/run/rayito`, which uid 1000 cannot write), and
//!   the result is bind-mounted onto the requested path through the
//!   symlink-proof walk of `adapters::mountpoint` — `mount(8)` resolves a
//!   path argument, and uid 1000 owns every allowed root under
//!   `/home/user`, so handing it the requested path could mount a
//!   sandbox-controlled tree over `/etc`.
//! - **Credentials**: the helper signs the tunnel with the execution role's
//!   IMDS credentials and nothing renews them without `systemd`'s watchdog.
//!   `mount` reports their expiry (`MountReceipt`) so `/resume` can remount
//!   a volume whose pause crossed it (Q129, EFS-12).
//! - **Flush**: `flush` runs one `syncfs` per volume on a throwaway thread
//!   (`bounded_sync::spawn_syncfs_thread`) and stops waiting at the
//!   caller's deadline (Q130, EFS-13).
//!
//! The helper's stderr is drained (bounded) only to pick one closed
//! `MountFailureClass`; it never reaches a log line or the wire.

use std::collections::{HashMap, HashSet};
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::{Duration, SystemTime};

use nix::sys::signal::Signal;
use rayd_core::orphans::ProcessTable;
use rayd_core::process::env::DEFAULT_PATH;
use rayd_core::suspend_sync::FilesystemSync;
use rayd_core::volume::{
    BoxFuture, FlushOutcome, MountFailure, MountFailureClass, MountPath, MountReceipt,
    MountSupport, ProbeOutcome, ProxyProcess, UnmountMode, VolumeMounter, VolumeSpec,
    is_still_running, spawned_between,
};
use tokio::process::Command;

use super::bounded_sync::{PlatformFilesystemSync, spawn_syncfs_thread};
use super::capabilities::{binary_on_path, detect_guest_capabilities, kernel_supports_filesystem};
use super::child_registry::ChildRegistry;
use super::credential_broker::ImdsCredentialBroker;
use super::mount_s3::drain_stderr_tail;
use super::mountpoint::{
    FILESYSTEM_ROOT, MountpointError, StatProbe, bind_onto, detach, open_dir_no_symlinks,
    stat_as_guest, unmount_no_symlinks,
};
use super::procfs_process_table::ProcfsProcessTable;
use super::sidecar_process::signal_process_group;
use crate::features::s3_mounts::AWS_REGION_ENV;

/// `util-linux`'s `mount`, which hands `-t efs` to `mount.efs`.
pub const MOUNT_BINARY: &str = "mount";
/// Where `amazon-efs-utils` 3.1.3 from the AL2023 repository installs its
/// mount helper and its proxy (`AWS_API_NOTES.md` §16 Q122).
pub const MOUNT_EFS_HELPER: &str = "/usr/sbin/mount.efs";
pub const EFS_PROXY_BINARY: &str = "/usr/sbin/efs-proxy";
/// The mount helper's filesystem type, and the kernel type it ends up
/// mounting (`/proc/filesystems`, Q79).
const EFS_FS_TYPE: &str = "efs";
const NFS4_FS_TYPE: &str = "nfs4";
/// `mount.efs` options, exactly the measured set (Q128) plus `ro` for a
/// read-only volume: TLS tunnel, IAM authentication with the execution
/// role, the volume's access point and, when known, the mount target's IP.
const OPTION_TLS: &str = "tls";
const OPTION_IAM: &str = "iam";
const OPTION_ACCESS_POINT: &str = "accesspoint";
const OPTION_MOUNT_TARGET_IP: &str = "mounttargetip";
const OPTION_READ_ONLY: &str = "ro";
/// The access point's root is the whole volume (`RootDirectory` of
/// `VolumeStore.create`), so the helper always mounts `/` of it.
const EXPORT_ROOT: &str = ":/";
/// Root-only mountpoint the helper mounts on before the bind onto the
/// requested path. `/run/rayito` is created by `rayd` (root, `0755`) and is
/// on `FilesystemService`'s deny list, so uid 1000 can neither create nor
/// swap anything here.
pub const STAGING_DIR: &str = "/run/rayito/efs-staging";
const STAGING_DIR_MODE: u32 = 0o700;
/// Bound on one run of the mount helper. Q128 measured p95 589 ms for a
/// working mount and Q123 ≈ 10 s for `mount.efs` to give up on an
/// unreachable mount target by itself (three 2049 connection attempts):
/// the bound sits above both, so the helper's own diagnosis wins over
/// `timeout`.
const MOUNT_HELPER_TIMEOUT: Duration = Duration::from_secs(15);
/// `SIGTERM` to a volume's `efs-proxy`, then this long for it to exit
/// before `SIGKILL`; `PROXY_KILL_GRACE` more to see it gone. Together they
/// stay well inside `hooks::PARTICIPANT_TERMINATE_TIMEOUT` (1 s), the
/// tightest budget an unmount runs under.
const PROXY_TERM_GRACE: Duration = Duration::from_millis(400);
const PROXY_KILL_GRACE: Duration = Duration::from_millis(200);
const PROXY_POLL_INTERVAL: Duration = Duration::from_millis(20);
/// Only a root process is ever taken for the volume's proxy: uid 1000 can
/// run `/usr/sbin/efs-proxy` itself, but never as root.
const ROOT_UID: u32 = 0;
/// `/proc/<pid>/exe` of a process.
const PROC_ROOT: &str = "/proc";
const PROC_EXE: &str = "exe";
/// The one variable besides `AWS_REGION` the helper inherits.
const PATH_ENV: &str = "PATH";

/// What `EfsUtilsMounter` needs from the guest, so its rules (proxy
/// attribution, the stop sequence, the flush bookkeeping) are tested with a
/// fake; `LinuxEfsHost` is the real one.
pub trait EfsHost: Send + Sync {
    /// Creates `STAGING_DIR` if needed and detaches whatever an earlier,
    /// interrupted mount left on it.
    fn prepare_staging(&self) -> Result<(), MountFailureClass>;
    /// Runs the mount helper with `args`, waiting at most `timeout`.
    fn run_helper(
        &self,
        args: Vec<String>,
        timeout: Duration,
    ) -> BoxFuture<'_, Result<(), MountFailureClass>>;
    /// Bind-mounts the staging mount onto `mount_path`, then detaches the
    /// staging mount.
    fn publish(&self, mount_path: &MountPath) -> Result<(), MountFailureClass>;
    /// Detaches the staging mount after a failed mount.
    fn discard_staging(&self);
    /// Lazily detaches `mount_path`; a path with nothing mounted is `Ok`.
    fn detach(&self, mount_path: &MountPath) -> Result<(), MountFailureClass>;
    /// Every running root `efs-proxy`.
    fn proxies(&self) -> Vec<ProxyProcess>;
    fn is_running(&self, proxy: ProxyProcess) -> bool;
    /// `SIGTERM` (or `SIGKILL` with `force`) to `proxy`, if it is still the
    /// same running process.
    fn stop(&self, proxy: ProxyProcess, force: bool);
    fn stat(&self, mount_path: &MountPath, budget: Duration) -> BoxFuture<'_, StatProbe>;
    fn filesystem_sync(&self) -> Arc<dyn FilesystemSync>;
}

/// Where a mount learns when the credentials it was signed with expire.
pub trait LeaseSource: Send + Sync {
    fn credentials_expire_at(&self) -> BoxFuture<'_, Option<SystemTime>>;
}

/// The execution role's lease from the broker every feature shares: the
/// same IMDS credentials `efs-utils` read a moment earlier (IMDS serves one
/// set until it rotates them, Q81), or an older cached set that expires no
/// later, which only makes `/resume` remount earlier, never later.
impl LeaseSource for ImdsCredentialBroker {
    fn credentials_expire_at(&self) -> BoxFuture<'_, Option<SystemTime>> {
        Box::pin(async move {
            self.ensure()
                .await
                .ok()
                .map(|credentials| credentials.expires_at)
        })
    }
}

pub struct EfsUtilsMounter {
    host: Arc<dyn EfsHost>,
    leases: Arc<dyn LeaseSource>,
    support: MountSupport,
    /// Serializes mounts, so the proxies one mount started are the only new
    /// ones between its two snapshots.
    mount_lock: tokio::sync::Mutex<()>,
    /// The proxies each mounted path started, stopped on its unmount.
    proxies: Mutex<HashMap<String, Vec<ProxyProcess>>>,
    /// Paths whose `syncfs` of an earlier `/suspend` has not returned.
    flushing: Arc<Mutex<HashSet<String>>>,
}

impl EfsUtilsMounter {
    #[must_use]
    pub fn new(
        host: Arc<dyn EfsHost>,
        leases: Arc<dyn LeaseSource>,
        support: MountSupport,
    ) -> Self {
        Self {
            host,
            leases,
            support,
            mount_lock: tokio::sync::Mutex::new(()),
            proxies: Mutex::new(HashMap::new()),
            flushing: Arc::new(Mutex::new(HashSet::new())),
        }
    }

    fn proxy_table(&self) -> MutexGuard<'_, HashMap<String, Vec<ProxyProcess>>> {
        self.proxies.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// `SIGTERM` to every proxy still running, `SIGKILL` to whatever
    /// survives `PROXY_TERM_GRACE`; logs (a count, never a pid list) any
    /// left after `PROXY_KILL_GRACE`.
    async fn stop_proxies(&self, proxies: &[ProxyProcess]) {
        let running = self.still_running(proxies);
        for proxy in &running {
            self.host.stop(*proxy, false);
        }
        let survivors = self.wait_stopped(&running, PROXY_TERM_GRACE).await;
        for proxy in &survivors {
            self.host.stop(*proxy, true);
        }
        let stubborn = self.wait_stopped(&survivors, PROXY_KILL_GRACE).await;
        if !stubborn.is_empty() {
            tracing::warn!(remaining = stubborn.len(), "efs-proxy did not stop");
        }
    }

    fn still_running(&self, proxies: &[ProxyProcess]) -> Vec<ProxyProcess> {
        proxies
            .iter()
            .copied()
            .filter(|proxy| self.host.is_running(*proxy))
            .collect()
    }

    async fn wait_stopped(&self, proxies: &[ProxyProcess], grace: Duration) -> Vec<ProxyProcess> {
        let deadline = tokio::time::Instant::now() + grace;
        loop {
            let running = self.still_running(proxies);
            if running.is_empty() || tokio::time::Instant::now() >= deadline {
                return running;
            }
            tokio::time::sleep(PROXY_POLL_INTERVAL).await;
        }
    }

    async fn mount_locked(&self, spec: &VolumeSpec) -> Result<(), MountFailureClass> {
        self.host.prepare_staging()?;
        let before = self.host.proxies();
        let helped = self
            .host
            .run_helper(helper_args(spec), MOUNT_HELPER_TIMEOUT)
            .await;
        let started = spawned_between(&before, &self.host.proxies());
        let published = helped.and_then(|()| self.host.publish(&spec.mount_path));
        if let Err(class) = published {
            self.host.discard_staging();
            self.stop_proxies(&started).await;
            return Err(class);
        }
        if started.is_empty() {
            tracing::warn!("efs volume mounted without an identifiable efs-proxy");
        }
        self.proxy_table()
            .entry(spec.mount_path.as_str().to_owned())
            .or_default()
            .extend(started);
        Ok(())
    }
}

impl VolumeMounter for EfsUtilsMounter {
    fn support(&self) -> MountSupport {
        self.support
    }

    fn mount(&self, spec: &VolumeSpec) -> BoxFuture<'_, Result<MountReceipt, MountFailure>> {
        let spec = spec.clone();
        Box::pin(async move {
            {
                let _serialized = self.mount_lock.lock().await;
                self.mount_locked(&spec)
                    .await
                    .map_err(|class| MountFailure { class })?;
            }
            Ok(MountReceipt {
                credentials_expire_at: self.leases.credentials_expire_at().await,
            })
        })
    }

    fn unmount(
        &self,
        path: &MountPath,
        _mode: UnmountMode,
    ) -> BoxFuture<'_, Result<(), MountFailure>> {
        let path = path.clone();
        Box::pin(async move {
            let detached = self.host.detach(&path);
            let proxies = self.proxy_table().remove(path.as_str()).unwrap_or_default();
            self.stop_proxies(&proxies).await;
            detached.map_err(|class| MountFailure { class })
        })
    }

    fn probe(&self, path: &MountPath, budget: Duration) -> BoxFuture<'_, ProbeOutcome> {
        let path = path.clone();
        Box::pin(async move {
            match self.host.stat(&path, budget).await {
                StatProbe::Answered => ProbeOutcome::Healthy,
                StatProbe::Failed => ProbeOutcome::Stale,
                StatProbe::TimedOut => ProbeOutcome::Hung,
            }
        })
    }

    fn flush(&self, path: &MountPath, deadline: Duration) -> BoxFuture<'_, FlushOutcome> {
        let key = path.as_str().to_owned();
        Box::pin(async move {
            if !lock_set(&self.flushing).insert(key.clone()) {
                return FlushOutcome::InFlight;
            }
            let (done_tx, done_rx) = tokio::sync::oneshot::channel();
            let flushing = Arc::clone(&self.flushing);
            let finished_key = key.clone();
            let spawned =
                spawn_syncfs_thread(self.host.filesystem_sync(), key.clone(), move |synced| {
                    lock_set(&flushing).remove(&finished_key);
                    let _ = done_tx.send(synced);
                });
            if spawned.is_err() {
                lock_set(&self.flushing).remove(&key);
                return FlushOutcome::Failed;
            }
            match tokio::time::timeout(deadline, done_rx).await {
                Ok(Ok(true)) => FlushOutcome::Flushed,
                Ok(Ok(false) | Err(_)) => FlushOutcome::Failed,
                Err(_deadline) => FlushOutcome::TimedOut,
            }
        })
    }
}

fn lock_set(set: &Mutex<HashSet<String>>) -> MutexGuard<'_, HashSet<String>> {
    set.lock().unwrap_or_else(PoisonError::into_inner)
}

/// The helper's argv (after `MOUNT_BINARY`) for `spec`, mounting on
/// `STAGING_DIR`.
fn helper_args(spec: &VolumeSpec) -> Vec<String> {
    let mut options = vec![
        OPTION_TLS.to_owned(),
        OPTION_IAM.to_owned(),
        format!("{OPTION_ACCESS_POINT}={}", spec.access_point_id.as_str()),
    ];
    if let Some(ip) = spec.mount_target_ip {
        let [a, b, c, d] = ip.octets();
        options.push(format!("{OPTION_MOUNT_TARGET_IP}={a}.{b}.{c}.{d}"));
    }
    if spec.read_only {
        options.push(OPTION_READ_ONLY.to_owned());
    }
    vec![
        "-t".to_owned(),
        EFS_FS_TYPE.to_owned(),
        "-o".to_owned(),
        options.join(","),
        format!("{}{EXPORT_ROOT}", spec.file_system_id.as_str()),
        STAGING_DIR.to_owned(),
    ]
}

/// One closed class for a failed helper run, from the last bytes of its
/// stderr (`mount.efs` and `mount.nfs4` messages; Q123: "Cannot connect to
/// file system mount target ip address … timeout"; Q134: "access denied
/// by server"). Checked in this order so an IAM denial that also mentions
/// the connection is still `IamDenied`.
fn classify_helper_failure(stderr_tail: &[u8]) -> MountFailureClass {
    let text = String::from_utf8_lossy(stderr_tail).to_lowercase();
    let mentions = |needles: &[&str]| needles.iter().any(|needle| text.contains(needle));
    if mentions(&[
        "access denied",
        "permission denied",
        "not authorized",
        "unauthorized",
    ]) {
        MountFailureClass::IamDenied
    } else if mentions(&["not found", "does not exist", "no such"]) {
        MountFailureClass::NotFound
    } else if mentions(&[
        "cannot connect",
        "connection refused",
        "unreachable",
        "timed out",
        "timeout",
    ]) {
        MountFailureClass::Network
    } else if mentions(&["certificate", "ssl", "tls"]) {
        MountFailureClass::Tls
    } else {
        MountFailureClass::Network
    }
}

/// Whether a process is the volume proxy: the binary Q122 located, run by
/// root. A deleted or replaced binary (`readlink` ends in ` (deleted)`) is
/// not it.
fn is_efs_proxy(exe: Option<&Path>, owner_uid: Option<u32>) -> bool {
    exe == Some(Path::new(EFS_PROXY_BINARY)) && owner_uid == Some(ROOT_UID)
}

/// Whether this boot can offer `volumes=`: `CAP_SYS_ADMIN` (only
/// `rayito-base-caps`, Q79), the `nfs4` client, `mount` and both
/// `amazon-efs-utils` binaries (Q122). No image Rayito publishes installs
/// `amazon-efs-utils` yet, so today this is only `true` on an image the
/// operator built with it. Read once at startup.
#[must_use]
pub fn detect_efs_supported() -> bool {
    detect_guest_capabilities().sys_admin()
        && kernel_supports_filesystem(NFS4_FS_TYPE)
        && binary_on_path(MOUNT_BINARY)
        && Path::new(MOUNT_EFS_HELPER).is_file()
        && Path::new(EFS_PROXY_BINARY).is_file()
}

/// `EfsHost` over the real guest.
#[derive(Debug, Default, Clone)]
pub struct LinuxEfsHost {
    /// The platform's `AWS_REGION` as `main` read it (`FeatureContext::
    /// region`), handed to the helper; `None` leaves `efs-utils` to its own
    /// fallbacks (`helper_environment`).
    region: Option<String>,
}

impl LinuxEfsHost {
    #[must_use]
    pub fn new(region: Option<String>) -> Self {
        Self { region }
    }

    fn proc_path(pid: i32) -> PathBuf {
        Path::new(PROC_ROOT).join(pid.to_string())
    }
}

impl EfsHost for LinuxEfsHost {
    fn prepare_staging(&self) -> Result<(), MountFailureClass> {
        open_dir_no_symlinks(Path::new(FILESYSTEM_ROOT), STAGING_DIR, true).map_err(walk_class)?;
        std::fs::set_permissions(
            STAGING_DIR,
            std::fs::Permissions::from_mode(STAGING_DIR_MODE),
        )
        .map_err(|_io_error| MountFailureClass::HelperMissing)?;
        let _nothing_left_over = detach(STAGING_DIR);
        Ok(())
    }

    fn run_helper(
        &self,
        args: Vec<String>,
        timeout: Duration,
    ) -> BoxFuture<'_, Result<(), MountFailureClass>> {
        Box::pin(run_mount_helper(
            args,
            helper_environment(self.region.as_deref()),
            timeout,
        ))
    }

    fn publish(&self, mount_path: &MountPath) -> Result<(), MountFailureClass> {
        let target = open_dir_no_symlinks(Path::new(FILESYSTEM_ROOT), mount_path.as_str(), true)
            .map_err(walk_class)?;
        bind_onto(STAGING_DIR, &target).map_err(|_refused| MountFailureClass::HelperMissing)?;
        if detach(STAGING_DIR).is_err() {
            tracing::warn!("efs staging mount not detached after publishing");
        }
        Ok(())
    }

    fn discard_staging(&self) {
        let _nothing_mounted = detach(STAGING_DIR);
    }

    fn detach(&self, mount_path: &MountPath) -> Result<(), MountFailureClass> {
        match unmount_no_symlinks(mount_path.as_str()) {
            Ok(())
            | Err(
                MountpointError::NotFound | MountpointError::Syscall(nix::errno::Errno::EINVAL),
            ) => Ok(()),
            Err(MountpointError::InvalidPath) => Err(MountFailureClass::InvalidPath),
            Err(MountpointError::Syscall(_)) => Err(MountFailureClass::Timeout),
        }
    }

    fn proxies(&self) -> Vec<ProxyProcess> {
        ProcfsProcessTable
            .snapshot()
            .into_iter()
            .filter(|entry| !entry.zombie)
            .filter(|entry| {
                let dir = Self::proc_path(entry.pid);
                let exe = std::fs::read_link(dir.join(PROC_EXE)).ok();
                let owner = std::fs::metadata(&dir).ok().map(|meta| meta.uid());
                is_efs_proxy(exe.as_deref(), owner)
            })
            .map(|entry| ProxyProcess {
                pid: entry.pid,
                start_ticks: entry.start_ticks,
            })
            .collect()
    }

    fn is_running(&self, proxy: ProxyProcess) -> bool {
        is_still_running(proxy, ProcfsProcessTable.entry(proxy.pid))
    }

    fn stop(&self, proxy: ProxyProcess, force: bool) {
        if !self.is_running(proxy) {
            return;
        }
        let signal = if force {
            Signal::SIGKILL
        } else {
            Signal::SIGTERM
        };
        let _ = nix::sys::signal::kill(nix::unistd::Pid::from_raw(proxy.pid), signal);
    }

    fn stat(&self, mount_path: &MountPath, budget: Duration) -> BoxFuture<'_, StatProbe> {
        let path = mount_path.as_str().to_owned();
        Box::pin(async move {
            tokio::task::spawn_blocking(move || stat_as_guest(&path, budget))
                .await
                .unwrap_or(StatProbe::Failed)
        })
    }

    fn filesystem_sync(&self) -> Arc<dyn FilesystemSync> {
        Arc::new(PlatformFilesystemSync)
    }
}

fn walk_class(error: MountpointError) -> MountFailureClass {
    match error {
        MountpointError::InvalidPath => MountFailureClass::InvalidPath,
        MountpointError::NotFound | MountpointError::Syscall(_) => MountFailureClass::NotFound,
    }
}

/// The helper's whole environment, rebuilt from scratch: `PATH`, plus
/// `AWS_REGION` when the platform set it. `efs-utils` 3.1.3 takes the
/// region from the `region` mount option, then `AWS_REGION`/
/// `AWS_DEFAULT_REGION`, then its `efs-utils.conf`, then IMDS
/// (`get_target_region` in `src/efs_utils_common/metadata.py` of
/// `aws/efs-utils` v3.1.3, read 2026-10-04); a product image ships
/// the configuration file untouched (it cannot know the Region it will run
/// in), so the variable is what tells it. Credentials still come from IMDS
/// (Q128), never from this environment.
fn helper_environment(region: Option<&str>) -> Vec<(&'static str, String)> {
    let mut environment = vec![(PATH_ENV, DEFAULT_PATH.to_owned())];
    if let Some(region) = region.filter(|region| !region.is_empty()) {
        environment.push((AWS_REGION_ENV, region.to_owned()));
    }
    environment
}

/// `mount -t efs ...` through `ChildRegistry`, with `environment` as its
/// whole environment (`helper_environment`), in its own process group so a
/// run past `timeout` is killed whole (the helper's own children included)
/// and then reaped off the caller's path.
async fn run_mount_helper(
    args: Vec<String>,
    environment: Vec<(&'static str, String)>,
    timeout: Duration,
) -> Result<(), MountFailureClass> {
    let mut command = Command::new(MOUNT_BINARY);
    command
        .args(&args)
        .env_clear()
        .envs(environment)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .process_group(0)
        .kill_on_drop(false);
    let mut child = ChildRegistry::process()
        .spawn(&mut command)
        .map_err(|_io_error| MountFailureClass::HelperMissing)?;
    let mut stderr = child.stderr.take();
    let waited = tokio::time::timeout(timeout, async {
        let read_tail = async {
            match stderr.as_mut() {
                Some(pipe) => drain_stderr_tail(pipe).await,
                None => Vec::new(),
            }
        };
        tokio::join!(child.wait(), read_tail)
    })
    .await;
    match waited {
        Ok((Ok(status), _)) if status.success() => Ok(()),
        Ok((_, tail)) => Err(classify_helper_failure(&tail)),
        Err(_deadline) => {
            if let Some(pid) = child.id() {
                signal_process_group(pid, Signal::SIGKILL as i32);
            }
            tokio::spawn(async move {
                let _ = child.wait().await;
            });
            Err(MountFailureClass::Timeout)
        }
    }
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::{AtomicBool, Ordering};

    use rayd_core::volume::{AccessPointId, FileSystemId, MountTargetIp};

    use super::*;
    use crate::adapters::bounded_sync::fake::{Behaviour, FakeFilesystemSync};

    const START: u64 = 77;

    fn spec(path: &str, read_only: bool, ip: Option<&str>) -> VolumeSpec {
        VolumeSpec {
            file_system_id: FileSystemId::parse("fs-0123abcd").unwrap(),
            access_point_id: AccessPointId::parse("fsap-0123abcd").unwrap(),
            mount_path: MountPath::parse(path).unwrap(),
            read_only,
            mount_target_ip: ip.map(|value| MountTargetIp::parse(value).unwrap()),
        }
    }

    fn proxy(pid: i32) -> ProxyProcess {
        ProxyProcess {
            pid,
            start_ticks: START,
        }
    }

    /// A guest where each helper run "starts" one proxy (pid 100, 101, ...)
    /// unless told to fail, and proxies stop on `SIGTERM` unless marked
    /// stubborn (then only `SIGKILL` stops them).
    #[derive(Default)]
    struct FakeHost {
        helper_fails_with: Mutex<Option<MountFailureClass>>,
        publish_fails_with: Mutex<Option<MountFailureClass>>,
        helper_starts_no_proxy: AtomicBool,
        stubborn: AtomicBool,
        running: Mutex<Vec<ProxyProcess>>,
        next_pid: Mutex<i32>,
        calls: Mutex<Vec<String>>,
        helper_args: Mutex<Vec<Vec<String>>>,
        stat: Mutex<Option<StatProbe>>,
        sync: Mutex<Option<Arc<FakeFilesystemSync>>>,
    }

    impl FakeHost {
        fn record(&self, call: impl Into<String>) {
            self.calls.lock().unwrap().push(call.into());
        }

        fn calls(&self) -> Vec<String> {
            self.calls.lock().unwrap().clone()
        }

        fn running(&self) -> Vec<ProxyProcess> {
            self.running.lock().unwrap().clone()
        }
    }

    impl EfsHost for FakeHost {
        fn prepare_staging(&self) -> Result<(), MountFailureClass> {
            self.record("prepare");
            Ok(())
        }

        fn run_helper(
            &self,
            args: Vec<String>,
            _timeout: Duration,
        ) -> BoxFuture<'_, Result<(), MountFailureClass>> {
            self.record("helper");
            self.helper_args.lock().unwrap().push(args);
            if !self.helper_starts_no_proxy.load(Ordering::SeqCst) {
                let mut next = self.next_pid.lock().unwrap();
                let pid = 100 + *next;
                *next += 1;
                self.running.lock().unwrap().push(proxy(pid));
            }
            let outcome = self.helper_fails_with.lock().unwrap().map_or(Ok(()), Err);
            Box::pin(async move { outcome })
        }

        fn publish(&self, mount_path: &MountPath) -> Result<(), MountFailureClass> {
            self.record(format!("publish {}", mount_path.as_str()));
            self.publish_fails_with.lock().unwrap().map_or(Ok(()), Err)
        }

        fn discard_staging(&self) {
            self.record("discard");
        }

        fn detach(&self, mount_path: &MountPath) -> Result<(), MountFailureClass> {
            self.record(format!("detach {}", mount_path.as_str()));
            Ok(())
        }

        fn proxies(&self) -> Vec<ProxyProcess> {
            self.running()
        }

        fn is_running(&self, proxy: ProxyProcess) -> bool {
            self.running().contains(&proxy)
        }

        fn stop(&self, proxy: ProxyProcess, force: bool) {
            self.record(format!(
                "{} {}",
                if force { "kill" } else { "term" },
                proxy.pid
            ));
            if force || !self.stubborn.load(Ordering::SeqCst) {
                self.running.lock().unwrap().retain(|p| *p != proxy);
            }
        }

        fn stat(&self, _mount_path: &MountPath, _budget: Duration) -> BoxFuture<'_, StatProbe> {
            let outcome = self.stat.lock().unwrap().unwrap_or(StatProbe::Answered);
            Box::pin(async move { outcome })
        }

        fn filesystem_sync(&self) -> Arc<dyn FilesystemSync> {
            self.sync
                .lock()
                .unwrap()
                .clone()
                .expect("test set a filesystem sync") as Arc<dyn FilesystemSync>
        }
    }

    struct FixedLease(Option<SystemTime>);

    impl LeaseSource for FixedLease {
        fn credentials_expire_at(&self) -> BoxFuture<'_, Option<SystemTime>> {
            let expiry = self.0;
            Box::pin(async move { expiry })
        }
    }

    fn mounter(host: &Arc<FakeHost>) -> EfsUtilsMounter {
        mounter_with_lease(host, None)
    }

    fn mounter_with_lease(host: &Arc<FakeHost>, lease: Option<SystemTime>) -> EfsUtilsMounter {
        EfsUtilsMounter::new(
            Arc::clone(host) as Arc<dyn EfsHost>,
            Arc::new(FixedLease(lease)),
            MountSupport::Supported,
        )
    }

    #[test]
    fn helper_args_are_the_measured_invocation_on_the_staging_dir() {
        assert_eq!(
            helper_args(&spec("/mnt/data", false, Some("10.0.1.25"))),
            [
                "-t",
                "efs",
                "-o",
                "tls,iam,accesspoint=fsap-0123abcd,mounttargetip=10.0.1.25",
                "fs-0123abcd:/",
                STAGING_DIR,
            ]
        );
    }

    #[test]
    fn the_helper_environment_is_path_plus_the_platform_region() {
        assert_eq!(
            helper_environment(Some("us-east-1")),
            [
                (PATH_ENV, DEFAULT_PATH.to_owned()),
                (AWS_REGION_ENV, "us-east-1".to_owned()),
            ]
        );
    }

    #[test]
    fn without_a_region_the_helper_only_gets_path() {
        let path_only = [(PATH_ENV, DEFAULT_PATH.to_owned())];
        assert_eq!(helper_environment(None), path_only);
        assert_eq!(helper_environment(Some("")), path_only);
    }

    #[test]
    fn a_read_only_volume_adds_ro_and_no_ip_leaves_mounttargetip_out() {
        let args = helper_args(&spec("/mnt/data", true, None));
        assert_eq!(args[3], "tls,iam,accesspoint=fsap-0123abcd,ro");
    }

    #[test]
    fn helper_failures_map_to_closed_classes() {
        assert_eq!(
            classify_helper_failure(b"mount.nfs4: access denied by server while mounting"),
            MountFailureClass::IamDenied
        );
        assert_eq!(
            classify_helper_failure(
                b"Cannot connect to file system mount target ip address 192.0.2.1. timeout"
            ),
            MountFailureClass::Network
        );
        assert_eq!(
            classify_helper_failure(b"AccessPointNotFound: access point does not exist"),
            MountFailureClass::NotFound
        );
        assert_eq!(
            classify_helper_failure(b"failed to verify the server certificate"),
            MountFailureClass::Tls
        );
        assert_eq!(classify_helper_failure(b""), MountFailureClass::Network);
    }

    #[test]
    fn only_a_root_process_of_the_installed_binary_is_a_proxy() {
        let binary = Path::new(EFS_PROXY_BINARY);
        assert!(is_efs_proxy(Some(binary), Some(ROOT_UID)));
        assert!(!is_efs_proxy(Some(binary), Some(1000)));
        assert!(!is_efs_proxy(
            Some(Path::new("/tmp/efs-proxy")),
            Some(ROOT_UID)
        ));
        assert!(!is_efs_proxy(
            Some(Path::new("/usr/sbin/efs-proxy (deleted)")),
            Some(ROOT_UID)
        ));
        assert!(!is_efs_proxy(None, Some(ROOT_UID)));
        assert!(!is_efs_proxy(Some(binary), None));
    }

    #[tokio::test]
    async fn unmount_stops_exactly_the_proxy_its_mount_started() {
        let host = Arc::new(FakeHost::default());
        host.running.lock().unwrap().push(proxy(7));
        let mounter = mounter(&host);
        mounter.mount(&spec("/mnt/a", false, None)).await.unwrap();
        mounter.mount(&spec("/mnt/b", false, None)).await.unwrap();
        assert_eq!(host.running(), [proxy(7), proxy(100), proxy(101)]);
        mounter
            .unmount(&MountPath::parse("/mnt/a").unwrap(), UnmountMode::Lazy)
            .await
            .unwrap();
        assert_eq!(host.running(), [proxy(7), proxy(101)]);
        assert!(host.calls().contains(&"detach /mnt/a".to_owned()));
        assert!(host.calls().contains(&"term 100".to_owned()));
    }

    #[tokio::test]
    async fn mount_unmount_cycles_never_leak_a_proxy() {
        let host = Arc::new(FakeHost::default());
        let mounter = mounter(&host);
        let path = MountPath::parse("/mnt/a").unwrap();
        for _ in 0..20 {
            mounter.mount(&spec("/mnt/a", false, None)).await.unwrap();
            mounter.unmount(&path, UnmountMode::Lazy).await.unwrap();
        }
        assert!(host.running().is_empty());
    }

    #[tokio::test]
    async fn a_proxy_that_ignores_sigterm_is_killed() {
        let host = Arc::new(FakeHost::default());
        host.stubborn.store(true, Ordering::SeqCst);
        let mounter = mounter(&host);
        mounter.mount(&spec("/mnt/a", false, None)).await.unwrap();
        mounter
            .unmount(&MountPath::parse("/mnt/a").unwrap(), UnmountMode::Lazy)
            .await
            .unwrap();
        assert!(host.running().is_empty());
        let calls = host.calls();
        assert!(calls.contains(&"term 100".to_owned()));
        assert!(calls.contains(&"kill 100".to_owned()));
    }

    #[tokio::test]
    async fn a_failed_helper_stops_what_it_started_and_discards_the_staging_mount() {
        let host = Arc::new(FakeHost::default());
        *host.helper_fails_with.lock().unwrap() = Some(MountFailureClass::IamDenied);
        let mounter = mounter(&host);
        let result = mounter.mount(&spec("/mnt/a", false, None)).await;
        assert_eq!(
            result,
            Err(MountFailure {
                class: MountFailureClass::IamDenied
            })
        );
        assert!(host.running().is_empty());
        assert!(host.calls().contains(&"discard".to_owned()));
        assert!(!host.calls().iter().any(|call| call.starts_with("publish")));
    }

    #[tokio::test]
    async fn a_refused_publish_is_reported_and_cleaned_up() {
        let host = Arc::new(FakeHost::default());
        *host.publish_fails_with.lock().unwrap() = Some(MountFailureClass::InvalidPath);
        let mounter = mounter(&host);
        let result = mounter.mount(&spec("/home/user/data", false, None)).await;
        assert_eq!(
            result,
            Err(MountFailure {
                class: MountFailureClass::InvalidPath
            })
        );
        assert!(host.running().is_empty());
        assert!(host.calls().contains(&"discard".to_owned()));
    }

    #[tokio::test]
    async fn a_mount_reports_the_lease_it_was_signed_with() {
        let host = Arc::new(FakeHost::default());
        let expiry = SystemTime::UNIX_EPOCH + Duration::from_secs(1_700_000_000);
        let mounter = mounter_with_lease(&host, Some(expiry));
        let receipt = mounter.mount(&spec("/mnt/a", false, None)).await.unwrap();
        assert_eq!(receipt.credentials_expire_at, Some(expiry));
    }

    #[tokio::test]
    async fn a_mount_without_an_identifiable_proxy_still_succeeds() {
        let host = Arc::new(FakeHost::default());
        host.helper_starts_no_proxy.store(true, Ordering::SeqCst);
        let mounter = mounter(&host);
        assert!(mounter.mount(&spec("/mnt/a", false, None)).await.is_ok());
        mounter
            .unmount(&MountPath::parse("/mnt/a").unwrap(), UnmountMode::Lazy)
            .await
            .unwrap();
        assert!(!host.calls().iter().any(|call| call.starts_with("term")));
    }

    #[tokio::test]
    async fn probe_outcomes_follow_the_stat_child() {
        let host = Arc::new(FakeHost::default());
        let mounter = mounter(&host);
        let path = MountPath::parse("/mnt/a").unwrap();
        let budget = Duration::from_secs(1);
        assert_eq!(mounter.probe(&path, budget).await, ProbeOutcome::Healthy);
        *host.stat.lock().unwrap() = Some(StatProbe::Failed);
        assert_eq!(mounter.probe(&path, budget).await, ProbeOutcome::Stale);
        *host.stat.lock().unwrap() = Some(StatProbe::TimedOut);
        assert_eq!(mounter.probe(&path, budget).await, ProbeOutcome::Hung);
    }

    #[tokio::test]
    async fn a_flush_that_returns_in_time_is_flushed() {
        let host = Arc::new(FakeHost::default());
        *host.sync.lock().unwrap() = Some(Arc::new(FakeFilesystemSync::new(&[(
            "/mnt/a",
            Behaviour::Syncs,
        )])));
        let mounter = mounter(&host);
        let path = MountPath::parse("/mnt/a").unwrap();
        assert_eq!(
            mounter.flush(&path, Duration::from_secs(5)).await,
            FlushOutcome::Flushed
        );
    }

    #[tokio::test]
    async fn a_failing_flush_is_failed() {
        let host = Arc::new(FakeHost::default());
        *host.sync.lock().unwrap() = Some(Arc::new(FakeFilesystemSync::new(&[(
            "/mnt/a",
            Behaviour::Fails,
        )])));
        let mounter = mounter(&host);
        let path = MountPath::parse("/mnt/a").unwrap();
        assert_eq!(
            mounter.flush(&path, Duration::from_secs(5)).await,
            FlushOutcome::Failed
        );
    }

    #[tokio::test]
    async fn a_hung_flush_times_out_and_the_next_suspend_does_not_pile_up() {
        let host = Arc::new(FakeHost::default());
        let sync = Arc::new(FakeFilesystemSync::new(&[(
            "/mnt/a",
            Behaviour::BlocksForever,
        )]));
        *host.sync.lock().unwrap() = Some(Arc::clone(&sync));
        let mounter = mounter(&host);
        let path = MountPath::parse("/mnt/a").unwrap();
        let deadline = Duration::from_millis(50);
        assert_eq!(mounter.flush(&path, deadline).await, FlushOutcome::TimedOut);
        assert_eq!(mounter.flush(&path, deadline).await, FlushOutcome::InFlight);
        assert_eq!(sync.calls(), 1);
    }

    #[cfg(target_os = "linux")]
    #[tokio::test]
    async fn the_real_host_stops_a_real_process_and_sees_it_gone() {
        // A stand-in for `efs-proxy`: a real child of this test process,
        // identified the way the adapter identifies a proxy (pid + start
        // time). `stop` sends `SIGTERM`; once it exits, its zombie (or its
        // absence after the wait below reaps it) is "not running".
        let mut sleep = Command::new("sleep");
        sleep.arg("30");
        let mut child = ChildRegistry::process().spawn(&mut sleep).unwrap();
        let pid = i32::try_from(child.id().unwrap()).unwrap();
        let entry = ProcfsProcessTable.entry(pid).unwrap();
        let sleeper = ProxyProcess {
            pid,
            start_ticks: entry.start_ticks,
        };
        let host = LinuxEfsHost::default();
        assert!(host.is_running(sleeper));
        host.stop(sleeper, false);
        let _ = child.wait().await;
        assert!(!host.is_running(sleeper));
        host.stop(sleeper, true);
    }

    #[test]
    fn the_real_host_never_takes_a_test_process_for_a_proxy() {
        assert!(LinuxEfsHost::default().proxies().is_empty());
    }

    #[test]
    fn this_test_environment_does_not_offer_efs() {
        // Neither CI nor the Lima VM installs `amazon-efs-utils`.
        assert!(!detect_efs_supported());
    }
}
