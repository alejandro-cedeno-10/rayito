//! Linux `FuseDevice` adapter (`m15-s3-mounts`, ADR-017): opens
//! `/dev/fuse` and performs the `mount(2)` the Linux FUSE kernel ABI
//! expects (`Documentation/filesystems/fuse.rst`: `fd`, `rootmode`,
//! `user_id`, `group_id`, `allow_other` as the mount data string), so a
//! later read/write under `mount_path` reaches the `mount-s3` daemon
//! (`adapters::mount_s3`) bound to the same descriptor. No AWS call and no
//! credential: this adapter only ever talks to the kernel.
//!
//! `rayd` runs as root, while every allowed mount root under `/home/user`
//! is writable by uid 1000: a path that is only checked lexically
//! (`rayd_core::mount_path`) could still be swapped for a symlink (to
//! `/usr/local/bin`, `/etc/cron.d`, ...) before `Configure` or between an
//! unmount and a relaunch, and both a plain `create_dir_all` and a path
//! `mount(2)` follow symlinks. So the mountpoint is never touched by path: it is walked one
//! component at a time from `/` with `O_NOFOLLOW` (`open_dir_no_symlinks`),
//! any symlink is rejected as `InvalidPath`, and `mount(2)`/`umount2(2)`
//! only ever see `/proc/self/fd/<dirfd>` — the directory that walk opened,
//! whatever the path names by the time the syscall runs.

use std::ffi::CString;
use std::fs::OpenOptions;
use std::os::fd::{AsRawFd, IntoRawFd, OwnedFd, RawFd};
use std::os::unix::fs::OpenOptionsExt;
use std::os::unix::process::CommandExt;
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use rayd_core::process::env::DEFAULT_PATH;
use rayd_core::s3_mount::{FuseDevice, MountErrorClass, S3Mount};

use super::child_registry::ChildRegistry;
use super::dir_walk::{MissingDir, WalkError, open_dir_beneath};
use super::exec_posture::ExecPosture;

const FUSE_DEVICE_PATH: &str = "/dev/fuse";
/// Where every mount-path walk starts; `rayd_core::mount_path` already
/// guarantees an absolute path under one of its `ALLOWED_ROOTS`.
const FILESYSTEM_ROOT: &str = "/";
/// `proc(5)`'s per-descriptor magic links: `mount(2)` and `umount2(2)` get
/// `/proc/self/fd/<n>[/<last>]` instead of the caller's path, so they act
/// on the directory `open_dir_no_symlinks` opened, never on whatever a
/// symlink swapped in afterwards points to (the same technique `runc` uses
/// for container mountpoints).
const PROC_SELF_FD: &str = "/proc/self/fd";
/// Mode of a mountpoint directory `attach` has to create: the same
/// `0o755` (minus umask) `std::fs::create_dir_all` used before this walk
/// replaced it.
const MOUNTPOINT_DIR_MODE: u32 = 0o755;
/// `S_IFDIR` (kernel `stat.h`): the FUSE ABI's `rootmode` is the root
/// inode's file-type bits, and every mount here is a directory.
const FUSE_ROOTMODE_DIR: u32 = 0o040_000;
/// The guest's default user (`image/Dockerfile`'s `user`, uid 1000).
/// `user_id`/`group_id` in the FUSE mount data are an *access* gate, not an
/// ownership one — they decide which uid's requests the kernel lets reach
/// the mount at all; the inode ownership `stat` reports comes from
/// `mount-s3` itself (its own `--uid`/`--gid`, `adapters::mount_s3`), which
/// must be given the same ids for the two to agree. `allow_other` widens
/// the *kernel's* gate so rayd's own root-run readiness probe
/// (`probe_ready`, below) can reach the mount too, same as any uid other
/// than 1000; `mount-s3`'s own `--uid`/`--gid` is what still makes every
/// file appear owned by the guest user despite that.
pub const GUEST_USER_ID: u32 = 1000;
pub const GUEST_GROUP_ID: u32 = 1000;
/// `O_NONBLOCK` on `/dev/fuse`'s own fd (distinct from the mount's
/// behaviour): libfuse always opens it this way so a request read never
/// blocks the attach itself.
const FUSE_DEVICE_OPEN_FLAGS: i32 = libc::O_NONBLOCK;
/// Upper bound on one `stat` readiness probe: long enough for a local FUSE
/// round-trip once `mount-s3` is actually answering, short enough that a
/// daemon still stuck in its own IMDS/S3 startup call never blocks the
/// caller's own retry loop (`rayd::features::s3_mounts::await_ready`) for
/// more than this one attempt.
const PROBE_PROCESS_TIMEOUT: Duration = Duration::from_millis(300);
/// How often `probe_ready` polls the probe child for an exit, while it
/// waits.
const PROBE_POLL_INTERVAL: Duration = Duration::from_millis(10);
/// Absolute, so the probe never resolves a bare name through `rayd`'s own
/// `PATH`: coreutils' `stat`, present in every Rayito image (AL2023 base,
/// `image/Dockerfile`).
const STAT_BINARY: &str = "/usr/bin/stat";

