//! The transfers of one sandbox (design D8): opaque ids, the phase
//! machine, the bounds (16 waiting or running, 2 moving bytes), the
//! retention of finished records (64 at most, 30 minutes each, evicted on
//! every admission and lookup) and the snapshots `GetTransfer` and
//! `WatchTransfer` serve. Pure: the adapter owns it behind a mutex and
//! reads the monotonic side of the `Clock` port for every call.

use std::collections::BTreeMap;
use std::fmt;
use std::time::Duration;

use thiserror::Error;

use super::barrier::BarrierTicket;
use super::error::TransferFailure;
use super::url_policy::TransferDirection;
use super::{TRANSFER_MAX_ACTIVE, TRANSFER_MAX_RUNNING, TRANSFER_RETAINED, TRANSFER_RETAINED_MAX};
use crate::code::{RandomError, RandomSource};
use crate::filesystem::Entry;

const ID_BYTES: usize = 16;

/// 32 lowercase hex characters from the random port.
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct TransferId(String);

impl TransferId {
    pub fn generate(random: &dyn RandomSource) -> Result<Self, RandomError> {
        let mut bytes = [0u8; ID_BYTES];
        random.fill(&mut bytes)?;
        Ok(Self(lower_hex(&bytes)))
    }

    /// `None` for anything but the generated shape, the empty id (the SDK's
    /// capability probe) included: the caller answers `NOT_FOUND`.
    #[must_use]
    pub fn parse(raw: &str) -> Option<Self> {
        let valid = raw.len() == ID_BYTES * 2
            && raw
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte));
        valid.then(|| Self(raw.to_owned()))
    }

    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

fn lower_hex(bytes: &[u8]) -> String {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    bytes
        .iter()
        .flat_map(|byte| {
            [
                DIGITS[usize::from(byte >> 4)],
                DIGITS[usize::from(byte & 0x0f)],
            ]
        })
        .map(char::from)
        .collect()
}

impl fmt::Display for TransferId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TransferPhase {
    Waiting,
    Running,
    Done,
    Failed,
    Cancelled,
}

impl TransferPhase {
    #[must_use]
    pub fn is_terminal(self) -> bool {
        matches!(self, Self::Done | Self::Failed | Self::Cancelled)
    }

    #[must_use]
    pub fn is_active(self) -> bool {
        !self.is_terminal()
    }

    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Waiting => "waiting",
            Self::Running => "running",
            Self::Done => "done",
            Self::Failed => "failed",
            Self::Cancelled => "cancelled",
        }
    }

    /// `Waiting → Running → Done | Failed`, `Running → Waiting` (requeue),
    /// `Waiting → Failed` (a probe's verdict, the expiry), and `Waiting |
    /// Running → Cancelled`.
    fn allows(self, to: Self) -> bool {
        matches!(
            (self, to),
            (
                Self::Waiting,
                Self::Running | Self::Failed | Self::Cancelled
            ) | (
                Self::Running,
                Self::Waiting | Self::Done | Self::Failed | Self::Cancelled
            )
        )
    }
}

impl fmt::Display for TransferPhase {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RegistryLimits {
    pub max_active: usize,
    pub max_running: usize,
    pub retained_max: usize,
    pub retained: Duration,
}

impl Default for RegistryLimits {
    fn default() -> Self {
        Self {
            max_active: TRANSFER_MAX_ACTIVE,
            max_running: TRANSFER_MAX_RUNNING,
            retained_max: TRANSFER_RETAINED_MAX,
            retained: TRANSFER_RETAINED,
        }
    }
}

#[derive(Debug, Error, Clone, Copy, PartialEq, Eq)]
pub enum RegistryError {
    #[error("demasiadas transferencias activas")]
    Full,
    #[error("transferencia desconocida")]
    Unknown,
    #[error("ya hay {max} transferencias moviendo bytes")]
    RunningFull { max: usize },
    #[error("transición {from} -> {to} no permitida")]
    IllegalTransition {
        from: TransferPhase,
        to: TransferPhase,
    },
    #[error("identificador de transferencia repetido")]
    DuplicateId,
}

/// What a transfer is when it is accepted. `request_path` (normalised)
/// and `destination` (canonical) are what the barrier matches; neither is
/// ever logged.
pub struct NewTransfer {
    pub direction: TransferDirection,
    pub armed: bool,
    pub bytes_total: u64,
    pub entry: Option<Entry>,
    pub request_path: String,
    pub destination: String,
}

/// How a transfer ended well: the written (import) or read (export)
/// entry, the sha256 of the bytes moved and, for a multipart export, the
/// `ETag` values in part order.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DoneOutcome {
    pub entry: Option<Entry>,
    pub sha256: String,
    pub bytes: u64,
    pub part_etags: Vec<String>,
}

