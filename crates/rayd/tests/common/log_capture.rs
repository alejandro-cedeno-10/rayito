//! Every JSON log line of a test binary, so the logging allowlist can be
//! asserted on what `rayd` actually writes. Portable on purpose: `m1_hello`
//! runs on the Windows host too, so this file is not behind `cfg(unix)`.
#![allow(dead_code, clippy::unwrap_used)]

use std::io;
use std::sync::{Arc, Mutex, OnceLock};

use tracing_subscriber::fmt::MakeWriter;

#[derive(Clone, Default)]
pub struct LogCapture {
    lines: Arc<Mutex<Vec<u8>>>,
}

impl LogCapture {
    pub fn text(&self) -> String {
        String::from_utf8_lossy(&self.lines.lock().unwrap()).into_owned()
    }
}

impl io::Write for LogCapture {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.lines.lock().unwrap().extend_from_slice(buf);
        Ok(buf.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

impl<'a> MakeWriter<'a> for LogCapture {
    type Writer = LogCapture;

    fn make_writer(&'a self) -> Self::Writer {
        self.clone()
    }
}

/// Installs the capturing subscriber once per test binary; it must run
/// before anything calls `rayd::logging::init()` or the stderr writer wins.
pub fn log_capture() -> LogCapture {
    static CAPTURE: OnceLock<LogCapture> = OnceLock::new();
    CAPTURE
        .get_or_init(|| {
            let capture = LogCapture::default();
            let _ = tracing_subscriber::fmt()
                .json()
                .with_writer(capture.clone())
                .with_target(false)
                .try_init();
            capture
        })
        .clone()
}