pub struct LinuxFuseDevice;

impl FuseDevice for LinuxFuseDevice {
    fn attach(&self, mount: &S3Mount) -> Result<RawFd, MountErrorClass> {
        let mountpoint = open_dir_no_symlinks(Path::new(FILESYSTEM_ROOT), &mount.mount_path, true)?;
        let device = OpenOptions::new()
            .read(true)
            .write(true)
            .custom_flags(FUSE_DEVICE_OPEN_FLAGS)
            .open(FUSE_DEVICE_PATH)
            .map_err(|_io_error| MountErrorClass::HelperMissing)?;
        let fd = device.into_raw_fd();
        let data = format!(
            "fd={fd},rootmode={FUSE_ROOTMODE_DIR:o},user_id={GUEST_USER_ID},group_id={GUEST_GROUP_ID},allow_other"
        );
        let target = proc_fd_path(&mountpoint, None);
        let result = raw_mount("mount-s3", &target, "fuse", &data);
        if let Err(class) = result {
            // SAFETY: `fd` came from `into_raw_fd()` above and nothing else
            // owns it yet; closing it on a failed mount prevents a leak.
            unsafe {
                libc::close(fd);
            }
            return Err(class);
        }
        Ok(fd)
    }

    fn detach(&self, mount_path: &str, fd: RawFd) -> Result<(), MountErrorClass> {
        let result = unmount_no_symlinks(mount_path);
        // SAFETY: `fd` is the descriptor `attach` returned for this exact
        // mount; `rayd::features::s3_mounts` owns it as `Option<RawFd>` and
        // only ever calls `detach` once per attach, taking the option so a
        // second call can never see the same number again.
        unsafe {
            libc::close(fd);
        }
        result
    }

    fn probe_ready(&self, mount_path: &str) -> bool {
        // A real subprocess, not an in-process `stat(2)`: once `mount(2)`
        // succeeds the kernel will block any filesystem operation under
        // `mount_path` until `mount-s3` answers FUSE_INIT, which may never
        // happen (a dead or hung daemon); running the probe as its own
        // child lets a bounded wait `kill()` it instead of blocking this
        // call, or a blocking thread in `spawn_blocking`'s own pool,
        // forever. Runs as the guest's own uid (never root): `allow_other`
        // lets the kernel accept that, but exercising it here is also what
        // proves a *guest* `stat`/`open` would succeed, which is the thing
        // `sbx.mounts == "mounted"` is actually promising the caller.
        let mut command = probe_command(mount_path);
        let mut child = match ChildRegistry::process().spawn(&mut command) {
            Ok(child) => child,
            Err(_spawn_error) => return false,
        };
        let deadline = Instant::now() + PROBE_PROCESS_TIMEOUT;
        loop {
            match child.try_wait() {
                Ok(Some(status)) => return status.success(),
                Ok(None) if Instant::now() < deadline => {
                    std::thread::sleep(PROBE_POLL_INTERVAL);
                }
                Ok(None) => {
                    // Still hung past the deadline: kill and reap it so it
                    // never lingers blocked on the mount forever (the
                    // leak the pre-fix `/resume` probe had with
                    // `kill_on_drop(false)` — this probe is not even a
                    // tokio child, so the equivalent discipline is doing
                    // both steps by hand).
                    let _ = child.kill();
                    let _ = child.wait();
                    return false;
                }
                Err(_wait_error) => return false,
            }
        }
    }
}

/// The readiness probe: `stat <mount_path>` as the guest user, through
/// `guest_command`.
fn probe_command(mount_path: &str) -> Command {
    let mut command = guest_command(
        STAT_BINARY,
        ExecPosture::as_user(GUEST_USER_ID, GUEST_GROUP_ID),
    );
    command.arg(mount_path);
    command
}

