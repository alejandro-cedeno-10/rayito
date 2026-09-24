//! `openpty` adapter (design D3, ADR-005). On Linux the shell is spawned
//! with the slave as stdin/stdout/stderr and, in `pre_exec`, made a session
//! leader (`setsid`) whose controlling terminal is that slave
//! (`TIOCSCTTY`) before the M2 limits and privilege drop; the master is
//! non-blocking under tokio's `AsyncFd`. Both descriptors are marked
//! close-on-exec right after `openpty` (which hands back inheritable
//! ones), and the shared `PreExecPlan` marks every descriptor above stdio
//! close-on-exec in the child right before `exec`, which also covers a
//! concurrent spawn forking between another terminal's `openpty` and its
//! `F_SETFD`: the shell and everything it runs see the slave only as fds
//! 0/1/2, never a master. `process_group(0)` is deliberately not used:
//! `std` would `setpgid` before `pre_exec` and `setsid` fails with `EPERM`
//! for a group leader. Off Linux every open answers `Unsupported` so the
//! gRPC surface still routes.

#[cfg(unix)]
pub use unix::{NixPty, NixPtyBackend, PtyMaster};
#[cfg(unix)]
pub type PlatformPtyBackend = unix::NixPtyBackend;

#[cfg(not(unix))]
pub use unsupported::{UnsupportedPty, UnsupportedPtyBackend};
#[cfg(not(unix))]
pub type PlatformPtyBackend = unsupported::UnsupportedPtyBackend;

#[cfg(unix)]
mod unix {
    use std::io;
    use std::os::fd::{AsFd, AsRawFd, OwnedFd};
    use std::os::unix::process::ExitStatusExt;
    use std::pin::Pin;
    use std::process::{ExitStatus, Stdio};
    use std::sync::Arc;
    use std::task::{Context, Poll};

