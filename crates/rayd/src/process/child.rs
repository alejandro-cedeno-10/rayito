//! What the pumps need from a spawned child beyond the domain port: its
//! pipes and its reaper. Kept in the adapter crate because these are tokio
//! I/O types.

use std::future::Future;
use std::io;
use std::pin::Pin;

use rayd_core::process::{ProcessSpawner, SpawnedChild, WaitOutcome};
use tokio::io::{AsyncRead, AsyncWrite};

pub type ChildReader = Box<dyn AsyncRead + Send + Unpin>;
pub type ChildWriter = Box<dyn AsyncWrite + Send + Unpin>;
pub type WaitFuture<'a> = Pin<Box<dyn Future<Output = io::Result<WaitOutcome>> + Send + 'a>>;

pub trait ChildIo: SpawnedChild + 'static {
    fn take_stdin(&mut self) -> Option<ChildWriter>;
    fn take_stdout(&mut self) -> Option<ChildReader>;
    fn take_stderr(&mut self) -> Option<ChildReader>;
    fn wait(&mut self) -> WaitFuture<'_>;
}

/// The bound `ProcessManager` needs: the domain port whose children also
/// expose their I/O.
pub trait Spawner: ProcessSpawner<Child: ChildIo> + Send + Sync + 'static {}

impl<T> Spawner for T where T: ProcessSpawner<Child: ChildIo> + Send + Sync + 'static {}