/// One full `TransferState`, plus whether an import has seen its object
/// (the barrier's "found", the cancel's "run the DELETE").
#[derive(Clone, PartialEq, Eq)]
pub struct TransferSnapshot {
    pub id: TransferId,
    pub direction: TransferDirection,
    pub phase: TransferPhase,
    pub bytes_done: u64,
    pub bytes_total: u64,
    pub probes: u32,
    pub entry: Option<Entry>,
    pub sha256: String,
    pub part_etags: Vec<String>,
    pub duration: Duration,
    pub failure: Option<TransferFailure>,
    pub object_seen: bool,
}

/// The entry carries a path; `Debug` leaves it out.
impl fmt::Debug for TransferSnapshot {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("TransferSnapshot")
            .field("id", &self.id)
            .field("direction", &self.direction)
            .field("phase", &self.phase)
            .field("bytes_done", &self.bytes_done)
            .field("bytes_total", &self.bytes_total)
            .field("probes", &self.probes)
            .field("failure", &self.failure)
            .field("object_seen", &self.object_seen)
            .finish_non_exhaustive()
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CancelOutcome {
    /// It was waiting or running; the adapter stops its task.
    Cancelled(TransferSnapshot),
    /// It had already finished; nothing changed.
    AlreadyFinished(TransferSnapshot),
}

struct Record {
    direction: TransferDirection,
    armed: bool,
    phase: TransferPhase,
    bytes_done: u64,
    bytes_total: u64,
    probes: u32,
    entry: Option<Entry>,
    sha256: String,
    part_etags: Vec<String>,
    request_path: String,
    destination: String,
    started_at: Duration,
    finished_at: Option<Duration>,
    failure: Option<TransferFailure>,
    object_seen: bool,
}

impl Record {
    fn snapshot(&self, id: &TransferId, now: Duration) -> TransferSnapshot {
        let end = self.finished_at.unwrap_or(now);
        TransferSnapshot {
            id: id.clone(),
            direction: self.direction,
            phase: self.phase,
            bytes_done: self.bytes_done,
            bytes_total: self.bytes_total,
            probes: self.probes,
            entry: self.entry.clone(),
            sha256: self.sha256.clone(),
            part_etags: self.part_etags.clone(),
            duration: end.saturating_sub(self.started_at),
            failure: self.failure,
            object_seen: self.object_seen,
        }
    }

    fn move_to(&mut self, to: TransferPhase, now: Duration) -> Result<(), RegistryError> {
        if !self.phase.allows(to) {
            return Err(RegistryError::IllegalTransition {
                from: self.phase,
                to,
            });
        }
        self.phase = to;
        if to.is_terminal() {
            self.finished_at = Some(now);
        }
        Ok(())
    }
}

#[derive(Default)]
pub struct TransferRegistry {
    limits: RegistryLimits,
    records: BTreeMap<TransferId, Record>,
}

impl TransferRegistry {
    #[must_use]
    pub fn new(limits: RegistryLimits) -> Self {
        Self {
            limits,
            records: BTreeMap::new(),
        }
    }

