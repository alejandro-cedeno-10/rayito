//! `rayd_core::orphans::ProcessTable` over `/proc` and `waitpid(2)`
//! (`rayd-orphan-reaper`): the only place `rayd` reaps a pid it did not
//! get from a `Child` handle, and always one exact pid at a time — never
//! `waitpid(-1, _)`, which could take an exit status tokio is waiting on.

use std::fs;

use rayd_core::orphans::{ProcEntry, ProcessTable};

/// Where the kernel publishes the process table.
const PROC_ROOT: &str = "/proc";
/// `proc(5)` field 3, the first after `(comm)`.
const STATE_FIELD: usize = 3;
/// `proc(5)` field 4.
const PPID_FIELD: usize = 4;
/// `proc(5)` field 22: start time in clock ticks after boot.
const START_TIME_FIELD: usize = 22;
/// `proc(5)` state letter of a zombie.
const ZOMBIE_STATE: &str = "Z";

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
        let stat = fs::read_to_string(format!("{PROC_ROOT}/{pid}/stat")).ok()?;
        parse_stat(pid, &stat)
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

/// `/proc/[pid]/stat`: `pid (comm) state ppid ...`. `comm` may itself
/// contain spaces or parentheses, so the split is on the *last* `)`.
fn parse_stat(pid: i32, text: &str) -> Option<ProcEntry> {
    let after_comm: Vec<&str> = text.rsplit_once(')')?.1.split_whitespace().collect();
    let field = |number: usize| after_comm.get(number - STATE_FIELD).copied();
    Some(ProcEntry {
        pid,
        zombie: field(STATE_FIELD)? == ZOMBIE_STATE,
        ppid: field(PPID_FIELD)?.parse().ok()?,
        start_ticks: field(START_TIME_FIELD)?.parse().ok()?,
    })
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
