//! Interactive terminals: how a `PtyStart` request becomes a spawn plan for
//! a login shell with a controlling terminal (identity, shell, environment,
//! cwd, window size), and what the adapter must provide to open one. The
//! output, subscribers, retention and caps are the process registry's: a
//! PTY is a `ProcessKind::Pty` entry whose single output stream is the
//! master side of the terminal. `openpty`, `setsid` and the pumps live in
//! the `rayd` adapters.

pub mod error;
pub mod ports;

use std::collections::BTreeMap;
use std::time::Duration;

pub use error::PtyError;
pub use ports::{PtyBackend, PtyChild};

use crate::process::cwd::{resolve_cwd, validate_cwd};
use crate::process::env::build_child_env;
use crate::process::identity::{ProcessIdentity, UserPolicy, resolve_username};
use crate::process::ports::{LookupError, UserLookup};
use crate::process::{ProcessConfigInfo, ProcessError, SpawnSpec, StdinMode, sandbox_limits};
use crate::run_payload::RunDefaults;

pub const DEFAULT_PTY_COLS: u16 = 80;
pub const DEFAULT_PTY_ROWS: u16 = 24;
/// Larger windows are a client mistake, not a terminal.
pub const MAX_PTY_DIMENSION: u32 = 4096;
/// Largest `data` message on the wire: one read of the master side.
pub const PTY_CHUNK_BYTES: usize = 16 * 1024;
pub const PTY_TERM: &str = "xterm-256color";
pub const PTY_LOCALE: &str = "C.UTF-8";
/// Interactive login shell, as a terminal emulator would start it.
pub const PTY_SHELL_ARGS: [&str; 2] = ["-i", "-l"];
/// After the shell exits, output of a background child still holding the
/// slave is drained this long before the PTY is closed.
pub const PTY_DRAIN_GRACE: Duration = Duration::from_millis(500);
/// Shell when the user database has none for the account.
pub const FALLBACK_SHELL: &str = "/bin/sh";

/// A validated terminal window: both dimensions in `1..=MAX_PTY_DIMENSION`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PtySize {
    cols: u16,
    rows: u16,
}

impl Default for PtySize {
    fn default() -> Self {
        Self {
            cols: DEFAULT_PTY_COLS,
            rows: DEFAULT_PTY_ROWS,
        }
    }
}

impl PtySize {
    pub fn new(cols: u32, rows: u32) -> Result<Self, PtyError> {
        let valid = |value: u32| (1..=MAX_PTY_DIMENSION).contains(&value);
        if !(valid(cols) && valid(rows)) {
            return Err(PtyError::InvalidSize {
                cols,
                rows,
                max: MAX_PTY_DIMENSION,
            });
        }
        Ok(Self {
            cols: u16::try_from(cols).unwrap_or(u16::MAX),
            rows: u16::try_from(rows).unwrap_or(u16::MAX),
        })
    }

    /// An absent `PtyStart.size` means `80×24`.
    pub fn from_request(size: Option<(u32, u32)>) -> Result<Self, PtyError> {
        match size {
            None => Ok(Self::default()),
            Some((cols, rows)) => Self::new(cols, rows),
        }
    }

    #[must_use]
    pub fn cols(self) -> u16 {
        self.cols
    }

    #[must_use]
    pub fn rows(self) -> u16 {
        self.rows
    }
}

/// The request shell, else the identity's login shell. A relative path is
/// refused: the shell is exec'd, never searched on `PATH`.
pub fn resolve_shell(
    request: Option<&str>,
    identity: &ProcessIdentity,
) -> Result<String, PtyError> {
    match request.filter(|shell| !shell.is_empty()) {
        None => Ok(if identity.shell.is_empty() {
            FALLBACK_SHELL.to_owned()
        } else {
            identity.shell.clone()
        }),
        Some(shell) if shell.starts_with('/') && !shell.contains('\0') => Ok(shell.to_owned()),
        Some(_) => Err(PtyError::InvalidShell),
    }
}

