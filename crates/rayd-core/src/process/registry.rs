//! The registry of live and recently ended processes: the live cap, the
//! per-pid ring and subscribers (charged against the sandbox-wide output
//! budget), the 30 s retention of terminal events (and the cap on how many
//! ended entries coexist, plus the early drop above the budget's high-water
//! mark), and the atomic "replay then subscribe" that makes
//! `Connect(from_seq)` gap-free.
//!
//! `S` is the subscriber sink the adapter fans events into; the registry
//! only stores, counts and asks it whether it is still open, so the domain
//! stays free of runtime types.

use std::collections::BTreeMap;
use std::time::Duration;

use bytes::Bytes;

use super::Pid;
use super::budget::OutputBudget;
use super::error::ProcessError;
use super::events::{OutputEvent, OutputStream, ProcessEnd};
use super::limits::StdinMode;
use super::ports::SubscriberSlot;
use super::ring::{DEFAULT_RING_CAPACITY_BYTES, OutputRing};
use super::spec::ProcessConfigInfo;

pub const DEFAULT_MAX_LIVE: usize = 256;
pub const DEFAULT_MAX_SUBSCRIBERS_PER_PID: usize = 8;
pub const DEFAULT_RETENTION: Duration = Duration::from_secs(30);
/// Ended entries keep their full ring until retention expires; with the
/// 1 MiB ring this bounds retained output at 256 MiB whatever the spawn rate.
pub const DEFAULT_MAX_RETAINED: usize = 256;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RegistryLimits {
    pub max_live: usize,
    pub max_subscribers_per_pid: usize,
    pub retention: Duration,
    pub max_retained: usize,
}

impl Default for RegistryLimits {
    fn default() -> Self {
        Self {
            max_live: DEFAULT_MAX_LIVE,
            max_subscribers_per_pid: DEFAULT_MAX_SUBSCRIBERS_PER_PID,
            retention: DEFAULT_RETENTION,
            max_retained: DEFAULT_MAX_RETAINED,
        }
    }
}

/// PTYs share the registry (and the live cap) from M5 on.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProcessKind {
    Process,
    Pty,
}

impl ProcessKind {
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Process => "process",
            Self::Pty => "pty",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct SubscriberId(u64);

/// One `List` row.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProcessSummary {
    pub pid: Pid,
    pub kind: ProcessKind,
    pub config: ProcessConfigInfo,
    pub tag: Option<String>,
}

/// What a `Connect` (or the `Start` stream itself) receives up front, and
/// whether it was registered for live events. An ended process never gets a
/// subscriber: everything it will ever say is in `replay` and `end`.
#[derive(Debug, PartialEq, Eq)]
pub struct Attachment {
    pub replay: Vec<OutputEvent>,
    pub end: Option<ProcessEnd>,
    pub subscriber: Option<SubscriberId>,
}

#[derive(Debug)]
struct Entry<S> {
    kind: ProcessKind,
    config: ProcessConfigInfo,
    tag: Option<String>,
    stdin_mode: StdinMode,
    ring: OutputRing,
    end: Option<ProcessEnd>,
    ended_at: Option<Duration>,
    subscribers: Vec<(SubscriberId, S)>,
}

impl<S> Entry<S> {
    fn is_live(&self) -> bool {
        self.end.is_none()
    }
}

#[derive(Debug)]
pub struct ProcessRegistry<S> {
    limits: RegistryLimits,
    budget: OutputBudget,
    entries: BTreeMap<Pid, Entry<S>>,
    next_subscriber: u64,
}

impl<S: Clone + SubscriberSlot> Default for ProcessRegistry<S> {
    fn default() -> Self {
        Self::new(RegistryLimits::default())
    }
}

impl<S: Clone + SubscriberSlot> ProcessRegistry<S> {
    /// A registry whose rings are bounded only by their own capacity.
    #[must_use]
    pub fn new(limits: RegistryLimits) -> Self {
        Self::with_budget(limits, OutputBudget::unlimited())
    }

