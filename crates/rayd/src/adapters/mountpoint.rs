//! Mountpoints a sandbox user can tamper with, handled without ever
//! following a symlink, plus the bounded `stat` probe both mount features
//! use (`fuse_device` for `mounts=`, ADR-017; `efs_mount` for `volumes=`,
//! ADR-018).
//!
//! `rayd` runs as root, while every allowed mount root under `/home/user`
//! is writable by uid 1000: a path that is only checked lexically
//! (`rayd_core::mount_path`) could still be swapped for a symlink (to
//! `/usr/local/bin`, `/etc/cron.d`, ...) before `Configure` or between an
//! unmount and a remount, and both a plain `create_dir_all` and a path
//! `mount(2)` follow symlinks. So the mountpoint is never touched by path:
//! it is walked one component at a time from `/` with `O_NOFOLLOW`
//! (`open_dir_no_symlinks`), any symlink is rejected as `InvalidPath`, and
//! `mount(2)`/`umount2(2)` only ever see `/proc/self/fd/<dirfd>` — the
//! directory that walk opened, whatever the path names by the time the
//! syscall runs (the same technique `runc` uses for container
//! mountpoints).

use std::ffi::CString;
use std::os::fd::{AsRawFd, OwnedFd};
use std::os::unix::process::CommandExt;
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use rayd_core::process::env::DEFAULT_PATH;

use super::child_registry::ChildRegistry;
use super::dir_walk::{MissingDir, WalkError, open_dir_beneath};
use super::exec_posture::ExecPosture;

/// Where every mount-path walk starts; `rayd_core::mount_path` already
/// guarantees an absolute path under one of its `ALLOWED_ROOTS`.
pub const FILESYSTEM_ROOT: &str = "/";
/// `proc(5)`'s per-descriptor magic links.
const PROC_SELF_FD: &str = "/proc/self/fd";
/// Mode of a mountpoint directory the walk has to create: the same
/// `0o755` (minus umask) `std::fs::create_dir_all` would use.
const MOUNTPOINT_DIR_MODE: u32 = 0o755;
/// The guest's default user (`image/Dockerfile`'s `user`, uid 1000): the
/// identity a readiness probe runs as, because proving that *it* can `stat`
/// the mount is what `mounted` promises the caller.
pub const GUEST_USER_ID: u32 = 1000;
pub const GUEST_GROUP_ID: u32 = 1000;
/// How often `stat_as_guest` polls the probe child for an exit.
const PROBE_POLL_INTERVAL: Duration = Duration::from_millis(10);
/// Absolute, so the probe never resolves a bare name through `rayd`'s own
/// `PATH`: coreutils' `stat`, present in every Rayito image (AL2023 base,
/// `image/Dockerfile`).
const STAT_BINARY: &str = "/usr/bin/stat";

/// Why a mountpoint operation was refused; each feature maps it onto its
/// own closed error class.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MountpointError {
    /// A component is a symlink or not a directory.
    InvalidPath,
    /// A component is missing (and was not to be created), or the path
    /// cannot be expressed as a C string.
    NotFound,
    /// The kernel refused the `mount(2)`/`umount2(2)` itself (`errno`
    /// kept for the caller's own classification).
    Syscall(nix::errno::Errno),
}

/// Opens `relative` (components separated by `/`, leading `/` ignored)
/// beneath `root` as an `O_PATH` directory descriptor, one component at a
/// time, never following a symlink in any of them (`dir_walk`, shared with
/// the filesystem RPCs): a component that is a symlink (or not a directory)
/// is `InvalidPath`. With `create_missing`, a missing component is created
/// (`mkdirat`, `MOUNTPOINT_DIR_MODE`) and then opened the same way, so a
/// racing swap between the two calls is still caught by the `O_NOFOLLOW`
/// open. Every step is relative to the descriptor of the previous one, so
/// renaming or replacing an ancestor after it was opened cannot redirect
/// the rest of the walk.
pub fn open_dir_no_symlinks(
    root: &Path,
    relative: &str,
    create_missing: bool,
) -> Result<OwnedFd, MountpointError> {
    let missing = if create_missing {
        MissingDir::Create(MOUNTPOINT_DIR_MODE)
    } else {
        MissingDir::Fail
    };
    open_dir_beneath(root, relative, missing).map_err(|error| match error {
        // The path no longer names a plain directory chain, which is
        // exactly what the walk exists to refuse.
        WalkError::Symlink | WalkError::NotADirectory => MountpointError::InvalidPath,
        WalkError::Io(_errno) => MountpointError::NotFound,
    })
}

