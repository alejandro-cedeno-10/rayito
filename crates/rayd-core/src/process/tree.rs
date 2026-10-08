//! The kill scope of a process started with `StartRequest.kill_tree`
//! (`agent-timeout-kill-tree`): its timeout and its `SIGKILL` reach every
//! descendant, also the ones that left its process group with `setsid` or
//! a double fork, which a `killpg` alone never touches.
//!
//! The spawner makes such a process a child subreaper
//! (`prctl(PR_SET_CHILD_SUBREAPER)`, kept across `execve`), so a
//! descendant whose parent exits re-parents to it instead of to PID 1 and
//! the whole tree stays reachable by walking `ppid` links from its root
//! while the root lives. [`ProcessTree`] remembers every member it has
//! signalled, so a `SIGTERM` that ends the root first still leaves the
//! members it found reachable for the `SIGKILL` that follows the grace.
//!
//! A member is always named by its pid *and* its kernel start time
//! (`orphans::ProcEntry`), and the adapter checks both again right before
//! signalling it, so a recycled pid is never signalled by mistake; nothing
//! outside the tree is ever signalled. Nothing here reaps: a killed member
//! re-parents to its subreaper or to PID 1, whose orphan reaper owns its
//! exit status (`rayd-orphan-reaper`).

use std::collections::HashSet;
use std::time::Duration;

use super::timeout::SIGKILL;
use crate::orphans::{ProcEntry, ProcessTable};

/// `SIGSTOP` (`signal(7)`, the generic numbering x86-64 and arm64 share).
/// A stopped process can neither fork nor exit by itself, so freezing a
/// tree before killing it leaves no member able to spawn one the walk
/// would miss.
pub const SIGSTOP: i32 = 19;

/// Bound on the freeze passes of [`ProcessTree::kill`]. Each pass stops
/// every member it sees; only a child forked between a pass's snapshot and
/// its parent's `SIGSTOP` shows up in the next one, so the passes needed
/// grow with how many generations fork *during* the walk, not with the
/// tree's size. A shell tool, its command and a double-forked daemon are
/// three generations; sixteen is headroom against a fork loop, which is
/// then killed as far as the last pass reached.
pub const MAX_FREEZE_PASSES: usize = 16;

/// How often a timed-out tree is checked during the `SIGTERM` grace: the
/// `SIGKILL` is skipped and the `EndEvent` sent as soon as nothing of the
/// tree is left, at most this late. Each check is one scan of `/proc`
/// (hundreds of entries in a sandbox), so ten a second stay negligible
/// next to the 5 s grace (`timeout::KILL_GRACE`).
pub const TREE_SETTLE_POLL: Duration = Duration::from_millis(100);

/// What a timeout or a `SIGKILL` of a process reaches.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum KillScope {
    /// Its process group (`killpg`): the default for every process.
    #[default]
    Group,
    /// Its group plus every descendant, wherever it moved
    /// (`StartRequest.kill_tree`).
    Tree,
}

impl KillScope {
    #[must_use]
    pub fn from_flag(kill_tree: bool) -> Self {
        if kill_tree { Self::Tree } else { Self::Group }
    }

    /// Whether the spawned process must be a child subreaper.
    #[must_use]
    pub fn needs_subreaper(self) -> bool {
        self == Self::Tree
    }
}

/// The process table plus the one call that signals a single member.
pub trait MemberSignaller: ProcessTable {
    /// Delivers `signal` to `member` only if its pid still names the same,
    /// non-zombie process (same start time); `true` when it was delivered.
    fn signal_member(&self, member: &ProcEntry, signal: i32) -> bool;
}

/// Whether `entry` is `member` itself: same pid and same start time.
fn same_process(entry: &ProcEntry, member: &ProcEntry) -> bool {
    entry.pid == member.pid && entry.start_ticks == member.start_ticks
}