/// What a terminal emulator sets before the shell starts; the `/run`
/// payload and the request may override any of them.
#[must_use]
pub fn pty_base_env(shell: &str) -> BTreeMap<String, String> {
    [
        ("TERM", PTY_TERM),
        ("LANG", PTY_LOCALE),
        ("LC_ALL", PTY_LOCALE),
        ("SHELL", shell),
    ]
    .into_iter()
    .map(|(key, value)| (key.to_owned(), value.to_owned()))
    .collect()
}

/// `PtyStart` after proto conversion.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct PtySpawnInput {
    pub size: Option<(u32, u32)>,
    pub envs: BTreeMap<String, String>,
    pub cwd: Option<String>,
    pub user: Option<String>,
    pub shell: Option<String>,
    /// `None` when `timeout_ms == 0`: no server deadline.
    pub timeout: Option<Duration>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PtyPlan {
    pub spec: SpawnSpec,
    pub size: PtySize,
    /// What `List` shows for the entry: the shell as `cmd`, the request
    /// envs, the resolved cwd.
    pub config: ProcessConfigInfo,
}

/// Size, identity, shell, environment and cwd, in that order, with no
/// filesystem or process access. Environment layers, last wins: identity
/// variables, PTY defaults, `/run` payload envs, request envs.
pub fn plan_pty(
    input: &PtySpawnInput,
    defaults: &RunDefaults,
    policy: UserPolicy,
    lookup: &dyn UserLookup,
) -> Result<PtyPlan, PtyError> {
    let size = PtySize::from_request(input.size)?;
    let username = resolve_username(input.user.as_deref(), defaults.user.as_deref());
    policy.authorize(&username)?;
    let identity = lookup.lookup(&username).map_err(lookup_error)?;
    policy.authorize_identity(&identity)?;
    let shell = resolve_shell(input.shell.as_deref(), &identity)?;
    let mut layered = pty_base_env(&shell);
    layered.extend(defaults.envs.clone());
    let env = build_child_env(&identity, &layered, &input.envs);
    let cwd = resolve_cwd(
        input.cwd.as_deref(),
        defaults.workdir.as_deref(),
        &identity.home,
    );
    validate_cwd(&cwd)?;
    let args: Vec<String> = PTY_SHELL_ARGS.iter().map(|arg| (*arg).to_owned()).collect();
    Ok(PtyPlan {
        spec: SpawnSpec {
            program: shell.clone(),
            args: args.clone(),
            env,
            cwd: cwd.clone(),
            identity,
            stdin: StdinMode::Pipe,
            limits: sandbox_limits(defaults),
        },
        size,
        config: ProcessConfigInfo {
            cmd: shell,
            args,
            envs: input.envs.clone(),
            cwd: Some(cwd),
        },
    })
}

