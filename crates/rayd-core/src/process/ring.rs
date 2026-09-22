//! Per-process ring of recent output, bounded by payload bytes and by the
//! sandbox-wide `OutputBudget`, so a client that reconnects with
//! `Connect(pid, from_seq)` gets what it missed without `rayd` ever holding
//! unbounded output.

use std::collections::VecDeque;

use bytes::Bytes;

use super::budget::OutputBudget;
use super::error::ProcessError;
use super::events::{OutputEvent, OutputStream};

pub const DEFAULT_RING_CAPACITY_BYTES: usize = 1024 * 1024;

#[derive(Debug)]
pub struct OutputRing {
    capacity_bytes: usize,
    budget: OutputBudget,
    events: VecDeque<OutputEvent>,
    retained_bytes: usize,
    oldest_seq: u64,
    next_seq: u64,
}

impl Default for OutputRing {
    fn default() -> Self {
        Self::new(DEFAULT_RING_CAPACITY_BYTES)
    }
}

impl OutputRing {
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
            retained_bytes: 0,
            oldest_seq: 1,
            next_seq: 1,
        }
    }

    /// Assigns the next `seq`, evicts whole events from the oldest end until
    /// the new one fits the ring and then the budget, and returns the event
    /// for fan-out. When the budget cannot take the chunk even with the
    /// ring empty, the event is delivered but not retained: the window
    /// starts right after it and a replay from before answers out of range.
    pub fn push(&mut self, stream: OutputStream, bytes: Bytes) -> OutputEvent {
        let event = OutputEvent {
            seq: self.next_seq,
            stream,
            bytes,
        };
        self.next_seq += 1;
        let cost = event.bytes.len();
        self.make_room_for(cost);
        if self.reserve(cost) {
            self.retained_bytes += cost;
            self.events.push_back(event.clone());
        } else {
            self.oldest_seq = self.next_seq;
        }
        event
    }

    #[must_use]
    pub fn next_seq(&self) -> u64 {
        self.next_seq
    }

    /// Oldest `seq` still retained, or `next_seq` when nothing is.
    #[must_use]
    pub fn oldest_seq(&self) -> u64 {
        self.events
            .front()
            .map_or(self.oldest_seq, |event| event.seq)
    }

    #[must_use]
    pub fn retained_bytes(&self) -> usize {
        self.retained_bytes
    }

    /// Events with `seq >= from_seq`. `from_seq == next_seq` is a valid empty
    /// replay (the client is up to date); anything older than the ring or
    /// past the next sequence is out of range.
    pub fn replay_from(&self, from_seq: u64) -> Result<Vec<OutputEvent>, ProcessError> {
        let oldest = self.oldest_seq();
        if from_seq < oldest || from_seq > self.next_seq {
            return Err(ProcessError::OutOfRange {
                oldest,
                next: self.next_seq,
            });
        }
        Ok(self
            .events
            .iter()
            .filter(|event| event.seq >= from_seq)
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
        self.retained_bytes -= evicted.bytes.len();
        self.budget.release(evicted.bytes.len());
        self.oldest_seq = evicted.seq.saturating_add(1);
        true
    }
}

