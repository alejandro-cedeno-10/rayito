//! Entry point: parses the flags, reads the guest's capabilities (and
//! installs the IMDS block when `CAP_NET_ADMIN` allows it, which also
//! decides whether the egress manager can enforce, ADR-012), wires the
//! domain, the process and PTY managers over one shared registry and one
//! shared output budget, the code manager with its kernel sidecar, the
//! persistence manager over the S3 store (execution role by `IMDSv2`) and
//! the tar archiver, the presigned-transfer manager over its credential-free
//! HTTPS client (ADR-010), the suspend broadcast, the metrics probe and the 5 s
//! metrics sampler with its history ring and the logical deadline's watcher
//! thread to the gRPC and hooks listeners on `0.0.0.0`, and stops both on
//! SIGTERM, Ctrl-C, the `/terminate` hook or a kill-mode deadline, which
//! exits with code 124 (ADR-011).

use std::collections::BTreeMap;
use std::path::Path;
use std::process::ExitCode;
use std::sync::Arc;

use anyhow::Context;
use rayd::adapters::{
    CredentialsSource, HyperSignedHttp, IdentitySwitch, ImdsBlock, ImdsState, OsRandomSource,
    PlatformMetricsProbe, S3ObjectStore, SpawnPlatform, USER_PROBE_CODE, USER_PROBE_PROGRAM,
    UserConnectProbe, detect_guest_capabilities, detect_spawn_platform, inherited_nofile_limits,
    install_imds_block, prepare_socket_root,
};
use rayd::code::{CodeSettings, platform_code_manager, sidecar_identity};
use rayd::filesystem::FilesystemManager;
use rayd::filesystem::{FilesystemSettings, platform_filesystem_manager};
use rayd::grpc::{PlatformProcessManager, Services, StreamSettings, TransferServices};
use rayd::hooks::HookServices;
use rayd::lifecycle::{
    DEFAULT_REAPER_INTERVAL, ExitParts, ExitReason, ExitTerminator, Reaper, StreamCloser,
    SuspendSignal, TimeoutWatcher, spawn_metrics_sampler, spawn_reaper, spawn_timeout_watcher,
};
use rayd::network::NetworkManager;
use rayd::persistence::platform_persistence_manager;
use rayd::process::{ManagerSettings, platform_manager, shared_registry_with_budget};
use rayd::pty::{PtySettings, platform_pty_manager};
use rayd::transfer::{TransferManager, TransferSettings};
use rayd_core::clock::SystemClock;
use rayd_core::code::SidecarConfig;
use rayd_core::filesystem::DenyList;
use rayd_core::metrics::MetricsProbe;
use rayd_core::metrics_history::{HISTORY_SAMPLE_INTERVAL, MetricsHistory};
use rayd_core::process::identity::ALLOW_ROOT_ENV;
use rayd_core::process::{
    OutputBudget, ProcessConfigInfo, ProcessEvent, RegistryLimits, SpawnInput, UserPolicy,
};
use rayd_core::sandbox_timeout::SelfTerminator;
use rayd_core::session::SandboxSession;
use tokio::net::TcpListener;
use tokio_stream::StreamExt;
use tokio_util::sync::CancellationToken;
use tonic::transport::server::TcpIncoming;

#[global_allocator]
static GLOBAL: mimalloc::MiMalloc = mimalloc::MiMalloc;

const DEFAULT_GRPC_PORT: u16 = 8080;
const DEFAULT_HOOKS_PORT: u16 = 9000;
const DEFAULT_SIDECAR_CMD: &str = "python3 -m rayito_kernel_sidecar";
const DEFAULT_SIDECAR_ROOT: &str = "/opt/rayito/sidecar";
const DEFAULT_SOCKET_ROOT: &str = "/run/rayito/k";
/// Wall-clock bound on the uid-1000 IMDS probe (the connect itself gives
/// up after 2 s).
const USER_PROBE_TIMEOUT_MS: u64 = 4_000;
/// The platform injects the region (`AWS_API_NOTES.md` §9); it is the
/// default bucket region of `Checkpoint`/`Restore`.
const REGION_ENV: &str = "AWS_REGION";
const USAGE: &str = "usage: rayd [--grpc-port <port>] [--hooks-port <port>] \
[--sidecar-cmd \"<program> <args...>\"] [--sidecar-root <dir>] [--socket-root <dir>] [--no-sidecar] \
[--persistence-credentials imds|default]";

