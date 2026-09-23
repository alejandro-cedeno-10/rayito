//! The egress application service (ADR-012, design D5-D7, D11): one
//! mutex serializes the `/run` deny-all, `UpdateNetwork`, the `/resume`
//! re-verification and the recovery; the verified enforcement and the
//! local proxy's variables are published through the session, where
//! `Health` and every spawn read them.
//!
//! Every public operation runs in its own task and the caller only awaits
//! it: a hook budget that expires or an RPC the client abandons never
//! drops a swap half-way. A failure while filling the idle table leaves
//! the old policy in force; any later failure, or a failed verification,
//! runs the deny-all recovery. If the recovery itself fails the
//! enforcement is `None`, which the SDK treats as fatal at `create()`.
//!
//! Logs carry counts, fixed step names and durations only; never an
//! entry, a target, the upstream address or a credential.

use std::net::{IpAddr, Ipv4Addr, SocketAddr};
use std::sync::Arc;
use std::time::{Duration, Instant};

use rayd_core::lifecycle::HookPhase;
use rayd_core::network::probe::{
    blackhole_count, classify_route_get, policy_rule_priorities, samples,
};
use rayd_core::network::route_plan::managed_families;
use rayd_core::network::{
    EgressEnforcement, EgressMode, EgressPolicy, NetworkError, NetworkSnapshot, PlannedStep,
    PolicyInput, RoutePlan, RouteStep, Slot, TargetGuard, egress_proxy_env, plan_recovery,
    plan_swap,
};
use rayd_core::session::SandboxSession;
use tokio::net::TcpStream;

use super::proxy::{LocalProxy, ProxyPolicy, ProxySeams};
use super::upstream::check_upstream;
use crate::adapters::egress_routes::{EgressRoutes, IpEgressRoutes};

/// The proxy self-connect of a proxy-only verification.
const PROXY_PROBE_TIMEOUT: Duration = Duration::from_secs(1);

/// What is installed right now. `slot` holds the rules of `plan`.
struct Installed {
    policy: EgressPolicy,
    plan: Option<RoutePlan>,
    slot: Option<Slot>,
    proxy: Option<LocalProxy>,
}

/// Clears the session's `egress_settling` when the `/run` deny-all task
/// ends, including by a panic, so `Health` never stays not ready.
struct SettleOnDrop(Arc<SandboxSession>);

impl Drop for SettleOnDrop {
    fn drop(&mut self) {
        self.0.egress_settled();
    }
}

/// How an apply failed, which decides whether the recovery runs.
enum ApplyError {
    /// Nothing changed (bad input, no capability, proxy start).
    Refused(NetworkError),
    /// The idle table was flushed again; the old policy is in force.
    RolledBack(&'static str),
    /// Rules may be half-switched: the caller runs the recovery.
    Broken(NetworkError),
}

pub struct NetworkManager {
    session: Arc<SandboxSession>,
    routes: Arc<dyn EgressRoutes>,
    net_admin: bool,
    seams: ProxySeams,
    installed: tokio::sync::Mutex<Installed>,
}

impl NetworkManager {
    #[must_use]
    pub fn new(
        session: Arc<SandboxSession>,
        routes: Arc<dyn EgressRoutes>,
        net_admin: bool,
        seams: ProxySeams,
    ) -> Arc<Self> {
        session.set_egress_enforcement(EgressEnforcement::None);
        Arc::new(Self {
            session,
            routes,
            net_admin,
            seams,
            installed: tokio::sync::Mutex::new(Installed {
                policy: EgressPolicy::default(),
                plan: None,
                slot: None,
                proxy: None,
            }),
        })
    }

    /// The real guest: `ip` for the routes, the system resolver and dialer
    /// for the proxy; `net_admin` comes from the boot's capability mask.
    #[must_use]
    pub fn platform(session: Arc<SandboxSession>, net_admin: bool) -> Arc<Self> {
        Self::new(
            session,
            Arc::new(IpEgressRoutes),
            net_admin,
            ProxySeams::default(),
        )
    }

    /// An image without `CAP_NET_ADMIN` (and the integration tests):
    /// enforcing policies are refused, unrestricted ones are accepted.
    #[must_use]
    pub fn unavailable(session: Arc<SandboxSession>) -> Arc<Self> {
        Self::platform(session, false)
    }

