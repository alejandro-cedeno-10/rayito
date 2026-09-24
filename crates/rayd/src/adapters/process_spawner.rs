//! `fork`/`exec` adapter. On Linux the child gets its own process group, a
//! from-scratch environment, resource limits, the privilege drop and every
//! descriptor above stdio marked close-on-exec, all in one `pre_exec`
//! (design D3). Off Linux every spawn answers `Unsupported` so the gRPC
//! surface still routes and the domain tests still run.

use std::fmt;
use std::sync::Arc;

use rayd_core::process::UserLookup;

/// Whether `rayd` can `setuid` to the requested identity (started as root,
/// the image) or must keep its own (developer machine, CI).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum IdentitySwitch {
    Enforce,
    KeepCurrent,
}

impl IdentitySwitch {
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Enforce => "enforce",
            Self::KeepCurrent => "keep_current",
        }
    }
}

impl fmt::Display for IdentitySwitch {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// Everything `main` and the tests need to build a `ProcessManager` for the
/// host they run on.
pub struct SpawnPlatform {
    pub spawner: PlatformSpawner,
    pub lookup: Arc<dyn UserLookup>,
    pub identity_switch: IdentitySwitch,
}

#[cfg(unix)]
pub use unix::{CurrentUserLookup, NixUserLookup, TokioChild, TokioProcessSpawner};
#[cfg(unix)]
pub type PlatformSpawner = unix::TokioProcessSpawner;
#[cfg(unix)]
pub(crate) use unix::PreExecPlan;
#[cfg(unix)]
pub use unix::{detect_spawn_platform, inherited_nofile_limits};

#[cfg(not(unix))]
pub use unsupported::{UnsupportedChild, UnsupportedLookup, UnsupportedSpawner};
#[cfg(not(unix))]
pub type PlatformSpawner = unsupported::UnsupportedSpawner;
#[cfg(not(unix))]
pub use unsupported::{detect_spawn_platform, inherited_nofile_limits};

#[cfg(unix)]
mod unix {
    use std::ffi::CString;
    use std::io;
    use std::os::fd::RawFd;
    use std::os::unix::process::ExitStatusExt;
    use std::process::{ExitStatus, Stdio};
    use std::sync::Arc;

    use nix::errno::Errno;
    use nix::sys::resource::{Resource, getrlimit, rlim_t, setrlimit};
    use nix::sys::signal::{Signal, killpg};
    use nix::unistd::{
        Gid, Uid, User, geteuid, getgid, getgrouplist, getgroups, getuid, setgid, setgroups, setuid,
    };
    use rayd_core::process::{
        LookupError, Pid, ProcessIdentity, ProcessSpawner, SignalError, SpawnError, SpawnSpec,
        SpawnedChild, StdinMode, UserLookup, WaitOutcome,
    };
    use rayd_core::pty::FALLBACK_SHELL;
    use tokio::process::{Child, Command};

    use super::{IdentitySwitch, SpawnPlatform};
    use crate::process::child::{ChildIo, ChildReader, ChildWriter, WaitFuture};

    #[must_use]
    pub fn detect_spawn_platform() -> SpawnPlatform {
        if geteuid().is_root() {
            SpawnPlatform {
                spawner: TokioProcessSpawner::new(IdentitySwitch::Enforce),
                lookup: Arc::new(NixUserLookup),
                identity_switch: IdentitySwitch::Enforce,
            }
        } else {
            SpawnPlatform {
                spawner: TokioProcessSpawner::new(IdentitySwitch::KeepCurrent),
                lookup: Arc::new(CurrentUserLookup),
                identity_switch: IdentitySwitch::KeepCurrent,
            }
        }
    }

    /// `(soft, hard)` `RLIMIT_NOFILE` of `rayd` itself, logged at boot so a
    /// platform that refuses to raise the hard limit shows up in `CloudWatch`.
    #[must_use]
    pub fn inherited_nofile_limits() -> Option<(u64, u64)> {
        getrlimit(Resource::RLIMIT_NOFILE).ok()
    }

    /// `getpwnam` + `getgrouplist`, resolved in the parent before `fork`.
    pub struct NixUserLookup;