    /// Evicts what expired, then refuses a 17th waiting or running transfer.
    pub fn admit(
        &mut self,
        id: TransferId,
        new: NewTransfer,
        now: Duration,
    ) -> Result<TransferSnapshot, RegistryError> {
        self.evict(now);
        if self.active_count() >= self.limits.max_active {
            return Err(RegistryError::Full);
        }
        if self.records.contains_key(&id) {
            return Err(RegistryError::DuplicateId);
        }
        let record = Record {
            direction: new.direction,
            armed: new.armed,
            phase: TransferPhase::Waiting,
            bytes_done: 0,
            bytes_total: new.bytes_total,
            probes: 0,
            entry: new.entry,
            sha256: String::new(),
            part_etags: Vec::new(),
            request_path: new.request_path,
            destination: new.destination,
            started_at: now,
            finished_at: None,
            failure: None,
            object_seen: false,
        };
        let snapshot = record.snapshot(&id, now);
        self.records.insert(id, record);
        Ok(snapshot)
    }

    /// The record behind a wire id; anything unknown, evicted or malformed
    /// (the empty probe id included) is `Unknown`.
    pub fn lookup(
        &mut self,
        raw_id: &str,
        now: Duration,
    ) -> Result<TransferSnapshot, RegistryError> {
        let id = TransferId::parse(raw_id).ok_or(RegistryError::Unknown)?;
        self.snapshot(&id, now)
    }

    pub fn snapshot(
        &mut self,
        id: &TransferId,
        now: Duration,
    ) -> Result<TransferSnapshot, RegistryError> {
        self.evict(now);
        self.records
            .get(id)
            .map(|record| record.snapshot(id, now))
            .ok_or(RegistryError::Unknown)
    }

    /// `Waiting → Running`, refused while `max_running` records already move
    /// bytes (the adapter's permits make that unreachable; the domain keeps
    /// the invariant anyway).
    pub fn start_running(
        &mut self,
        id: &TransferId,
        now: Duration,
    ) -> Result<TransferSnapshot, RegistryError> {
        if self.running_count() >= self.limits.max_running {
            return Err(RegistryError::RunningFull {
                max: self.limits.max_running,
            });
        }
        self.update(id, now, |record, now| {
            record.move_to(TransferPhase::Running, now)
        })
    }

    /// `Running → Waiting` after an interruption or a suspend; the bytes
    /// moved so far no longer count.
    pub fn requeue(
        &mut self,
        id: &TransferId,
        now: Duration,
    ) -> Result<TransferSnapshot, RegistryError> {
        self.update(id, now, |record, now| {
            record.move_to(TransferPhase::Waiting, now)?;
            record.bytes_done = 0;
            Ok(())
        })
    }

    /// One more `GET` issued by an import; `object_seen` sticks once true.
    pub fn record_probe(
        &mut self,
        id: &TransferId,
        object_seen: bool,
        now: Duration,
    ) -> Result<TransferSnapshot, RegistryError> {
        self.update(id, now, |record, _| {
            record.probes = record.probes.saturating_add(1);
            record.object_seen |= object_seen;
            Ok(())
        })
    }

    /// The size an import learned from `Content-Length` once admitted.
    pub fn set_total(
        &mut self,
        id: &TransferId,
        bytes_total: u64,
        now: Duration,
    ) -> Result<TransferSnapshot, RegistryError> {
        self.update(id, now, |record, _| {
            record.bytes_total = bytes_total;
            Ok(())
        })
    }

    pub fn set_progress(
        &mut self,
        id: &TransferId,
        bytes_done: u64,
        now: Duration,
    ) -> Result<TransferSnapshot, RegistryError> {
        self.update(id, now, |record, _| {
            record.bytes_done = bytes_done;
            Ok(())
        })
    }