    #[must_use]
    pub fn net_admin(&self) -> bool {
        self.net_admin
    }

    #[must_use]
    pub fn enforcement(&self) -> EgressEnforcement {
        self.session.egress_enforcement()
    }

    /// The phase gate every unary RPC but `Health` shares: only `Running`
    /// and `Resumed` are served; the refusing phase comes back.
    pub fn phase_gate(&self) -> Result<(), HookPhase> {
        self.session.stream_gate()
    }

    /// `/run` with `network.enforce`: local proxy up, deny-all installed and
    /// verified; any failure runs the recovery once. The result is only
    /// logged: the hook answers 200 whatever happens. However the task
    /// ends (returned, panicked or dropped with the runtime) `Health` stops
    /// waiting for it, with whatever enforcement was published.
    pub async fn enforce_deny_all_at_run(self: &Arc<Self>) -> EgressEnforcement {
        let manager = self.clone();
        let settle = SettleOnDrop(self.session.clone());
        let task = tokio::spawn(async move {
            let _settle = settle;
            manager.enforce_deny_all_locked().await
        });
        task.await.unwrap_or(EgressEnforcement::None)
    }

    async fn enforce_deny_all_locked(&self) -> EgressEnforcement {
        let mut installed = self.installed.lock().await;
        let outcome = self.apply(&mut installed, EgressPolicy::deny_all()).await;
        if let Err(error) = outcome {
            let step = match &error {
                ApplyError::Refused(error) | ApplyError::Broken(error) => step_name(error),
                ApplyError::RolledBack(step) => step,
            };
            self.recover(&mut installed).await;
            if self.enforcement() == EgressEnforcement::None {
                tracing::warn!(step, "egress_enforce_failed");
            }
        }
        self.enforcement()
    }

    /// `UpdateNetwork`: replace the whole policy (E2B semantics).
    pub async fn update(
        self: &Arc<Self>,
        input: PolicyInput,
    ) -> Result<NetworkSnapshot, NetworkError> {
        let policy = EgressPolicy::parse(input)?;
        if policy.requires_enforcement() && !self.net_admin {
            return Err(NetworkError::NoNetAdmin);
        }
        if let Some(upstream) = policy.upstream() {
            check_upstream(upstream, &self.seams).await?;
        }
        let manager = self.clone();
        let task = tokio::spawn(async move { manager.update_locked(policy).await });
        task.await
            .unwrap_or(Err(NetworkError::InstallFailed { step: "task" }))
    }

    async fn update_locked(&self, policy: EgressPolicy) -> Result<NetworkSnapshot, NetworkError> {
        let mut installed = self.installed.lock().await;
        if !self.net_admin {
            installed.policy = policy;
            self.session.set_egress_enforcement(EgressEnforcement::None);
            return Ok(self.snapshot_of(&installed));
        }
        match self.apply(&mut installed, policy).await {
            Ok(()) => Ok(self.snapshot_of(&installed)),
            Err(ApplyError::Refused(error)) => Err(error),
            Err(ApplyError::RolledBack(step)) => Err(NetworkError::InstallFailed { step }),
            Err(ApplyError::Broken(error)) => {
                self.recover(&mut installed).await;
                Err(error)
            }
        }
    }

    pub async fn snapshot(&self) -> NetworkSnapshot {
        let installed = self.installed.lock().await;
        self.snapshot_of(&installed)
    }

    fn snapshot_of(&self, installed: &Installed) -> NetworkSnapshot {
        NetworkSnapshot::of(
            &installed.policy,
            self.enforcement(),
            installed.proxy.as_ref().map(LocalProxy::port),
        )
    }

    /// `/resume`: routes and the proxy live in the memory snapshot, so the
    /// expected path is "verified, nothing changed". Otherwise the stored
    /// plan is reinstalled behind an emergency deny-all, and deny-all is
    /// the fallback.
    pub async fn reverify_after_resume(self: &Arc<Self>) -> EgressEnforcement {
        let manager = self.clone();
        let task = tokio::spawn(async move { manager.reverify_locked().await });
        task.await.unwrap_or(EgressEnforcement::None)
    }

