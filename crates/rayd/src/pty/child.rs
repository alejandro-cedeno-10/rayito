//! What the PTY manager needs from an opened terminal beyond the domain
//! port: the master side as async reader and writer, a resize handle and
//! the reaper of the shell. Kept in the adapter crate because these are
//! tokio I/O types.

use std::io;
use std::sync::Arc;

use rayd_core::pty::{PtyBackend, PtyChild, PtySize};

use crate::process::child::{ChildReader, ChildWriter, WaitFuture};

/// `ioctl(TIOCSWINSZ)` on the master; the kernel signals the foreground
/// group with `SIGWINCH` by itself.
pub trait PtyResizer: Send + Sync {
    fn resize(&self, size: PtySize) -> io::Result<()>;
}

pub trait PtyIo: PtyChild + 'static {
    fn take_reader(&mut self) -> Option<ChildReader>;
    fn take_writer(&mut self) -> Option<ChildWriter>;
    fn resize_handle(&self) -> Arc<dyn PtyResizer>;
    fn wait(&mut self) -> WaitFuture<'_>;
}

/// The bound `PtyManager` needs: the domain port whose terminals also
/// expose their I/O.
pub trait PtySpawner: PtyBackend<Pty: PtyIo> + Send + Sync + 'static {}

impl<T> PtySpawner for T where T: PtyBackend<Pty: PtyIo> + Send + Sync + 'static {}
