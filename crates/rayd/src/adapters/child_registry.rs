//! Which pids `rayd` spawned directly (M15 foundations, Q80 finding).
//! `process_spawner`, `pty_backend` and `sidecar_process` each register a
//! pid right after `fork`/`exec` and unregister it once their own
//! `Child::wait` has consumed its exit status. `adapters::orphan_reaper`
//! consults this registry so it never calls `waitpid` on a pid tokio is
//! still waiting on itself — doing so would steal the exit status out from
//! under that `wait()` and make it hang forever.

use std::collections::HashSet;
use std::sync::Mutex;

#[derive(Debug, Default)]
pub struct ChildRegistry {
    pids: Mutex<HashSet<i32>>,
}

impl ChildRegistry {
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    pub fn register(&self, pid: i32) {
        self.lock().insert(pid);
    }

    pub fn unregister(&self, pid: i32) {
        self.lock().remove(&pid);
    }

    #[must_use]
    pub fn is_owned(&self, pid: i32) -> bool {
        self.lock().contains(&pid)
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, HashSet<i32>> {
        self.pids
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_registered_pid_is_owned_until_unregistered() {
        let registry = ChildRegistry::new();
        assert!(!registry.is_owned(42));
        registry.register(42);
        assert!(registry.is_owned(42));
        registry.unregister(42);
        assert!(!registry.is_owned(42));
    }

    #[test]
    fn unregistering_an_unknown_pid_is_a_no_op() {
        let registry = ChildRegistry::new();
        registry.unregister(99);
        assert!(!registry.is_owned(99));
    }

    #[test]
    fn registering_the_same_pid_twice_is_idempotent() {
        let registry = ChildRegistry::new();
        registry.register(7);
        registry.register(7);
        assert!(registry.is_owned(7));
        registry.unregister(7);
        assert!(!registry.is_owned(7));
    }
}