    /// Every ring of this registry charges `budget`, shared with whatever
    /// other registries `main` hands the same budget to.
    #[must_use]
    pub fn with_budget(limits: RegistryLimits, budget: OutputBudget) -> Self {
        Self {
            limits,
            budget,
            entries: BTreeMap::new(),
            next_subscriber: 1,
        }
    }

    #[must_use]
    pub fn limits(&self) -> RegistryLimits {
        self.limits
    }

    #[must_use]
    pub fn budget(&self) -> &OutputBudget {
        &self.budget
    }

    /// Checked before spawning so that a refused `Start` never forks.
    pub fn ensure_capacity(&self) -> Result<(), ProcessError> {
        if self.live_count() >= self.limits.max_live {
            return Err(ProcessError::TooManyProcesses {
                max: self.limits.max_live,
            });
        }
        Ok(())
    }

    /// A retained entry with the same pid (kernel pid reuse within the
    /// retention window) is replaced: the new process owns the number now.
    pub fn register(
        &mut self,
        pid: Pid,
        kind: ProcessKind,
        config: ProcessConfigInfo,
        tag: Option<String>,
        stdin_mode: StdinMode,
    ) -> Result<(), ProcessError> {
        self.ensure_capacity()?;
        self.entries.insert(
            pid,
            Entry {
                kind,
                config,
                tag,
                stdin_mode,
                ring: OutputRing::with_budget(DEFAULT_RING_CAPACITY_BYTES, self.budget.clone()),
                end: None,
                ended_at: None,
                subscribers: Vec::new(),
            },
        );
        Ok(())
    }

    pub fn push_output(
        &mut self,
        pid: Pid,
        stream: OutputStream,
        bytes: Bytes,
    ) -> Result<OutputEvent, ProcessError> {
        let entry = self.entry_mut(pid)?;
        Ok(entry.ring.push(stream, bytes))
    }

    /// Snapshot of the sinks to fan an event out to, taken under the same
    /// lock as `push_output` so ordering per subscriber is by `seq`.
    #[must_use]
    pub fn subscribers(&self, pid: Pid) -> Vec<(SubscriberId, S)> {
        self.entries
            .get(&pid)
            .map(|entry| entry.subscribers.clone())
            .unwrap_or_default()
    }

    /// Replay from `from_seq` (0 = nothing) and, if the process is still
    /// live, subscribe `sink` for what comes next. Range and cap are checked
    /// before anything is stored, so a refused `Connect` leaves no trace.
    /// Slots whose client already went away are reclaimed first, so a silent
    /// process cannot hold dead subscribers against the cap.
    pub fn attach(&mut self, pid: Pid, from_seq: u64, sink: S) -> Result<Attachment, ProcessError> {
        let max = self.limits.max_subscribers_per_pid;
        let id = SubscriberId(self.next_subscriber);
        let entry = self.entry_mut(pid)?;
        let replay = if from_seq == 0 {
            Vec::new()
        } else {
            entry.ring.replay_from(from_seq)?
        };
        if !entry.is_live() {
            return Ok(Attachment {
                replay,
                end: entry.end.clone(),
                subscriber: None,
            });
        }
        entry.subscribers.retain(|(_, slot)| slot.is_open());
        if entry.subscribers.len() >= max {
            return Err(ProcessError::TooManySubscribers { pid, max });
        }
        entry.subscribers.push((id, sink));
        self.next_subscriber += 1;
        Ok(Attachment {
            replay,
            end: None,
            subscriber: Some(id),
        })
    }

    pub fn detach(&mut self, pid: Pid, subscriber: SubscriberId) {
        if let Some(entry) = self.entries.get_mut(&pid) {
            entry.subscribers.retain(|(id, _)| *id != subscriber);
        }
    }