#[derive(Debug, Clone, PartialEq, Eq)]
struct Args {
    grpc_port: u16,
    hooks_port: u16,
    sidecar_cmd: Vec<String>,
    sidecar_root: String,
    socket_root: String,
    no_sidecar: bool,
    persistence_credentials: CredentialsSource,
}

impl Args {
    fn sidecar_config(&self) -> Option<SidecarConfig> {
        (!self.no_sidecar).then(|| {
            SidecarConfig::new(
                self.sidecar_cmd.clone(),
                &self.sidecar_root,
                &self.socket_root,
            )
        })
    }
}

fn parse_args(raw: impl IntoIterator<Item = String>) -> anyhow::Result<Args> {
    let mut args = Args {
        grpc_port: DEFAULT_GRPC_PORT,
        hooks_port: DEFAULT_HOOKS_PORT,
        sidecar_cmd: split_command(DEFAULT_SIDECAR_CMD),
        sidecar_root: DEFAULT_SIDECAR_ROOT.to_owned(),
        socket_root: DEFAULT_SOCKET_ROOT.to_owned(),
        no_sidecar: false,
        persistence_credentials: CredentialsSource::Imds,
    };
    let mut raw = raw.into_iter();
    while let Some(flag) = raw.next() {
        if flag == "--no-sidecar" {
            args.no_sidecar = true;
            continue;
        }
        let value = raw
            .next()
            .with_context(|| format!("{flag} needs a value\n{USAGE}"))?;
        match flag.as_str() {
            "--grpc-port" => args.grpc_port = parse_port(&flag, &value)?,
            "--hooks-port" => args.hooks_port = parse_port(&flag, &value)?,
            "--sidecar-cmd" => {
                args.sidecar_cmd = split_command(&value);
                if args.sidecar_cmd.is_empty() {
                    anyhow::bail!("{flag}: the command is empty\n{USAGE}");
                }
            }
            "--sidecar-root" => args.sidecar_root = value,
            "--socket-root" => args.socket_root = value,
            "--persistence-credentials" => {
                args.persistence_credentials = CredentialsSource::parse(&value)
                    .with_context(|| format!("{flag}: `{value}` is not imds|default\n{USAGE}"))?;
            }
            _ => anyhow::bail!("unknown flag {flag}\n{USAGE}"),
        }
    }
    Ok(args)
}

fn parse_port(flag: &str, value: &str) -> anyhow::Result<u16> {
    value
        .parse()
        .with_context(|| format!("{flag}: `{value}` is not a port"))
}

fn split_command(raw: &str) -> Vec<String> {
    raw.split_whitespace().map(str::to_owned).collect()
}