impl Drop for OutputRing {
    fn drop(&mut self) {
        self.budget.release(self.retained_bytes);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn push(ring: &mut OutputRing, text: &str) -> u64 {
        ring.push(
            OutputStream::Stdout,
            Bytes::copy_from_slice(text.as_bytes()),
        )
        .seq
    }

    #[test]
    fn seq_starts_at_one_and_counts_both_streams() {
        let mut ring = OutputRing::default();
        assert_eq!(ring.next_seq(), 1);
        assert_eq!(push(&mut ring, "a"), 1);
        let err = ring.push(OutputStream::Stderr, Bytes::from_static(b"b"));
        assert_eq!(err.seq, 2);
        assert_eq!(err.stream, OutputStream::Stderr);
        assert_eq!(ring.next_seq(), 3);
    }

    #[test]
    fn evicts_whole_events_by_payload_bytes() {
        let mut ring = OutputRing::new(10);
        push(&mut ring, "aaaa");
        push(&mut ring, "bbbb");
        assert_eq!(ring.retained_bytes(), 8);
        push(&mut ring, "cccc");
        assert_eq!(ring.retained_bytes(), 8);
        assert_eq!(ring.oldest_seq(), 2);
        let replay = ring.replay_from(2).unwrap();
        assert_eq!(replay.len(), 2);
        assert_eq!(&replay[0].bytes[..], b"bbbb");
        assert_eq!(&replay[1].bytes[..], b"cccc");
    }

    #[test]
    fn replay_boundaries() {
        let mut ring = OutputRing::default();
        assert_eq!(ring.replay_from(1).unwrap(), vec![]);
        assert_eq!(
            ring.replay_from(0),
            Err(ProcessError::OutOfRange { oldest: 1, next: 1 })
        );
        push(&mut ring, "a");
        push(&mut ring, "b");
        assert_eq!(ring.replay_from(1).unwrap().len(), 2);
        assert_eq!(ring.replay_from(2).unwrap().len(), 1);
        assert_eq!(ring.replay_from(3).unwrap(), vec![]);
        assert_eq!(
            ring.replay_from(4),
            Err(ProcessError::OutOfRange { oldest: 1, next: 3 })
        );
    }

    #[test]
    fn replay_below_the_oldest_retained_is_out_of_range() {
        let mut ring = OutputRing::new(4);
        push(&mut ring, "aaaa");
        push(&mut ring, "bbbb");
        assert_eq!(
            ring.replay_from(1),
            Err(ProcessError::OutOfRange { oldest: 2, next: 3 })
        );
    }

    #[test]
    fn an_event_larger_than_the_ring_is_kept_alone() {
        let mut ring = OutputRing::new(2);
        push(&mut ring, "a");
        push(&mut ring, "bbbb");
        assert_eq!(ring.oldest_seq(), 2);
        assert_eq!(ring.retained_bytes(), 4);
    }

    #[test]
    fn two_rings_share_the_budget_and_the_pusher_evicts_its_own_bytes_first() {
        let budget = OutputBudget::new(8, 8);
        let mut first = OutputRing::with_budget(64, budget.clone());
        let mut second = OutputRing::with_budget(64, budget.clone());
        push(&mut first, "aaaa");
        push(&mut second, "bbbb");
        assert_eq!(budget.level(), 8);
        push(&mut second, "cc");
        assert_eq!(second.oldest_seq(), 2);
        assert_eq!(second.retained_bytes(), 2);
        assert_eq!(first.retained_bytes(), 4, "the other ring is untouched");
        assert_eq!(budget.level(), 6);
        assert_eq!(second.replay_from(2).unwrap().len(), 1);
        assert!(second.replay_from(1).is_err());
    }

    #[test]
    fn a_chunk_the_budget_cannot_take_is_delivered_but_not_retained() {
        let budget = OutputBudget::new(4, 4);
        let mut ring = OutputRing::with_budget(64, budget.clone());
        push(&mut ring, "ab");
        let big = ring.push(OutputStream::Stdout, Bytes::from_static(b"cdefgh"));
        assert_eq!(big.seq, 2);
        assert_eq!(&big.bytes[..], b"cdefgh");
        assert_eq!(ring.retained_bytes(), 0);
        assert_eq!(ring.oldest_seq(), 3);
        assert_eq!(ring.next_seq(), 3);
        assert_eq!(budget.level(), 0);
        assert_eq!(
            ring.replay_from(2),
            Err(ProcessError::OutOfRange { oldest: 3, next: 3 })
        );
        assert_eq!(ring.replay_from(3).unwrap(), vec![]);
        push(&mut ring, "xy");
        assert_eq!(ring.replay_from(3).unwrap().len(), 1);
    }

    #[test]
    fn dropping_a_ring_releases_its_bytes() {
        let budget = OutputBudget::new(8, 8);
        {
            let mut ring = OutputRing::with_budget(64, budget.clone());
            push(&mut ring, "aaaaaaaa");
            assert_eq!(budget.level(), 8);
        }
        assert_eq!(budget.level(), 0);
    }
}