    /// Records the terminal event and hands back the subscribers, now
    /// detached, so the adapter delivers the end exactly once to each. Once
    /// more than `max_retained` ended entries coexist, the oldest ended ones
    /// are dropped early (before their retention window closes).
    pub fn mark_ended(
        &mut self,
        pid: Pid,
        end: ProcessEnd,
        now: Duration,
    ) -> Result<Vec<(SubscriberId, S)>, ProcessError> {
        let entry = self.entry_mut(pid)?;
        entry.end = Some(end);
        entry.ended_at = Some(now);
        let subscribers = std::mem::take(&mut entry.subscribers);
        self.evict_excess_retained();
        Ok(subscribers)
    }

    fn evict_excess_retained(&mut self) {
        let excess = self
            .retained_count()
            .saturating_sub(self.limits.max_retained);
        for pid in self.ended_oldest_first().into_iter().take(excess) {
            self.entries.remove(&pid);
        }
    }

    fn ended_oldest_first(&self) -> Vec<Pid> {
        let mut retained: Vec<(Duration, Pid)> = self
            .entries
            .iter()
            .filter_map(|(pid, entry)| entry.ended_at.map(|ended_at| (ended_at, *pid)))
            .collect();
        retained.sort_unstable();
        retained.into_iter().map(|(_, pid)| pid).collect()
    }

    /// Retention is a courtesy, memory is a limit: while the shared budget
    /// sits above its high-water mark, ended entries go oldest-first
    /// regardless of age. Live entries are never touched. Returns the pids
    /// dropped.
    pub fn drop_ended_over_budget(&mut self) -> Vec<Pid> {
        let mut dropped = Vec::new();
        for pid in self.ended_oldest_first() {
            if !self.budget.above_high_water() {
                break;
            }
            self.entries.remove(&pid);
            dropped.push(pid);
        }
        dropped
    }

    /// Drops entries whose retention window closed; returns their pids.
    pub fn reap_expired(&mut self, now: Duration) -> Vec<Pid> {
        let retention = self.limits.retention;
        let expired: Vec<Pid> = self
            .entries
            .iter()
            .filter(|(_, entry)| {
                entry
                    .ended_at
                    .is_some_and(|ended_at| now.saturating_sub(ended_at) >= retention)
            })
            .map(|(pid, _)| *pid)
            .collect();
        for pid in &expired {
            self.entries.remove(pid);
        }
        expired
    }

    #[must_use]
    pub fn live(&self) -> Vec<ProcessSummary> {
        self.entries
            .iter()
            .filter(|(_, entry)| entry.is_live())
            .map(|(pid, entry)| ProcessSummary {
                pid: *pid,
                kind: entry.kind,
                config: entry.config.clone(),
                tag: entry.tag.clone(),
            })
            .collect()
    }

    #[must_use]
    pub fn live_count(&self) -> usize {
        self.entries
            .values()
            .filter(|entry| entry.is_live())
            .count()
    }

    /// Ended entries still answering `Connect`.
    #[must_use]
    pub fn retained_count(&self) -> usize {
        self.entries.len() - self.live_count()
    }

    #[must_use]
    pub fn is_live(&self, pid: Pid) -> bool {
        self.entries.get(&pid).is_some_and(Entry::is_live)
    }

    /// Which service owns the pid; `NotFound` for unknown and expired pids.
    pub fn kind(&self, pid: Pid) -> Result<ProcessKind, ProcessError> {
        self.entries
            .get(&pid)
            .map(|entry| entry.kind)
            .ok_or(ProcessError::NotFound { pid })
    }

    /// `Ok` only when the pid exists and belongs to `expected`; an ended
    /// entry keeps its kind while retained so a late `Connect` still gets
    /// the right answer.
    pub fn ensure_kind(&self, pid: Pid, expected: ProcessKind) -> Result<(), ProcessError> {
        let kind = self.kind(pid)?;
        if kind == expected {
            Ok(())
        } else {
            Err(ProcessError::WrongKind { pid, expected })
        }
    }

    #[must_use]
    pub fn contains(&self, pid: Pid) -> bool {
        self.entries.contains_key(&pid)
    }