fn lookup_error(error: LookupError) -> PtyError {
    PtyError::Process(match error {
        LookupError::UnknownUser => ProcessError::UnknownUser,
        LookupError::Failed(reason) => ProcessError::UserLookupFailed(reason),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::process::ResourceLimits;
    use crate::process::error::CwdRejection;

    struct FakeUserLookup;

    impl UserLookup for FakeUserLookup {
        fn lookup(&self, username: &str) -> Result<ProcessIdentity, LookupError> {
            match username {
                "user" => Ok(ProcessIdentity {
                    uid: 1000,
                    gid: 1000,
                    groups: vec![1000],
                    username: "user".to_owned(),
                    home: "/home/user".to_owned(),
                    shell: "/bin/bash".to_owned(),
                }),
                "noshell" => Ok(ProcessIdentity {
                    uid: 1001,
                    gid: 1001,
                    groups: vec![1001],
                    username: "noshell".to_owned(),
                    home: "/home/noshell".to_owned(),
                    shell: String::new(),
                }),
                "root" => Ok(ProcessIdentity {
                    uid: 0,
                    gid: 0,
                    groups: vec![0],
                    username: "root".to_owned(),
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

    fn plan(input: &PtySpawnInput, defaults: &RunDefaults) -> Result<PtyPlan, PtyError> {
        plan_pty(input, defaults, UserPolicy::default(), &FakeUserLookup)
    }

    fn env_of<'a>(spec: &'a SpawnSpec, key: &str) -> Option<&'a str> {
        spec.env
            .iter()
            .find(|(name, _)| name == key)
            .map(|(_, value)| value.as_str())
    }

    fn envs(pairs: &[(&str, &str)]) -> BTreeMap<String, String> {
        pairs
            .iter()
            .map(|(key, value)| ((*key).to_owned(), (*value).to_owned()))
            .collect()
    }

    #[test]
    fn size_defaults_to_80_by_24_and_is_bounded() {
        assert_eq!(PtySize::default().cols(), 80);
        assert_eq!(PtySize::default().rows(), 24);
        assert_eq!(PtySize::from_request(None), Ok(PtySize::default()));
        let big = PtySize::new(4096, 4096).unwrap();
        assert_eq!((big.cols(), big.rows()), (4096, 4096));
        assert_eq!(
            PtySize::new(4097, 24),
            Err(PtyError::InvalidSize {
                cols: 4097,
                rows: 24,
                max: 4096
            })
        );
        assert_eq!(
            PtySize::new(0, 0),
            Err(PtyError::InvalidSize {
                cols: 0,
                rows: 0,
                max: 4096
            })
        );
        assert!(PtySize::from_request(Some((5000, 24))).is_err());
        assert!(PtySize::from_request(Some((100, 30))).is_ok());
    }

    #[test]
    fn shell_comes_from_the_identity_unless_requested() {
        let user = FakeUserLookup.lookup("user").unwrap();
        assert_eq!(resolve_shell(None, &user).unwrap(), "/bin/bash");
        assert_eq!(resolve_shell(Some(""), &user).unwrap(), "/bin/bash");
        assert_eq!(resolve_shell(Some("/bin/zsh"), &user).unwrap(), "/bin/zsh");
        assert_eq!(
            resolve_shell(Some("zsh"), &user),
            Err(PtyError::InvalidShell)
        );
        assert_eq!(
            resolve_shell(Some("/bin/z\0sh"), &user),
            Err(PtyError::InvalidShell)
        );
        let noshell = FakeUserLookup.lookup("noshell").unwrap();
        assert_eq!(resolve_shell(None, &noshell).unwrap(), "/bin/sh");
    }

    #[test]
    fn base_env_has_the_terminal_variables() {
        let env = pty_base_env("/bin/bash");
        assert_eq!(env.get("TERM").map(String::as_str), Some("xterm-256color"));
        assert_eq!(env.get("LANG").map(String::as_str), Some("C.UTF-8"));
        assert_eq!(env.get("LC_ALL").map(String::as_str), Some("C.UTF-8"));
        assert_eq!(env.get("SHELL").map(String::as_str), Some("/bin/bash"));
        assert_eq!(env.len(), 4);
    }

    #[test]
    fn plan_spawns_the_login_shell_interactively_with_a_pipe() {
        let plan = plan(&PtySpawnInput::default(), &RunDefaults::default()).unwrap();
        assert_eq!(plan.spec.program, "/bin/bash");
        assert_eq!(plan.spec.args, vec!["-i", "-l"]);
        assert_eq!(plan.spec.stdin, StdinMode::Pipe);
        assert_eq!(plan.spec.cwd, "/home/user");
        assert_eq!(plan.spec.identity.uid, 1000);
        assert_eq!(plan.spec.limits, ResourceLimits::default());
        assert_eq!(plan.size, PtySize::default());
        assert_eq!(plan.config.cmd, "/bin/bash");
        assert_eq!(plan.config.args, vec!["-i", "-l"]);
        assert_eq!(plan.config.cwd.as_deref(), Some("/home/user"));
        assert!(plan.config.envs.is_empty());
        assert_eq!(env_of(&plan.spec, "TERM"), Some("xterm-256color"));
        assert_eq!(env_of(&plan.spec, "SHELL"), Some("/bin/bash"));
        assert_eq!(env_of(&plan.spec, "HOME"), Some("/home/user"));
        assert_eq!(env_of(&plan.spec, "USER"), Some("user"));
        assert!(env_of(&plan.spec, "PATH").is_some());
    }

    #[test]
    fn payload_cpu_seconds_reach_the_shell_limits() {
        let budgeted = RunDefaults {
            cpu_seconds: Some(2),
            ..RunDefaults::default()
        };
        let limited = plan(&PtySpawnInput::default(), &budgeted).unwrap();
        assert_eq!(limited.spec.limits.cpu_seconds, Some(2));
    }

    #[test]
    fn env_layers_identity_then_pty_defaults_then_payload_then_request() {
        let defaults = RunDefaults {
            envs: envs(&[("LANG", "es_ES.UTF-8"), ("FOO", "payload")]),
            ..RunDefaults::default()
        };
        let input = PtySpawnInput {
            envs: envs(&[("TERM", "vt100"), ("FOO", "request")]),
            ..PtySpawnInput::default()
        };
        let plan = plan(&input, &defaults).unwrap();
        assert_eq!(env_of(&plan.spec, "TERM"), Some("vt100"));
        assert_eq!(env_of(&plan.spec, "LANG"), Some("es_ES.UTF-8"));
        assert_eq!(env_of(&plan.spec, "LC_ALL"), Some("C.UTF-8"));
        assert_eq!(env_of(&plan.spec, "FOO"), Some("request"));
        assert_eq!(plan.config.envs, input.envs);
    }

    #[test]
    fn requested_shell_size_cwd_and_user_are_honoured() {
        let input = PtySpawnInput {
            size: Some((100, 30)),
            cwd: Some("/tmp".to_owned()),
            user: Some("user".to_owned()),
            shell: Some("/bin/zsh".to_owned()),
            timeout: Some(Duration::from_secs(1)),
            ..PtySpawnInput::default()
        };
        let plan = plan(&input, &RunDefaults::default()).unwrap();
        assert_eq!(plan.spec.program, "/bin/zsh");
        assert_eq!(env_of(&plan.spec, "SHELL"), Some("/bin/zsh"));
        assert_eq!(plan.spec.cwd, "/tmp");
        assert_eq!((plan.size.cols(), plan.size.rows()), (100, 30));
        assert_eq!(plan.config.cmd, "/bin/zsh");
    }

    #[test]
    fn size_is_checked_before_the_user_lookup() {
        let input = PtySpawnInput {
            size: Some((0, 24)),
            user: Some("nobody".to_owned()),
            ..PtySpawnInput::default()
        };
        assert!(matches!(
            plan(&input, &RunDefaults::default()),
            Err(PtyError::InvalidSize { .. })
        ));
    }

    #[test]
    fn a_system_account_is_refused_although_it_is_not_root() {
        let operator = PtySpawnInput {
            user: Some("operator".to_owned()),
            ..PtySpawnInput::default()
        };
        assert_eq!(
            plan(&operator, &RunDefaults::default()),
            Err(PtyError::Process(ProcessError::PrivilegedAccount))
        );
    }

    #[test]
    fn root_unknown_users_relative_shells_and_cwds_are_refused() {
        let root = PtySpawnInput {
            user: Some("root".to_owned()),
            ..PtySpawnInput::default()
        };
        assert_eq!(
            plan(&root, &RunDefaults::default()),
            Err(PtyError::Process(ProcessError::RootNotAllowed))
        );
        let allowed = plan_pty(
            &root,
            &RunDefaults::default(),
            UserPolicy { allow_root: true },
            &FakeUserLookup,
        )
        .unwrap();
        assert_eq!(allowed.spec.program, "/bin/sh");
        let unknown = PtySpawnInput {
            user: Some("nobody".to_owned()),
            ..PtySpawnInput::default()
        };
        assert_eq!(
            plan(&unknown, &RunDefaults::default()),
            Err(PtyError::Process(ProcessError::UnknownUser))
        );
        let relative = PtySpawnInput {
            shell: Some("bash".to_owned()),
            ..PtySpawnInput::default()
        };
        assert_eq!(
            plan(&relative, &RunDefaults::default()),
            Err(PtyError::InvalidShell)
        );
        let cwd = PtySpawnInput {
            cwd: Some("relative".to_owned()),
            ..PtySpawnInput::default()
        };
        assert_eq!(
            plan(&cwd, &RunDefaults::default()),
            Err(PtyError::Process(ProcessError::InvalidCwd(
                CwdRejection::NotAbsolute
            )))
        );
    }
}
