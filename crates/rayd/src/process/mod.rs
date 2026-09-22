//! Process application layer of `rayd`: the manager that runs pumps, stdin,
//! timeouts and reaping over the `rayd-core` registry, the runtime pieces
//! it shares with the PTY manager, the subscriber channel with its stall
//! rule, and the child I/O contract of the spawner.

pub mod child;
pub mod manager;
pub mod runtime;
pub mod subscriber;

pub use child::{ChildIo, ChildReader, ChildWriter, Spawner, WaitFuture};
pub use manager::{
    ManagerSettings, OUTPUT_CHUNK_BYTES, ProcessManager, platform_manager, shared_registry,
    shared_registry_with_budget,
};
pub use runtime::{ProcessControl, SharedRegistry};
pub use subscriber::{
    DEFAULT_STALL_TIMEOUT, Delivery, SUBSCRIBER_CHANNEL_CAPACITY, SubscriberReceiver,
    SubscriberSink, SubscriberStream,
};