    pub fn finish_done(
        &mut self,
        id: &TransferId,
        outcome: DoneOutcome,
        now: Duration,
    ) -> Result<TransferSnapshot, RegistryError> {
        self.update(id, now, |record, now| {
            record.move_to(TransferPhase::Done, now)?;
            record.bytes_done = outcome.bytes;
            record.bytes_total = outcome.bytes;
            record.sha256 = outcome.sha256;
            record.part_etags = outcome.part_etags;
            if outcome.entry.is_some() {
                record.entry = outcome.entry;
            }
            Ok(())
        })
    }

    pub fn finish_failed(
        &mut self,
        id: &TransferId,
        failure: TransferFailure,
        now: Duration,
    ) -> Result<TransferSnapshot, RegistryError> {
        self.update(id, now, |record, now| {
            record.move_to(TransferPhase::Failed, now)?;
            record.failure = Some(failure);
            Ok(())
        })
    }

    /// Idempotent: a waiting or running transfer becomes `Cancelled`, a
    /// finished one stays as it is.
    pub fn cancel(
        &mut self,
        raw_id: &str,
        failure: TransferFailure,
        now: Duration,
    ) -> Result<CancelOutcome, RegistryError> {
        let id = TransferId::parse(raw_id).ok_or(RegistryError::Unknown)?;
        self.evict(now);
        let record = self.records.get_mut(&id).ok_or(RegistryError::Unknown)?;
        if record.phase.is_terminal() {
            return Ok(CancelOutcome::AlreadyFinished(record.snapshot(&id, now)));
        }
        record.move_to(TransferPhase::Cancelled, now)?;
        record.failure = Some(failure);
        Ok(CancelOutcome::Cancelled(record.snapshot(&id, now)))
    }

    /// `/terminate`: every waiting or running record becomes `Cancelled`.
    pub fn cancel_all(&mut self, failure: TransferFailure, now: Duration) -> Vec<TransferId> {
        let mut cancelled = Vec::new();
        for (id, record) in &mut self.records {
            if record.phase.is_active() && record.move_to(TransferPhase::Cancelled, now).is_ok() {
                record.failure = Some(failure);
                cancelled.push(id.clone());
            }
        }
        cancelled
    }

    #[must_use]
    pub fn active_count(&self) -> usize {
        self.count(|record| record.phase.is_active())
    }

    #[must_use]
    pub fn running_count(&self) -> usize {
        self.count(|record| record.phase == TransferPhase::Running)
    }

    /// Imports with `wait_for_object` still waiting or running: the
    /// barrier's scope.
    #[must_use]
    pub fn armed_count(&self) -> usize {
        self.count(|record| record.armed && record.phase.is_active())
    }

    #[must_use]
    pub fn armed_tickets(&self) -> Vec<BarrierTicket> {
        self.records
            .iter()
            .filter(|(_, record)| record.armed && record.phase.is_active())
            .map(|(id, record)| BarrierTicket {
                id: id.clone(),
                phase: record.phase,
                request_path: record.request_path.clone(),
                destination: record.destination.clone(),
            })
            .collect()
    }

    #[must_use]
    pub fn len(&self) -> usize {
        self.records.len()
    }

    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.records.is_empty()
    }

    fn count(&self, predicate: impl Fn(&Record) -> bool) -> usize {
        self.records
            .values()
            .filter(|record| predicate(record))
            .count()
    }

    fn update(
        &mut self,
        id: &TransferId,
        now: Duration,
        change: impl FnOnce(&mut Record, Duration) -> Result<(), RegistryError>,
    ) -> Result<TransferSnapshot, RegistryError> {
        let record = self.records.get_mut(id).ok_or(RegistryError::Unknown)?;
        change(record, now)?;
        Ok(record.snapshot(id, now))
    }

