//! Progress accounting shared between the blocking archive thread, the
//! async upload/download side and the gRPC sampler (design D7/D8): plain
//! atomics, a snapshot, and the sampling rule (at most one `progress` per
//! tick and only when something changed).

use std::sync::atomic::{AtomicU64, Ordering};

#[derive(Debug, Default)]
pub struct Counters {
    files_done: AtomicU64,
    bytes_read: AtomicU64,
    bytes_uploaded: AtomicU64,
    bytes_downloaded: AtomicU64,
    bytes_written: AtomicU64,
    skipped: AtomicU64,
}

/// One consistent-enough reading of the counters.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Snapshot {
    pub files_done: u64,
    pub bytes_read: u64,
    pub bytes_uploaded: u64,
    pub bytes_downloaded: u64,
    pub bytes_written: u64,
    pub skipped: u64,
}

impl Counters {
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    pub fn add_files_done(&self, n: u64) {
        self.files_done.fetch_add(n, Ordering::Relaxed);
    }

    pub fn add_bytes_read(&self, n: u64) {
        self.bytes_read.fetch_add(n, Ordering::Relaxed);
    }

    pub fn add_bytes_uploaded(&self, n: u64) {
        self.bytes_uploaded.fetch_add(n, Ordering::Relaxed);
    }

    pub fn add_bytes_downloaded(&self, n: u64) {
        self.bytes_downloaded.fetch_add(n, Ordering::Relaxed);
    }

    pub fn add_bytes_written(&self, n: u64) {
        self.bytes_written.fetch_add(n, Ordering::Relaxed);
    }

    pub fn add_skipped(&self, n: u64) {
        self.skipped.fetch_add(n, Ordering::Relaxed);
    }

    #[must_use]
    pub fn snapshot(&self) -> Snapshot {
        Snapshot {
            files_done: self.files_done.load(Ordering::Relaxed),
            bytes_read: self.bytes_read.load(Ordering::Relaxed),
            bytes_uploaded: self.bytes_uploaded.load(Ordering::Relaxed),
            bytes_downloaded: self.bytes_downloaded.load(Ordering::Relaxed),
            bytes_written: self.bytes_written.load(Ordering::Relaxed),
            skipped: self.skipped.load(Ordering::Relaxed),
        }
    }
}

/// Emits a snapshot only when it differs from the last one emitted; the
/// caller decides the tick (1 s in `rayd`).
#[derive(Debug, Default)]
pub struct ProgressSampler {
    last: Option<Snapshot>,
}

impl ProgressSampler {
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    pub fn sample(&mut self, counters: &Counters) -> Option<Snapshot> {
        let current = counters.snapshot();
        if self.last == Some(current) {
            return None;
        }
        self.last = Some(current);
        Some(current)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn counters_accumulate_and_snapshots_are_plain_data() {
        let counters = Counters::new();
        counters.add_files_done(2);
        counters.add_bytes_read(10);
        counters.add_bytes_uploaded(8);
        counters.add_bytes_downloaded(4);
        counters.add_bytes_written(3);
        counters.add_skipped(1);
        counters.add_files_done(1);
        assert_eq!(
            counters.snapshot(),
            Snapshot {
                files_done: 3,
                bytes_read: 10,
                bytes_uploaded: 8,
                bytes_downloaded: 4,
                bytes_written: 3,
                skipped: 1,
            }
        );
    }

    #[test]
    fn sampler_emits_only_on_change() {
        let counters = Counters::new();
        let mut sampler = ProgressSampler::new();
        assert_eq!(sampler.sample(&counters), Some(Snapshot::default()));
        assert_eq!(sampler.sample(&counters), None);
        counters.add_bytes_read(1);
        assert_eq!(sampler.sample(&counters).map(|s| s.bytes_read), Some(1));
        assert_eq!(sampler.sample(&counters), None);
    }
}
