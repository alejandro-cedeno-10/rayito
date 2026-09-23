//! The pure halves of the hooks that touch the kernel: how the sidecar is
//! spawned (design D4), what `/run` sends to rotate the default kernel
//! (D11), the cell `/validate` runs and how its state answers the hook
//! (D9), and how the `/resume` probe reply becomes restarts and the
//! `kernel_state_lost` flag.

use std::collections::BTreeMap;

use super::context::{ContextId, ContextRegistry, DEFAULT_CONTEXT_ID};
use super::protocol::SidecarOp;
use crate::process::env::build_child_env;
use crate::process::{ProcessIdentity, ResourceLimits, SpawnSpec, StdinMode};

/// `/validate` executes this in the default context so Lambda samples the
/// pandas, matplotlib and chart-extractor pages for prefetch. It is the
/// path the acceptance test exercises.
pub const VALIDATE_CELL: &str = "import pandas as pd, numpy as np\n\
import matplotlib.pyplot as plt\n\
df = pd.DataFrame({\"x\": np.arange(50), \"y\": np.random.default_rng(0).random(50)})\n\
df.describe()\n\
plt.plot(df.x, df.y)\n\
plt.show()\n\
df\n";

/// What the sidecar runs in every live kernel on `reseed` (the sidecar
/// owns the text; this copy documents the contract for `/resume`).
pub const RESEED_CELL: &str = "import random as _r; _r.seed()\n\
import sys as _s\n\
if \"numpy\" in _s.modules:\n    _s.modules[\"numpy\"].random.seed()\n\
del _r, _s\n";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SidecarConfig {
    /// Program and leading arguments (`python3 -m rayito_kernel_sidecar`).
    pub command: Vec<String>,
    pub sidecar_root: String,
    pub socket_root: String,
}

impl SidecarConfig {
    #[must_use]
    pub fn new(command: Vec<String>, sidecar_root: &str, socket_root: &str) -> Self {
        Self {
            command,
            sidecar_root: sidecar_root.to_owned(),
            socket_root: socket_root.to_owned(),
        }
    }
}

/// The sidecar is spawned like any sandbox child (identity, limits,
/// environment from scratch) with the four flags and the interpreter
/// variables it needs; the `/run` payload `envs` are never part of it and
/// neither is its `limits.cpu_seconds` (`RLIMIT_CPU` counts cumulative CPU
/// time: a limited kernel would die mid-session), which is why the spec is
/// built without `RunDefaults`.
#[must_use]
pub fn sidecar_spawn_spec(identity: &ProcessIdentity, config: &SidecarConfig) -> SpawnSpec {
    let mut command = config.command.iter();
    let program = command
        .next()
        .cloned()
        .unwrap_or_else(|| "python3".to_owned());
    let mut args: Vec<String> = command.cloned().collect();
    args.extend([
        "--socket-root".to_owned(),
        config.socket_root.clone(),
        "--sidecar-root".to_owned(),
        config.sidecar_root.clone(),
        "--default-cwd".to_owned(),
        identity.home.clone(),
        "--default-context-id".to_owned(),
        DEFAULT_CONTEXT_ID.to_owned(),
    ]);
    let sidecar_env: BTreeMap<String, String> = [
        ("PYTHONPATH", format!("{}/src", config.sidecar_root)),
        ("JUPYTER_PATH", format!("{}/jupyter", config.sidecar_root)),
        ("PYTHONUNBUFFERED", "1".to_owned()),
        ("PYTHONDONTWRITEBYTECODE", "1".to_owned()),
        ("LANG", "C.UTF-8".to_owned()),
        ("LC_ALL", "C.UTF-8".to_owned()),
    ]
    .into_iter()
    .map(|(key, value)| (key.to_owned(), value))
    .collect();
    SpawnSpec {
        program,
        args,
        env: build_child_env(identity, &BTreeMap::new(), &BTreeMap::new(), &sidecar_env),
        cwd: identity.home.clone(),
        identity: identity.clone(),
        stdin: StdinMode::Pipe,
        limits: ResourceLimits::default(),
    }
}