/// A short-lived helper `rayd` runs as the guest user: an environment
/// rebuilt from scratch holding only `PATH` (nothing of `rayd`'s own
/// environment — the image ARN, `RAYITO_*`, operator `--env` variables —
/// may reach a uid-1000 process, whose `/proc/<pid>/environ` any other
/// uid-1000 process can read while it runs; SEC-3, T18), no stdio, and
/// `posture` between `fork` and `exec` (identity drop, descriptor seal,
/// signal reset).
fn guest_command(program: &str, posture: ExecPosture) -> Command {
    let mut command = Command::new(program);
    command
        .env_clear()
        .env("PATH", DEFAULT_PATH)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    // SAFETY: `ExecPosture::apply` only issues setgroups/setgid/setuid,
    // close_range (or fcntl) and sigaction/sigprocmask on values computed
    // before the fork; it allocates nothing and takes no lock.
    unsafe {
        command.pre_exec(move || posture.apply());
    }
    command
}

/// Opens `relative` (components separated by `/`, leading `/` ignored)
/// beneath `root` as an `O_PATH` directory descriptor, one component at a
/// time, never following a symlink in any of them (`dir_walk`): a
/// component that is a symlink (or not a directory) is `InvalidPath`. With
/// `create_missing`, a missing component is created (`mkdirat`,
/// `MOUNTPOINT_DIR_MODE`) and then opened the same way, so a racing swap
/// between the two calls is still caught by the `O_NOFOLLOW` open.
fn open_dir_no_symlinks(
    root: &Path,
    relative: &str,
    create_missing: bool,
) -> Result<OwnedFd, MountErrorClass> {
    let missing = if create_missing {
        MissingDir::Create(MOUNTPOINT_DIR_MODE)
    } else {
        MissingDir::Fail
    };
    open_dir_beneath(root, relative, missing).map_err(|error| match error {
        // The path no longer names a plain directory chain, which is
        // exactly what the walk exists to refuse.
        WalkError::Symlink | WalkError::NotADirectory => MountErrorClass::InvalidPath,
        WalkError::Io(_errno) => MountErrorClass::NotFound,
    })
}

/// `/proc/self/fd/<n>`, or `/proc/self/fd/<n>/<last>` to name a child of
/// the opened directory (only `unmount_no_symlinks` does that, to reach
/// the mount stacked on top of `<last>`).
fn proc_fd_path(dir: &OwnedFd, last: Option<&str>) -> String {
    let base = format!("{PROC_SELF_FD}/{}", dir.as_raw_fd());
    last.map_or_else(|| base.clone(), |name| format!("{base}/{name}"))
}

/// `umount2(MNT_DETACH | UMOUNT_NOFOLLOW)` on `mount_path`, with every
/// ancestor resolved by `open_dir_no_symlinks` (so a symlinked parent can
/// never redirect it to an unrelated mountpoint such as `/dev`) and the
/// last component itself never followed (`UMOUNT_NOFOLLOW`).
/// `MNT_DETACH` lets the unmount succeed even if a reader is still inside
/// the mount, matching `/suspend`'s "no extra step" contract (ADR-017:
/// unmount never blocks a hook).
fn unmount_no_symlinks(mount_path: &str) -> Result<(), MountErrorClass> {
    let (parent, last) = mount_path
        .rsplit_once('/')
        .ok_or(MountErrorClass::InvalidPath)?;
    if last.is_empty() {
        return Err(MountErrorClass::InvalidPath);
    }
    let parent = open_dir_no_symlinks(Path::new(FILESYSTEM_ROOT), parent, false)?;
    let target = c_string(&proc_fd_path(&parent, Some(last)))?;
    // SAFETY: `target` is a valid, nul-terminated C string for the
    // duration of this call, and `parent` (which its magic link names)
    // stays open until after it.
    let result =
        unsafe { libc::umount2(target.as_ptr(), libc::MNT_DETACH | libc::UMOUNT_NOFOLLOW) };
    if result == 0 {
        Ok(())
    } else {
        Err(MountErrorClass::Timeout)
    }
}

