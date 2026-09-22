//! The registry of live and recently ended executions (design D9): one
//! ring (charged against the sandbox-wide output budget) and one
//! subscriber list per execution, the per-execution subscriber cap, the
//! retention of ended executions on the running clock (the cap on how
//! many coexist, and the early drop above the budget's high-water mark),
//! and the atomic "replay then subscribe" that makes `Reattach(from_seq)`
//! gap-free. `S` is the sink the adapter fans events into; the registry
//! only stores it, counts it and asks whether its client is still there.

use std::collections::BTreeMap;
use std::time::Duration;

use super::context::ContextId;
use super::error::CodeError;
use super::execution::{ExecuteOutput, ExecutionId};
use super::ring::{EXECUTE_RING_CAPACITY_BYTES, ExecuteRing};
use crate::process::{OutputBudget, SubscriberSlot};

pub const DEFAULT_MAX_EXECUTION_SUBSCRIBERS: usize = 8;
pub const DEFAULT_EXECUTION_RETENTION: Duration = Duration::from_secs(30);
/// With the 4 MiB ring this bounds retained executions at 128 MiB.
pub const DEFAULT_MAX_RETAINED_EXECUTIONS: usize = 32;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ExecutionLimits {
    pub max_subscribers: usize,
    pub retention: Duration,
    pub max_retained: usize,
}