/// `/run` rotates the pre-warmed default kernel: new connection file (new
/// HMAC key), fresh PRNG state, the payload's environment.
#[must_use]
pub fn run_rotation_request(envs: BTreeMap<String, String>) -> SidecarOp {
    SidecarOp::RestartContext {
        context_id: DEFAULT_CONTEXT_ID.to_owned(),
        envs,
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ValidationOutcome {
    Validated,
    Failed { error_name: String },
}

impl ValidationOutcome {
    #[must_use]
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Validated => "validated",
            Self::Failed { .. } => "validate_failed",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub enum ValidationState {
    #[default]
    Idle,
    Running,
    Done(ValidationOutcome),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ValidateDecision {
    /// First call: start the cell and answer 503.
    Start,
    /// Still running: 503.
    Retry,
    /// Finished: 200 with the outcome.
    Done(ValidationOutcome),
}

#[must_use]
pub fn validate_hook_decision(state: &ValidationState) -> ValidateDecision {
    match state {
        ValidationState::Idle => ValidateDecision::Start,
        ValidationState::Running => ValidateDecision::Retry,
        ValidationState::Done(outcome) => ValidateDecision::Done(outcome.clone()),
    }
}

/// What the sidecar's `resume` reply says about each kernel.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ProbeOutcome {
    pub alive: Vec<ContextId>,
    pub lost: Vec<ContextId>,
}

impl ProbeOutcome {
    #[must_use]
    pub fn kernel_state_lost(&self) -> bool {
        !self.lost.is_empty()
    }
}

/// Splits the `contexts[{context_id, alive}]` of the reply.
#[must_use]
pub fn probe_outcome(contexts: &[(ContextId, bool)]) -> ProbeOutcome {
    let mut outcome = ProbeOutcome::default();
    for (context_id, alive) in contexts {
        if *alive {
            outcome.alive.push(context_id.clone());
        } else {
            outcome.lost.push(context_id.clone());
        }
    }
    outcome
}

/// One `restart_context` per lost kernel, with the envs the context was
/// created with; contexts unknown to the registry are skipped.
#[must_use]
pub fn restart_after_resume(lost: &[ContextId], registry: &ContextRegistry) -> Vec<SidecarOp> {
    lost.iter()
        .filter_map(|context_id| {
            registry
                .get(context_id)
                .ok()
                .map(|entry| SidecarOp::RestartContext {
                    context_id: context_id.as_str().to_owned(),
                    envs: entry.envs.clone(),
                })
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn identity() -> ProcessIdentity {
        ProcessIdentity {
            uid: 1000,
            gid: 1000,
            groups: vec![1000],
            username: "user".to_owned(),
            home: "/home/user".to_owned(),
            shell: "/bin/bash".to_owned(),
        }
    }

    fn config() -> SidecarConfig {
        SidecarConfig::new(
            vec![
                "python3".to_owned(),
                "-m".to_owned(),
                "rayito_kernel_sidecar".to_owned(),
            ],
            "/opt/rayito/sidecar",
            "/run/rayito/k",
        )
    }

    fn env_of<'a>(spec: &'a SpawnSpec, key: &str) -> Option<&'a str> {
        spec.env
            .iter()
            .find(|(name, _)| name == key)
            .map(|(_, value)| value.as_str())
    }

    #[test]
    fn spawn_spec_has_the_program_flags_identity_and_env_from_scratch() {
        let spec = sidecar_spawn_spec(&identity(), &config());
        assert_eq!(spec.program, "python3");
        assert_eq!(
            spec.args,
            vec![
                "-m",
                "rayito_kernel_sidecar",
                "--socket-root",
                "/run/rayito/k",
                "--sidecar-root",
                "/opt/rayito/sidecar",
                "--default-cwd",
                "/home/user",
                "--default-context-id",
                "default",
            ]
        );
        assert_eq!(spec.cwd, "/home/user");
        assert_eq!(spec.identity.uid, 1000);
        assert_eq!(spec.stdin, StdinMode::Pipe);
        assert_eq!(spec.limits, ResourceLimits::default());
        assert_eq!(
            spec.limits.cpu_seconds, None,
            "the sidecar and its kernels never get RLIMIT_CPU: it counts cumulative CPU time"
        );
        assert_eq!(env_of(&spec, "PYTHONPATH"), Some("/opt/rayito/sidecar/src"));
        assert_eq!(
            env_of(&spec, "JUPYTER_PATH"),
            Some("/opt/rayito/sidecar/jupyter")
        );
        assert_eq!(env_of(&spec, "PYTHONUNBUFFERED"), Some("1"));
        assert_eq!(env_of(&spec, "PYTHONDONTWRITEBYTECODE"), Some("1"));
        assert_eq!(env_of(&spec, "LANG"), Some("C.UTF-8"));
        assert_eq!(env_of(&spec, "LC_ALL"), Some("C.UTF-8"));
        assert_eq!(env_of(&spec, "HOME"), Some("/home/user"));
        assert_eq!(env_of(&spec, "USER"), Some("user"));
        assert_eq!(env_of(&spec, "LOGNAME"), Some("user"));
        assert!(env_of(&spec, "PATH").is_some());
        assert_eq!(spec.env.len(), 10);
        assert!(spec.env.iter().all(|(key, _)| !key.starts_with("AWS_")));
    }

    #[test]
    fn a_custom_command_keeps_its_leading_arguments() {
        let config = SidecarConfig::new(
            vec![
                "python3".to_owned(),
                "tests/fixtures/fake_sidecar.py".to_owned(),
                "--warmup-ms".to_owned(),
                "500".to_owned(),
            ],
            "/tmp/sidecar",
            "/tmp/k",
        );
        let spec = sidecar_spawn_spec(&identity(), &config);
        assert_eq!(spec.program, "python3");
        assert_eq!(
            &spec.args[..3],
            ["tests/fixtures/fake_sidecar.py", "--warmup-ms", "500"]
        );
        assert_eq!(spec.args[3], "--socket-root");
    }

    #[test]
    fn rotation_targets_the_default_context_with_the_payload_envs() {
        let envs: BTreeMap<String, String> = [("M4".to_owned(), "1".to_owned())].into();
        assert_eq!(
            run_rotation_request(envs.clone()),
            SidecarOp::RestartContext {
                context_id: "default".to_owned(),
                envs,
            }
        );
    }

    #[test]
    fn validate_cell_touches_pandas_matplotlib_and_the_frame() {
        assert!(VALIDATE_CELL.contains("import pandas"));
        assert!(VALIDATE_CELL.contains("plt.show()"));
        assert!(VALIDATE_CELL.trim_end().ends_with("df"));
        assert!(RESEED_CELL.contains("random.seed()"));
    }

    #[test]
    fn probe_outcome_splits_alive_and_lost_kernels() {
        let a = ContextId::parse("ctx-a").unwrap();
        let b = ContextId::parse("ctx-b").unwrap();
        let outcome = probe_outcome(&[
            (ContextId::default_context(), true),
            (a.clone(), false),
            (b.clone(), true),
        ]);
        assert_eq!(outcome.alive, vec![ContextId::default_context(), b]);
        assert_eq!(outcome.lost, vec![a]);
        assert!(outcome.kernel_state_lost());
        assert!(!probe_outcome(&[]).kernel_state_lost());
    }

    #[test]
    fn restart_after_resume_targets_lost_contexts_with_their_envs() {
        use super::super::context::ContextEntry;
        use super::super::language::Language;
        let mut registry = ContextRegistry::default();
        let a = ContextId::parse("ctx-a").unwrap();
        let envs: BTreeMap<String, String> = [("A".to_owned(), "1".to_owned())].into();
        registry
            .register(ContextEntry::new(
                ContextId::default_context(),
                Language::Python,
                "/home/user".to_owned(),
                BTreeMap::new(),
            ))
            .unwrap();
        registry
            .register(ContextEntry::new(
                a.clone(),
                Language::Python,
                "/home/user".to_owned(),
                envs.clone(),
            ))
            .unwrap();
        let unknown = ContextId::parse("ctx-zzz").unwrap();
        let ops = restart_after_resume(&[a.clone(), unknown], &registry);
        assert_eq!(
            ops,
            vec![SidecarOp::RestartContext {
                context_id: "ctx-a".to_owned(),
                envs
            }]
        );
        assert!(restart_after_resume(&[], &registry).is_empty());
    }

    #[test]
    fn validate_decisions_follow_the_state() {
        assert_eq!(
            validate_hook_decision(&ValidationState::Idle),
            ValidateDecision::Start
        );
        assert_eq!(
            validate_hook_decision(&ValidationState::Running),
            ValidateDecision::Retry
        );
        assert_eq!(
            validate_hook_decision(&ValidationState::Done(ValidationOutcome::Validated)),
            ValidateDecision::Done(ValidationOutcome::Validated)
        );
        let failed = ValidationOutcome::Failed {
            error_name: "KernelDied".to_owned(),
        };
        assert_eq!(failed.as_str(), "validate_failed");
        assert_eq!(ValidationOutcome::Validated.as_str(), "validated");
    }
}
