//! The operating system's random source, read per call: nothing is seeded
//! at boot, so identifiers generated after `/run` differ between the
//! `MicroVM`s cloned from one snapshot (`AWS_API_NOTES.md` §15).

use rayd_core::code::{RandomError, RandomSource};

#[derive(Debug, Default, Clone, Copy)]
pub struct OsRandomSource;

impl RandomSource for OsRandomSource {
    fn fill(&self, buf: &mut [u8]) -> Result<(), RandomError> {
        getrandom::fill(buf).map_err(|error| RandomError(error.to_string()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn consecutive_reads_differ() {
        let mut first = [0u8; 16];
        let mut second = [0u8; 16];
        OsRandomSource.fill(&mut first).unwrap();
        OsRandomSource.fill(&mut second).unwrap();
        assert_ne!(first, second);
        assert_ne!(first, [0u8; 16]);
    }
}
