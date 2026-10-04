//! What every child of `rayd` gets between `fork` and `exec`, whoever
//! launches it: every descriptor above the ones it is meant to inherit
//! marked close-on-exec, and every signal disposition back to `SIG_DFL`
//! with the mask cleared. `PreExecPlan` (`process_spawner`: commands, PTY
//! shells, the kernel sidecar) applies it after its limits and identity
//! drop; `ExecPosture` applies it, after a fixed identity drop, for the
//! internal launchers that are not user code (`fuse_device`'s readiness
//! probe, `mount_s3`'s daemon), so no launcher hands a child another
//! terminal's master or a root-held descriptor (SEC-3, T18). Everything
//! here runs in the forked child: no allocation, no lock.

use std::io;
use std::os::fd::RawFd;

use nix::errno::Errno;
use nix::sys::resource::{Resource, getrlimit};
use nix::sys::signal::{
    SaFlags, SigAction, SigHandler, SigSet, SigmaskHow, Signal, pthread_sigmask, sigaction,
};
use nix::unistd::{Gid, Uid, setgid, setgroups, setuid};

/// First descriptor a child must not inherit: 0/1/2 are its stdio.
pub(crate) const FIRST_NON_STDIO_FD: RawFd = 3;

/// Upper bound of the `fcntl` fallback when `close_range` is missing,
/// so a huge `NOFILE` soft limit cannot stall every spawn.
pub(crate) const FALLBACK_DESCRIPTOR_CEILING: RawFd = 4096;

/// The posture of an internal launcher: an optional fixed identity
/// (`setgroups` to its one group, `setgid`, `setuid`), then the descriptor
/// seal from `first_sealed_fd` up and the signal reset. Built in the
/// parent, so `apply` only issues syscalls on precomputed values.
#[derive(Debug, Clone, Copy)]
pub(crate) struct ExecPosture {
    identity: Option<(Gid, Uid)>,
    first_sealed_fd: RawFd,
    descriptor_ceiling: RawFd,
}

impl ExecPosture {
    /// A child that runs as `uid`/`gid` with no supplementary group but its
    /// own, inheriting only stdio.
    #[must_use]
    pub(crate) fn as_user(uid: u32, gid: u32) -> Self {
        Self {
            identity: Some((Gid::from_raw(gid), Uid::from_raw(uid))),
            first_sealed_fd: FIRST_NON_STDIO_FD,
            descriptor_ceiling: fallback_descriptor_ceiling(),
        }
    }

    /// The same seal and signal reset without an identity change: what a
    /// test, which cannot switch users, applies.
    #[cfg(test)]
    #[must_use]
    pub(crate) fn keep_identity() -> Self {
        Self {
            identity: None,
            first_sealed_fd: FIRST_NON_STDIO_FD,
            descriptor_ceiling: fallback_descriptor_ceiling(),
        }
    }

    /// Leaves `slot` (a descriptor the child must inherit, `dup2`ed there
    /// before `apply`) and everything below it open across `exec`.
    #[must_use]
    pub(crate) fn inheriting_up_to(self, slot: RawFd) -> Self {
        Self {
            first_sealed_fd: slot.saturating_add(1),
            ..self
        }
    }

    /// Runs in the forked child, right before `exec`.
    pub(crate) fn apply(&self) -> io::Result<()> {
        if let Some((gid, uid)) = self.identity {
            setgroups(&[gid]).map_err(io_error)?;
            setgid(gid).map_err(io_error)?;
            setuid(uid).map_err(io_error)?;
        }
        seal_descriptors_from(self.first_sealed_fd, self.descriptor_ceiling);
        reset_signal_dispositions()
    }
}

/// Every signal `rayd` (or whatever launched it, `nohup` included) may
/// have set to `SIG_DFL`/`SIG_IGN`/a handler goes back to `SIG_DFL`, and
/// the blocked set is cleared, right before `exec`: a handler's function
/// pointer would be invalid in the child's new image anyway, but `exec`
/// only resets *that* case on its own, never `SIG_IGN`. `SIGKILL` and
/// `SIGSTOP` refuse `sigaction` with `EINVAL`, which is not a failure
/// here: they are never anything but the default. Both calls only
/// rewrite kernel-held, per-process state from values fixed at compile
/// time; neither allocates nor takes a lock.
pub(crate) fn reset_signal_dispositions() -> io::Result<()> {
    let default = SigAction::new(SigHandler::SigDfl, SaFlags::empty(), SigSet::empty());
    for signal in Signal::iterator() {
        // SAFETY: see the function's doc comment.
        match unsafe { sigaction(signal, &default) } {
            Ok(_) | Err(Errno::EINVAL) => {}
            Err(error) => return Err(io_error(error)),
        }
    }
    pthread_sigmask(SigmaskHow::SIG_SETMASK, Some(&SigSet::empty()), None).map_err(io_error)
}

/// `rayd` opens descriptors without `O_CLOEXEC` for an instant (`openpty`
/// hands back an inheritable master and slave that only become
/// close-on-exec a few instructions later) and may itself inherit some
/// (a runner's pipes). A `fork` from a concurrent spawn landing in that
/// window would hand another terminal's master to the child. Runs in the
/// child: `std` has already `dup2`ed stdin/stdout/stderr onto 0/1/2
/// (which clears their flag) and its own exec-error pipe is already
/// close-on-exec, so marking every descriptor from `first` up leaves what
/// the child must inherit intact and closes the window without closing
/// anything before `exec`. `close_range(CLOSE_RANGE_CLOEXEC)` (Linux 5.11)
/// does it in one syscall; any refusal (`ENOSYS` or `EINVAL` before 5.11,
/// `EPERM` from a seccomp profile older than the syscall) gets the bounded
/// `fcntl` loop instead of failing the spawn. Neither allocates.
#[cfg(target_os = "linux")]
pub(crate) fn seal_descriptors_from(first: RawFd, ceiling: RawFd) {
    if close_range_close_on_exec(first).is_err() {
        mark_close_on_exec_between(first, ceiling);
    }
}