fn raw_mount(source: &str, target: &str, fstype: &str, data: &str) -> Result<(), MountErrorClass> {
    let source = c_string(source)?;
    let target = c_string(target)?;
    let fstype = c_string(fstype)?;
    let data = c_string(data)?;
    // SAFETY: every pointer is a valid, nul-terminated C string owned by a
    // local `CString` kept alive until after the call; `flags = 0` (no
    // `MS_*` bit) is what a FUSE mount needs beyond the kernel ABI's own
    // `rootmode`/`user_id`/`group_id`/`allow_other` data string.
    let result = unsafe {
        libc::mount(
            source.as_ptr(),
            target.as_ptr(),
            fstype.as_ptr(),
            0,
            data.as_ptr().cast(),
        )
    };
    if result == 0 {
        Ok(())
    } else {
        Err(classify_mount_errno())
    }
}

fn classify_mount_errno() -> MountErrorClass {
    match nix::errno::Errno::last() {
        // Neither errno is an AWS/IAM decision: `mount(2)` itself only
        // ever denies for a kernel-side reason (missing `CAP_SYS_ADMIN`,
        // or the target already busy) — the execution role's own
        // permissions are decided later, inside `mount-s3`'s own process,
        // and surface through `adapters::mount_s3`'s exit classification
        // instead.
        nix::errno::Errno::ENOENT | nix::errno::Errno::ENOTDIR => MountErrorClass::NotFound,
        nix::errno::Errno::EACCES
        | nix::errno::Errno::EPERM
        | nix::errno::Errno::ENODEV
        | nix::errno::Errno::ENOSYS => MountErrorClass::HelperMissing,
        nix::errno::Errno::ETIMEDOUT => MountErrorClass::Timeout,
        _other => MountErrorClass::Network,
    }
}

fn c_string(value: &str) -> Result<CString, MountErrorClass> {
    CString::new(value).map_err(|_nul_error| MountErrorClass::NotFound)
}

#[cfg(test)]
mod tests {
    use std::os::fd::FromRawFd;

    use super::*;

    #[test]
    fn classify_mount_errno_maps_a_privilege_error_to_helper_missing_never_iam() {
        // `Errno::last()` reflects whatever the test process's last libc
        // call set; force a known value first so the mapping itself is
        // what gets exercised, not ambient process state. `EPERM`/`EACCES`
        // from `mount(2)` are never an IAM decision (see
        // `classify_mount_errno`'s own comment).
        nix::errno::Errno::EACCES.set();
        assert_eq!(classify_mount_errno(), MountErrorClass::HelperMissing);
        nix::errno::Errno::EPERM.set();
        assert_eq!(classify_mount_errno(), MountErrorClass::HelperMissing);
        nix::errno::Errno::ENOENT.set();
        assert_eq!(classify_mount_errno(), MountErrorClass::NotFound);
        nix::errno::Errno::ENODEV.set();
        assert_eq!(classify_mount_errno(), MountErrorClass::HelperMissing);
        nix::errno::Errno::ETIMEDOUT.set();
        assert_eq!(classify_mount_errno(), MountErrorClass::Timeout);
        nix::errno::Errno::ECONNRESET.set();
        assert_eq!(classify_mount_errno(), MountErrorClass::Network);
    }

    #[test]
    fn a_path_with_an_interior_nul_is_rejected_before_any_syscall() {
        assert_eq!(c_string("/mnt/da\0ta"), Err(MountErrorClass::NotFound));
    }

    #[test]
    fn the_walk_creates_missing_directories_beneath_the_root() {
        let root = tempfile::tempdir().unwrap();
        let opened = open_dir_no_symlinks(root.path(), "/mnt/data/nested", true);
        assert!(opened.is_ok());
        assert!(root.path().join("mnt/data/nested").is_dir());
    }

    #[test]
    fn the_walk_without_create_reports_a_missing_directory_as_not_found() {
        let root = tempfile::tempdir().unwrap();
        assert_eq!(
            open_dir_no_symlinks(root.path(), "/mnt/data", false).err(),
            Some(MountErrorClass::NotFound)
        );
    }

    #[test]
    fn a_symlinked_mount_directory_is_rejected_and_never_followed() {
        // The attack: uid 1000 swaps its own `/home/user/<sub>` for a
        // symlink to a system directory before `attach` (or between a
        // detach and a relaunch). The walk must refuse it, and must not
        // create anything inside the symlink's target either.
        let root = tempfile::tempdir().unwrap();
        let system_dir = root.path().join("etc/cron.d");
        std::fs::create_dir_all(&system_dir).unwrap();
        std::fs::create_dir_all(root.path().join("home/user")).unwrap();
        std::os::unix::fs::symlink(&system_dir, root.path().join("home/user/data")).unwrap();
        assert_eq!(
            open_dir_no_symlinks(root.path(), "/home/user/data", true).err(),
            Some(MountErrorClass::InvalidPath)
        );
        assert_eq!(
            open_dir_no_symlinks(root.path(), "/home/user/data/nested", true).err(),
            Some(MountErrorClass::InvalidPath)
        );
        assert_eq!(std::fs::read_dir(&system_dir).unwrap().count(), 0);
    }

