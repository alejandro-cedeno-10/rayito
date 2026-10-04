//! `rayd_core::orphans::ProcessTable` over `/proc` and `waitpid(2)`
//! (`rayd-orphan-reaper`): the only place `rayd` reaps a pid it did not
//! get from a `Child` handle, and always one exact pid at a time — never
//! `waitpid(-1, _)`, which could take an exit status tokio is waiting on.
//!
//! The same table backs `rayd_core::code::KernelProcesses`: the kernel
//! pids the sidecar reports are checked against it before they are
//! recorded and again before their group is signalled, the only other
//! place `rayd` acts on a pid it did not spawn.

use std::fs;

use rayd_core::code::{KernelPidRejection, KernelProcess, KernelProcesses, ProcessFacts};
use rayd_core::orphans::{ProcEntry, ProcessTable};

use super::signal_process_group;

/// Where the kernel publishes the process table.
const PROC_ROOT: &str = "/proc";
/// `proc(5)` field 3, the first after `(comm)`.
const STATE_FIELD: usize = 3;
/// `proc(5)` field 4.
const PPID_FIELD: usize = 4;
/// `proc(5)` field 5: the process group.
const PGRP_FIELD: usize = 5;
/// `proc(5)` field 22: start time in clock ticks after boot.
const START_TIME_FIELD: usize = 22;
/// `proc(5)` state letter of a zombie.
const ZOMBIE_STATE: &str = "Z";
/// The `status` line with the real, effective, saved and filesystem uids,
/// in that order (`proc(5)`).
const STATUS_UID_PREFIX: &str = "Uid:";

#[derive(Debug, Default, Clone, Copy)]
pub struct ProcfsProcessTable;

impl ProcessTable for ProcfsProcessTable {
    fn snapshot(&self) -> Vec<ProcEntry> {
        let Ok(entries) = fs::read_dir(PROC_ROOT) else {
            return Vec::new();
        };
        entries
            .filter_map(Result::ok)
            .filter_map(|entry| entry.file_name().to_str()?.parse().ok())
            .filter_map(|pid| self.entry(pid))
            .collect()
    }

    fn entry(&self, pid: i32) -> Option<ProcEntry> {
        parse_stat(pid, &read_stat(pid)?)
    }

    #[cfg(unix)]
    fn reap(&self, pid: i32) {
        use nix::sys::wait::{WaitPidFlag, waitpid};
        use nix::unistd::Pid;
        // WNOHANG: a zombie is already dead, so this never blocks. The
        // result is intentionally discarded: `ECHILD` (not, or no longer,
        // our child) and a real status both mean "done" for this pid.
        let _ = waitpid(Pid::from_raw(pid), Some(WaitPidFlag::WNOHANG));
    }

    #[cfg(not(unix))]
    fn reap(&self, _pid: i32) {}
}

impl KernelProcesses for ProcfsProcessTable {
    fn admit(&self, pid: u32, sidecar_pid: u32) -> Result<KernelProcess, KernelPidRejection> {
        KernelProcess::admit(
            pid,
            sidecar_pid,
            process_facts(pid),
            process_facts(sidecar_pid),
        )
    }

    fn signal(&self, kernel: &KernelProcess, signal: i32) -> bool {
        if !kernel.may_signal(process_facts(kernel.pid())) {
            return false;
        }
        signal_process_group(kernel.pid(), signal);
        true
    }
}

fn read_stat(pid: impl std::fmt::Display) -> Option<String> {
    fs::read_to_string(format!("{PROC_ROOT}/{pid}/stat")).ok()
}

/// `stat` and `status` of one pid. `stat` is read again after `status`
/// and must name the same process (start time), so a pid recycled between
/// the two reads never mixes two processes' facts.
fn process_facts(pid: u32) -> Option<ProcessFacts> {
    let stat = read_stat(pid)?;
    let status = fs::read_to_string(format!("{PROC_ROOT}/{pid}/status")).ok()?;
    let facts = parse_facts(&stat, &status)?;
    let again = parse_facts(&read_stat(pid)?, &status)?;
    (again.start_ticks == facts.start_ticks).then_some(facts)
}

/// The fields after `(comm)` of a `/proc/[pid]/stat` line, the first being
/// field 3 (`STATE_FIELD`). `comm` may itself contain spaces or
/// parentheses, so the split is on the *last* `)`.
fn stat_fields(text: &str) -> Option<Vec<&str>> {
    Some(text.rsplit_once(')')?.1.split_whitespace().collect())
}

fn stat_field<'a>(fields: &[&'a str], number: usize) -> Option<&'a str> {
    fields.get(number.checked_sub(STATE_FIELD)?).copied()
}

/// `/proc/[pid]/stat`: `pid (comm) state ppid ...`.
fn parse_stat(pid: i32, text: &str) -> Option<ProcEntry> {
    let fields = stat_fields(text)?;
    Some(ProcEntry {
        pid,
        zombie: stat_field(&fields, STATE_FIELD)? == ZOMBIE_STATE,
        ppid: stat_field(&fields, PPID_FIELD)?.parse().ok()?,
        start_ticks: stat_field(&fields, START_TIME_FIELD)?.parse().ok()?,
    })
}

fn parse_facts(stat: &str, status: &str) -> Option<ProcessFacts> {
    let fields = stat_fields(stat)?;
    Some(ProcessFacts {
        ppid: stat_field(&fields, PPID_FIELD)?.parse().ok()?,
        pgrp: stat_field(&fields, PGRP_FIELD)?.parse().ok()?,
        start_ticks: stat_field(&fields, START_TIME_FIELD)?.parse().ok()?,
        uid: real_uid(status)?,
    })
}

