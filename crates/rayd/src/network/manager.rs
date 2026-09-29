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

use rayd_core::lifecycle::{Hook, HookPhase};
use rayd_core::network::probe::{
    VerifyFailure, check_rules, check_sample, check_table, expected_rule_priorities,
    requires_proxy_connect, samples, verification_ipv6,
};
use rayd_core::network::route_plan::managed_families;
use rayd_core::network::{
    DnsGuardStep, EgressEnforcement, EgressPolicy, Installation, NetworkError, NetworkSnapshot,
    PlannedStep, PolicyInput, RESUME_VERIFY_BUDGET, RUN_ENFORCE_BUDGET, RoutePlan, RouteStep,
    TargetGuard, egress_proxy_env, plan_dns_guard_install, plan_dns_guard_remove,
    plan_dns_guard_rollback, plan_recovery, plan_swap,
};
use rayd_core::session::SandboxSession;
use tokio::net::TcpStream;

use super::proxy::{LocalProxy, ProxyPolicy, ProxySeams};
use super::upstream::check_upstream;
use crate::adapters::egress_routes::{EgressRoutes, IpEgressRoutes};

/// The proxy self-connect of a proxy-only verification.
const PROXY_PROBE_TIMEOUT: Duration = Duration::from_secs(1);

/// What is installed right now: the domain state and the running proxy.
struct Installed {
    state: Installation,
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
                state: Installation::default(),
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

    /// The `/run` egress use case (ADR-012): with `network.enforce`,
    /// deny-all is installed and verified before the hook answers, so no
    /// user code runs unprotected, and the kernel rotation that follows
    /// already gets the proxy variables. The work outlives an expired
    /// sub-budget (enforcement stays `None` and `Health` not ready until it
    /// settles); the hook answers 200 either way.
    pub async fn on_run(self: &Arc<Self>) {
        if !self.session.network_enforce() {
            return;
        }
        if !self.net_admin {
            tracing::warn!(reason = "no CAP_NET_ADMIN", "egress_enforce_unavailable");
            self.session.egress_settled();
            return;
        }
        let enforced =
            tokio::time::timeout(RUN_ENFORCE_BUDGET, self.enforce_deny_all_at_run()).await;
        if let Ok(enforcement) = enforced {
            tracing::info!(hook = %Hook::Run, enforcement = enforcement.as_str(), "egress enforced");
        } else {
            tracing::warn!(step = "budget", "egress_enforce_failed");
        }
    }