    /// `NotFound` for unknown and for ended pids: input only reaches a live
    /// process.
    pub fn stdin_mode(&self, pid: Pid) -> Result<StdinMode, ProcessError> {
        match self.entries.get(&pid) {
            Some(entry) if entry.is_live() => Ok(entry.stdin_mode),
            _ => Err(ProcessError::NotFound { pid }),
        }
    }

    fn entry_mut(&mut self, pid: Pid) -> Result<&mut Entry<S>, ProcessError> {
        self.entries
            .get_mut(&pid)
            .ok_or(ProcessError::NotFound { pid })
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;
    use std::sync::atomic::{AtomicBool, Ordering};

    use super::*;

    impl SubscriberSlot for &str {
        fn is_open(&self) -> bool {
            true
        }
    }

    /// A sink whose client can "go away" between two registry calls.
    #[derive(Debug, Clone)]
    struct FakeSink {
        open: Arc<AtomicBool>,
    }

    impl FakeSink {
        fn open() -> Self {
            Self {
                open: Arc::new(AtomicBool::new(true)),
            }
        }

        fn close(&self) {
            self.open.store(false, Ordering::Relaxed);
        }
    }

    impl SubscriberSlot for FakeSink {
        fn is_open(&self) -> bool {
            self.open.load(Ordering::Relaxed)
        }
    }

    type Registry = ProcessRegistry<&'static str>;

    fn limits(max_live: usize) -> RegistryLimits {
        RegistryLimits {
            max_live,
            max_subscribers_per_pid: 2,
            retention: Duration::from_secs(30),
            max_retained: 256,
        }
    }

    fn register(registry: &mut Registry, pid: u32) -> Result<(), ProcessError> {
        register_in(registry, pid)
    }

    fn register_in<S: Clone + SubscriberSlot>(
        registry: &mut ProcessRegistry<S>,
        pid: u32,
    ) -> Result<(), ProcessError> {
        registry.register(
            Pid(pid),
            ProcessKind::Process,
            ProcessConfigInfo::default(),
            Some("tag".to_owned()),
            StdinMode::Pipe,
        )
    }

    fn push(registry: &mut Registry, pid: u32, text: &str) -> u64 {
        registry
            .push_output(
                Pid(pid),
                OutputStream::Stdout,
                Bytes::copy_from_slice(text.as_bytes()),
            )
            .unwrap()
            .seq
    }

    #[test]
    fn live_cap_refuses_before_registering() {
        let mut registry = Registry::new(limits(2));
        register(&mut registry, 1).unwrap();
        register(&mut registry, 2).unwrap();
        assert_eq!(
            registry.ensure_capacity(),
            Err(ProcessError::TooManyProcesses { max: 2 })
        );
        assert_eq!(
            register(&mut registry, 3),
            Err(ProcessError::TooManyProcesses { max: 2 })
        );
        assert_eq!(registry.live_count(), 2);
        registry
            .mark_ended(Pid(1), ProcessEnd::exited(0), Duration::ZERO)
            .unwrap();
        assert_eq!(register(&mut registry, 3), Ok(()));
    }

    #[test]
    fn default_limits_are_the_contract_values() {
        let registry = Registry::default();
        assert_eq!(registry.limits().max_live, 256);
        assert_eq!(registry.limits().max_subscribers_per_pid, 8);
        assert_eq!(registry.limits().retention, Duration::from_secs(30));
        assert_eq!(registry.limits().max_retained, 256);
    }

    #[test]
    fn closed_slots_are_reclaimed_before_the_cap_is_applied() {
        let mut registry: ProcessRegistry<FakeSink> = ProcessRegistry::new(limits(8));
        register_in(&mut registry, 1).unwrap();
        let first = FakeSink::open();
        let second = FakeSink::open();
        registry.attach(Pid(1), 0, first.clone()).unwrap();
        registry.attach(Pid(1), 0, second.clone()).unwrap();
        assert!(matches!(
            registry.attach(Pid(1), 0, FakeSink::open()),
            Err(ProcessError::TooManySubscribers { .. })
        ));
        first.close();
        let reclaimed = registry.attach(Pid(1), 0, FakeSink::open()).unwrap();
        assert!(reclaimed.subscriber.is_some());
        assert_eq!(registry.subscribers(Pid(1)).len(), 2);
        assert!(matches!(
            registry.attach(Pid(1), 0, FakeSink::open()),
            Err(ProcessError::TooManySubscribers { .. })
        ));
        second.close();
        registry.detach(Pid(1), reclaimed.subscriber.unwrap());
        assert!(registry.attach(Pid(1), 0, FakeSink::open()).is_ok());
        assert_eq!(registry.subscribers(Pid(1)).len(), 1);
    }

    #[test]
    fn retained_entries_are_capped_oldest_ended_first() {
        let mut registry = Registry::new(RegistryLimits {
            max_live: 1024,
            max_subscribers_per_pid: 2,
            retention: Duration::from_secs(30),
            max_retained: 256,
        });
        for pid in 1..=300 {
            register(&mut registry, pid).unwrap();
            push(&mut registry, pid, "output");
            registry
                .mark_ended(
                    Pid(pid),
                    ProcessEnd::exited(0),
                    Duration::from_millis(u64::from(pid)),
                )
                .unwrap();
        }
        assert_eq!(registry.retained_count(), 256);
        assert_eq!(registry.live_count(), 0);
        for evicted in 1..=44 {
            assert_eq!(
                registry.attach(Pid(evicted), 0, "x"),
                Err(ProcessError::NotFound { pid: Pid(evicted) }),
                "pid {evicted}"
            );
        }
        assert_eq!(
            registry.attach(Pid(45), 1, "x").unwrap().end,
            Some(ProcessEnd::exited(0))
        );
        assert!(registry.contains(Pid(300)));
    }

    #[test]
    fn retained_cap_never_touches_live_entries() {
        let mut registry = Registry::new(RegistryLimits {
            max_live: 8,
            max_subscribers_per_pid: 2,
            retention: Duration::from_secs(30),
            max_retained: 1,
        });
        register(&mut registry, 1).unwrap();
        register(&mut registry, 2).unwrap();
        register(&mut registry, 3).unwrap();
        registry
            .mark_ended(Pid(2), ProcessEnd::exited(0), Duration::from_secs(1))
            .unwrap();
        registry
            .mark_ended(Pid(3), ProcessEnd::exited(0), Duration::from_secs(2))
            .unwrap();
        assert!(registry.is_live(Pid(1)));
        assert!(!registry.contains(Pid(2)));
        assert!(registry.contains(Pid(3)));
        assert_eq!(registry.retained_count(), 1);
    }

    #[test]
    fn subscriber_cap_and_detach() {
        let mut registry = Registry::new(limits(8));
        register(&mut registry, 1).unwrap();
        let first = registry.attach(Pid(1), 0, "a").unwrap();
        registry.attach(Pid(1), 0, "b").unwrap();
        assert_eq!(
            registry.attach(Pid(1), 0, "c"),
            Err(ProcessError::TooManySubscribers {
                pid: Pid(1),
                max: 2
            })
        );
        registry.detach(Pid(1), first.subscriber.unwrap());
        assert!(registry.attach(Pid(1), 0, "c").is_ok());
        assert_eq!(registry.subscribers(Pid(1)).len(), 2);
    }

    #[test]
    fn attach_replays_from_seq_and_subscribes_atomically() {
        let mut registry = Registry::new(limits(8));
        register(&mut registry, 1).unwrap();
        push(&mut registry, 1, "a");
        push(&mut registry, 1, "b");
        let attachment = registry.attach(Pid(1), 1, "x").unwrap();
        assert_eq!(attachment.replay.len(), 2);
        assert!(attachment.subscriber.is_some());
        assert_eq!(attachment.end, None);
        let only_new = registry.attach(Pid(1), 0, "y").unwrap();
        assert!(only_new.replay.is_empty());
        assert_eq!(
            registry.attach(Pid(1), 999, "z"),
            Err(ProcessError::OutOfRange { oldest: 1, next: 3 })
        );
        assert_eq!(registry.subscribers(Pid(1)).len(), 2);
    }

    #[test]
    fn attach_after_end_replays_the_end_without_subscribing() {
        let mut registry = Registry::new(limits(8));
        register(&mut registry, 1).unwrap();
        push(&mut registry, 1, "a");
        registry.attach(Pid(1), 0, "live").unwrap();
        let drained = registry
            .mark_ended(Pid(1), ProcessEnd::exited(3), Duration::from_secs(1))
            .unwrap();
        assert_eq!(drained.len(), 1);
        assert!(registry.subscribers(Pid(1)).is_empty());
        let attachment = registry.attach(Pid(1), 1, "late").unwrap();
        assert_eq!(attachment.replay.len(), 1);
        assert_eq!(attachment.end, Some(ProcessEnd::exited(3)));
        assert_eq!(attachment.subscriber, None);
        assert!(!registry.is_live(Pid(1)));
        assert!(registry.contains(Pid(1)));
    }

    #[test]
    fn retention_expires_after_the_window() {
        let mut registry = Registry::new(limits(8));
        register(&mut registry, 1).unwrap();
        register(&mut registry, 2).unwrap();
        registry
            .mark_ended(Pid(1), ProcessEnd::exited(0), Duration::from_secs(10))
            .unwrap();
        assert_eq!(registry.reap_expired(Duration::from_secs(39)), vec![]);
        assert_eq!(registry.reap_expired(Duration::from_secs(40)), vec![Pid(1)]);
        assert!(!registry.contains(Pid(1)));
        assert!(registry.contains(Pid(2)));
        assert_eq!(
            registry.attach(Pid(1), 0, "x"),
            Err(ProcessError::NotFound { pid: Pid(1) })
        );
    }

    #[test]
    fn live_excludes_ended_and_carries_tag_and_kind() {
        let mut registry = Registry::new(limits(8));
        register(&mut registry, 1).unwrap();
        register(&mut registry, 2).unwrap();
        registry
            .mark_ended(Pid(2), ProcessEnd::signaled(9), Duration::ZERO)
            .unwrap();
        let live = registry.live();
        assert_eq!(live.len(), 1);
        assert_eq!(live[0].pid, Pid(1));
        assert_eq!(live[0].kind, ProcessKind::Process);
        assert_eq!(live[0].tag.as_deref(), Some("tag"));
    }

    #[test]
    fn stdin_mode_is_only_answered_for_live_pids() {
        let mut registry = Registry::new(limits(8));
        register(&mut registry, 1).unwrap();
        assert_eq!(registry.stdin_mode(Pid(1)), Ok(StdinMode::Pipe));
        assert_eq!(
            registry.stdin_mode(Pid(9)),
            Err(ProcessError::NotFound { pid: Pid(9) })
        );
        registry
            .mark_ended(Pid(1), ProcessEnd::exited(0), Duration::ZERO)
            .unwrap();
        assert_eq!(
            registry.stdin_mode(Pid(1)),
            Err(ProcessError::NotFound { pid: Pid(1) })
        );
    }

    #[test]
    fn kind_is_answered_for_live_and_retained_pids_only() {
        let mut registry = Registry::new(limits(8));
        register(&mut registry, 1).unwrap();
        registry
            .register(
                Pid(2),
                ProcessKind::Pty,
                ProcessConfigInfo::default(),
                None,
                StdinMode::Pipe,
            )
            .unwrap();
        assert_eq!(registry.kind(Pid(1)), Ok(ProcessKind::Process));
        assert_eq!(registry.kind(Pid(2)), Ok(ProcessKind::Pty));
        assert_eq!(
            registry.kind(Pid(3)),
            Err(ProcessError::NotFound { pid: Pid(3) })
        );
        assert_eq!(registry.ensure_kind(Pid(2), ProcessKind::Pty), Ok(()));
        assert_eq!(
            registry.ensure_kind(Pid(2), ProcessKind::Process),
            Err(ProcessError::WrongKind {
                pid: Pid(2),
                expected: ProcessKind::Process
            })
        );
        assert_eq!(
            registry.ensure_kind(Pid(1), ProcessKind::Pty),
            Err(ProcessError::WrongKind {
                pid: Pid(1),
                expected: ProcessKind::Pty
            })
        );
        registry
            .mark_ended(Pid(2), ProcessEnd::exited(0), Duration::ZERO)
            .unwrap();
        assert_eq!(registry.kind(Pid(2)), Ok(ProcessKind::Pty));
        assert_eq!(registry.reap_expired(Duration::from_secs(60)), vec![Pid(2)]);
        assert_eq!(
            registry.kind(Pid(2)),
            Err(ProcessError::NotFound { pid: Pid(2) })
        );
        assert_eq!(ProcessKind::Pty.as_str(), "pty");
    }

    #[test]
    fn pid_reuse_replaces_a_retained_entry() {
        let mut registry = Registry::new(limits(8));
        register(&mut registry, 1).unwrap();
        push(&mut registry, 1, "old");
        registry
            .mark_ended(Pid(1), ProcessEnd::exited(0), Duration::ZERO)
            .unwrap();
        register(&mut registry, 1).unwrap();
        assert!(registry.is_live(Pid(1)));
        assert_eq!(registry.attach(Pid(1), 1, "x").unwrap().replay, vec![]);
    }

    /// Two ended entries and one live ring fill a 64 KiB budget: the reaper
    /// drops the oldest ended entry although its retention window is open,
    /// stops as soon as the level is below the high-water mark, and never
    /// touches the live ring.
    #[test]
    fn early_reap_under_memory_pressure_drops_ended_entries_oldest_first() {
        let budget = OutputBudget::new(64 * 1024, 48 * 1024);
        let mut registry = Registry::with_budget(limits(8), budget.clone());
        let chunk = "x".repeat(20 * 1024);
        for pid in 1..=3 {
            register(&mut registry, pid).unwrap();
            push(&mut registry, pid, &chunk);
        }
        assert_eq!(budget.level(), 60 * 1024);
        registry
            .mark_ended(Pid(1), ProcessEnd::exited(0), Duration::from_secs(1))
            .unwrap();
        registry
            .mark_ended(Pid(2), ProcessEnd::exited(0), Duration::from_secs(2))
            .unwrap();
        assert!(budget.above_high_water());
        assert_eq!(registry.reap_expired(Duration::from_secs(5)), vec![]);
        assert_eq!(registry.drop_ended_over_budget(), vec![Pid(1)]);
        assert!(!budget.above_high_water());
        assert_eq!(budget.level(), 40 * 1024);
        assert!(registry.contains(Pid(2)), "below the mark the reaper stops");
        assert!(registry.is_live(Pid(3)));
        assert_eq!(registry.attach(Pid(3), 1, "x").unwrap().replay.len(), 1);
        assert!(registry.drop_ended_over_budget().is_empty());
    }

    #[test]
    fn dropped_and_reaped_entries_release_their_bytes() {
        let budget = OutputBudget::new(64 * 1024, 64 * 1024);
        let mut registry = Registry::with_budget(limits(8), budget.clone());
        register(&mut registry, 1).unwrap();
        push(&mut registry, 1, "abcd");
        registry
            .mark_ended(Pid(1), ProcessEnd::exited(0), Duration::ZERO)
            .unwrap();
        assert_eq!(budget.level(), 4);
        registry.reap_expired(Duration::from_secs(60));
        assert_eq!(budget.level(), 0);
        register(&mut registry, 2).unwrap();
        push(&mut registry, 2, "ef");
        register(&mut registry, 2).unwrap();
        assert_eq!(budget.level(), 0, "pid reuse released the old ring");
        assert_eq!(registry.budget().capacity(), 64 * 1024);
    }
}