impl Default for ExecutionLimits {
    fn default() -> Self {
        Self {
            max_subscribers: DEFAULT_MAX_EXECUTION_SUBSCRIBERS,
            retention: DEFAULT_EXECUTION_RETENTION,
            max_retained: DEFAULT_MAX_RETAINED_EXECUTIONS,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct SubscriberId(u64);

/// What a `Reattach` (or the originating `Execute`) receives up front and
/// whether it was registered for live events. An ended execution never gets
/// a subscriber: everything it will ever say is in `replay`.
#[derive(Debug, PartialEq, Eq)]
pub struct Attachment {
    pub replay: Vec<ExecuteOutput>,
    pub ended: bool,
    pub subscriber: Option<SubscriberId>,
}

#[derive(Debug)]
struct ExecutionEntry<S> {
    context_id: ContextId,
    ring: ExecuteRing,
    ended_at: Option<Duration>,
    subscribers: Vec<(SubscriberId, S)>,
}

impl<S> ExecutionEntry<S> {
    fn is_live(&self) -> bool {
        self.ended_at.is_none()
    }
}

#[derive(Debug)]
pub struct ExecutionRegistry<S> {
    limits: ExecutionLimits,
    budget: OutputBudget,
    entries: BTreeMap<ExecutionId, ExecutionEntry<S>>,
    next_subscriber: u64,
}

impl<S: Clone + SubscriberSlot> Default for ExecutionRegistry<S> {
    fn default() -> Self {
        Self::new(ExecutionLimits::default())
    }
}

impl<S: Clone + SubscriberSlot> ExecutionRegistry<S> {
    #[must_use]
    pub fn new(limits: ExecutionLimits) -> Self {
        Self::with_budget(limits, OutputBudget::unlimited())
    }

    /// Every ring charges `budget`, shared with the process registry.
    #[must_use]
    pub fn with_budget(limits: ExecutionLimits, budget: OutputBudget) -> Self {
        Self {
            limits,
            budget,
            entries: BTreeMap::new(),
            next_subscriber: 1,
        }
    }

    #[must_use]
    pub fn limits(&self) -> ExecutionLimits {
        self.limits
    }

    #[must_use]
    pub fn budget(&self) -> &OutputBudget {
        &self.budget
    }

    pub fn register(&mut self, execution_id: ExecutionId, context_id: ContextId) {
        self.entries.insert(
            execution_id,
            ExecutionEntry {
                context_id,
                ring: ExecuteRing::with_budget(EXECUTE_RING_CAPACITY_BYTES, self.budget.clone()),
                ended_at: None,
                subscribers: Vec::new(),
            },
        );
    }

    /// Records the event in the ring and returns the sinks to fan it out
    /// to, taken under the same call so per-subscriber order is by `seq`.
    /// Unknown executions (already reaped) drop the event.
    pub fn push(
        &mut self,
        execution_id: &ExecutionId,
        event: ExecuteOutput,
    ) -> Vec<(SubscriberId, S)> {
        let Some(entry) = self.entries.get_mut(execution_id) else {
            return Vec::new();
        };
        entry.ring.push(event);
        entry.subscribers.clone()
    }

    /// Replay from `from_seq` (0 = nothing) and, if the execution is still
    /// live, subscribe `sink` for what comes next. The context must match
    /// (a mismatch is reported as not found so ids stay opaque); range and
    /// cap are checked before anything is stored.
    pub fn attach(
        &mut self,
        context_id: &ContextId,
        execution_id: &ExecutionId,
        from_seq: u64,
        sink: S,
    ) -> Result<Attachment, CodeError> {
        let max = self.limits.max_subscribers;
        let id = SubscriberId(self.next_subscriber);
        let entry = self
            .entries
            .get_mut(execution_id)
            .filter(|entry| entry.context_id == *context_id)
            .ok_or(CodeError::ExecutionNotFound)?;
        let replay = entry.ring.replay_from(from_seq)?;
        if !entry.is_live() {
            return Ok(Attachment {
                replay,
                ended: true,
                subscriber: None,
            });
        }
        entry.subscribers.retain(|(_, slot)| slot.is_open());
        if entry.subscribers.len() >= max {
            return Err(CodeError::TooManySubscribers { max });
        }
        entry.subscribers.push((id, sink));
        self.next_subscriber += 1;
        Ok(Attachment {
            replay,
            ended: false,
            subscriber: Some(id),
        })
    }

    pub fn detach(&mut self, execution_id: &ExecutionId, subscriber: SubscriberId) {
        if let Some(entry) = self.entries.get_mut(execution_id) {
            entry.subscribers.retain(|(id, _)| *id != subscriber);
        }
    }

    /// Every sink of every live execution (for `/suspend`).
    #[must_use]
    pub fn live_subscribers(&self) -> Vec<S> {
        self.entries
            .values()
            .filter(|entry| entry.is_live())
            .flat_map(|entry| entry.subscribers.iter().map(|(_, sink)| sink.clone()))
            .collect()
    }

    /// Marks the execution ended on the running clock and hands back its
    /// subscribers, now detached; once more than `max_retained` ended
    /// executions coexist the oldest ended ones are dropped early.
    pub fn mark_ended(
        &mut self,
        execution_id: &ExecutionId,
        running_now: Duration,
    ) -> Vec<(SubscriberId, S)> {
        let Some(entry) = self.entries.get_mut(execution_id) else {
            return Vec::new();
        };
        entry.ended_at = Some(running_now);
        let subscribers = std::mem::take(&mut entry.subscribers);
        self.evict_excess_retained();
        subscribers
    }

    /// Drops ended executions whose retention window closed; returns them.
    pub fn reap_expired(&mut self, running_now: Duration) -> Vec<ExecutionId> {
        let retention = self.limits.retention;
        let expired: Vec<ExecutionId> = self
            .entries
            .iter()
            .filter(|(_, entry)| {
                entry
                    .ended_at
                    .is_some_and(|ended_at| running_now.saturating_sub(ended_at) >= retention)
            })
            .map(|(id, _)| id.clone())
            .collect();
        for id in &expired {
            self.entries.remove(id);
        }
        expired
    }

    #[must_use]
    pub fn len(&self) -> usize {
        self.entries.len()
    }

    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    #[must_use]
    pub fn retained_count(&self) -> usize {
        self.entries
            .values()
            .filter(|entry| !entry.is_live())
            .count()
    }

    #[must_use]
    pub fn is_live(&self, execution_id: &ExecutionId) -> bool {
        self.entries
            .get(execution_id)
            .is_some_and(ExecutionEntry::is_live)
    }

    #[must_use]
    pub fn contains(&self, execution_id: &ExecutionId) -> bool {
        self.entries.contains_key(execution_id)
    }

    /// While the shared budget sits above its high-water mark, ended
    /// executions go oldest-first regardless of their retention window;
    /// live ones are never touched. Returns the ids dropped.
    pub fn drop_ended_over_budget(&mut self) -> Vec<ExecutionId> {
        let mut dropped = Vec::new();
        for id in self.ended_oldest_first() {
            if !self.budget.above_high_water() {
                break;
            }
            self.entries.remove(&id);
            dropped.push(id);
        }
        dropped
    }

    fn evict_excess_retained(&mut self) {
        let excess = self
            .retained_count()
            .saturating_sub(self.limits.max_retained);
        for id in self.ended_oldest_first().into_iter().take(excess) {
            self.entries.remove(&id);
        }
    }

    fn ended_oldest_first(&self) -> Vec<ExecutionId> {
        let mut retained: Vec<(Duration, ExecutionId)> = self
            .entries
            .iter()
            .filter_map(|(id, entry)| entry.ended_at.map(|ended_at| (ended_at, id.clone())))
            .collect();
        retained.sort_unstable();
        retained.into_iter().map(|(_, id)| id).collect()
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;
    use std::sync::atomic::{AtomicBool, Ordering};

    use super::*;

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

    type Registry = ExecutionRegistry<FakeSink>;

    fn exec(n: u64) -> ExecutionId {
        ExecutionId::from_raw(&format!("exec-{n:016x}"))
    }

    fn ctx() -> ContextId {
        ContextId::default_context()
    }

    fn stdout(seq: u64) -> ExecuteOutput {
        ExecuteOutput::Stdout {
            seq,
            text: "x".to_owned(),
            timestamp_unix_ns: 0,
        }
    }

    fn limits(max_retained: usize) -> ExecutionLimits {
        ExecutionLimits {
            max_subscribers: 8,
            retention: Duration::from_secs(30),
            max_retained,
        }
    }

    fn registered(n: u64) -> Registry {
        let mut registry = Registry::default();
        registry.register(exec(n), ctx());
        registry
    }

    #[test]
    fn defaults_are_the_contract_values() {
        let registry = Registry::default();
        assert_eq!(registry.limits().max_subscribers, 8);
        assert_eq!(registry.limits().retention, Duration::from_secs(30));
        assert_eq!(registry.limits().max_retained, 32);
        assert!(registry.is_empty());
    }

    #[test]
    fn attach_replays_then_subscribes_and_push_fans_out_in_seq_order() {
        let mut registry = registered(1);
        assert!(registry.push(&exec(1), stdout(1)).is_empty());
        registry.push(&exec(1), stdout(2));
        let sink = FakeSink::open();
        let attachment = registry.attach(&ctx(), &exec(1), 1, sink).unwrap();
        assert_eq!(attachment.replay.len(), 2);
        assert!(!attachment.ended);
        assert!(attachment.subscriber.is_some());
        let sinks = registry.push(&exec(1), stdout(3));
        assert_eq!(sinks.len(), 1);
        let live_only = registry
            .attach(&ctx(), &exec(1), 0, FakeSink::open())
            .unwrap();
        assert!(live_only.replay.is_empty());
        assert_eq!(registry.live_subscribers().len(), 2);
        assert_eq!(
            registry
                .attach(&ctx(), &exec(1), 9, FakeSink::open())
                .unwrap_err(),
            CodeError::ReplayOutOfRange { oldest: 1, next: 4 }
        );
        assert!(registry.push(&exec(9), stdout(1)).is_empty());
    }

    #[test]
    fn the_ninth_subscriber_is_refused_and_closed_slots_are_reclaimed() {
        let mut registry = registered(1);
        let first = FakeSink::open();
        registry.attach(&ctx(), &exec(1), 0, first.clone()).unwrap();
        for _ in 0..7 {
            registry
                .attach(&ctx(), &exec(1), 0, FakeSink::open())
                .unwrap();
        }
        assert_eq!(
            registry
                .attach(&ctx(), &exec(1), 0, FakeSink::open())
                .unwrap_err(),
            CodeError::TooManySubscribers { max: 8 }
        );
        first.close();
        let reclaimed = registry
            .attach(&ctx(), &exec(1), 0, FakeSink::open())
            .unwrap();
        registry.detach(&exec(1), reclaimed.subscriber.unwrap());
        assert!(
            registry
                .attach(&ctx(), &exec(1), 0, FakeSink::open())
                .is_ok()
        );
    }

    #[test]
    fn a_context_mismatch_or_unknown_id_is_not_found() {
        let mut registry = registered(1);
        let other = ContextId::parse("ctx-000000000000").unwrap();
        assert_eq!(
            registry
                .attach(&other, &exec(1), 0, FakeSink::open())
                .unwrap_err(),
            CodeError::ExecutionNotFound
        );
        assert_eq!(
            registry
                .attach(&ctx(), &exec(2), 0, FakeSink::open())
                .unwrap_err(),
            CodeError::ExecutionNotFound
        );
    }

    #[test]
    fn an_ended_execution_replays_with_ended_and_no_subscriber() {
        let mut registry = registered(1);
        registry.push(&exec(1), stdout(1));
        let live = FakeSink::open();
        registry.attach(&ctx(), &exec(1), 0, live).unwrap();
        registry.push(
            &exec(1),
            ExecuteOutput::End {
                seq: 2,
                execution_count: 1,
            },
        );
        let drained = registry.mark_ended(&exec(1), Duration::from_secs(1));
        assert_eq!(drained.len(), 1);
        assert!(registry.live_subscribers().is_empty());
        assert!(!registry.is_live(&exec(1)));
        let late = registry
            .attach(&ctx(), &exec(1), 1, FakeSink::open())
            .unwrap();
        assert_eq!(late.replay.len(), 2);
        assert!(late.ended);
        assert_eq!(late.subscriber, None);
        assert_eq!(registry.retained_count(), 1);
        assert!(registry.mark_ended(&exec(7), Duration::ZERO).is_empty());
    }

    #[test]
    fn retention_runs_on_the_running_clock() {
        let mut registry = registered(1);
        registry.register(exec(2), ctx());
        registry.mark_ended(&exec(1), Duration::from_secs(10));
        assert_eq!(registry.reap_expired(Duration::from_secs(39)), vec![]);
        assert_eq!(
            registry.reap_expired(Duration::from_secs(40)),
            vec![exec(1)]
        );
        assert!(!registry.contains(&exec(1)));
        assert!(registry.contains(&exec(2)));
        assert_eq!(registry.len(), 1);
        assert_eq!(
            registry
                .attach(&ctx(), &exec(1), 0, FakeSink::open())
                .unwrap_err(),
            CodeError::ExecutionNotFound
        );
    }

    #[test]
    fn ended_executions_are_dropped_early_above_the_high_water_mark() {
        let budget = OutputBudget::new(64 * 1024, 48 * 1024);
        let mut registry = Registry::with_budget(limits(32), budget.clone());
        let text = "x".repeat(20 * 1024);
        for n in 1..=3 {
            registry.register(exec(n), ctx());
            registry.push(
                &exec(n),
                ExecuteOutput::Stdout {
                    seq: 1,
                    text: text.clone(),
                    timestamp_unix_ns: 0,
                },
            );
        }
        registry.mark_ended(&exec(1), Duration::from_secs(1));
        registry.mark_ended(&exec(2), Duration::from_secs(2));
        assert!(budget.above_high_water());
        assert_eq!(registry.reap_expired(Duration::from_secs(5)), vec![]);
        assert_eq!(registry.drop_ended_over_budget(), vec![exec(1)]);
        assert!(!budget.above_high_water());
        assert!(registry.contains(&exec(2)));
        assert!(registry.is_live(&exec(3)));
        registry.reap_expired(Duration::from_secs(60));
        assert_eq!(budget.level(), 20 * 1024, "only the live ring is charged");
        assert_eq!(registry.budget().high_water(), 48 * 1024);
    }

    #[test]
    fn the_thirty_third_retained_execution_evicts_the_oldest() {
        let mut registry = Registry::new(limits(32));
        for n in 1..=33 {
            registry.register(exec(n), ctx());
            registry.mark_ended(&exec(n), Duration::from_millis(n));
        }
        assert_eq!(registry.retained_count(), 32);
        assert!(!registry.contains(&exec(1)));
        assert!(registry.contains(&exec(2)));
        assert!(registry.contains(&exec(33)));
        registry.register(exec(40), ctx());
        assert_eq!(registry.len(), 33);
        assert!(registry.is_live(&exec(40)));
    }
}
