//! Per-execution ring of recent events, bounded by the bytes of text they
//! carry and by the sandbox-wide `OutputBudget`, so a client that comes
//! back with `Reattach(from_seq)` after a resume or a dropped stream gets
//! what it missed without `rayd` holding unbounded output (design D9).

use std::collections::VecDeque;

use super::error::CodeError;
use super::execution::ExecuteOutput;
use crate::process::OutputBudget;

pub const EXECUTE_RING_CAPACITY_BYTES: usize = 4 * 1024 * 1024;

#[derive(Debug)]
pub struct ExecuteRing {
    capacity_bytes: usize,
    budget: OutputBudget,
    events: VecDeque<ExecuteOutput>,
    oldest_seq: u64,
    next_seq: u64,
    retained_bytes: usize,
}

impl Default for ExecuteRing {
    fn default() -> Self {
        Self::new(EXECUTE_RING_CAPACITY_BYTES)
    }
}

impl ExecuteRing {
    #[must_use]
    pub fn new(capacity_bytes: usize) -> Self {
        Self::with_budget(capacity_bytes, OutputBudget::unlimited())
    }

    #[must_use]
    pub fn with_budget(capacity_bytes: usize, budget: OutputBudget) -> Self {
        Self {
            capacity_bytes,
            budget,
            events: VecDeque::new(),
            oldest_seq: 1,
            next_seq: 1,
            retained_bytes: 0,
        }
    }

    /// Stores an event that already carries its `seq` (the tracker numbers
    /// them), evicting the oldest ones until it fits the ring and then the
    /// budget. An event larger than the whole ring, or one the budget
    /// cannot take with the ring empty, evicts everything and is not
    /// stored: the window then starts right after it.
    pub fn push(&mut self, event: ExecuteOutput) {
        let seq = event.seq();
        let cost = event_cost(&event);
        self.next_seq = seq.saturating_add(1);
        if cost > self.capacity_bytes {
            self.evict_all();
            return;
        }
        self.make_room_for(cost);
        if !self.reserve(cost) {
            self.evict_all();
            return;
        }
        self.retained_bytes += cost;
        self.events.push_back(event);
    }

    #[must_use]
    pub fn next_seq(&self) -> u64 {
        self.next_seq
    }

    /// Oldest `seq` still retained, or `next_seq` when nothing is.
    #[must_use]
    pub fn oldest_seq(&self) -> u64 {
        self.oldest_seq
    }

    #[must_use]
    pub fn retained_bytes(&self) -> usize {
        self.retained_bytes
    }

    #[must_use]
    pub fn len(&self) -> usize {
        self.events.len()
    }

    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.events.is_empty()
    }

    /// Events with `seq >= from_seq`; `0` means "live only" and replays
    /// nothing. `from_seq == next_seq` is a valid empty replay; anything
    /// older than the window or past the next sequence is out of range.
    pub fn replay_from(&self, from_seq: u64) -> Result<Vec<ExecuteOutput>, CodeError> {
        if from_seq == 0 {
            return Ok(Vec::new());
        }
        let oldest = self.oldest_seq();
        if from_seq < oldest || from_seq > self.next_seq {
            return Err(CodeError::ReplayOutOfRange {
                oldest,
                next: self.next_seq,
            });
        }
        Ok(self
            .events
            .iter()
            .filter(|event| event.seq() >= from_seq)
            .cloned()
            .collect())
    }

    fn make_room_for(&mut self, incoming: usize) {
        while self.retained_bytes + incoming > self.capacity_bytes {
            if !self.evict_oldest() {
                return;
            }
        }
    }

    fn reserve(&mut self, cost: usize) -> bool {
        while !self.budget.charge(cost) {
            if !self.evict_oldest() {
                return false;
            }
        }
        true
    }

    fn evict_oldest(&mut self) -> bool {
        let Some(evicted) = self.events.pop_front() else {
            return false;
        };
        let cost = event_cost(&evicted);
        self.retained_bytes -= cost;
        self.budget.release(cost);
        self.oldest_seq = evicted.seq().saturating_add(1);
        true
    }

    fn evict_all(&mut self) {
        self.events.clear();
        self.budget.release(self.retained_bytes);
        self.retained_bytes = 0;
        self.oldest_seq = self.next_seq;
    }
}

impl Drop for ExecuteRing {
    fn drop(&mut self) {
        self.budget.release(self.retained_bytes);
    }
}