#[tokio::main(flavor = "multi_thread")]
async fn main() -> anyhow::Result<ExitCode> {
    let args = parse_args(std::env::args().skip(1))?;
    rayd::logging::init().map_err(|error| anyhow::anyhow!("tracing init failed: {error}"))?;

    let session = Arc::new(SandboxSession::new(
        Arc::new(SystemClock::new()),
        rayd::AGENT_VERSION,
    ));
    let shutdown = CancellationToken::new();
    spawn_stop_signal_handler(shutdown.clone());
    let imds = Arc::new(ImdsState::default());
    let net_admin = install_imds_block_if_allowed(&imds).await;
    let network = NetworkManager::platform(session.clone(), net_admin);
    let platform = detect_spawn_platform();
    log_spawn_platform(&platform);
    let policy = UserPolicy::from_env_flag(std::env::var(ALLOW_ROOT_ENV).ok().as_deref());
    let files = filesystem_manager(&session, &platform, policy);
    let output_budget = OutputBudget::default();
    let code = code_manager(&session, &platform, policy, &args, &output_budget)?;
    let _supervisor = code.spawn_supervisor();
    let registry = shared_registry_with_budget(RegistryLimits::default(), output_budget);
    let ptys = platform_pty_manager(
        session.clone(),
        &platform,
        policy,
        registry.clone(),
        PtySettings::default(),
    );
    tracing::info!(pty_devices = ptys.pty_devices(), "pty backend configured");
    let persistence = persistence_manager(&session, &platform, policy, &args).await;
    let processes = platform_manager(
        session.clone(),
        platform,
        policy,
        registry,
        ManagerSettings::default(),
    );
    let reapers: Vec<Arc<dyn Reaper>> = vec![processes.clone(), code.clone()];
    let _reaper = spawn_reaper(DEFAULT_REAPER_INTERVAL, reapers);
    let metrics: Arc<dyn MetricsProbe> = Arc::new(PlatformMetricsProbe::default());
    let metrics_history = Arc::new(MetricsHistory::default());
    let _sampler = spawn_metrics_sampler(
        session.clone(),
        metrics.clone(),
        metrics_history.clone(),
        HISTORY_SAMPLE_INTERVAL,
    );
    let suspend = Arc::new(SuspendSignal::new());
    let transfers = transfer_services(&session, &files, &suspend);
    let (exit_reason, timeout) = deadline(&session, &shutdown, &suspend, &processes, &code)?;

    let (grpc_listener, hooks_listener) = bind_listeners(&args).await?;

    let user_probe = user_connect_probe(processes.clone());
    let grpc = rayd::grpc::router_with_transfers(
        Services {
            session: session.clone(),
            processes,
            ptys,
            files,
            code: code.clone(),
            metrics,
            metrics_history,
            suspend: suspend.clone(),
            imds: imds.clone(),
            persistence,
            timeout: timeout.clone(),
            network: network.clone(),
        },
        StreamSettings::default(),
        transfers,
    )
    .serve_with_incoming_shutdown(
        TcpIncoming::from(grpc_listener).with_nodelay(Some(true)),
        shutdown.clone().cancelled_owned(),
    );
    let hooks = axum::serve(
        hooks_listener,
        rayd::hooks::router_with(HookServices {
            session,
            code,
            suspend,
            shutdown: shutdown.clone(),
            imds,
            user_probe: Some(user_probe),
            timeout,
            network,
        }),
    )
    .with_graceful_shutdown(shutdown.clone().cancelled_owned());

    tokio::try_join!(
        async { grpc.await.context("gRPC listener failed") },
        async { hooks.await.context("hooks listener failed") },
    )?;
    tracing::info!(reason = exit_reason.as_str(), "rayd stopped");
    Ok(ExitCode::from(exit_reason.exit_code()))
}

/// The code manager over the kernel sidecar (none with `--no-sidecar`),
/// with the socket root prepared for the sidecar identity; one log line
/// says how code execution is configured.
fn code_manager(
    session: &Arc<SandboxSession>,
    platform: &SpawnPlatform,
    policy: UserPolicy,
    args: &Args,
    output_budget: &OutputBudget,
) -> anyhow::Result<Arc<rayd::code::CodeManager>> {
    let code_settings = CodeSettings {
        sidecar: args.sidecar_config(),
        output_budget: output_budget.clone(),
        ..CodeSettings::default()
    };
    if code_settings.sidecar.is_some() {
        let identity = sidecar_identity(session, platform.lookup.as_ref(), policy)
            .map_err(|error| anyhow::anyhow!("resolving the sidecar identity: {error}"))?;
        prepare_socket_root(Path::new(&args.socket_root), &identity)
            .with_context(|| format!("preparing the socket root {}", args.socket_root))?;
    }
    let code = platform_code_manager(session.clone(), platform, policy, code_settings)
        .map_err(|error| anyhow::anyhow!("building the code manager: {error}"))?;
    tracing::info!(
        sidecar = !args.no_sidecar,
        sidecar_root = %args.sidecar_root,
        socket_root = %args.socket_root,
        "code execution configured"
    );
    Ok(code)
}