/// The live (non-zombie) processes of `table` that descend from any of
/// `roots`, parents before children, roots excluded. A root expands only
/// if `table` still holds it as itself, so the children of a recycled pid
/// are never taken for its descendants.
#[must_use]
pub fn descendants(table: &[ProcEntry], roots: &[ProcEntry]) -> Vec<ProcEntry> {
    let mut seen: HashSet<i32> = roots.iter().map(|root| root.pid).collect();
    let mut frontier: Vec<i32> = roots
        .iter()
        .filter(|root| table.iter().any(|entry| same_process(entry, root)))
        .map(|root| root.pid)
        .collect();
    let mut found = Vec::new();
    while !frontier.is_empty() {
        let children: Vec<ProcEntry> = table
            .iter()
            .filter(|entry| !entry.zombie && frontier.contains(&entry.ppid))
            .filter(|entry| seen.insert(entry.pid))
            .copied()
            .collect();
        frontier = children.iter().map(|child| child.pid).collect();
        found.extend(children);
    }
    found
}

/// One process started with [`KillScope::Tree`] and the members of its
/// tree signalled so far.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProcessTree {
    root: ProcEntry,
    members: Vec<ProcEntry>,
}

impl ProcessTree {
    /// Over `root`, the spawned process as the table showed it right after
    /// `fork`.
    #[must_use]
    pub fn new(root: ProcEntry) -> Self {
        Self {
            root,
            members: Vec::new(),
        }
    }

    #[must_use]
    pub fn root(&self) -> ProcEntry {
        self.root
    }

    /// Delivers `signal` to every member reachable now (the known ones
    /// still alive and every descendant of the root and of them), and
    /// remembers them. The root itself is left to the caller's `killpg`.
    /// Returns how many took it.
    pub fn signal(&mut self, port: &dyn MemberSignaller, signal: i32) -> usize {
        let found = self.reachable(&port.snapshot(), &[]);
        let delivered = found
            .iter()
            .filter(|member| port.signal_member(member, signal))
            .count();
        self.remember(found);
        delivered
    }

    /// Freezes the root and, pass after pass, every descendant it finds
    /// (`SIGSTOP`), then kills every frozen descendant (`SIGKILL`). The
    /// root stays stopped for the caller's `killpg`, which reports whether
    /// the process was there. Returns how many descendants were killed.
    pub fn kill(&mut self, port: &dyn MemberSignaller) -> usize {
        port.signal_member(&self.root, SIGSTOP);
        let mut frozen: Vec<ProcEntry> = Vec::new();
        for _ in 0..MAX_FREEZE_PASSES {
            let fresh: Vec<ProcEntry> = self
                .reachable(&port.snapshot(), &frozen)
                .into_iter()
                .filter(|entry| !frozen.iter().any(|member| same_process(entry, member)))
                .collect();
            if fresh.is_empty() {
                break;
            }
            for member in &fresh {
                port.signal_member(member, SIGSTOP);
            }
            frozen.extend(fresh);
        }
        let killed = frozen
            .iter()
            .filter(|member| port.signal_member(member, SIGKILL))
            .count();
        self.remember(frozen);
        killed
    }

    /// Whether neither the root nor any remembered member is still alive
    /// as itself: nothing of the tree is left to signal.
    #[must_use]
    pub fn is_gone(&self, port: &dyn ProcessTable) -> bool {
        let table = port.snapshot();
        !self.anchors().iter().any(|member| {
            table
                .iter()
                .any(|entry| !entry.zombie && same_process(entry, member))
        })
    }

    /// Every member reachable in `table`: the known ones (remembered, plus
    /// `also`) still alive as themselves, then every descendant of the root
    /// and of them, parents first; never the root. Walking from the known
    /// members too is what reaches those re-parented away from a root that
    /// already exited.
    fn reachable(&self, table: &[ProcEntry], also: &[ProcEntry]) -> Vec<ProcEntry> {
        let mut found: Vec<ProcEntry> = Vec::new();
        for member in self.members.iter().chain(also) {
            let alive = table
                .iter()
                .any(|entry| !entry.zombie && same_process(entry, member));
            if alive && !found.iter().any(|known| same_process(known, member)) {
                found.push(*member);
            }
        }
        let mut anchors = vec![self.root];
        anchors.extend(found.iter().copied());
        for entry in descendants(table, &anchors) {
            if !found.iter().any(|known| same_process(known, &entry)) {
                found.push(entry);
            }
        }
        found
    }