    impl UserLookup for NixUserLookup {
        fn lookup(&self, username: &str) -> Result<ProcessIdentity, LookupError> {
            let user = User::from_name(username)
                .map_err(|error| LookupError::Failed(errno_name(error)))?
                .ok_or(LookupError::UnknownUser)?;
            let c_name = CString::new(username).map_err(|_| LookupError::UnknownUser)?;
            let groups = getgrouplist(&c_name, user.gid)
                .map_err(|error| LookupError::Failed(errno_name(error)))?;
            let shell = login_shell(Some(&user));
            Ok(ProcessIdentity {
                uid: user.uid.as_raw(),
                gid: user.gid.as_raw(),
                groups: groups.iter().map(|gid| gid.as_raw()).collect(),
                username: user.name,
                home: user.dir.to_string_lossy().into_owned(),
                shell,
            })
        }
    }

    /// `pw_shell`, or the POSIX fallback when the entry is empty or absent.
    fn login_shell(user: Option<&User>) -> String {
        user.map(|user| user.shell.to_string_lossy().into_owned())
            .filter(|shell| !shell.is_empty())
            .unwrap_or_else(|| FALLBACK_SHELL.to_owned())
    }

    /// Unprivileged `rayd` cannot switch users, so every request resolves to
    /// the identity it already runs with (the policy gate still refuses
    /// `root` by name before reaching here).
    pub struct CurrentUserLookup;

    impl UserLookup for CurrentUserLookup {
        fn lookup(&self, _username: &str) -> Result<ProcessIdentity, LookupError> {
            let uid = getuid();
            let gid = getgid();
            let user = User::from_uid(uid).ok().flatten();
            let groups = getgroups().unwrap_or_else(|_| vec![gid]);
            let username = user
                .as_ref()
                .map_or_else(|| uid.as_raw().to_string(), |user| user.name.clone());
            let home = user.as_ref().map_or_else(
                || std::env::var("HOME").unwrap_or_else(|_| "/".to_owned()),
                |user| user.dir.to_string_lossy().into_owned(),
            );
            Ok(ProcessIdentity {
                uid: uid.as_raw(),
                gid: gid.as_raw(),
                groups: groups.iter().map(|gid| gid.as_raw()).collect(),
                username,
                home,
                shell: login_shell(user.as_ref()),
            })
        }
    }

    pub struct TokioProcessSpawner {
        identity_switch: IdentitySwitch,
    }

    impl TokioProcessSpawner {
        #[must_use]
        pub fn new(identity_switch: IdentitySwitch) -> Self {
            Self { identity_switch }
        }
    }

    impl ProcessSpawner for TokioProcessSpawner {
        type Child = TokioChild;

        fn spawn(&self, spec: &SpawnSpec) -> Result<TokioChild, SpawnError> {
            let plan = PreExecPlan::new(spec, self.identity_switch)
                .map_err(|error| spawn_error(&error))?;
            let mut command = Command::new(&spec.program);
            command
                .args(&spec.args)
                .env_clear()
                .envs(spec.env.iter().map(|(key, value)| (key, value)))
                .current_dir(&spec.cwd)
                .process_group(0)
                .stdin(stdio_for(spec.stdin))
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .kill_on_drop(false);
            // SAFETY: `apply` only issues setrlimit/setgroups/setgid/setuid and
            // close_range (or fcntl) on values computed before the fork; it
            // allocates nothing and takes no locks, which is what
            // async-signal-safety requires here.
            unsafe { command.pre_exec(move || plan.apply()) };
            let child = command.spawn().map_err(|error| spawn_error(&error))?;
            let pid = child
                .id()
                .ok_or_else(|| SpawnError::Failed("child has no pid".to_owned()))?;
            Ok(TokioChild {
                pid: Pid(pid),
                inner: child,
            })
        }

        fn signal_group(&self, pid: Pid, signal: i32) -> Result<(), SignalError> {
            let signal =
                Signal::try_from(signal).map_err(|error| SignalError::Failed(errno_name(error)))?;
            let group = i32::try_from(pid.0)
                .map_err(|_| SignalError::Failed("pid does not fit pid_t".to_owned()))?;
            killpg(nix::unistd::Pid::from_raw(group), signal).map_err(|error| match error {
                Errno::ESRCH => SignalError::NoSuchProcess,
                other => SignalError::Failed(errno_name(other)),
            })
        }
    }

