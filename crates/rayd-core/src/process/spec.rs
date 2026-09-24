//! From a `Start` request to a fully resolved spawn plan: policy, identity,
//! environment, cwd and limits, in that order, with no filesystem or process
//! access so the whole chain runs on any host with a fake `UserLookup`.

use std::collections::BTreeMap;
use std::time::Duration;

use super::cwd::{resolve_cwd, validate_cwd};
use super::env::build_child_env;
use super::error::ProcessError;
use super::identity::{ProcessIdentity, UserPolicy, resolve_username};
use super::limits::{ResourceLimits, StdinMode};
use super::ports::{LookupError, UserLookup};
use crate::run_payload::RunDefaults;

/// The `ProcessConfig` of the request, kept verbatim for `List`.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ProcessConfigInfo {
    pub cmd: String,
    pub args: Vec<String>,
    pub envs: BTreeMap<String, String>,
    pub cwd: Option<String>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SpawnInput {
    pub config: ProcessConfigInfo,
    pub user: Option<String>,
    /// `None` when `timeout_ms == 0`: no server deadline.
    pub timeout: Option<Duration>,
    pub stdin: StdinMode,
    pub tag: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SpawnSpec {
    pub program: String,
    pub args: Vec<String>,
    pub env: Vec<(String, String)>,
    pub cwd: String,
    pub identity: ProcessIdentity,
    pub stdin: StdinMode,
    pub limits: ResourceLimits,
}

/// `egress_env` is the local proxy's variables (empty until it runs),
/// layered under the payload and request `envs`.
pub fn plan_spawn(
    input: &SpawnInput,
    defaults: &RunDefaults,
    egress_env: &BTreeMap<String, String>,
    policy: UserPolicy,
    lookup: &dyn UserLookup,
) -> Result<SpawnSpec, ProcessError> {
    if input.config.cmd.is_empty() {
        return Err(ProcessError::EmptyCommand);
    }
    let username = resolve_username(input.user.as_deref(), defaults.user.as_deref());
    policy.authorize(&username)?;
    let identity = lookup.lookup(&username).map_err(lookup_error)?;
    policy.authorize_identity(&identity)?;
    let env = build_child_env(&identity, egress_env, &defaults.envs, &input.config.envs);
    let cwd = resolve_cwd(
        input.config.cwd.as_deref(),
        defaults.workdir.as_deref(),
        &identity.home,
    );
    validate_cwd(&cwd)?;
    Ok(SpawnSpec {
        program: input.config.cmd.clone(),
        args: input.config.args.clone(),
        env,
        cwd,
        identity,
        stdin: input.stdin,
        limits: sandbox_limits(defaults),
    })
}

/// The M2 posture plus the sandbox's optional CPU budget (`limits.cpu_seconds`
/// of the run payload): every process and PTY gets it, the sidecar never.
#[must_use]
pub fn sandbox_limits(defaults: &RunDefaults) -> ResourceLimits {
    ResourceLimits {
        cpu_seconds: defaults.cpu_seconds,
        ..ResourceLimits::default()
    }
}

fn lookup_error(error: LookupError) -> ProcessError {
    match error {
        LookupError::UnknownUser => ProcessError::UnknownUser,
        LookupError::Failed(reason) => ProcessError::UserLookupFailed(reason),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::process::error::CwdRejection;

    struct FakeUserLookup;

    impl UserLookup for FakeUserLookup {
        fn lookup(&self, username: &str) -> Result<ProcessIdentity, LookupError> {
            match username {
                "user" => Ok(ProcessIdentity {
                    uid: 1000,
                    gid: 1000,
                    groups: vec![1000, 27],
                    username: "user".to_owned(),
                    home: "/home/user".to_owned(),
                    shell: "/bin/bash".to_owned(),
                }),
                "root" => Ok(ProcessIdentity {
                    uid: 0,
                    gid: 0,
                    groups: vec![0],
                    username: "root".to_owned(),
                    home: "/root".to_owned(),
                    shell: "/bin/sh".to_owned(),
                }),
                "toor" => Ok(ProcessIdentity {
                    uid: 0,
                    gid: 0,
                    groups: vec![0],
                    username: "toor".to_owned(),
                    home: "/root".to_owned(),
                    shell: "/bin/sh".to_owned(),
                }),
                "operator" => Ok(ProcessIdentity {
                    uid: 11,
                    gid: 0,
                    groups: vec![0],
                    username: "operator".to_owned(),
                    home: "/root".to_owned(),
                    shell: "/bin/sh".to_owned(),
                }),
                _ => Err(LookupError::UnknownUser),
            }
        }
    }

    fn input(cmd: &str) -> SpawnInput {
        SpawnInput {
            config: ProcessConfigInfo {
                cmd: cmd.to_owned(),
                args: vec!["-l".to_owned(), "-c".to_owned(), "echo hola".to_owned()],
                ..ProcessConfigInfo::default()
            },
            ..SpawnInput::default()
        }
    }

    fn plan(input: &SpawnInput, defaults: &RunDefaults) -> Result<SpawnSpec, ProcessError> {
        plan_spawn(
            input,
            defaults,
            &BTreeMap::new(),
            UserPolicy::default(),
            &FakeUserLookup,
        )
    }

    #[test]
    fn defaults_produce_the_user_identity_home_cwd_and_base_env() {
        let spec = plan(&input("/bin/bash"), &RunDefaults::default()).unwrap();
        assert_eq!(spec.program, "/bin/bash");
        assert_eq!(spec.args, vec!["-l", "-c", "echo hola"]);
        assert_eq!(spec.identity.uid, 1000);
        assert_eq!(spec.identity.groups, vec![1000, 27]);
        assert_eq!(spec.cwd, "/home/user");
        assert_eq!(spec.stdin, StdinMode::Null);
        assert_eq!(spec.limits, ResourceLimits::default());
        assert!(
            spec.env
                .contains(&("HOME".to_owned(), "/home/user".to_owned()))
        );
    }

    #[test]
    fn empty_cmd_is_rejected_first() {
        assert_eq!(
            plan(&input(""), &RunDefaults::default()),
            Err(ProcessError::EmptyCommand)
        );
    }

    #[test]
    fn unknown_user_is_rejected() {
        let mut request = input("/bin/sh");
        request.user = Some("nobody-here".to_owned());
        assert_eq!(
            plan(&request, &RunDefaults::default()),
            Err(ProcessError::UnknownUser)
        );
    }

    #[test]
    fn a_system_account_is_refused_although_it_is_not_root() {
        let mut request = input("/bin/sh");
        request.user = Some("operator".to_owned());
        assert_eq!(
            plan(&request, &RunDefaults::default()),
            Err(ProcessError::PrivilegedAccount)
        );
        let spec = plan_spawn(
            &request,
            &RunDefaults::default(),
            &BTreeMap::new(),
            UserPolicy { allow_root: true },
            &FakeUserLookup,
        )
        .unwrap();
        assert_eq!(spec.identity.uid, 11);
    }

    #[test]
    fn root_is_refused_by_name_and_by_uid_unless_allowed() {
        let mut request = input("/bin/sh");
        request.user = Some("root".to_owned());
        assert_eq!(
            plan(&request, &RunDefaults::default()),
            Err(ProcessError::RootNotAllowed)
        );
        request.user = Some("toor".to_owned());
        assert_eq!(
            plan(&request, &RunDefaults::default()),
            Err(ProcessError::RootNotAllowed)
        );
        let spec = plan_spawn(
            &request,
            &RunDefaults::default(),
            &BTreeMap::new(),
            UserPolicy { allow_root: true },
            &FakeUserLookup,
        )
        .unwrap();
        assert_eq!(spec.identity.uid, 0);
        assert_eq!(spec.cwd, "/root");
    }

    #[test]
    fn payload_defaults_feed_user_workdir_and_envs() {
        let defaults = RunDefaults {
            envs: [("FOO".to_owned(), "sandbox".to_owned())].into(),
            user: Some("user".to_owned()),
            workdir: Some("/srv/app".to_owned()),
            cpu_seconds: None,
        };
        let mut request = input("/bin/sh");
        request
            .config
            .envs
            .insert("BAR".to_owned(), "req".to_owned());
        let spec = plan(&request, &defaults).unwrap();
        assert_eq!(spec.cwd, "/srv/app");
        assert!(spec.env.contains(&("FOO".to_owned(), "sandbox".to_owned())));
        assert!(spec.env.contains(&("BAR".to_owned(), "req".to_owned())));
        assert_eq!(spec.limits.cpu_seconds, None);
    }

    #[test]
    fn the_egress_proxy_variables_reach_the_spawn_under_the_request_envs() {
        let egress: BTreeMap<String, String> = [
            ("HTTPS_PROXY".to_owned(), "http://127.0.0.1:4000".to_owned()),
            ("NO_PROXY".to_owned(), "localhost".to_owned()),
        ]
        .into();
        let mut request = input("/bin/sh");
        request
            .config
            .envs
            .insert("NO_PROXY".to_owned(), "*".to_owned());
        let spec = plan_spawn(
            &request,
            &RunDefaults::default(),
            &egress,
            UserPolicy::default(),
            &FakeUserLookup,
        )
        .unwrap();
        assert!(
            spec.env
                .contains(&("HTTPS_PROXY".to_owned(), "http://127.0.0.1:4000".to_owned()))
        );
        assert!(spec.env.contains(&("NO_PROXY".to_owned(), "*".to_owned())));
    }

    #[test]
    fn payload_cpu_seconds_reach_the_spawn_limits() {
        let defaults = RunDefaults {
            cpu_seconds: Some(3),
            ..RunDefaults::default()
        };
        let spec = plan(&input("/bin/sh"), &defaults).unwrap();
        assert_eq!(spec.limits.cpu_seconds, Some(3));
        assert_eq!(spec.limits.cpu_rlimit(), Some((3, 8)));
        assert_eq!(spec.limits.nproc, ResourceLimits::default().nproc);
    }

    #[test]
    fn request_cwd_wins_and_is_validated() {
        let mut request = input("/bin/sh");
        request.config.cwd = Some("/tmp".to_owned());
        assert_eq!(plan(&request, &RunDefaults::default()).unwrap().cwd, "/tmp");
        request.config.cwd = Some("relative".to_owned());
        assert_eq!(
            plan(&request, &RunDefaults::default()),
            Err(ProcessError::InvalidCwd(CwdRejection::NotAbsolute))
        );
    }

    #[test]
    fn stdin_mode_and_tag_pass_through() {
        let mut request = input("/bin/sh");
        request.stdin = StdinMode::Pipe;
        request.tag = Some("m2".to_owned());
        let spec = plan(&request, &RunDefaults::default()).unwrap();
        assert_eq!(spec.stdin, StdinMode::Pipe);
        assert_eq!(request.tag.as_deref(), Some("m2"));
    }
}
