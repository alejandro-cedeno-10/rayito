//! Per-thread filesystem identity for the blocking calls of the filesystem
//! adapters (design D3): `setfsgid` then `setfsuid` on entry, restored in
//! reverse when the guard drops, so a blocking-pool thread never carries a
//! request's identity into the next task. The kernel drops the
//! `CAP_DAC_*`/`CAP_FOWNER`/`CAP_CHOWN` family from the thread while its
//! fsuid is not 0, which is exactly what makes `EACCES` real. `KeepCurrent`
//! is a no-op everywhere; `Enforce` is Linux-only.

use rayd_core::filesystem::{FsIdentity, FsIoError};

use super::IdentitySwitch;

#[must_use = "the identity is restored when the guard drops"]
pub struct FsIdentityGuard {
    previous: Option<Previous>,
}

impl FsIdentityGuard {
    pub fn enter(switch: IdentitySwitch, id: &FsIdentity) -> Result<Self, FsIoError> {
        match switch {
            IdentitySwitch::KeepCurrent => Ok(Self { previous: None }),
            IdentitySwitch::Enforce => Self::switch_to(id),
        }
    }

    #[must_use]
    pub fn is_switched(&self) -> bool {
        self.previous.is_some()
    }
}

#[cfg(any(target_os = "linux", target_os = "android"))]
struct Previous {
    uid: nix::unistd::Uid,
    gid: nix::unistd::Gid,
}

#[cfg(any(target_os = "linux", target_os = "android"))]
mod linux {
    use nix::unistd::{Gid, Uid, setfsgid, setfsuid};
    use rayd_core::filesystem::{FsIdentity, FsIoError};

    use super::{FsIdentityGuard, Previous};

    /// `setfsuid(-1)` reports the current value without changing it.
    const PROBE: u32 = u32::MAX;

    impl FsIdentityGuard {
        pub(super) fn switch_to(id: &FsIdentity) -> Result<Self, FsIoError> {
            let uid = Uid::from_raw(id.uid);
            let gid = Gid::from_raw(id.gid);
            let previous = Previous {
                gid: setfsgid(gid),
                uid: setfsuid(uid),
            };
            let guard = Self {
                previous: Some(previous),
            };
            let applied = (
                setfsuid(Uid::from_raw(PROBE)),
                setfsgid(Gid::from_raw(PROBE)),
            );
            if applied == (uid, gid) {
                Ok(guard)
            } else {
                Err(FsIoError::PermissionDenied)
            }
        }
    }

    impl Drop for FsIdentityGuard {
        fn drop(&mut self) {
            if let Some(previous) = self.previous.take() {
                setfsuid(previous.uid);
                setfsgid(previous.gid);
            }
        }
    }
}

#[cfg(not(any(target_os = "linux", target_os = "android")))]
enum Previous {}

#[cfg(not(any(target_os = "linux", target_os = "android")))]
impl FsIdentityGuard {
    fn switch_to(_id: &FsIdentity) -> Result<Self, FsIoError> {
        Err(FsIoError::Unsupported)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn identity(uid: u32) -> FsIdentity {
        FsIdentity {
            uid,
            gid: uid,
            home: "/tmp".to_owned(),
        }
    }

    #[test]
    fn keep_current_never_switches() {
        let guard = FsIdentityGuard::enter(IdentitySwitch::KeepCurrent, &identity(1000)).unwrap();
        assert!(!guard.is_switched());
    }

    #[cfg(any(target_os = "linux", target_os = "android"))]
    #[test]
    fn enforce_switches_the_calling_thread_and_restores_on_drop() {
        use nix::unistd::{Uid, geteuid, setfsuid};
        if !geteuid().is_root() {
            let refused = FsIdentityGuard::enter(IdentitySwitch::Enforce, &identity(65_534));
            assert!(matches!(refused, Err(FsIoError::PermissionDenied)));
            return;
        }
        let probe = || setfsuid(Uid::from_raw(u32::MAX)).as_raw();
        let (switched_tx, switched_rx) = std::sync::mpsc::channel::<()>();
        let (probe_tx, probe_rx) = std::sync::mpsc::channel::<u32>();
        let bystander = std::thread::spawn(move || {
            switched_rx.recv().unwrap();
            probe_tx
                .send(setfsuid(Uid::from_raw(u32::MAX)).as_raw())
                .unwrap();
        });
        let guard = FsIdentityGuard::enter(IdentitySwitch::Enforce, &identity(1000)).unwrap();
        assert!(guard.is_switched());
        assert_eq!(probe(), 1000);
        switched_tx.send(()).unwrap();
        assert_eq!(
            probe_rx.recv().unwrap(),
            0,
            "a thread that already existed keeps its own identity"
        );
        bystander.join().unwrap();
        drop(guard);
        assert_eq!(probe(), 0);
    }
}