    use nix::errno::Errno;
    use nix::fcntl::{FcntlArg, FdFlag, OFlag, fcntl};
    use nix::pty::{OpenptyResult, Winsize, openpty};
    use nix::sys::signal::{Signal, killpg};
    use nix::sys::stat::{Mode, fchmod};
    use nix::unistd::{Gid, Uid, fchown, setsid};
    use rayd_core::process::{Pid, SignalError, SpawnError, SpawnSpec, WaitOutcome};
    use rayd_core::pty::{PtyBackend, PtyChild, PtySize};
    use tokio::io::unix::AsyncFd;
    use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};
    use tokio::process::{Child, Command};

    use crate::adapters::IdentitySwitch;
    use crate::adapters::process_spawner::PreExecPlan;
    use crate::process::child::{ChildReader, ChildWriter, WaitFuture};
    use crate::pty::child::{PtyIo, PtyResizer};

    /// Mode of the slave once it belongs to the user: as `login` sets it.
    const SLAVE_MODE: u32 = 0o620;

    pub struct NixPtyBackend {
        identity_switch: IdentitySwitch,
    }

    impl NixPtyBackend {
        #[must_use]
        pub fn new(identity_switch: IdentitySwitch) -> Self {
            Self { identity_switch }
        }
    }

    impl PtyBackend for NixPtyBackend {
        type Pty = NixPty;

        fn open(&self, spec: &SpawnSpec, size: PtySize) -> Result<NixPty, SpawnError> {
            let plan = PreExecPlan::new(spec, self.identity_switch)
                .map_err(|error| SpawnError::Failed(io_error_name(&error)))?;
            let OpenptyResult { master, slave } =
                openpty(Some(&winsize(size)), None).map_err(failed)?;
            close_on_exec(&master)?;
            close_on_exec(&slave)?;
            if self.identity_switch == IdentitySwitch::Enforce {
                grant_slave(&slave, spec)?;
            }
            let stdin = slave
                .try_clone()
                .map_err(|error| SpawnError::Failed(io_error_name(&error)))?;
            let stdout = slave
                .try_clone()
                .map_err(|error| SpawnError::Failed(io_error_name(&error)))?;
            let mut command = Command::new(&spec.program);
            command
                .args(&spec.args)
                .env_clear()
                .envs(spec.env.iter().map(|(key, value)| (key, value)))
                .current_dir(&spec.cwd)
                .stdin(Stdio::from(stdin))
                .stdout(Stdio::from(stdout))
                .stderr(Stdio::from(slave))
                .kill_on_drop(false);
            // SAFETY: `setsid`, the `TIOCSCTTY` ioctl and `apply` (limits,
            // identity, then close_range or fcntl on every descriptor above
            // stdio) only issue syscalls on values computed before the fork;
            // they allocate nothing and take no locks, which is what
            // async-signal-safety requires here.
            unsafe {
                command.pre_exec(move || {
                    setsid().map_err(io_error)?;
                    take_controlling_terminal()?;
                    plan.apply()
                });
            }
            let child = command.spawn().map_err(|error| spawn_error(&error))?;
            let pid = child
                .id()
                .ok_or_else(|| SpawnError::Failed("child has no pid".to_owned()))?;
            let master = PtyMaster::new(master)
                .map_err(|error| SpawnError::Failed(io_error_name(&error)))?;
            Ok(NixPty {
                pid: Pid(pid),
                child,
                master: Some(master.clone()),
                writer: Some(master.clone()),
                resizer: master,
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

    /// `openpty` returns inheritable descriptors: left as they are, the
    /// shell and every job it starts would keep a copy of the master (able
    /// to read and write the terminal, keeping `/dev/pts/N` allocated and
    /// defeating the hangup that follows `Kill`). `try_clone` already dups
    /// with `O_CLOEXEC`; the child's `dup2` onto 0/1/2 clears the flag on
    /// the stdio copies only. Setting the flag here keeps the pair out of
    /// `rayd`'s own helpers (`ip`); a user process forked by a concurrent
    /// spawn before this line runs is covered by the plan's seal instead.
    fn close_on_exec(fd: &OwnedFd) -> Result<(), SpawnError> {
        fcntl(fd, FcntlArg::F_SETFD(FdFlag::FD_CLOEXEC))
            .map(drop)
            .map_err(failed)
    }

    /// `openpty` grants the slave to the caller (root); programs that reopen
    /// `/dev/tty` as the user need it owned by the user.
    fn grant_slave(slave: &OwnedFd, spec: &SpawnSpec) -> Result<(), SpawnError> {
        fchown(
            slave,
            Some(Uid::from_raw(spec.identity.uid)),
            Some(Gid::from_raw(spec.identity.gid)),
        )
        .map_err(failed)?;
        fchmod(slave, Mode::from_bits_truncate(SLAVE_MODE)).map_err(failed)?;
        Ok(())
    }

    /// Runs in the child after `setsid`: fd 0 is the slave.
    fn take_controlling_terminal() -> io::Result<()> {
        // SAFETY: `ioctl(TIOCSCTTY)` on an open descriptor with a plain
        // integer argument; no memory is passed.
        let result = unsafe { libc::ioctl(0, libc::TIOCSCTTY as _, 0) };
        if result == -1 {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    }

    fn winsize(size: PtySize) -> Winsize {
        Winsize {
            ws_row: size.rows(),
            ws_col: size.cols(),
            ws_xpixel: 0,
            ws_ypixel: 0,
        }
    }

    /// The master side, shared by the reader, the writer and the resizer.
    #[derive(Clone)]
    pub struct PtyMaster {
        fd: Arc<AsyncFd<OwnedFd>>,
    }

    impl PtyMaster {
        fn new(master: OwnedFd) -> io::Result<Self> {
            let flags = fcntl(&master, FcntlArg::F_GETFL).map_err(io_error)?;
            let flags = OFlag::from_bits_truncate(flags) | OFlag::O_NONBLOCK;
            fcntl(&master, FcntlArg::F_SETFL(flags)).map_err(io_error)?;
            Ok(Self {
                fd: Arc::new(AsyncFd::new(master)?),
            })
        }
    }

    /// `EIO` on the master means every slave descriptor is closed: the
    /// terminal is finished, reported as EOF.
    impl AsyncRead for PtyMaster {
        fn poll_read(
            self: Pin<&mut Self>,
            cx: &mut Context<'_>,
            buf: &mut ReadBuf<'_>,
        ) -> Poll<io::Result<()>> {
            loop {
                let mut guard = std::task::ready!(self.fd.poll_read_ready(cx))?;
                let unfilled = buf.initialize_unfilled();
                let read = guard.try_io(|inner| read_master(inner.get_ref(), unfilled));
                match read {
                    Ok(Ok(length)) => {
                        buf.advance(length);
                        return Poll::Ready(Ok(()));
                    }
                    Ok(Err(error)) if error.raw_os_error() == Some(Errno::EIO as i32) => {
                        return Poll::Ready(Ok(()));
                    }
                    Ok(Err(error)) => return Poll::Ready(Err(error)),
                    Err(_would_block) => {}
                }
            }
        }
    }

    impl AsyncWrite for PtyMaster {
        fn poll_write(
            self: Pin<&mut Self>,
            cx: &mut Context<'_>,
            data: &[u8],
        ) -> Poll<io::Result<usize>> {
            loop {
                let mut guard = std::task::ready!(self.fd.poll_write_ready(cx))?;
                match guard.try_io(|inner| write_master(inner.get_ref(), data)) {
                    Ok(result) => return Poll::Ready(result),
                    Err(_would_block) => {}
                }
            }
        }

        fn poll_flush(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<io::Result<()>> {
            Poll::Ready(Ok(()))
        }

        fn poll_shutdown(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<io::Result<()>> {
            Poll::Ready(Ok(()))
        }
    }

    impl PtyResizer for PtyMaster {
        fn resize(&self, size: PtySize) -> io::Result<()> {
            let window = libc::winsize {
                ws_row: size.rows(),
                ws_col: size.cols(),
                ws_xpixel: 0,
                ws_ypixel: 0,
            };
            // SAFETY: `ioctl(TIOCSWINSZ)` reads the `winsize` struct passed by
            // pointer for the duration of the call only.
            let result = unsafe {
                libc::ioctl(
                    self.fd.as_raw_fd(),
                    libc::TIOCSWINSZ as _,
                    std::ptr::from_ref(&window),
                )
            };
            if result == -1 {
                return Err(io::Error::last_os_error());
            }
            Ok(())
        }
    }

    fn read_master(fd: &OwnedFd, buffer: &mut [u8]) -> io::Result<usize> {
        nix::unistd::read(fd, buffer).map_err(io_error)
    }

    fn write_master(fd: &OwnedFd, data: &[u8]) -> io::Result<usize> {
        nix::unistd::write(fd.as_fd(), data).map_err(io_error)
    }

    pub struct NixPty {
        pid: Pid,
        child: Child,
        master: Option<PtyMaster>,
        writer: Option<PtyMaster>,
        resizer: PtyMaster,
    }

    impl PtyChild for NixPty {
        fn pid(&self) -> Pid {
            self.pid
        }
    }

    impl PtyIo for NixPty {
        fn take_reader(&mut self) -> Option<ChildReader> {
            self.master
                .take()
                .map(|master| Box::new(master) as ChildReader)
        }

        fn take_writer(&mut self) -> Option<ChildWriter> {
            self.writer
                .take()
                .map(|master| Box::new(master) as ChildWriter)
        }

        fn resize_handle(&self) -> Arc<dyn PtyResizer> {
            Arc::new(self.resizer.clone())
        }

        fn wait(&mut self) -> WaitFuture<'_> {
            Box::pin(async move { self.child.wait().await.map(wait_outcome) })
        }
    }

    fn wait_outcome(status: ExitStatus) -> WaitOutcome {
        status
            .code()
            .map(WaitOutcome::Exited)
            .or_else(|| status.signal().map(WaitOutcome::Signaled))
            .unwrap_or(WaitOutcome::Exited(-1))
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

    fn failed(errno: Errno) -> SpawnError {
        SpawnError::Failed(errno_name(errno))
    }

    fn io_error(errno: Errno) -> io::Error {
        io::Error::from_raw_os_error(errno as i32)
    }

    fn io_error_name(error: &io::Error) -> String {
        crate::adapters::io_error_name(error)
    }

    fn errno_name(errno: Errno) -> String {
        format!("{errno:?}")
    }
}

#[cfg(not(unix))]
mod unsupported {
    use std::sync::Arc;

    use rayd_core::process::{Pid, SignalError, SpawnError, SpawnSpec};
    use rayd_core::pty::{PtyBackend, PtyChild, PtySize};

    use crate::adapters::IdentitySwitch;
    use crate::process::child::{ChildReader, ChildWriter, WaitFuture};
    use crate::pty::child::{PtyIo, PtyResizer};

    #[derive(Debug, Default, Clone, Copy)]
    pub struct UnsupportedPtyBackend;

    impl UnsupportedPtyBackend {
        #[must_use]
        pub fn new(_identity_switch: IdentitySwitch) -> Self {
            Self
        }
    }

    impl PtyBackend for UnsupportedPtyBackend {
        type Pty = UnsupportedPty;

        fn open(&self, _spec: &SpawnSpec, _size: PtySize) -> Result<UnsupportedPty, SpawnError> {
            Err(SpawnError::Unsupported)
        }

        fn signal_group(&self, _pid: Pid, _signal: i32) -> Result<(), SignalError> {
            Err(SignalError::NoSuchProcess)
        }
    }

    /// Never constructed: the backend always fails first.
    pub enum UnsupportedPty {}

    impl PtyChild for UnsupportedPty {
        fn pid(&self) -> Pid {
            match *self {}
        }
    }

    impl PtyIo for UnsupportedPty {
        fn take_reader(&mut self) -> Option<ChildReader> {
            match *self {}
        }

        fn take_writer(&mut self) -> Option<ChildWriter> {
            match *self {}
        }

        fn resize_handle(&self) -> Arc<dyn PtyResizer> {
            match *self {}
        }

        fn wait(&mut self) -> WaitFuture<'_> {
            match *self {}
        }
    }
}