/// The first number of the `Uid:` line.
fn real_uid(status: &str) -> Option<u32> {
    status
        .lines()
        .find_map(|line| line.strip_prefix(STATUS_UID_PREFIX))?
        .split_whitespace()
        .next()?
        .parse()
        .ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    const ZOMBIE_LINE: &str = "50 (sleep) Z 1 50 50 0 -1 4227084 0 0 0 0 0 0 0 0 20 0 1 0 7731 0 0";
    const RUNNING_LINE: &str =
        "50 (sleep) S 1 50 50 0 -1 4227084 0 0 0 0 0 0 0 0 20 0 1 0 7731 2 3";

    #[test]
    fn a_zombie_line_parses_to_its_pid_ppid_and_start_time() {
        assert_eq!(
            parse_stat(50, ZOMBIE_LINE),
            Some(ProcEntry {
                pid: 50,
                ppid: 1,
                zombie: true,
                start_ticks: 7731,
            })
        );
    }

    #[test]
    fn a_running_process_is_not_a_zombie() {
        let entry = parse_stat(50, RUNNING_LINE).unwrap();
        assert!(!entry.zombie);
        assert_eq!(entry.start_ticks, 7731);
    }

    #[test]
    fn a_comm_containing_spaces_and_parens_does_not_confuse_the_split() {
        let stat = "50 (my (weird) prog) Z 7 50 50 0 -1 4227084 0 0 0 0 0 0 0 0 20 0 1 0 99";
        let entry = parse_stat(50, stat).unwrap();
        assert_eq!((entry.ppid, entry.zombie, entry.start_ticks), (7, true, 99));
    }

    #[test]
    fn a_malformed_line_is_skipped_rather_than_panicking() {
        assert_eq!(parse_stat(50, "garbage"), None);
        assert_eq!(parse_stat(50, "50 (sleep) Z"), None);
        assert_eq!(parse_stat(50, "50 (sleep) Z notanumber"), None);
        assert_eq!(parse_stat(50, "50 (sleep) Z 1 50 50"), None);
    }

    const STATUS: &str = "Name:\tsleep\nUmask:\t0022\nState:\tS (sleeping)\nTgid:\t50\nPid:\t50\nPPid:\t40\nUid:\t1000\t1001\t1002\t1003\nGid:\t1000\t1000\t1000\t1000\n";

    #[test]
    fn facts_take_the_parent_group_start_time_and_real_uid() {
        let stat = "50 (sle ep)) S 40 50 50 0 -1 4227084 0 0 0 0 0 0 0 0 20 0 1 0 7731 2 3";
        assert_eq!(
            parse_facts(stat, STATUS),
            Some(ProcessFacts {
                ppid: 40,
                pgrp: 50,
                start_ticks: 7731,
                uid: 1000,
            })
        );
    }

    #[test]
    fn facts_without_a_uid_line_or_a_full_stat_are_none() {
        assert_eq!(parse_facts(RUNNING_LINE, "Name:\tsleep\n"), None);
        assert_eq!(parse_facts("50 (sleep) S 40", STATUS), None);
    }

    /// A real `sleep` in its own process group, child of this test
    /// process, standing in for a kernel the sidecar (this process)
    /// started; killed and reaped on drop.
    #[cfg(target_os = "linux")]
    struct LiveKernel(std::process::Child);

    #[cfg(target_os = "linux")]
    impl LiveKernel {
        fn start() -> Self {
            use std::os::unix::process::CommandExt;
            let mut command = std::process::Command::new("sleep");
            command.arg("30").process_group(0);
            Self(
                crate::adapters::ChildRegistry::process()
                    .spawn(&mut command)
                    .unwrap(),
            )
        }

        fn pid(&self) -> u32 {
            self.0.id()
        }
    }

    #[cfg(target_os = "linux")]
    impl Drop for LiveKernel {
        fn drop(&mut self) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn a_live_group_leader_child_is_admitted_and_its_group_signalled() {
        let mut kernel = LiveKernel::start();
        let table = ProcfsProcessTable;
        let admitted = table.admit(kernel.pid(), std::process::id()).unwrap();
        assert_eq!(admitted.pid(), kernel.pid());
        assert!(table.signal(&admitted, nix::sys::signal::Signal::SIGKILL as i32));
        let status = kernel.0.wait().unwrap();
        assert!(!status.success());
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn forged_pids_are_rejected_by_the_live_table() {
        let kernel = LiveKernel::start();
        let table = ProcfsProcessTable;
        let own = std::process::id();
        assert_eq!(table.admit(0, own), Err(KernelPidRejection::Reserved));
        assert_eq!(table.admit(1, own), Err(KernelPidRejection::Reserved));
        assert_eq!(
            table.admit(own, own),
            Err(KernelPidRejection::NotSidecarChild),
            "this process is not its own child"
        );
        assert_eq!(
            table.admit(kernel.pid(), kernel.pid()),
            Err(KernelPidRejection::NotSidecarChild),
            "a real kernel reported by another sidecar"
        );
        assert_eq!(
            table.admit(u32::MAX - 1, own),
            Err(KernelPidRejection::Gone)
        );
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn the_live_table_contains_this_process_as_itself() {
        let own_pid = i32::try_from(std::process::id()).unwrap();
        let table = ProcfsProcessTable;
        let own = table.entry(own_pid).unwrap();
        assert!(!own.zombie);
        assert!(table.snapshot().contains(&own));
    }
}
