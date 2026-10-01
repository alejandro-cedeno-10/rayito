//! Adapter side of PID-1 zombie reaping (M15 foundations, Q80 finding):
//! reads `/proc/[pid]/stat` for zombies re-parented to `rayd`'s own pid and
//! calls `waitpid(pid, WNOHANG)` on exactly the ones
//! `rayd_core::orphans::orphans_to_reap` names — never `waitpid(-1, _)`,
//! which could steal the exit status tokio's own `Child::wait` is waiting
//! on for a pid `rayd` spawned directly (`ChildRegistry`).
//!
//! Only meaningful when `rayd` actually is PID 1 (the normal case inside the
//! `MicroVM`; integration tests and a developer's shell are not), so
//! `reap_expired` is a no-op everywhere else: wired into `main`'s existing
//! 5 s `Reaper` tick (`lifecycle::spawn_reaper`) unconditionally, it costs
//! one `/proc` scan and never reaps a pid outside a real init process.

use std::fs;
use std::sync::Arc;

use rayd_core::orphans::{ZombieEntry, orphans_to_reap};

use crate::lifecycle::Reaper;

use super::child_registry::ChildRegistry;

pub struct OrphanReaper {
    registry: Arc<ChildRegistry>,
    own_pid: i32,
}

impl OrphanReaper {
    #[must_use]
    pub fn new(registry: Arc<ChildRegistry>) -> Self {
        Self {
            registry,
            own_pid: own_pid(),
        }
    }
}

impl Reaper for OrphanReaper {
    fn reap_expired(&self) {
        if self.own_pid != 1 {
            return;
        }
        let zombies = read_zombies();
        let registry = &self.registry;
        for pid in orphans_to_reap(&zombies, self.own_pid, |pid| registry.is_owned(pid)) {
            reap(pid);
        }
    }
}

#[cfg(unix)]
fn own_pid() -> i32 {
    // A real pid always fits i32 (Linux caps pid_max well under 2^31); a
    // value that somehow didn't would just never equal 1, i.e. never match
    // the PID-1 guard, which is the safe direction to fail in.
    i32::try_from(std::process::id()).unwrap_or(-1)
}

#[cfg(not(unix))]
fn own_pid() -> i32 {
    -1 // never 1: this adapter only ever runs on the Linux guest.
}

fn read_zombies() -> Vec<ZombieEntry> {
    let Ok(entries) = fs::read_dir("/proc") else {
        return Vec::new();
    };
    entries
        .filter_map(Result::ok)
        .filter_map(|entry| {
            let pid: i32 = entry.file_name().to_str()?.parse().ok()?;
            let stat = fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
            parse_stat(pid, &stat)
        })
        .collect()
}

/// `/proc/[pid]/stat` (`proc(5)`): `pid (comm) state ppid ...`. `comm` may
/// itself contain spaces or parentheses, so the split is on the *last*
/// `)`, never the first.
fn parse_stat(pid: i32, text: &str) -> Option<ZombieEntry> {
    let after_comm = text.rsplit_once(')')?.1;
    let mut fields = after_comm.split_whitespace();
    if fields.next()? != "Z" {
        return None;
    }
    let parent_pid: i32 = fields.next()?.parse().ok()?;
    Some(ZombieEntry {
        pid,
        ppid: parent_pid,
    })
}

#[cfg(unix)]
fn reap(pid: i32) {
    use nix::sys::wait::{WaitPidFlag, waitpid};
    use nix::unistd::Pid;
    // WNOHANG: a zombie is already dead, so this never blocks; the result
    // is intentionally discarded — ECHILD (already reaped by someone else
    // this tick) and a real status are both "done" for this pid.
    let _ = waitpid(Pid::from_raw(pid), Some(WaitPidFlag::WNOHANG));
}

#[cfg(not(unix))]
fn reap(_pid: i32) {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_zombie_line_parses_to_its_pid_and_ppid() {
        let stat = "50 (sleep) Z 1 50 50 0 -1 1077944576 0 0 0 0 0 0 0 0 20 0 1 0";
        assert_eq!(parse_stat(50, stat), Some(ZombieEntry { pid: 50, ppid: 1 }));
    }

    #[test]
    fn a_running_process_is_not_a_zombie() {
        let stat = "50 (sleep) S 1 50 50 0 -1 1077944576 0 0 0 0 0 0 0 0 20 0 1 0";
        assert_eq!(parse_stat(50, stat), None);
    }

    #[test]
    fn a_comm_containing_spaces_and_parens_does_not_confuse_the_split() {
        let stat = "50 (my (weird) prog) Z 7 50 50 0 -1 1077944576 0 0 0 0 0 0 0 0 20 0 1 0";
        assert_eq!(parse_stat(50, stat), Some(ZombieEntry { pid: 50, ppid: 7 }));
    }

    #[test]
    fn a_malformed_line_is_skipped_rather_than_panicking() {
        assert_eq!(parse_stat(50, "garbage"), None);
        assert_eq!(parse_stat(50, "50 (sleep) Z"), None);
        assert_eq!(parse_stat(50, "50 (sleep) Z notanumber"), None);
    }

    #[test]
    fn reap_expired_does_nothing_when_rayd_is_not_pid_1() {
        // Every test runner's own pid is never 1; this just proves the
        // guard short-circuits before touching /proc at all.
        let reaper = OrphanReaper {
            registry: Arc::new(ChildRegistry::new()),
            own_pid: 4321,
        };
        reaper.reap_expired();
    }
}