    /// First descriptor a child must not inherit: 0/1/2 are its stdio.
    const FIRST_NON_STDIO_FD: RawFd = 3;

    /// Upper bound of the `fcntl` fallback when `close_range` is missing,
    /// so a huge `NOFILE` soft limit cannot stall every spawn.
    const FALLBACK_DESCRIPTOR_CEILING: RawFd = 4096;

    /// Everything the child does between `fork` and `exec`, precomputed in
    /// the parent. Order matters: limits first (raising the hard `NOFILE`
    /// needs privilege), then `setgroups`, `setgid`, `setuid`, and last
    /// every descriptor above stdio marked close-on-exec. Shared by the
    /// three launchers of user code (processes, PTY shells and the kernel
    /// sidecar, which receives nothing but its three pipes) so all of them
    /// get the same posture; only the sandbox's own processes and PTYs
    /// carry a CPU budget.
    pub(crate) struct PreExecPlan {
        limits: [(Resource, rlim_t, rlim_t); 3],
        cpu: Option<(rlim_t, rlim_t)>,
        identity: Option<(Vec<Gid>, Gid, Uid)>,
        descriptor_ceiling: RawFd,
    }

    impl PreExecPlan {
        pub(crate) fn new(spec: &SpawnSpec, switch: IdentitySwitch) -> io::Result<Self> {
            let limits = [
                limit_plan(Resource::RLIMIT_NPROC, spec.limits.nproc)?,
                limit_plan(Resource::RLIMIT_NOFILE, spec.limits.nofile)?,
                limit_plan(Resource::RLIMIT_CORE, spec.limits.core)?,
            ];
            let cpu = spec.limits.cpu_rlimit();
            let identity = match switch {
                IdentitySwitch::Enforce => Some((
                    spec.identity
                        .groups
                        .iter()
                        .copied()
                        .map(Gid::from_raw)
                        .collect(),
                    Gid::from_raw(spec.identity.gid),
                    Uid::from_raw(spec.identity.uid),
                )),
                IdentitySwitch::KeepCurrent => None,
            };
            Ok(Self {
                limits,
                cpu,
                identity,
                descriptor_ceiling: fallback_descriptor_ceiling(),
            })
        }

        /// Runs in the child. A limit is first set as requested; if the
        /// platform refuses to raise the hard limit (no `CAP_SYS_RESOURCE`)
        /// it is clamped to the inherited hard limit instead of failing the
        /// spawn. `RLIMIT_CPU` only ever lowers (soft `N`, hard `N + grace`),
        /// so it never needs the clamp: `SIGXCPU` at the soft limit,
        /// `SIGKILL` at the hard one. The descriptor seal comes last, right
        /// before `exec`.
        pub(crate) fn apply(&self) -> io::Result<()> {
            for (resource, desired, clamped) in &self.limits {
                setrlimit(*resource, *desired, *desired)
                    .or_else(|_| setrlimit(*resource, *clamped, *clamped))
                    .map_err(io_error)?;
            }
            if let Some((soft, hard)) = self.cpu {
                setrlimit(Resource::RLIMIT_CPU, soft, hard).map_err(io_error)?;
            }
            if let Some((groups, gid, uid)) = &self.identity {
                setgroups(groups).map_err(io_error)?;
                setgid(*gid).map_err(io_error)?;
                setuid(*uid).map_err(io_error)?;
            }
            seal_descriptors_above_stdio(self.descriptor_ceiling);
            Ok(())
        }
    }

    /// `rayd` opens descriptors without `O_CLOEXEC` for an instant (`openpty`
    /// hands back an inheritable master and slave that only become
    /// close-on-exec a few instructions later) and may itself inherit some
    /// (a runner's pipes). A `fork` from a concurrent spawn landing in that
    /// window would hand another terminal's master to user code. Runs in
    /// the child: `std` has already `dup2`ed stdin/stdout/stderr onto 0/1/2
    /// (which clears their flag) and its own exec-error pipe is already
    /// close-on-exec, so marking every descriptor from 3 up leaves stdio
    /// intact and closes the window without closing anything before `exec`.
    /// `close_range(CLOSE_RANGE_CLOEXEC)` (Linux 5.11) does it in one
    /// syscall; any refusal (`ENOSYS` or `EINVAL` before 5.11, `EPERM` from
    /// a seccomp profile older than the syscall) gets the bounded `fcntl`
    /// loop instead of failing the spawn. Neither allocates.
    #[cfg(target_os = "linux")]
    fn seal_descriptors_above_stdio(ceiling: RawFd) {
        if close_range_close_on_exec().is_err() {
            mark_close_on_exec_below(ceiling);
        }
    }

