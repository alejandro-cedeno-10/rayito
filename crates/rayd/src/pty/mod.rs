//! PTY application layer of `rayd`: the manager that opens terminals over
//! the platform backend, pumps the master side into the shared process
//! registry and serves input, resize and kill, plus the terminal I/O
//! contract of the backend.

pub mod child;
pub mod manager;

pub use child::{PtyIo, PtyResizer, PtySpawner};
pub use manager::{
    PTY_DEVICE_PATH, PtyManager, PtySettings, platform_pty_manager, pty_devices_present,
};