/// `/proc/self/fd/<n>`, or `/proc/self/fd/<n>/<last>` to name a child of
/// the opened directory (only `unmount_no_symlinks` does that, to reach
/// the mount stacked on top of `<last>`).
#[must_use]
pub fn proc_fd_path(dir: &OwnedFd, last: Option<&str>) -> String {
    let base = format!("{PROC_SELF_FD}/{}", dir.as_raw_fd());
    last.map_or_else(|| base.clone(), |name| format!("{base}/{name}"))
}

/// `umount2(MNT_DETACH | UMOUNT_NOFOLLOW)` on `mount_path`, with every
/// ancestor resolved by `open_dir_no_symlinks` (so a symlinked parent can
/// never redirect it to an unrelated mountpoint such as `/dev`) and the
/// last component itself never followed (`UMOUNT_NOFOLLOW`).
/// `MNT_DETACH` lets the unmount succeed even if a reader is still inside
/// the mount, so unmounting never blocks a hook.
pub fn unmount_no_symlinks(mount_path: &str) -> Result<(), MountpointError> {
    let (parent, last) = mount_path
        .rsplit_once('/')
        .ok_or(MountpointError::InvalidPath)?;
    if last.is_empty() {
        return Err(MountpointError::InvalidPath);
    }
    let parent = open_dir_no_symlinks(Path::new(FILESYSTEM_ROOT), parent, false)?;
    detach(&proc_fd_path(&parent, Some(last)))
}

/// `umount2(MNT_DETACH | UMOUNT_NOFOLLOW)` on a path only `rayd` (root)
/// can create or rename (a staging directory under `/run/rayito`), or on
/// a `/proc/self/fd` magic link.
pub fn detach(target: &str) -> Result<(), MountpointError> {
    let target = c_string(target)?;
    // SAFETY: `target` is a valid, nul-terminated C string for the
    // duration of this call.
    let result =
        unsafe { libc::umount2(target.as_ptr(), libc::MNT_DETACH | libc::UMOUNT_NOFOLLOW) };
    if result == 0 {
        Ok(())
    } else {
        Err(MountpointError::Syscall(nix::errno::Errno::last()))
    }
}

/// Bind-mounts `source` (a root-only staging path) onto the directory
/// `target` names, through its `/proc/self/fd` magic link.
pub fn bind_onto(source: &str, target: &OwnedFd) -> Result<(), MountpointError> {
    let source = c_string(source)?;
    let target = c_string(&proc_fd_path(target, None))?;
    // SAFETY: both pointers are valid, nul-terminated C strings owned by
    // local `CString`s kept alive until after the call; a bind mount takes
    // no filesystem type and no data.
    let result = unsafe {
        libc::mount(
            source.as_ptr(),
            target.as_ptr(),
            std::ptr::null(),
            libc::MS_BIND,
            std::ptr::null(),
        )
    };
    if result == 0 {
        Ok(())
    } else {
        Err(MountpointError::Syscall(nix::errno::Errno::last()))
    }
}

pub fn c_string(value: &str) -> Result<CString, MountpointError> {
    CString::new(value).map_err(|_nul_error| MountpointError::NotFound)
}

/// How one bounded `stat` probe ended.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StatProbe {
    /// `stat` exited 0.
    Answered,
    /// `stat` exited non-zero (`EACCES`, `ESTALE`, `ENOENT`, ...), or could
    /// not be started.
    Failed,
    /// Still running at `timeout`: killed and reaped.
    TimedOut,
}