    /// Finished records older than the retention go; then the oldest
    /// finished ones until at most `retained_max` remain.
    fn evict(&mut self, now: Duration) {
        let retained = self.limits.retained;
        self.records.retain(|_, record| {
            record
                .finished_at
                .is_none_or(|finished| now.saturating_sub(finished) < retained)
        });
        let mut finished: Vec<(Duration, TransferId)> = self
            .records
            .iter()
            .filter_map(|(id, record)| record.finished_at.map(|at| (at, id.clone())))
            .collect();
        if finished.len() <= self.limits.retained_max {
            return;
        }
        finished.sort();
        let excess = finished.len() - self.limits.retained_max;
        for (_, id) in finished.into_iter().take(excess) {
            self.records.remove(&id);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::transfer::error::FailureReason;

    struct CountingRandom(std::sync::atomic::AtomicU8);

    impl RandomSource for CountingRandom {
        fn fill(&self, buf: &mut [u8]) -> Result<(), RandomError> {
            let next = self.0.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            buf.fill(next);
            Ok(())
        }
    }

    fn secs(value: u64) -> Duration {
        Duration::from_secs(value)
    }

    fn id(n: usize) -> TransferId {
        TransferId::parse(&format!("{n:032x}")).unwrap()
    }

    fn import(path: &str, armed: bool) -> NewTransfer {
        NewTransfer {
            direction: TransferDirection::Import,
            armed,
            bytes_total: 0,
            entry: None,
            request_path: path.to_owned(),
            destination: path.to_owned(),
        }
    }

    fn done(bytes: u64) -> DoneOutcome {
        DoneOutcome {
            entry: None,
            sha256: "ab".repeat(32),
            bytes,
            part_etags: Vec::new(),
        }
    }

    fn cancelled() -> TransferFailure {
        TransferFailure::of(FailureReason::Cancelled)
    }

    #[test]
    fn ids_are_32_lowercase_hex_and_anything_else_is_unknown() {
        let random = CountingRandom(std::sync::atomic::AtomicU8::new(0xab));
        let generated = TransferId::generate(&random).unwrap();
        assert_eq!(generated.as_str(), "ab".repeat(16));
        assert_eq!(TransferId::parse(generated.as_str()), Some(generated));
        for bad in [
            "",
            "AB".repeat(16).as_str(),
            &"a".repeat(31),
            &"g".repeat(32),
        ] {
            assert_eq!(TransferId::parse(bad), None, "{bad}");
        }
        let mut registry = TransferRegistry::default();
        assert_eq!(registry.lookup("", secs(0)), Err(RegistryError::Unknown));
        assert_eq!(
            registry.lookup(&"0".repeat(32), secs(0)),
            Err(RegistryError::Unknown)
        );
        assert_eq!(
            registry.cancel("", cancelled(), secs(0)),
            Err(RegistryError::Unknown)
        );
    }

    #[test]
    fn the_seventeenth_active_transfer_is_refused_until_one_finishes() {
        let mut registry = TransferRegistry::default();
        for n in 0..TRANSFER_MAX_ACTIVE {
            registry.admit(id(n), import("/a", false), secs(0)).unwrap();
        }
        assert_eq!(
            registry.admit(id(99), import("/a", false), secs(0)).err(),
            Some(RegistryError::Full)
        );
        registry
            .finish_failed(&id(0), TransferFailure::of(FailureReason::Expired), secs(1))
            .unwrap();
        assert!(registry.admit(id(99), import("/a", false), secs(1)).is_ok());
        assert_eq!(registry.active_count(), TRANSFER_MAX_ACTIVE);
        assert_eq!(
            registry.admit(id(99), import("/a", false), secs(1)).err(),
            Some(RegistryError::Full)
        );
    }

    #[test]
    fn at_most_two_transfers_move_bytes_at_once() {
        let mut registry = TransferRegistry::default();
        for n in 0..3 {
            registry.admit(id(n), import("/a", false), secs(0)).unwrap();
        }
        registry.start_running(&id(0), secs(0)).unwrap();
        registry.start_running(&id(1), secs(0)).unwrap();
        assert_eq!(
            registry.start_running(&id(2), secs(0)).err(),
            Some(RegistryError::RunningFull { max: 2 })
        );
        registry.requeue(&id(1), secs(1)).unwrap();
        assert_eq!(registry.running_count(), 1);
        assert_eq!(
            registry.start_running(&id(2), secs(1)).unwrap().phase,
            TransferPhase::Running
        );
    }

    #[test]
    fn illegal_transitions_leave_the_record_unchanged() {
        let mut registry = TransferRegistry::default();
        registry.admit(id(1), import("/a", false), secs(0)).unwrap();
        assert_eq!(
            registry.finish_done(&id(1), done(1), secs(0)).err(),
            Some(RegistryError::IllegalTransition {
                from: TransferPhase::Waiting,
                to: TransferPhase::Done
            })
        );
        assert_eq!(
            registry.requeue(&id(1), secs(0)).err(),
            Some(RegistryError::IllegalTransition {
                from: TransferPhase::Waiting,
                to: TransferPhase::Waiting
            })
        );
        registry.start_running(&id(1), secs(0)).unwrap();
        registry.finish_done(&id(1), done(7), secs(2)).unwrap();
        assert!(registry.start_running(&id(1), secs(3)).is_err());
        assert!(
            registry
                .finish_failed(&id(1), cancelled(), secs(3))
                .is_err()
        );
        let snapshot = registry.snapshot(&id(1), secs(9)).unwrap();
        assert_eq!(snapshot.phase, TransferPhase::Done);
        assert_eq!(snapshot.bytes_done, 7);
        assert_eq!(snapshot.duration, secs(2), "frozen at finish");
        assert_eq!(snapshot.failure, None);
    }

    #[test]
    fn a_waiting_import_may_fail_without_running() {
        let mut registry = TransferRegistry::default();
        registry.admit(id(1), import("/a", true), secs(0)).unwrap();
        let failed = registry
            .finish_failed(&id(1), TransferFailure::of(FailureReason::Expired), secs(5))
            .unwrap();
        assert_eq!(failed.phase, TransferPhase::Failed);
        assert_eq!(
            failed.failure.map(|f| f.reason),
            Some(FailureReason::Expired)
        );
        assert_eq!(registry.armed_count(), 0);
    }

    #[test]
    fn cancel_is_idempotent_and_never_touches_a_finished_record() {
        let mut registry = TransferRegistry::default();
        registry.admit(id(1), import("/a", true), secs(0)).unwrap();
        registry.admit(id(2), import("/b", false), secs(0)).unwrap();
        registry.start_running(&id(2), secs(0)).unwrap();
        registry.finish_done(&id(2), done(3), secs(1)).unwrap();
        let first = registry
            .cancel(id(1).as_str(), cancelled(), secs(2))
            .unwrap();
        let CancelOutcome::Cancelled(snapshot) = first else {
            panic!("expected a cancellation");
        };
        assert_eq!(snapshot.phase, TransferPhase::Cancelled);
        assert!(matches!(
            registry.cancel(id(1).as_str(), cancelled(), secs(3)),
            Ok(CancelOutcome::AlreadyFinished(_))
        ));
        let finished = registry
            .cancel(id(2).as_str(), cancelled(), secs(3))
            .unwrap();
        let CancelOutcome::AlreadyFinished(snapshot) = finished else {
            panic!("a finished record is never cancelled");
        };
        assert_eq!(snapshot.phase, TransferPhase::Done);
        assert_eq!(snapshot.failure, None);
    }

    #[test]
    fn finished_records_are_kept_thirty_minutes() {
        let mut registry = TransferRegistry::default();
        registry.admit(id(1), import("/a", false), secs(0)).unwrap();
        registry.start_running(&id(1), secs(0)).unwrap();
        registry.finish_done(&id(1), done(1), secs(10)).unwrap();
        assert!(registry.snapshot(&id(1), secs(10 + 1_799)).is_ok());
        assert_eq!(
            registry.snapshot(&id(1), secs(10 + 1_800)).err(),
            Some(RegistryError::Unknown)
        );
        assert!(registry.is_empty());
    }

    #[test]
    fn beyond_sixty_four_finished_records_the_oldest_go_first() {
        let mut registry = TransferRegistry::default();
        let total = TRANSFER_RETAINED_MAX + 2;
        for n in 0..total {
            let at = secs(u64::try_from(n).unwrap());
            registry.admit(id(n), import("/a", false), at).unwrap();
            registry
                .finish_failed(&id(n), TransferFailure::of(FailureReason::Expired), at)
                .unwrap();
        }
        let now = secs(u64::try_from(total).unwrap());
        assert_eq!(
            registry.snapshot(&id(0), now).err(),
            Some(RegistryError::Unknown)
        );
        assert_eq!(
            registry.snapshot(&id(1), now).err(),
            Some(RegistryError::Unknown)
        );
        assert!(registry.snapshot(&id(2), now).is_ok());
        assert_eq!(registry.len(), TRANSFER_RETAINED_MAX);
    }

    #[test]
    fn active_records_are_never_evicted() {
        let limits = RegistryLimits {
            retained_max: 0,
            retained: secs(1),
            ..RegistryLimits::default()
        };
        let mut registry = TransferRegistry::new(limits);
        registry.admit(id(1), import("/a", true), secs(0)).unwrap();
        assert!(registry.snapshot(&id(1), secs(100_000)).is_ok());
    }

    #[test]
    fn requeue_resets_progress_and_probes_accumulate() {
        let mut registry = TransferRegistry::default();
        registry.admit(id(1), import("/a", true), secs(0)).unwrap();
        registry.record_probe(&id(1), false, secs(1)).unwrap();
        let seen = registry.record_probe(&id(1), true, secs(2)).unwrap();
        assert_eq!(seen.probes, 2);
        assert!(seen.object_seen);
        registry.start_running(&id(1), secs(2)).unwrap();
        registry.set_total(&id(1), 100, secs(2)).unwrap();
        registry.set_progress(&id(1), 40, secs(3)).unwrap();
        let requeued = registry.requeue(&id(1), secs(4)).unwrap();
        assert_eq!(requeued.phase, TransferPhase::Waiting);
        assert_eq!(requeued.bytes_done, 0);
        assert_eq!(requeued.bytes_total, 100);
        assert!(
            registry
                .record_probe(&id(1), false, secs(5))
                .unwrap()
                .object_seen
        );
    }

    #[test]
    fn armed_tickets_are_the_active_waiting_imports() {
        let mut registry = TransferRegistry::default();
        registry
            .admit(id(1), import("/home/user/a", true), secs(0))
            .unwrap();
        registry
            .admit(id(2), import("/home/user/b", false), secs(0))
            .unwrap();
        registry
            .admit(id(3), import("/home/user/c", true), secs(0))
            .unwrap();
        registry
            .finish_failed(&id(3), TransferFailure::of(FailureReason::Expired), secs(1))
            .unwrap();
        assert_eq!(registry.armed_count(), 1);
        let tickets = registry.armed_tickets();
        assert_eq!(tickets.len(), 1);
        assert_eq!(tickets[0].id, id(1));
        assert_eq!(tickets[0].destination, "/home/user/a");
        let cancelled_ids = registry.cancel_all(cancelled(), secs(2));
        assert_eq!(cancelled_ids, vec![id(1), id(2)]);
        assert_eq!(registry.active_count(), 0);
    }

    #[test]
    fn snapshots_never_show_the_entry_path_in_debug() {
        let mut registry = TransferRegistry::default();
        let snapshot = registry
            .admit(id(1), import("/home/user/secret.txt", true), secs(0))
            .unwrap();
        assert!(!format!("{snapshot:?}").contains("secret"));
        assert_eq!(TransferPhase::Cancelled.to_string(), "cancelled");
        assert!(TransferPhase::Failed.is_terminal());
        assert!(TransferPhase::Waiting.is_active());
    }
}