    #[test]
    fn a_symlinked_ancestor_is_rejected_too() {
        let root = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(root.path().join("usr/local/bin")).unwrap();
        std::fs::create_dir_all(root.path().join("home")).unwrap();
        std::os::unix::fs::symlink(root.path().join("usr/local"), root.path().join("home/user"))
            .unwrap();
        assert_eq!(
            open_dir_no_symlinks(root.path(), "/home/user/bin", true).err(),
            Some(MountErrorClass::InvalidPath)
        );
    }

    #[test]
    fn a_regular_file_in_place_of_a_directory_is_invalid_path() {
        let root = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(root.path().join("mnt")).unwrap();
        std::fs::write(root.path().join("mnt/data"), b"").unwrap();
        assert_eq!(
            open_dir_no_symlinks(root.path(), "/mnt/data", true).err(),
            Some(MountErrorClass::InvalidPath)
        );
    }

    #[test]
    fn mount_targets_are_proc_self_fd_magic_links_never_the_path() {
        let root = tempfile::tempdir().unwrap();
        let dir = open_dir_no_symlinks(root.path(), "/mnt", true).unwrap();
        let fd = dir.as_raw_fd();
        assert_eq!(proc_fd_path(&dir, None), format!("/proc/self/fd/{fd}"));
        assert_eq!(
            proc_fd_path(&dir, Some("data")),
            format!("/proc/self/fd/{fd}/data")
        );
    }

    /// The probe never inherits `rayd`'s environment: only `PATH` is set,
    /// on an environment cleared first, and the binary is absolute.
    #[test]
    fn the_probe_command_sets_only_path_on_a_cleared_environment() {
        let command = probe_command("/mnt/data");
        assert_eq!(command.get_program(), STAT_BINARY);
        let envs: Vec<_> = command.get_envs().collect();
        assert_eq!(
            envs,
            vec![(
                std::ffi::OsStr::new("PATH"),
                Some(std::ffi::OsStr::new(DEFAULT_PATH))
            )]
        );
        assert_eq!(
            command.get_args().collect::<Vec<_>>(),
            vec![std::ffi::OsStr::new("/mnt/data")]
        );
    }

    /// What a guest helper actually sees after `exec`: `PATH` and nothing
    /// else of the parent's environment, and no descriptor of the parent
    /// above stdio, even one opened without `O_CLOEXEC` (the `openpty`
    /// window `process_spawner` seals against).
    #[test]
    fn a_guest_helper_sees_only_path_and_no_inherited_descriptor() {
        let raw = unsafe { libc::open(c"/dev/null".as_ptr(), libc::O_RDONLY) };
        assert!(raw > 2, "open /dev/null");
        let inheritable = unsafe { OwnedFd::from_raw_fd(raw) };
        let script = format!(
            "env; test -e /proc/self/fd/{} && echo LEAKED_FD",
            inheritable.as_raw_fd()
        );
        let mut command = guest_command("/bin/sh", ExecPosture::keep_identity());
        command.args(["-c", &script]).stdout(Stdio::piped());
        let child = ChildRegistry::process().spawn(&mut command).unwrap();
        let output = child.wait_with_output().unwrap();
        let stdout = String::from_utf8(output.stdout).unwrap();
        let names: Vec<&str> = stdout
            .lines()
            .filter_map(|line| line.split('=').next())
            .filter(|name| !SHELL_SET_VARIABLES.contains(name))
            .collect();
        assert_eq!(names, vec!["PATH"], "{stdout}");
        assert!(!stdout.contains("LEAKED_FD"), "{stdout}");
    }

    /// Variables a POSIX shell sets on its own at startup (`dash`, `bash`
    /// as `sh`), not inherited from the parent.
    const SHELL_SET_VARIABLES: [&str; 3] = ["PWD", "SHLVL", "_"];

    #[test]
    fn probe_ready_is_false_for_a_path_with_an_interior_nul() {
        // `stat` itself rejects the argument; this just proves the probe
        // never panics on a value that can't reach the syscall layer.
        assert!(!LinuxFuseDevice.probe_ready("/mnt/da\0ta"));
    }
}