/// The presigned-transfer manager (ADR-010) over the hyper client with the
/// OS trust store; without a trust store the transfer RPCs answer
/// `UNIMPLEMENTED` and the barrier never waits. One log line says which.
fn transfer_services(
    session: &Arc<SandboxSession>,
    files: &Arc<FilesystemManager>,
    suspend: &Arc<SuspendSignal>,
) -> TransferServices {
    match HyperSignedHttp::new() {
        Ok(http) => {
            let manager = TransferManager::new(
                session.clone(),
                files.clone(),
                suspend.clone(),
                http,
                Arc::new(OsRandomSource),
                TransferSettings::default(),
            );
            tracing::info!(transfers = true, "transfers configured");
            TransferServices {
                barrier: manager.barrier(),
                backend: manager,
            }
        }
        Err(error) => {
            tracing::warn!(transfers = false, reason = %error, "transfers unavailable");
            TransferServices::unavailable()
        }
    }
}

/// The logical deadline (ADR-011): the watcher thread over the real exit
/// sequence, parked until `/run` installs a lifecycle, and the exit reason
/// that sequence records for `main`.
fn deadline(
    session: &Arc<SandboxSession>,
    shutdown: &CancellationToken,
    suspend: &Arc<SuspendSignal>,
    processes: &Arc<PlatformProcessManager>,
    code: &Arc<rayd::code::CodeManager>,
) -> anyhow::Result<(Arc<ExitReason>, Arc<TimeoutWatcher>)> {
    let exit_reason = Arc::new(ExitReason::default());
    let terminator: Arc<dyn SelfTerminator> = Arc::new(ExitTerminator::new(ExitParts {
        runtime: tokio::runtime::Handle::current(),
        shutdown: shutdown.clone(),
        suspend: suspend.clone(),
        processes: processes.clone(),
        code: code.clone(),
        reason: exit_reason.clone(),
        settings: session.settings().timeout,
    }));
    let closer = StreamCloser::new(code.clone(), suspend.clone());
    let watcher = spawn_timeout_watcher(session.clone(), terminator, closer)
        .context("starting the timeout watcher")?;
    Ok((exit_reason, watcher))
}

/// The filesystem manager over the default deny list plus this binary's
/// own path; one log line says how many prefixes are denied.
fn filesystem_manager(
    session: &Arc<SandboxSession>,
    platform: &SpawnPlatform,
    policy: UserPolicy,
) -> Arc<rayd::filesystem::FilesystemManager> {
    let deny = deny_list();
    tracing::info!(
        denied_prefixes = deny.prefixes().len(),
        "filesystem policy loaded"
    );
    platform_filesystem_manager(
        session.clone(),
        platform,
        policy,
        deny,
        FilesystemSettings::default(),
    )
}

/// The S3 store (credentials per the flag, default region from
/// `AWS_REGION`) and the tar archiver behind `Checkpoint`/`Restore`; one
/// log line says which credentials and whether a region is known.
async fn persistence_manager(
    session: &Arc<SandboxSession>,
    platform: &SpawnPlatform,
    policy: UserPolicy,
    args: &Args,
) -> Arc<rayd::persistence::PlatformPersistenceManager> {
    let region = std::env::var(REGION_ENV)
        .ok()
        .filter(|region| !region.is_empty());
    let store = Arc::new(S3ObjectStore::new(args.persistence_credentials, region.clone()).await);
    tracing::info!(
        credentials = args.persistence_credentials.as_str(),
        region_known = region.is_some(),
        "persistence configured"
    );
    platform_persistence_manager(session.clone(), platform, policy, store, region)
}