    /// Only the `fcntl` loop off Linux.
    #[cfg(not(target_os = "linux"))]
    fn seal_descriptors_above_stdio(ceiling: RawFd) {
        mark_close_on_exec_below(ceiling);
    }

    /// Raw `syscall` because musl has no `close_range` wrapper; the call
    /// takes three integers and touches no memory, and reading `errno`
    /// afterwards is async-signal-safe.
    #[cfg(target_os = "linux")]
    fn close_range_close_on_exec() -> Result<(), Errno> {
        let result = unsafe {
            libc::syscall(
                libc::SYS_close_range,
                FIRST_NON_STDIO_FD.unsigned_abs(),
                libc::c_uint::MAX,
                libc::CLOSE_RANGE_CLOEXEC,
            )
        };
        if result == -1 {
            return Err(Errno::last());
        }
        Ok(())
    }

    /// `F_SETFD` takes a plain integer and touches no memory; a number that
    /// is not open answers `EBADF`, which is exactly "nothing to seal".
    fn mark_close_on_exec_below(ceiling: RawFd) {
        for fd in FIRST_NON_STDIO_FD..ceiling {
            unsafe { libc::fcntl(fd, libc::F_SETFD, libc::FD_CLOEXEC) };
        }
    }

    /// `rayd`'s own `NOFILE` soft limit bounds the numbers it can hold,
    /// capped at [`FALLBACK_DESCRIPTOR_CEILING`]; read in the parent so
    /// the child's lowered limit does not matter.
    fn fallback_descriptor_ceiling() -> RawFd {
        getrlimit(Resource::RLIMIT_NOFILE)
            .ok()
            .and_then(|(soft, _)| RawFd::try_from(soft).ok())
            .map_or(FALLBACK_DESCRIPTOR_CEILING, |soft| {
                soft.min(FALLBACK_DESCRIPTOR_CEILING)
            })
    }

    fn limit_plan(resource: Resource, desired: u64) -> io::Result<(Resource, rlim_t, rlim_t)> {
        let (_, hard) = getrlimit(resource).map_err(io_error)?;
        Ok((resource, desired, desired.min(hard)))
    }

    pub struct TokioChild {
        pid: Pid,
        inner: Child,
    }

    impl SpawnedChild for TokioChild {
        fn pid(&self) -> Pid {
            self.pid
        }
    }

    impl ChildIo for TokioChild {
        fn take_stdin(&mut self) -> Option<ChildWriter> {
            self.inner
                .stdin
                .take()
                .map(|stdin| Box::new(stdin) as ChildWriter)
        }

        fn take_stdout(&mut self) -> Option<ChildReader> {
            self.inner
                .stdout
                .take()
                .map(|stdout| Box::new(stdout) as ChildReader)
        }

        fn take_stderr(&mut self) -> Option<ChildReader> {
            self.inner
                .stderr
                .take()
                .map(|stderr| Box::new(stderr) as ChildReader)
        }

        fn wait(&mut self) -> WaitFuture<'_> {
            Box::pin(async move { self.inner.wait().await.map(wait_outcome) })
        }
    }

    fn wait_outcome(status: ExitStatus) -> WaitOutcome {
        status
            .code()
            .map(WaitOutcome::Exited)
            .or_else(|| status.signal().map(WaitOutcome::Signaled))
            .unwrap_or(WaitOutcome::Exited(-1))
    }

    fn stdio_for(mode: StdinMode) -> Stdio {
        match mode {
            StdinMode::Null => Stdio::null(),
            StdinMode::Pipe => Stdio::piped(),
        }
    }

    fn spawn_error(error: &io::Error) -> SpawnError {
        match error.raw_os_error().map(Errno::from_raw) {
            Some(errno @ (Errno::ENOENT | Errno::EACCES | Errno::ENOTDIR)) => {
                SpawnError::CannotExecute(errno_name(errno))
            }
            Some(errno) => SpawnError::Failed(errno_name(errno)),
            None => SpawnError::Failed(error.kind().to_string()),
        }
    }

