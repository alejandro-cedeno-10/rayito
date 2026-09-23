//! gRPC adapter: the seven `rayito.v1` services on one tonic router, behind
//! the access-token layer and, inside it, the logical deadline's gate
//! (ADR-011). Every server stream is wrapped so `/suspend` (or the
//! deadline) closes it in the form its schema allows (design D7).
//! `FilesystemService` accepts gzip requests and sends gzip responses only
//! to calls that opt in (`CompressionOptInLayer`, outermost); its transfer
//! RPCs and the read-after-upload barrier of every service come from
//! `TransferServices` (ADR-010).

mod access_token;
mod client_abort;
mod code;
mod compression;
mod filesystem;
mod health;
mod keepalive;
mod lifecycle;
mod network;
mod persistence;
mod process;
mod pty;
mod reject;
mod timeout_gate;
mod transfer;

use std::sync::Arc;
use std::time::Duration;

use rayd_core::code::{EXECUTE_KEEPALIVE_INTERVAL, KernelStatus};
use rayd_core::metrics::MetricsProbe;
use rayd_core::metrics_history::MetricsHistory;
use rayd_core::session::SandboxSession;
use rayito_proto::v1::code_service_server::CodeServiceServer;
use rayito_proto::v1::filesystem_service_server::FilesystemServiceServer;
use rayito_proto::v1::health_service_server::HealthServiceServer;
use rayito_proto::v1::lifecycle_service_server::LifecycleServiceServer;
use rayito_proto::v1::network_service_server::NetworkServiceServer;
use rayito_proto::v1::process_service_server::ProcessServiceServer;
use rayito_proto::v1::pty_service_server::PtyServiceServer;
use tonic::codec::CompressionEncoding;
use tonic::transport::Server;
use tonic::transport::server::Router;
use tower::layer::util::{Identity, Stack};

pub use access_token::AccessTokenLayer;
pub use client_abort::{ClientAbort, ClientAbortLayer};
pub use code::CodeGrpc;
pub use compression::{COMPRESSION_OPT_IN_HEADER, CompressionOptInLayer};
pub use filesystem::{DEFAULT_WATCH_KEEPALIVE_INTERVAL, FilesystemGrpc};
use health::HealthGrpc;
pub use keepalive::{DEFAULT_KEEPALIVE_INTERVAL, KeepAliveStream};
use lifecycle::LifecycleGrpc;
use network::NetworkGrpc;
pub use persistence::{PersistenceGrpc, status_for as persistence_status_for};
pub use process::ProcessGrpc;
pub use pty::PtyGrpc;
pub use timeout_gate::SandboxTimeoutGateLayer;
pub use transfer::TransferGrpc;

use crate::adapters::{ImdsState, PlatformPtyBackend, PlatformSpawner};
use crate::code::CodeManager;
use crate::filesystem::FilesystemManager;
use crate::lifecycle::{SuspendSignal, TimeoutWatcher};
use crate::network::NetworkManager;
use crate::persistence::PersistenceBackend;
use crate::process::ProcessManager;
use crate::pty::PtyManager;
use crate::transfer::{TransferBackend, TransferBarrier, UnavailableTransfers};

pub const HTTP2_KEEPALIVE_INTERVAL: Duration = Duration::from_secs(30);
pub const HTTP2_KEEPALIVE_TIMEOUT: Duration = Duration::from_secs(10);
pub const MAX_CONCURRENT_STREAMS: u32 = 256;

pub type GrpcRouter = Router<
    Stack<
        ClientAbortLayer,
        Stack<
            SandboxTimeoutGateLayer,
            Stack<AccessTokenLayer, Stack<CompressionOptInLayer, Identity>>,
        >,
    >,
>;
pub type PlatformProcessManager = ProcessManager<PlatformSpawner>;
pub type PlatformPtyManager = PtyManager<PlatformPtyBackend>;

/// Knobs the integration tests shrink; production uses the defaults.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct StreamSettings {
    pub keepalive_interval: Duration,
    pub watch_keepalive_interval: Duration,
    pub execute_keepalive_interval: Duration,
}

impl Default for StreamSettings {
    fn default() -> Self {
        Self {
            keepalive_interval: DEFAULT_KEEPALIVE_INTERVAL,
            watch_keepalive_interval: DEFAULT_WATCH_KEEPALIVE_INTERVAL,
            execute_keepalive_interval: EXECUTE_KEEPALIVE_INTERVAL,
        }
    }
}