/// Both listeners on `0.0.0.0`, logged once they are bound.
async fn bind_listeners(args: &Args) -> anyhow::Result<(TcpListener, TcpListener)> {
    let grpc_listener = TcpListener::bind(("0.0.0.0", args.grpc_port))
        .await
        .with_context(|| format!("binding gRPC port {}", args.grpc_port))?;
    let hooks_listener = TcpListener::bind(("0.0.0.0", args.hooks_port))
        .await
        .with_context(|| format!("binding hooks port {}", args.hooks_port))?;
    tracing::info!(
        grpc_port = args.grpc_port,
        hooks_port = args.hooks_port,
        version = rayd::AGENT_VERSION,
        "rayd listening"
    );
    Ok((grpc_listener, hooks_listener))
}

/// One `capabilities` line per boot with the facts the hardening depends
/// on; with `CAP_NET_ADMIN` the IMDS policy route goes in before the
/// listeners start (so it is part of the memory snapshot), otherwise
/// `imds_block_unavailable` and `rayd` keeps serving (fail-open). Returns
/// whether the guest grants `CAP_NET_ADMIN`, which also decides whether
/// the egress policy can be enforced (ADR-012).
async fn install_imds_block_if_allowed(imds: &ImdsState) -> bool {
    let guest = detect_guest_capabilities();
    tracing::info!(
        net_admin = guest.net_admin(),
        sys_admin = guest.sys_admin(),
        sys_resource = guest.sys_resource(),
        sys_ptrace = guest.sys_ptrace(),
        cgroup2_root = guest.cgroup2_root,
        "capabilities"
    );
    if !guest.net_admin() {
        tracing::info!(reason = "no CAP_NET_ADMIN", "imds_block_unavailable");
        return false;
    }
    match install_imds_block().await {
        ImdsBlock::Installed => {
            imds.mark_installed();
            tracing::info!(rule_present = true, "imds block installed");
        }
        ImdsBlock::Unavailable { reason } => {
            tracing::warn!(reason = %reason, "imds_block_unavailable");
        }
    }
    true
}

/// The uid-1000 half of the IMDS verification: a `python3` connect run as
/// any sandbox process would be (M2 posture through the process manager),
/// `Some(true)` when it exits 0 (reachable), `Some(false)` on a non-zero
/// exit, `None` when it could not run or answer in time.
fn user_connect_probe(processes: Arc<PlatformProcessManager>) -> UserConnectProbe {
    Arc::new(move || {
        let processes = processes.clone();
        Box::pin(async move { run_user_probe(&processes).await })
    })
}

async fn run_user_probe(processes: &Arc<PlatformProcessManager>) -> Option<bool> {
    let input = SpawnInput {
        config: ProcessConfigInfo {
            cmd: USER_PROBE_PROGRAM.to_owned(),
            args: vec!["-c".to_owned(), USER_PROBE_CODE.to_owned()],
            envs: BTreeMap::new(),
            cwd: None,
        },
        timeout: Some(std::time::Duration::from_millis(USER_PROBE_TIMEOUT_MS)),
        ..SpawnInput::default()
    };
    let (_, mut stream) = match processes.start(input).await {
        Ok(started) => started,
        Err(error) => {
            tracing::warn!(error = %error, "imds user probe could not start");
            return None;
        }
    };
    while let Some(event) = stream.next().await {
        if let ProcessEvent::Ended(end) = event {
            return Some(end.exited && end.exit_code == 0);
        }
    }
    None
}

/// The default deny list plus the canonical path of this executable, so a
/// dev build under `target/` is protected like the installed binary.
fn deny_list() -> DenyList {
    let current_exe = std::env::current_exe()
        .and_then(std::fs::canonicalize)
        .map(|path| path.to_string_lossy().into_owned());
    DenyList::new(current_exe.ok())
}

/// One line at boot says how children will be spawned: whether identities
/// are enforced and what `RLIMIT_NOFILE` this process inherited (a hard limit
/// under 4096 means the child limit gets clamped, M0 Q20).
fn log_spawn_platform(platform: &SpawnPlatform) {
    let (nofile_soft, nofile_hard) = inherited_nofile_limits().unwrap_or((0, 0));
    match platform.identity_switch {
        IdentitySwitch::Enforce => tracing::info!(
            identity_switch = %platform.identity_switch,
            nofile_soft,
            nofile_hard,
            "process spawning configured"
        ),
        IdentitySwitch::KeepCurrent => tracing::warn!(
            identity_switch = %platform.identity_switch,
            nofile_soft,
            nofile_hard,
            "rayd is not root: processes run as its own user and limits are clamped"
        ),
    }
}