    fn io_error(errno: Errno) -> io::Error {
        io::Error::from_raw_os_error(errno as i32)
    }

    fn errno_name(errno: Errno) -> String {
        format!("{errno:?}")
    }

    #[cfg(test)]
    mod tests {
        use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};

        use super::{
            FALLBACK_DESCRIPTOR_CEILING, FIRST_NON_STDIO_FD, fallback_descriptor_ceiling,
            mark_close_on_exec_below,
        };

        /// `/dev/null` without `O_CLOEXEC`, as `openpty` hands its pair back.
        fn inheritable() -> OwnedFd {
            let raw = unsafe { libc::open(c"/dev/null".as_ptr(), libc::O_RDONLY) };
            assert!(raw >= FIRST_NON_STDIO_FD, "open /dev/null");
            let fd = unsafe { OwnedFd::from_raw_fd(raw) };
            assert!(!close_on_exec(&fd));
            fd
        }

        fn close_on_exec(fd: &OwnedFd) -> bool {
            let flags = unsafe { libc::fcntl(fd.as_raw_fd(), libc::F_GETFD) };
            flags & libc::FD_CLOEXEC != 0
        }

        #[cfg(target_os = "linux")]
        #[test]
        fn close_range_marks_an_inheritable_descriptor() {
            let fd = inheritable();
            super::close_range_close_on_exec().unwrap();
            assert!(close_on_exec(&fd));
        }

        #[test]
        fn the_old_kernel_fallback_marks_an_inheritable_descriptor() {
            let fd = inheritable();
            mark_close_on_exec_below(fd.as_raw_fd() + 1);
            assert!(close_on_exec(&fd));
        }

        #[test]
        fn the_fallback_ceiling_is_bounded() {
            let ceiling = fallback_descriptor_ceiling();
            assert!(ceiling > FIRST_NON_STDIO_FD);
            assert!(ceiling <= FALLBACK_DESCRIPTOR_CEILING);
        }
    }
}

#[cfg(not(unix))]
mod unsupported {
    use std::sync::Arc;

    use rayd_core::process::{
        LookupError, Pid, ProcessIdentity, ProcessSpawner, SignalError, SpawnError, SpawnSpec,
        SpawnedChild, UserLookup,
    };

    use super::{IdentitySwitch, SpawnPlatform};
    use crate::process::child::{ChildIo, ChildReader, ChildWriter, WaitFuture};

    #[must_use]
    pub fn detect_spawn_platform() -> SpawnPlatform {
        SpawnPlatform {
            spawner: UnsupportedSpawner,
            lookup: Arc::new(UnsupportedLookup),
            identity_switch: IdentitySwitch::KeepCurrent,
        }
    }

    #[must_use]
    pub fn inherited_nofile_limits() -> Option<(u64, u64)> {
        None
    }

    pub struct UnsupportedLookup;

    impl UserLookup for UnsupportedLookup {
        fn lookup(&self, _username: &str) -> Result<ProcessIdentity, LookupError> {
            Err(LookupError::Failed(
                "user database is not available on this platform".to_owned(),
            ))
        }
    }

    #[derive(Debug, Default, Clone, Copy)]
    pub struct UnsupportedSpawner;

    impl ProcessSpawner for UnsupportedSpawner {
        type Child = UnsupportedChild;

        fn spawn(&self, _spec: &SpawnSpec) -> Result<UnsupportedChild, SpawnError> {
            Err(SpawnError::Unsupported)
        }

        fn signal_group(&self, _pid: Pid, _signal: i32) -> Result<(), SignalError> {
            Err(SignalError::NoSuchProcess)
        }
    }

    /// Never constructed: the spawner always fails first.
    pub enum UnsupportedChild {}

    impl SpawnedChild for UnsupportedChild {
        fn pid(&self) -> Pid {
            match *self {}
        }
    }

    impl ChildIo for UnsupportedChild {
        fn take_stdin(&mut self) -> Option<ChildWriter> {
            match *self {}
        }

        fn take_stdout(&mut self) -> Option<ChildReader> {
            match *self {}
        }

        fn take_stderr(&mut self) -> Option<ChildReader> {
            match *self {}
        }

        fn wait(&mut self) -> WaitFuture<'_> {
            match *self {}
        }
    }
}