/// Bytes an event holds against the ring: output text, every string of a
/// result bundle, an error's value and traceback; markers cost nothing.
#[must_use]
pub fn event_cost(event: &ExecuteOutput) -> usize {
    match event {
        ExecuteOutput::Started { .. } | ExecuteOutput::End { .. } => 0,
        ExecuteOutput::Stdout { text, .. } | ExecuteOutput::Stderr { text, .. } => text.len(),
        ExecuteOutput::Result { bundle, .. } => {
            let fields = [
                &bundle.text,
                &bundle.html,
                &bundle.markdown,
                &bundle.latex,
                &bundle.json,
                &bundle.javascript,
                &bundle.png,
                &bundle.jpeg,
                &bundle.svg,
                &bundle.pdf,
                &bundle.chart,
                &bundle.data,
            ];
            let known: usize = fields
                .iter()
                .map(|field| field.as_ref().map_or(0, String::len))
                .sum();
            let extra: usize = bundle
                .extra
                .iter()
                .map(|(mime, value)| mime.len() + value.len())
                .sum();
            known + extra
        }
        ExecuteOutput::Error { error, .. } => {
            error.value.len() + error.traceback.iter().map(String::len).sum::<usize>()
        }
    }
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;

    use super::*;
    use crate::code::execution::{ExecutionErrorInfo, ExecutionId, ResultBundle};

    fn stdout(seq: u64, text: &str) -> ExecuteOutput {
        ExecuteOutput::Stdout {
            seq,
            text: text.to_owned(),
            timestamp_unix_ns: 0,
        }
    }

    fn started(seq: u64) -> ExecuteOutput {
        ExecuteOutput::Started {
            seq,
            execution_id: ExecutionId::from_raw("exec-1"),
            execution_count: 1,
        }
    }

    fn seqs(events: &[ExecuteOutput]) -> Vec<u64> {
        events.iter().map(ExecuteOutput::seq).collect()
    }

    #[test]
    fn push_and_replay_follow_the_seq_the_tracker_assigned() {
        let mut ring = ExecuteRing::default();
        assert_eq!(ring.next_seq(), 1);
        assert_eq!(ring.oldest_seq(), 1);
        ring.push(started(1));
        ring.push(stdout(2, "a"));
        ring.push(stdout(3, "b"));
        assert_eq!(ring.next_seq(), 4);
        assert_eq!(ring.len(), 3);
        assert_eq!(seqs(&ring.replay_from(1).unwrap()), vec![1, 2, 3]);
        assert_eq!(seqs(&ring.replay_from(3).unwrap()), vec![3]);
        assert_eq!(ring.replay_from(4).unwrap(), vec![]);
        assert_eq!(ring.replay_from(0).unwrap(), vec![]);
        assert_eq!(
            ring.replay_from(5),
            Err(CodeError::ReplayOutOfRange { oldest: 1, next: 4 })
        );
    }

    #[test]
    fn eviction_keeps_the_newest_events_within_capacity() {
        let mut ring = ExecuteRing::new(10);
        ring.push(started(1));
        ring.push(stdout(2, "aaaa"));
        ring.push(stdout(3, "bbbb"));
        assert_eq!(ring.retained_bytes(), 8);
        ring.push(stdout(4, "cccc"));
        assert_eq!(ring.retained_bytes(), 8);
        assert_eq!(ring.oldest_seq(), 3);
        assert_eq!(seqs(&ring.replay_from(3).unwrap()), vec![3, 4]);
        assert_eq!(
            ring.replay_from(2),
            Err(CodeError::ReplayOutOfRange { oldest: 3, next: 5 })
        );
        assert_eq!(
            ring.replay_from(1),
            Err(CodeError::ReplayOutOfRange { oldest: 3, next: 5 })
        );
    }

    #[test]
    fn an_oversized_event_evicts_everything_and_is_not_stored() {
        let mut ring = ExecuteRing::new(4);
        ring.push(started(1));
        ring.push(stdout(2, "ab"));
        ring.push(stdout(3, "too large"));
        assert!(ring.is_empty());
        assert_eq!(ring.retained_bytes(), 0);
        assert_eq!(ring.oldest_seq(), 4);
        assert_eq!(ring.next_seq(), 4);
        assert_eq!(ring.replay_from(4).unwrap(), vec![]);
        assert_eq!(
            ring.replay_from(3),
            Err(CodeError::ReplayOutOfRange { oldest: 4, next: 4 })
        );
        ring.push(stdout(4, "ok"));
        assert_eq!(seqs(&ring.replay_from(4).unwrap()), vec![4]);
    }

    #[test]
    fn rings_share_the_budget_and_evict_their_own_events_first() {
        let budget = OutputBudget::new(8, 8);
        let mut first = ExecuteRing::with_budget(64, budget.clone());
        let mut second = ExecuteRing::with_budget(64, budget.clone());
        first.push(stdout(1, "aaaa"));
        second.push(started(1));
        second.push(stdout(2, "bbbb"));
        assert_eq!(budget.level(), 8);
        second.push(stdout(3, "cc"));
        assert_eq!(second.oldest_seq(), 3);
        assert_eq!(second.retained_bytes(), 2);
        assert_eq!(first.retained_bytes(), 4);
        assert_eq!(budget.level(), 6);
        second.push(stdout(4, "dddddddddd"));
        assert!(
            second.is_empty(),
            "an event the budget cannot take is not stored"
        );
        assert_eq!(second.oldest_seq(), 5);
        assert_eq!(budget.level(), 4);
        drop(first);
        assert_eq!(budget.level(), 0);
    }

    #[test]
    fn result_cost_counts_every_mime_string_and_errors_count_their_text() {
        let mut mime = BTreeMap::new();
        mime.insert("text/plain".to_owned(), "12".to_owned());
        mime.insert("image/png".to_owned(), "abcd".to_owned());
        mime.insert("x/y".to_owned(), "z".to_owned());
        let result = ExecuteOutput::Result {
            seq: 1,
            bundle: Box::new(ResultBundle::from_mime(true, mime)),
        };
        assert_eq!(event_cost(&result), 2 + 4 + 3 + 1);
        let error = ExecuteOutput::Error {
            seq: 2,
            error: ExecutionErrorInfo {
                name: "ZeroDivisionError".to_owned(),
                value: "division by zero".to_owned(),
                traceback: vec!["a".to_owned(), "bc".to_owned()],
            },
        };
        assert_eq!(event_cost(&error), 16 + 3);
        assert_eq!(event_cost(&started(1)), 0);
        assert_eq!(
            event_cost(&ExecuteOutput::End {
                seq: 3,
                execution_count: 1
            }),
            0
        );
    }
}