fn spawn_stop_signal_handler(shutdown: CancellationToken) {
    tokio::spawn(async move {
        wait_for_stop_signal().await;
        tracing::info!("stop signal received");
        shutdown.cancel();
    });
}

#[cfg(unix)]
async fn wait_for_stop_signal() {
    use tokio::signal::unix::{SignalKind, signal};
    match signal(SignalKind::terminate()) {
        Ok(mut sigterm) => {
            tokio::select! {
                _ = sigterm.recv() => {}
                _ = tokio::signal::ctrl_c() => {}
            }
        }
        Err(error) => {
            tracing::warn!(reason = %error, "SIGTERM handler unavailable; only Ctrl-C stops rayd");
            let _ = tokio::signal::ctrl_c().await;
        }
    }
}

#[cfg(not(unix))]
async fn wait_for_stop_signal() {
    let _ = tokio::signal::ctrl_c().await;
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(flags: &[&str]) -> anyhow::Result<Args> {
        parse_args(flags.iter().map(|flag| (*flag).to_owned()))
    }

    #[test]
    fn defaults_match_the_image_cmd() {
        let args = parse(&[]).unwrap();
        assert_eq!(args.grpc_port, 8080);
        assert_eq!(args.hooks_port, 9000);
        assert_eq!(
            args.sidecar_cmd,
            vec!["python3", "-m", "rayito_kernel_sidecar"]
        );
        assert_eq!(args.sidecar_root, "/opt/rayito/sidecar");
        assert_eq!(args.socket_root, "/run/rayito/k");
        assert!(!args.no_sidecar);
        assert_eq!(args.persistence_credentials, CredentialsSource::Imds);
        let config = args.sidecar_config().unwrap();
        assert_eq!(config.socket_root, "/run/rayito/k");
    }

    #[test]
    fn both_ports_can_be_overridden() {
        let args = parse(&["--grpc-port", "18080", "--hooks-port", "19000"]).unwrap();
        assert_eq!(args.grpc_port, 18080);
        assert_eq!(args.hooks_port, 19000);
    }

    #[test]
    fn sidecar_flags_are_parsed() {
        let args = parse(&[
            "--sidecar-cmd",
            "python3 tests/fixtures/fake_sidecar.py --warmup-ms 500",
            "--sidecar-root",
            "kernel-sidecar",
            "--socket-root",
            "/tmp/k",
        ])
        .unwrap();
        assert_eq!(
            args.sidecar_cmd,
            vec![
                "python3",
                "tests/fixtures/fake_sidecar.py",
                "--warmup-ms",
                "500"
            ]
        );
        assert_eq!(args.sidecar_root, "kernel-sidecar");
        assert_eq!(args.socket_root, "/tmp/k");
        let disabled = parse(&["--no-sidecar", "--grpc-port", "1"]).unwrap();
        assert!(disabled.no_sidecar);
        assert_eq!(disabled.sidecar_config(), None);
        assert_eq!(disabled.grpc_port, 1);
    }

    #[test]
    fn rejects_unknown_flags_bad_ports_and_empty_commands() {
        assert!(parse(&["--port", "1"]).is_err());
        assert!(parse(&["--grpc-port"]).is_err());
        assert!(parse(&["--grpc-port", "70000"]).is_err());
        assert!(parse(&["--sidecar-cmd", "  "]).is_err());
        assert!(parse(&["--persistence-credentials", "env"]).is_err());
    }

    #[test]
    fn persistence_credentials_flag_is_parsed() {
        let args = parse(&["--persistence-credentials", "default"]).unwrap();
        assert_eq!(args.persistence_credentials, CredentialsSource::Default);
    }
}