    /// The root and every member already known.
    fn anchors(&self) -> Vec<ProcEntry> {
        let mut anchors = vec![self.root];
        anchors.extend(self.members.iter().copied());
        anchors
    }

    fn remember(&mut self, found: Vec<ProcEntry>) {
        for member in found {
            if !self
                .members
                .iter()
                .any(|known| same_process(known, &member))
            {
                self.members.push(member);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Mutex;

    use super::*;
    use crate::process::timeout::SIGTERM;

    const INIT: i32 = 1;
    const ROOT: i32 = 100;
    const START: u64 = 1_000;

    fn process(pid: i32, parent: i32) -> ProcEntry {
        ProcEntry {
            pid,
            ppid: parent,
            zombie: false,
            start_ticks: START + u64::try_from(pid).unwrap(),
        }
    }

    /// A process table that obeys signals: `SIGKILL` turns a member into a
    /// zombie re-parented to `INIT`; every delivery is recorded.
    struct FakeTree {
        entries: Mutex<Vec<ProcEntry>>,
        signals: Mutex<Vec<(i32, i32)>>,
        /// Forked by `parent` the first time it is stopped, as if the
        /// fork raced the freeze: `(parent, child)`.
        fork_on_stop: Mutex<Option<(i32, i32)>>,
    }

    impl FakeTree {
        fn new(entries: Vec<ProcEntry>) -> Self {
            Self {
                entries: Mutex::new(entries),
                signals: Mutex::new(Vec::new()),
                fork_on_stop: Mutex::new(None),
            }
        }

        fn signalled(&self, signal: i32) -> Vec<i32> {
            self.signals
                .lock()
                .unwrap()
                .iter()
                .filter(|(_, sent)| *sent == signal)
                .map(|(pid, _)| *pid)
                .collect()
        }

        fn alive(&self, pid: i32) -> bool {
            self.entries
                .lock()
                .unwrap()
                .iter()
                .any(|entry| entry.pid == pid && !entry.zombie)
        }

        fn exit(&self, pid: i32) {
            self.entries
                .lock()
                .unwrap()
                .retain(|entry| entry.pid != pid);
        }

        fn reparent_children_of(&self, pid: i32, to: i32) {
            for entry in self.entries.lock().unwrap().iter_mut() {
                if entry.ppid == pid {
                    entry.ppid = to;
                }
            }
        }
    }

    impl ProcessTable for FakeTree {
        fn snapshot(&self) -> Vec<ProcEntry> {
            self.entries.lock().unwrap().clone()
        }

        fn entry(&self, pid: i32) -> Option<ProcEntry> {
            self.snapshot().into_iter().find(|entry| entry.pid == pid)
        }

        fn reap(&self, _pid: i32) {}
    }

    impl MemberSignaller for FakeTree {
        fn signal_member(&self, member: &ProcEntry, signal: i32) -> bool {
            let mut entries = self.entries.lock().unwrap();
            let Some(entry) = entries
                .iter_mut()
                .find(|entry| same_process(entry, member) && !entry.zombie)
            else {
                return false;
            };
            if signal == SIGKILL {
                entry.zombie = true;
                entry.ppid = INIT;
            }
            drop(entries);
            self.signals.lock().unwrap().push((member.pid, signal));
            if signal == SIGSTOP {
                let fork = self
                    .fork_on_stop
                    .lock()
                    .unwrap()
                    .take_if(|(parent, _)| *parent == member.pid);
                if let Some((parent, child)) = fork {
                    self.entries.lock().unwrap().push(process(child, parent));
                }
            }
            true
        }
    }

    /// The root (100) with a child in its group (101), a tool that started
    /// its own session (102) whose daemon was double-forked and adopted by
    /// the root (103), and an unrelated process (200) under init.
    fn agent_tree() -> FakeTree {
        FakeTree::new(vec![
            process(INIT, 0),
            process(ROOT, INIT),
            process(101, ROOT),
            process(102, ROOT),
            process(103, ROOT),
            process(104, 103),
            process(200, INIT),
        ])
    }

    #[test]
    fn descendants_walk_parents_first_and_skip_the_roots() {
        let tree = agent_tree();
        let found: Vec<i32> = descendants(&tree.snapshot(), &[process(ROOT, INIT)])
            .iter()
            .map(|entry| entry.pid)
            .collect();
        assert_eq!(found, vec![101, 102, 103, 104]);
    }

    #[test]
    fn a_recycled_root_pid_has_no_descendants() {
        let tree = agent_tree();
        let stale = ProcEntry {
            start_ticks: 1,
            ..process(ROOT, INIT)
        };
        assert!(descendants(&tree.snapshot(), &[stale]).is_empty());
    }

    #[test]
    fn zombies_are_not_members() {
        let mut entries = agent_tree().snapshot();
        entries.push(ProcEntry {
            zombie: true,
            ..process(105, ROOT)
        });
        let found = descendants(&entries, &[process(ROOT, INIT)]);
        assert!(found.iter().all(|entry| entry.pid != 105));
    }

    #[test]
    fn kill_freezes_the_root_then_kills_every_descendant_and_nothing_else() {
        let port = agent_tree();
        let mut tree = ProcessTree::new(process(ROOT, INIT));
        assert_eq!(tree.kill(&port), 4);
        assert_eq!(port.signalled(SIGSTOP), vec![ROOT, 101, 102, 103, 104]);
        assert_eq!(port.signalled(SIGKILL), vec![101, 102, 103, 104]);
        assert!(port.alive(ROOT), "the root is left for the caller's killpg");
        assert!(port.alive(200));
        assert!(port.alive(INIT));
    }

    #[test]
    fn a_child_forked_during_the_freeze_is_caught_by_the_next_pass() {
        let port = agent_tree();
        *port.fork_on_stop.lock().unwrap() = Some((104, 106));
        let mut tree = ProcessTree::new(process(ROOT, INIT));
        assert_eq!(tree.kill(&port), 5);
        assert!(!port.alive(106));
    }

    #[test]
    fn term_reaches_the_tree_and_kill_still_finds_members_after_the_root_exits() {
        let port = agent_tree();
        let mut tree = ProcessTree::new(process(ROOT, INIT));
        assert_eq!(tree.signal(&port, SIGTERM), 4);
        assert_eq!(port.signalled(SIGTERM), vec![101, 102, 103, 104]);
        // The root exits on SIGTERM and its children re-parent to init, out
        // of reach of a walk from the root.
        port.exit(ROOT);
        port.reparent_children_of(ROOT, INIT);
        assert!(!tree.is_gone(&port));
        assert_eq!(tree.kill(&port), 4);
        assert!(!port.alive(103) && !port.alive(104));
        assert!(port.alive(200));
        assert!(tree.is_gone(&port));
    }

    #[test]
    fn a_member_whose_pid_was_recycled_is_never_signalled() {
        let port = agent_tree();
        let mut tree = ProcessTree::new(process(ROOT, INIT));
        tree.signal(&port, SIGTERM);
        port.exit(ROOT);
        port.exit(102);
        port.entries.lock().unwrap().push(ProcEntry {
            start_ticks: 9,
            ..process(102, INIT)
        });
        tree.kill(&port);
        assert!(port.alive(102), "the new owner of pid 102 is not a member");
    }

    #[test]
    fn the_scope_flag_maps_to_tree_and_only_tree_needs_a_subreaper() {
        assert_eq!(KillScope::from_flag(false), KillScope::Group);
        assert_eq!(KillScope::from_flag(true), KillScope::Tree);
        assert!(!KillScope::Group.needs_subreaper());
        assert!(KillScope::Tree.needs_subreaper());
        assert_eq!(KillScope::default(), KillScope::Group);
    }
}