    async fn reverify_locked(&self) -> EgressEnforcement {
        let mut installed = self.installed.lock().await;
        if !self.net_admin || (installed.slot.is_none() && installed.proxy.is_none()) {
            return self.enforcement();
        }
        let Ok(local) = self.routes.local_addresses().await else {
            self.recover(&mut installed).await;
            return self.enforcement();
        };
        publish_proxy_policy(&installed, &local);
        if self.verify_and_publish(&installed).await {
            return self.enforcement();
        }
        let plan = installed
            .plan
            .clone()
            .unwrap_or_else(|| RoutePlan::deny_all(self.routes.ipv6_present()));
        let reinstalled = self.run_planned(&plan_recovery(&plan)).await;
        installed.slot = Some(Slot::A);
        installed.plan = Some(plan);
        if reinstalled.is_ok() && self.verify_and_publish(&installed).await {
            tracing::warn!(step = "reinstall", "egress_policy_reinstalled_after_resume");
            return self.enforcement();
        }
        self.recover(&mut installed).await;
        self.enforcement()
    }

    async fn apply(
        &self,
        installed: &mut Installed,
        policy: EgressPolicy,
    ) -> Result<(), ApplyError> {
        let started = Instant::now();
        let ipv6 = self.routes.ipv6_present();
        let plan = RoutePlan::for_policy(&policy, ipv6).map_err(ApplyError::Refused)?;
        let local = self.routes.local_addresses().await.map_err(|_| {
            ApplyError::Refused(NetworkError::InstallFailed {
                step: "local_addresses",
            })
        })?;
        if policy.requires_enforcement() {
            self.ensure_proxy(installed, &local)
                .await
                .map_err(ApplyError::Refused)?;
        }
        let swap = plan_swap(installed.slot, plan.as_ref(), ipv6);
        if let Err(step) = self.run_steps(&swap.fill).await {
            let _ = self.run_steps(&swap.rollback()).await;
            tracing::warn!(step, "egress_update_failed");
            return Err(ApplyError::RolledBack(step));
        }
        let committed = self.run_steps(&swap.commit).await;
        installed.slot = swap.next_slot;
        installed.plan = plan;
        installed.policy = policy;
        publish_proxy_policy(installed, &local);
        if let Err(step) = committed {
            tracing::warn!(step, "egress_update_failed");
            return Err(ApplyError::Broken(NetworkError::InstallFailed { step }));
        }
        if !self.verify_and_publish(installed).await {
            return Err(ApplyError::Broken(NetworkError::VerifyFailed));
        }
        log_applied(installed, started.elapsed());
        Ok(())
    }

    async fn ensure_proxy(
        &self,
        installed: &mut Installed,
        local: &[IpAddr],
    ) -> Result<(), NetworkError> {
        if installed.proxy.is_some() {
            return Ok(());
        }
        let initial = ProxyPolicy {
            policy: installed.policy.clone(),
            guard: TargetGuard::new(local.iter().copied()),
        };
        let proxy = LocalProxy::start(initial, self.seams.clone())
            .await
            .map_err(|_| NetworkError::InstallFailed {
                step: "proxy_start",
            })?;
        self.session.set_egress_env(egress_proxy_env(proxy.port()));
        tracing::info!(port = proxy.port(), "egress_proxy_started");
        installed.proxy = Some(proxy);
        Ok(())
    }

    /// Emergency deny-all, both slots cleared, deny-all in slot `A`,
    /// verified; `None` when any of it fails.
    async fn recover(&self, installed: &mut Installed) {
        let deny_all = RoutePlan::deny_all(self.routes.ipv6_present());
        let recovered = self.run_planned(&plan_recovery(&deny_all)).await;
        installed.policy = EgressPolicy::deny_all();
        installed.plan = Some(deny_all);
        installed.slot = Some(Slot::A);
        let local = self.routes.local_addresses().await.unwrap_or_default();
        publish_proxy_policy(installed, &local);
        if let Err(step) = recovered {
            self.session.set_egress_enforcement(EgressEnforcement::None);
            tracing::error!(step, "egress_recovery_failed");
            return;
        }
        if !self.verify_and_publish(installed).await {
            self.session.set_egress_enforcement(EgressEnforcement::None);
            tracing::error!(step = "verify", "egress_recovery_failed");
        }
    }