/// `stat <mount_path>` as the guest user, in its own child process, waiting
/// at most `timeout`. A real subprocess, not an in-process `stat(2)`: a
/// hung FUSE daemon or an unreachable NFS server blocks any filesystem call
/// under the mount, and a child can be killed where a blocked thread
/// cannot. Synchronous (it polls); callers run it on the blocking pool.
#[must_use]
pub fn stat_as_guest(mount_path: &str, timeout: Duration) -> StatProbe {
    let mut command = probe_command(mount_path);
    let Ok(mut child) = ChildRegistry::process().spawn(&mut command) else {
        return StatProbe::Failed;
    };
    let deadline = Instant::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(status)) if status.success() => return StatProbe::Answered,
            Ok(Some(_)) | Err(_) => return StatProbe::Failed,
            Ok(None) if Instant::now() < deadline => std::thread::sleep(PROBE_POLL_INTERVAL),
            Ok(None) => {
                // Kill and reap by hand so it never lingers blocked on the
                // mount: this is a `std` child, not a tokio one.
                let _ = child.kill();
                let _ = child.wait();
                return StatProbe::TimedOut;
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

#[cfg(test)]
mod tests {
    use std::os::fd::FromRawFd;

    use super::*;

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
    fn a_path_with_an_interior_nul_is_rejected_before_any_syscall() {
        assert_eq!(c_string("/mnt/da\0ta"), Err(MountpointError::NotFound));
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
            Some(MountpointError::NotFound)
        );
    }

    #[test]
    fn a_symlinked_mount_directory_is_rejected_and_never_followed() {
        // The attack: uid 1000 swaps its own `/home/user/<sub>` for a
        // symlink to a system directory before a mount (or between an
        // unmount and a remount). The walk must refuse it, and must not
        // create anything inside the symlink's target either.
        let root = tempfile::tempdir().unwrap();
        let system_dir = root.path().join("etc/cron.d");
        std::fs::create_dir_all(&system_dir).unwrap();
        std::fs::create_dir_all(root.path().join("home/user")).unwrap();
        std::os::unix::fs::symlink(&system_dir, root.path().join("home/user/data")).unwrap();
        assert_eq!(
            open_dir_no_symlinks(root.path(), "/home/user/data", true).err(),
            Some(MountpointError::InvalidPath)
        );
        assert_eq!(
            open_dir_no_symlinks(root.path(), "/home/user/data/nested", true).err(),
            Some(MountpointError::InvalidPath)
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
            Some(MountpointError::InvalidPath)
        );
    }

    #[test]
    fn a_regular_file_in_place_of_a_directory_is_invalid_path() {
        let root = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(root.path().join("mnt")).unwrap();
        std::fs::write(root.path().join("mnt/data"), b"").unwrap();
        assert_eq!(
            open_dir_no_symlinks(root.path(), "/mnt/data", true).err(),
            Some(MountpointError::InvalidPath)
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

    #[test]
    fn unmounting_a_path_without_a_parent_component_is_invalid() {
        assert_eq!(
            unmount_no_symlinks("relative"),
            Err(MountpointError::InvalidPath)
        );
        assert_eq!(
            unmount_no_symlinks("/mnt/"),
            Err(MountpointError::InvalidPath)
        );
    }

    #[test]
    fn detaching_something_that_is_not_mounted_reports_the_errno() {
        // Unprivileged in the test VM (and not a mountpoint anyway): the
        // kernel refuses, and the refusal is reported, never panicked on.
        let dir = tempfile::tempdir().unwrap();
        assert!(matches!(
            detach(&dir.path().to_string_lossy()),
            Err(MountpointError::Syscall(_))
        ));
    }

    #[test]
    fn a_stat_probe_of_an_existing_directory_answers() {
        // Runs as uid 1000 only when the test process may switch to it;
        // otherwise `setuid` fails in the child and the probe reports
        // `Failed`, which is still never a hang.
        let outcome = stat_as_guest("/", Duration::from_secs(5));
        assert!(matches!(outcome, StatProbe::Answered | StatProbe::Failed));
    }

    #[test]
    fn a_stat_probe_of_a_path_with_an_interior_nul_fails_without_panicking() {
        assert_eq!(
            stat_as_guest("/mnt/da\0ta", Duration::from_millis(300)),
            StatProbe::Failed
        );
    }
}