/// Only the `fcntl` loop off Linux.
#[cfg(not(target_os = "linux"))]
pub(crate) fn seal_descriptors_from(first: RawFd, ceiling: RawFd) {
    mark_close_on_exec_between(first, ceiling);
}

/// Raw `syscall` because musl has no `close_range` wrapper; the call
/// takes three integers and touches no memory, and reading `errno`
/// afterwards is async-signal-safe.
#[cfg(target_os = "linux")]
fn close_range_close_on_exec(first: RawFd) -> Result<(), Errno> {
    let result = unsafe {
        libc::syscall(
            libc::SYS_close_range,
            first.unsigned_abs(),
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
fn mark_close_on_exec_between(first: RawFd, ceiling: RawFd) {
    for fd in first..ceiling {
        unsafe { libc::fcntl(fd, libc::F_SETFD, libc::FD_CLOEXEC) };
    }
}

/// `rayd`'s own `NOFILE` soft limit bounds the numbers it can hold,
/// capped at [`FALLBACK_DESCRIPTOR_CEILING`]; read in the parent so
/// the child's lowered limit does not matter.
#[must_use]
pub(crate) fn fallback_descriptor_ceiling() -> RawFd {
    getrlimit(Resource::RLIMIT_NOFILE)
        .ok()
        .and_then(|(soft, _)| RawFd::try_from(soft).ok())
        .map_or(FALLBACK_DESCRIPTOR_CEILING, |soft| {
            soft.min(FALLBACK_DESCRIPTOR_CEILING)
        })
}

fn io_error(errno: Errno) -> io::Error {
    io::Error::from_raw_os_error(errno as i32)
}

#[cfg(test)]
mod tests {
    use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};

    use nix::sys::signal::{SigHandler, Signal, signal};
    use nix::sys::wait::{WaitStatus, waitpid};
    use nix::unistd::{ForkResult, fork};

    use super::*;

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
        close_range_close_on_exec(FIRST_NON_STDIO_FD).unwrap();
        assert!(close_on_exec(&fd));
    }

    #[test]
    fn the_old_kernel_fallback_marks_an_inheritable_descriptor() {
        let fd = inheritable();
        mark_close_on_exec_between(FIRST_NON_STDIO_FD, fd.as_raw_fd() + 1);
        assert!(close_on_exec(&fd));
    }

    /// `inheriting_up_to(slot)` leaves `slot` itself inheritable and seals
    /// everything above it, which is what `mount_s3` needs for the FUSE
    /// descriptor it `dup2`s onto its fixed slot.
    #[test]
    fn a_posture_inheriting_a_slot_seals_only_above_it() {
        let kept = inheritable();
        let sealed = inheritable();
        let (low, high) = if kept.as_raw_fd() < sealed.as_raw_fd() {
            (kept, sealed)
        } else {
            (sealed, kept)
        };
        let posture = ExecPosture::keep_identity().inheriting_up_to(low.as_raw_fd());
        seal_descriptors_from(posture.first_sealed_fd, posture.descriptor_ceiling);
        assert!(!close_on_exec(&low));
        assert!(close_on_exec(&high));
    }

    /// Forks a child that inherits `SIGHUP` ignored (as one launched
    /// under `nohup` would), the exact scenario that used to let a
    /// hung-up shell's foreground job survive a `Kill`
    /// (`m5_pty::kill_takes_the_foreground_job_down_with_the_shell`).
    /// After `reset_signal_dispositions` the child's own `raise` must
    /// run the kernel's default action (terminate), not be swallowed.
    #[test]
    fn reset_signal_dispositions_lets_an_inherited_sighup_ignore_go() {
        // SAFETY: only this process's own `SIGHUP` disposition changes,
        // restored in the parent branch below before anything else in
        // the test binary can observe it.
        let previous = unsafe { signal(Signal::SIGHUP, SigHandler::SigIgn) }.unwrap();
        // SAFETY: the child touches only async-signal-safe state
        // (`reset_signal_dispositions`, `raise`, `_exit`) and never
        // returns through Rust's normal unwinding path.
        match unsafe { fork() }.unwrap() {
            ForkResult::Child => {
                if reset_signal_dispositions().is_err() {
                    unsafe { libc::_exit(2) };
                }
                unsafe { libc::raise(libc::SIGHUP) };
                unsafe { libc::_exit(0) };
            }
            ForkResult::Parent { child } => {
                // SAFETY: restores the disposition this test changed.
                unsafe { signal(Signal::SIGHUP, previous) }.unwrap();
                assert_eq!(
                    waitpid(child, None).unwrap(),
                    WaitStatus::Signaled(child, Signal::SIGHUP, false)
                );
            }
        }
    }

    #[test]
    fn the_fallback_ceiling_is_bounded() {
        let ceiling = fallback_descriptor_ceiling();
        assert!(ceiling > FIRST_NON_STDIO_FD);
        assert!(ceiling <= FALLBACK_DESCRIPTOR_CEILING);
    }
}