    async fn run_steps(&self, steps: &[RouteStep]) -> Result<(), &'static str> {
        for step in steps {
            if self.routes.execute(step).await.is_err() {
                return Err(step.name());
            }
        }
        Ok(())
    }

    async fn run_planned(&self, steps: &[PlannedStep]) -> Result<(), &'static str> {
        for planned in steps {
            let executed = self.routes.execute(&planned.step).await;
            if executed.is_err() && !planned.tolerate_failure {
                return Err(planned.step.name());
            }
        }
        Ok(())
    }

    /// Verifies what `installed` says and publishes the enforcement it
    /// earns (`None` when the check fails).
    async fn verify_and_publish(&self, installed: &Installed) -> bool {
        let verdict = self.verify(installed).await;
        let enforcement = match verdict {
            Ok(_) => EgressEnforcement::verified(installed.policy.mode()),
            Err(_) => EgressEnforcement::None,
        };
        self.session.set_egress_enforcement(enforcement);
        match verdict {
            Ok(samples) => {
                tracing::info!(ok = true, samples, "egress_verify");
                true
            }
            Err(failed_check) => {
                tracing::warn!(ok = false, failed_check, "egress_verify");
                false
            }
        }
    }

    async fn verify(&self, installed: &Installed) -> Result<usize, &'static str> {
        let ipv6 = installed
            .plan
            .as_ref()
            .map_or_else(|| self.routes.ipv6_present(), RoutePlan::ipv6);
        let expected: Vec<u32> = installed.slot.map(Slot::priority).into_iter().collect();
        for family in managed_families(ipv6) {
            let rules = self
                .routes
                .show_rules(*family)
                .await
                .map_err(|_| "rule_show")?;
            if policy_rule_priorities(&rules) != expected {
                return Err("rule");
            }
        }
        let (Some(plan), Some(slot)) = (&installed.plan, installed.slot) else {
            return Ok(0);
        };
        for family in plan.families() {
            let table = self
                .routes
                .show_table(*family, slot.table())
                .await
                .map_err(|_| "table_show")?;
            if blackhole_count(&table) != plan.prefixes(*family).len() {
                return Err("table");
            }
        }
        let expectations = samples(&installed.policy, plan);
        for (destination, expected) in &expectations {
            let (code, stdout) = self
                .routes
                .route_get(*destination)
                .await
                .map_err(|_| "route_get")?;
            if classify_route_get(code, &stdout) != *expected {
                return Err("sample");
            }
        }
        if installed.policy.mode() == EgressMode::ProxyOnly {
            let port = installed
                .proxy
                .as_ref()
                .map(LocalProxy::port)
                .ok_or("proxy")?;
            let target = SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), port);
            let connected =
                tokio::time::timeout(PROXY_PROBE_TIMEOUT, TcpStream::connect(target)).await;
            if !matches!(connected, Ok(Ok(_))) {
                return Err("proxy");
            }
        }
        Ok(expectations.len())
    }
}

fn publish_proxy_policy(installed: &Installed, local: &[IpAddr]) {
    if let Some(proxy) = &installed.proxy {
        proxy.set_policy(ProxyPolicy {
            policy: installed.policy.clone(),
            guard: TargetGuard::new(local.iter().copied()),
        });
    }
}

fn step_name(error: &NetworkError) -> &'static str {
    match error {
        NetworkError::InstallFailed { step } => step,
        NetworkError::VerifyFailed => "verify",
        NetworkError::NoNetAdmin => "no_net_admin",
        _ => "plan",
    }
}

fn log_applied(installed: &Installed, elapsed: Duration) {
    let policy = &installed.policy;
    let routes = |family| {
        installed
            .plan
            .as_ref()
            .map_or(0, |plan| plan.prefixes(family).len())
    };
    tracing::info!(
        mode = policy.mode().as_str(),
        allow_count = policy.raw_allow().len(),
        deny_count = policy.raw_deny().len(),
        hostname_count = policy.hostname_entries(),
        routes_v4 = routes(rayd_core::network::Family::V4),
        routes_v6 = routes(rayd_core::network::Family::V6),
        proxy_configured = policy.upstream().is_some(),
        duration_ms = u64::try_from(elapsed.as_millis()).unwrap_or(u64::MAX),
        "egress_policy_applied"
    );
}