/// Everything the five services are built from; `main` and the tests
/// assemble it. `imds` is the IMDS block state `Health` reports
/// (`ImdsState::default()` where no block was ever attempted);
/// `persistence` is the backend of `Checkpoint`/`Restore`
/// (`UnavailablePersistence` where no store is wired); `metrics_history` is
/// the ring `MetricsHistory` serves (`main` feeds it with the sampler, tests
/// seed it directly); `timeout` is the deadline watcher `SetTimeout` wakes
/// (`TimeoutWatcher::detached` where no lifecycle is ever installed);
/// `network` is the egress manager behind `NetworkService`
/// (`NetworkManager::unavailable` where the guest cannot enforce).
pub struct Services {
    pub session: Arc<SandboxSession>,
    pub processes: Arc<PlatformProcessManager>,
    pub ptys: Arc<PlatformPtyManager>,
    pub files: Arc<FilesystemManager>,
    pub code: Arc<CodeManager>,
    pub metrics: Arc<dyn MetricsProbe>,
    pub metrics_history: Arc<MetricsHistory>,
    pub suspend: Arc<SuspendSignal>,
    pub imds: Arc<ImdsState>,
    pub persistence: Arc<dyn PersistenceBackend>,
    pub timeout: Arc<TimeoutWatcher>,
    pub network: Arc<NetworkManager>,
}

/// The transfer backend of `FilesystemService` and the barrier every
/// service consults; `unavailable()` answers `UNIMPLEMENTED` like an agent
/// older than the RPCs and never waits.
#[derive(Clone)]
pub struct TransferServices {
    pub backend: Arc<dyn TransferBackend>,
    pub barrier: TransferBarrier,
}

impl TransferServices {
    #[must_use]
    pub fn unavailable() -> Self {
        Self {
            backend: Arc::new(UnavailableTransfers),
            barrier: TransferBarrier::disabled(),
        }
    }
}

/// The fully configured tonic router. Callers pick the listener: `main` binds
/// `0.0.0.0:<grpc-port>`, tests bind `127.0.0.1:0`.
#[must_use]
pub fn router(services: Services) -> GrpcRouter {
    router_with_settings(services, StreamSettings::default())
}

#[must_use]
pub fn router_with_settings(services: Services, settings: StreamSettings) -> GrpcRouter {
    router_with_transfers(services, settings, TransferServices::unavailable())
}

#[must_use]
pub fn router_with_transfers(
    services: Services,
    settings: StreamSettings,
    transfers: TransferServices,
) -> GrpcRouter {
    let Services {
        session,
        processes,
        ptys,
        files,
        code,
        metrics,
        metrics_history,
        suspend,
        imds,
        persistence,
        timeout,
        network,
    } = services;
    let kernel_status: Arc<dyn KernelStatus> = code.clone();
    let lifecycle = LifecycleGrpc::new(session.clone(), timeout);
    let mut server = Server::builder()
        .tcp_nodelay(true)
        .http2_keepalive_interval(Some(HTTP2_KEEPALIVE_INTERVAL))
        .http2_keepalive_timeout(Some(HTTP2_KEEPALIVE_TIMEOUT))
        .max_concurrent_streams(Some(MAX_CONCURRENT_STREAMS))
        .layer(CompressionOptInLayer)
        .layer(AccessTokenLayer::new(session.clone()))
        .layer(SandboxTimeoutGateLayer::new(session.clone()))
        .layer(ClientAbortLayer);
    server
        .add_service(HealthServiceServer::new(HealthGrpc::new(
            session,
            metrics,
            metrics_history,
            kernel_status,
            imds,
        )))
        .add_service(ProcessServiceServer::new(
            ProcessGrpc::with_keepalive_interval(
                processes,
                suspend.clone(),
                settings.keepalive_interval,
            )
            .with_barrier(transfers.barrier.clone()),
        ))
        .add_service(
            FilesystemServiceServer::new(
                FilesystemGrpc::with_keepalive_interval(
                    files,
                    suspend.clone(),
                    settings.watch_keepalive_interval,
                )
                .with_persistence(persistence, settings.keepalive_interval)
                .with_transfers(
                    transfers.backend,
                    transfers.barrier.clone(),
                    settings.keepalive_interval,
                ),
            )
            .accept_compressed(CompressionEncoding::Gzip)
            .send_compressed(CompressionEncoding::Gzip),
        )
        .add_service(PtyServiceServer::new(
            PtyGrpc::with_keepalive_interval(ptys, suspend.clone(), settings.keepalive_interval)
                .with_barrier(transfers.barrier.clone()),
        ))
        .add_service(CodeServiceServer::new(
            CodeGrpc::with_keepalive_interval(code, suspend, settings.execute_keepalive_interval)
                .with_barrier(transfers.barrier),
        ))
        .add_service(LifecycleServiceServer::new(lifecycle))
        .add_service(NetworkServiceServer::new(NetworkGrpc::new(network)))
}