    /// The `/resume` egress use case: the installed policy is re-verified
    /// synchronously. An expired sub-budget reports `None` (the SDK sees it
    /// in `Health`) and the hook still answers 200: a non-200 hook answer
    /// is never used. The re-verification task keeps running and may still
    /// publish after that `None` (reported, not changed here).
    pub async fn on_resume(self: &Arc<Self>) {
        let verified =
            tokio::time::timeout(RESUME_VERIFY_BUDGET, self.reverify_after_resume()).await;
        if verified.is_err() {
            self.session.set_egress_enforcement(EgressEnforcement::None);
            tracing::warn!(
                budget_ms = u64::try_from(RESUME_VERIFY_BUDGET.as_millis()).unwrap_or(u64::MAX),
                "egress_resume_verify_timeout"
            );
        }
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
            installed.state.set_policy_only(policy);
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
            installed.state.policy(),
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
        if !self.net_admin
            || installed
                .state
                .nothing_to_reverify(installed.proxy.is_some())
        {
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
        let plan = installed.state.reinstall_plan(self.routes.ipv6_present());
        let reinstalled = self.run_planned(&plan_recovery(&plan)).await;
        installed.state.after_reinstall(plan);
        if reinstalled.is_ok() {
            let want = installed.state.wants_dns_guard(self.routes.ipv6_present());
            self.ensure_dns_guard(&mut installed, want).await;
        }
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
        let swap = plan_swap(installed.state.slot(), plan.as_ref(), ipv6);
        if let Err(step) = self.run_steps(&swap.fill).await {
            let _ = self.run_steps(&swap.rollback()).await;
            tracing::warn!(step, "egress_update_failed");
            return Err(ApplyError::RolledBack(step));
        }
        let committed = self.run_steps(&swap.commit).await;
        installed.state.after_swap(policy, plan, swap.next_slot);
        publish_proxy_policy(installed, &local);
        if let Err(step) = committed {
            tracing::warn!(step, "egress_update_failed");
            return Err(ApplyError::Broken(NetworkError::InstallFailed { step }));
        }
        let want_dns_guard = installed.state.wants_dns_guard(ipv6);
        self.ensure_dns_guard(installed, want_dns_guard).await;
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
            policy: installed.state.policy().clone(),
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

    /// Emergency deny-all, both slots cleared, deny-all in `RECOVERY_SLOT`,
    /// verified; `None` when any of it fails.
    async fn recover(&self, installed: &mut Installed) {
        let deny_all = RoutePlan::deny_all(self.routes.ipv6_present());
        let recovered = self.run_planned(&plan_recovery(&deny_all)).await;
        installed.state.after_recovery(deny_all);
        let local = self.routes.local_addresses().await.unwrap_or_default();
        publish_proxy_policy(installed, &local);
        if let Err(step) = recovered {
            self.session.set_egress_enforcement(EgressEnforcement::None);
            tracing::error!(step, "egress_recovery_failed");
            return;
        }
        self.ensure_dns_guard(installed, true).await;
        if !self.verify_and_publish(installed).await {
            self.session.set_egress_enforcement(EgressEnforcement::None);
            tracing::error!(step = "verify", "egress_recovery_failed");
        }
    }

    /// Egress option A (ADR-012 addendum, `SECURITY.md` T17): installs or
    /// removes the DNS block for uid ≥ 1000, best effort. Failures are
    /// logged and leave the installation's `dns_guard` at its previous value (so
    /// the next transition retries); they never fail the caller, matching
    /// the in-guest enforcement's own "best effort" framing (`ip rule
    /// ipproto`/`dport` support, or the underlying routes/proxy, are the
    /// hard boundary, not this hardening on top of it). A failed install is
    /// rolled back to the exact pre-install state; a failed removal is left
    /// as-is, always a safe (if over-blocking) intermediate step.
    async fn ensure_dns_guard(&self, installed: &mut Installed, want: bool) {
        if installed.state.dns_guard() == want {
            return;
        }
        let families = managed_families(self.routes.ipv6_present());
        if want {
            let steps = plan_dns_guard_install(families);
            let mut applied: Vec<DnsGuardStep> = Vec::with_capacity(steps.len());
            for step in &steps {
                if self.routes.execute_dns_guard(step).await.is_err() {
                    for undo in plan_dns_guard_rollback(&applied) {
                        let _ = self.routes.execute_dns_guard(&undo).await;
                    }
                    tracing::warn!(step = step.name(), want, "egress_dns_guard_failed");
                    return;
                }
                applied.push(*step);
            }
        } else {
            for step in &plan_dns_guard_remove(families) {
                if self.routes.execute_dns_guard(step).await.is_err() {
                    tracing::warn!(step = step.name(), want, "egress_dns_guard_failed");
                    return;
                }
            }
        }
        installed.state.set_dns_guard(want);
        tracing::info!(installed = want, "egress_dns_guard_applied");
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
            Ok(_) => EgressEnforcement::verified(installed.state.policy().mode()),
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
        let state = &installed.state;
        let ipv6 = verification_ipv6(state.plan(), self.routes.ipv6_present());
        let expected = expected_rule_priorities(state.slot());
        for family in managed_families(ipv6) {
            let rules = self
                .routes
                .show_rules(*family)
                .await
                .map_err(|_| VerifyFailure::RuleShow.as_str())?;
            check_rules(&expected, &rules).map_err(VerifyFailure::as_str)?;
        }
        let (Some(plan), Some(slot)) = (state.plan(), state.slot()) else {
            return Ok(0);
        };
        for family in plan.families() {
            let table = self
                .routes
                .show_table(*family, slot.table())
                .await
                .map_err(|_| VerifyFailure::TableShow.as_str())?;
            check_table(plan, *family, &table).map_err(VerifyFailure::as_str)?;
        }
        let expectations = samples(state.policy(), plan);
        for (destination, expected) in &expectations {
            let (code, stdout) = self
                .routes
                .route_get(*destination)
                .await
                .map_err(|_| VerifyFailure::RouteGet.as_str())?;
            check_sample(*expected, code, &stdout).map_err(VerifyFailure::as_str)?;
        }
        if requires_proxy_connect(state.policy()) {
            let port = installed
                .proxy
                .as_ref()
                .map(LocalProxy::port)
                .ok_or(VerifyFailure::Proxy.as_str())?;
            let target = SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), port);
            let connected =
                tokio::time::timeout(PROXY_PROBE_TIMEOUT, TcpStream::connect(target)).await;
            if !matches!(connected, Ok(Ok(_))) {
                return Err(VerifyFailure::Proxy.as_str());
            }
        }
        Ok(expectations.len())
    }
}

fn publish_proxy_policy(installed: &Installed, local: &[IpAddr]) {
    if let Some(proxy) = &installed.proxy {
        proxy.set_policy(ProxyPolicy {
            policy: installed.state.policy().clone(),
            guard: TargetGuard::new(local.iter().copied()),
        });
    }
}

fn step_name(error: &NetworkError) -> &'static str {
    match error {
        NetworkError::InstallFailed { step } => step,
        NetworkError::VerifyFailed => "verify",
        NetworkError::NoNetAdmin => "no_net_admin",
        NetworkError::InvalidEntry { .. }
        | NetworkError::HostnameInDenyOut { .. }
        | NetworkError::TooManyEntries { .. }
        | NetworkError::TooManyHostnames
        | NetworkError::PolicyTooComplex
        | NetworkError::InvalidProxyAddress
        | NetworkError::InvalidProxyCredentials
        | NetworkError::ProxyForbiddenAddress
        | NetworkError::ProxyUnresolvable => "plan",
    }
}

fn log_applied(installed: &Installed, elapsed: Duration) {
    let policy = installed.state.policy();
    let routes = |family| {
        installed
            .state
            .plan()
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
