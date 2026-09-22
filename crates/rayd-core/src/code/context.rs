//! Contexts: one kernel each, identified by `ContextId`, registered in a
//! pure `ContextRegistry` that knows the cap, the protected default and the
//! listing order (design D3).

use std::collections::BTreeMap;
use std::fmt;

use super::MAX_CONTEXTS;
use super::error::CodeError;
use super::language::Language;
use super::ports::RandomSource;
use crate::process::cwd::{resolve_cwd, validate_cwd};
use crate::process::{CwdRejection, ProcessError};
use crate::run_payload::RunDefaults;

pub const DEFAULT_CONTEXT_ID: &str = "default";
const CONTEXT_ID_MAX_CHARS: usize = 64;
const GENERATED_ID_BYTES: usize = 6;

#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct ContextId(String);

impl ContextId {
    pub fn parse(raw: &str) -> Result<Self, CodeError> {
        let valid = !raw.is_empty()
            && raw.len() <= CONTEXT_ID_MAX_CHARS
            && raw
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-');
        if valid {
            Ok(Self(raw.to_owned()))
        } else {
            Err(CodeError::InvalidContextId)
        }
    }

    /// Empty selects the default context; anything else must parse.
    pub fn parse_or_default(raw: Option<&str>) -> Result<Self, CodeError> {
        match raw {
            None | Some("") => Ok(Self::default_context()),
            Some(raw) => Self::parse(raw),
        }
    }

    #[must_use]
    pub fn default_context() -> Self {
        Self(DEFAULT_CONTEXT_ID.to_owned())
    }

    /// `ctx-<12 hex>` from six random bytes.
    pub fn generate(random: &dyn RandomSource) -> Result<Self, CodeError> {
        let mut bytes = [0u8; GENERATED_ID_BYTES];
        random
            .fill(&mut bytes)
            .map_err(|error| CodeError::Internal(error.to_string()))?;
        Ok(Self(format!("ctx-{}", hex(&bytes))))
    }

    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }

    #[must_use]
    pub fn is_default(&self) -> bool {
        self.0 == DEFAULT_CONTEXT_ID
    }
}

impl fmt::Display for ContextId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

pub(super) fn hex(bytes: &[u8]) -> String {
    use std::fmt::Write as _;
    bytes.iter().fold(String::new(), |mut out, byte| {
        let _ = write!(out, "{byte:02x}");
        out
    })
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ContextInfo {
    pub context_id: ContextId,
    pub language: Language,
    pub cwd: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ContextState {
    Starting,
    Ready,
    Restarting,
    Dead,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ContextEntry {
    pub info: ContextInfo,
    pub envs: BTreeMap<String, String>,
    pub state: ContextState,
    pub kernel_pid: Option<u32>,
    pub in_flight: u32,
}

impl ContextEntry {
    #[must_use]
    pub fn new(
        context_id: ContextId,
        language: Language,
        cwd: String,
        envs: BTreeMap<String, String>,
    ) -> Self {
        Self {
            info: ContextInfo {
                context_id,
                language,
                cwd,
            },
            envs,
            state: ContextState::Starting,
            kernel_pid: None,
            in_flight: 0,
        }
    }
}

/// What `CreateContext` carries after proto conversion.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct CreateContextInput {
    pub language: String,
    pub cwd: Option<String>,
    pub envs: BTreeMap<String, String>,
}

/// What a context starts with once its request passed `plan_context`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ContextPlan {
    pub language: Language,
    pub cwd: String,
    pub envs: BTreeMap<String, String>,
}

/// Language, cwd and envs checks with no filesystem access; whether the
/// directory exists is learnt from the kernel start, whether the image
/// ships the language from the sidecar's `ready`.
pub fn plan_context(
    input: &CreateContextInput,
    defaults: &RunDefaults,
    home: &str,
) -> Result<ContextPlan, CodeError> {
    let language = Language::parse(&input.language)?;
    let cwd = resolve_cwd(input.cwd.as_deref(), defaults.workdir.as_deref(), home);
    validate_cwd(&cwd).map_err(cwd_error)?;
    validate_envs(&input.envs)?;
    Ok(ContextPlan {
        language,
        cwd,
        envs: input.envs.clone(),
    })
}

pub fn validate_envs(envs: &BTreeMap<String, String>) -> Result<(), CodeError> {
    let valid = envs.iter().all(|(key, value)| {
        !key.is_empty() && !key.contains('=') && !key.contains('\0') && !value.contains('\0')
    });
    if valid {
        Ok(())
    } else {
        Err(CodeError::InvalidEnvs)
    }
}

fn cwd_error(error: ProcessError) -> CodeError {
    match error {
        ProcessError::InvalidCwd(rejection) => CodeError::InvalidCwd(rejection.to_string()),
        other => CodeError::Internal(other.to_string()),
    }
}

#[must_use]
pub fn cwd_not_a_directory() -> CodeError {
    CodeError::InvalidCwd(CwdRejection::NotADirectory.to_string())
}

/// Live contexts of one sidecar instance, listed default first and then in
/// creation order.
#[derive(Debug)]
pub struct ContextRegistry {
    entries: BTreeMap<ContextId, ContextEntry>,
    order: Vec<ContextId>,
    max_contexts: usize,
}

impl Default for ContextRegistry {
    fn default() -> Self {
        Self::new(MAX_CONTEXTS)
    }
}

impl ContextRegistry {
    #[must_use]
    pub fn new(max_contexts: usize) -> Self {
        Self {
            entries: BTreeMap::new(),
            order: Vec::new(),
            max_contexts,
        }
    }

    pub fn register(&mut self, entry: ContextEntry) -> Result<(), CodeError> {
        let id = entry.info.context_id.clone();
        if self.entries.contains_key(&id) {
            return Err(CodeError::Internal("context already registered".to_owned()));
        }
        if !id.is_default() && self.entries.len() >= self.max_contexts {
            return Err(CodeError::TooManyContexts {
                max: self.max_contexts,
            });
        }
        self.entries.insert(id.clone(), entry);
        self.order.push(id);
        Ok(())
    }

    pub fn get(&self, id: &ContextId) -> Result<&ContextEntry, CodeError> {
        self.entries.get(id).ok_or(CodeError::ContextNotFound)
    }

    #[must_use]
    pub fn contains(&self, id: &ContextId) -> bool {
        self.entries.contains_key(id)
    }

    /// The default context is never removed: `kernel_ready` always has a
    /// referent.
    pub fn remove(&mut self, id: &ContextId) -> Result<ContextEntry, CodeError> {
        if id.is_default() {
            return Err(CodeError::DefaultContextProtected);
        }
        let entry = self.entries.remove(id).ok_or(CodeError::ContextNotFound)?;
        self.order.retain(|known| known != id);
        Ok(entry)
    }

    pub fn set_state(&mut self, id: &ContextId, state: ContextState) -> Result<(), CodeError> {
        self.entry_mut(id)?.state = state;
        Ok(())
    }

    pub fn set_kernel_pid(&mut self, id: &ContextId, pid: Option<u32>) -> Result<(), CodeError> {
        self.entry_mut(id)?.kernel_pid = pid;
        Ok(())
    }

    pub fn set_envs(
        &mut self,
        id: &ContextId,
        envs: BTreeMap<String, String>,
    ) -> Result<(), CodeError> {
        self.entry_mut(id)?.envs = envs;
        Ok(())
    }

    pub fn begin_execution(&mut self, id: &ContextId) -> Result<(), CodeError> {
        let entry = self.entry_mut(id)?;
        entry.in_flight = entry.in_flight.saturating_add(1);
        Ok(())
    }

    pub fn end_execution(&mut self, id: &ContextId) {
        if let Some(entry) = self.entries.get_mut(id) {
            entry.in_flight = entry.in_flight.saturating_sub(1);
        }
    }

    #[must_use]
    pub fn list(&self) -> Vec<ContextInfo> {
        let mut listed: Vec<ContextInfo> = Vec::with_capacity(self.entries.len());
        if let Some(default) = self.entries.get(&ContextId::default_context()) {
            listed.push(default.info.clone());
        }
        for id in &self.order {
            if id.is_default() {
                continue;
            }
            if let Some(entry) = self.entries.get(id) {
                listed.push(entry.info.clone());
            }
        }
        listed
    }

    #[must_use]
    pub fn kernel_pids(&self) -> Vec<u32> {
        self.entries
            .values()
            .filter_map(|entry| entry.kernel_pid)
            .collect()
    }

    #[must_use]
    pub fn len(&self) -> usize {
        self.entries.len()
    }

    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    /// The sidecar died: every context is gone with it.
    pub fn clear(&mut self) {
        self.entries.clear();
        self.order.clear();
    }

    fn entry_mut(&mut self, id: &ContextId) -> Result<&mut ContextEntry, CodeError> {
        self.entries.get_mut(id).ok_or(CodeError::ContextNotFound)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::code::ports::RandomError;

    struct FixedRandom(u8);

    impl RandomSource for FixedRandom {
        fn fill(&self, buf: &mut [u8]) -> Result<(), RandomError> {
            buf.fill(self.0);
            Ok(())
        }
    }

    fn entry(id: &str) -> ContextEntry {
        ContextEntry::new(
            ContextId::parse(id).unwrap(),
            Language::Python,
            "/home/user".to_owned(),
            BTreeMap::new(),
        )
    }

    #[test]
    fn context_id_syntax() {
        assert!(ContextId::parse("default").is_ok());
        assert!(ContextId::parse("ctx-0123456789ab").is_ok());
        assert!(ContextId::parse("a_B-9").is_ok());
        assert_eq!(ContextId::parse(""), Err(CodeError::InvalidContextId));
        assert_eq!(
            ContextId::parse("has space"),
            Err(CodeError::InvalidContextId)
        );
        assert_eq!(ContextId::parse("ñ"), Err(CodeError::InvalidContextId));
        assert_eq!(
            ContextId::parse(&"x".repeat(65)),
            Err(CodeError::InvalidContextId)
        );
        assert!(ContextId::parse(&"x".repeat(64)).is_ok());
        assert!(ContextId::parse_or_default(None).unwrap().is_default());
        assert!(ContextId::parse_or_default(Some("")).unwrap().is_default());
        assert_eq!(
            ContextId::parse_or_default(Some("ctx-1")).unwrap().as_str(),
            "ctx-1"
        );
    }

    #[test]
    fn generated_ids_are_ctx_plus_twelve_hex() {
        let id = ContextId::generate(&FixedRandom(0xab)).unwrap();
        assert_eq!(id.as_str(), "ctx-abababababab");
        assert!(!id.is_default());
    }

    #[test]
    fn plan_context_validates_language_cwd_and_envs() {
        let defaults = RunDefaults::default();
        let plan = plan_context(
            &CreateContextInput {
                language: String::new(),
                cwd: None,
                envs: [("A".to_owned(), "1".to_owned())].into(),
            },
            &defaults,
            "/home/user",
        )
        .unwrap();
        assert_eq!(plan.cwd, "/home/user");
        assert_eq!(plan.language, Language::Python);
        assert_eq!(plan.envs.get("A").map(String::as_str), Some("1"));
        assert!(
            plan_context(
                &CreateContextInput {
                    language: "python".to_owned(),
                    cwd: Some("/tmp".to_owned()),
                    envs: BTreeMap::new(),
                },
                &defaults,
                "/home/user",
            )
            .is_ok()
        );
        assert_eq!(
            plan_context(
                &CreateContextInput {
                    language: "bash".to_owned(),
                    ..CreateContextInput::default()
                },
                &defaults,
                "/home/user",
            )
            .unwrap()
            .language,
            Language::Bash
        );
        assert_eq!(
            plan_context(
                &CreateContextInput {
                    language: "r".to_owned(),
                    ..CreateContextInput::default()
                },
                &defaults,
                "/home/user",
            ),
            Err(CodeError::InvalidLanguage)
        );
        assert_eq!(
            plan_context(
                &CreateContextInput {
                    cwd: Some("relative".to_owned()),
                    ..CreateContextInput::default()
                },
                &defaults,
                "/home/user",
            ),
            Err(CodeError::InvalidCwd("is not an absolute path".to_owned()))
        );
        assert_eq!(
            plan_context(
                &CreateContextInput {
                    envs: [("A=B".to_owned(), "1".to_owned())].into(),
                    ..CreateContextInput::default()
                },
                &defaults,
                "/home/user",
            ),
            Err(CodeError::InvalidEnvs)
        );
        let payload_workdir = RunDefaults {
            workdir: Some("/srv".to_owned()),
            ..RunDefaults::default()
        };
        assert_eq!(
            plan_context(
                &CreateContextInput::default(),
                &payload_workdir,
                "/home/user"
            )
            .unwrap()
            .cwd,
            "/srv"
        );
    }

    #[test]
    fn validate_envs_rejects_bad_keys_and_values() {
        assert!(validate_envs(&BTreeMap::new()).is_ok());
        assert_eq!(
            validate_envs(&[(String::new(), "1".to_owned())].into()),
            Err(CodeError::InvalidEnvs)
        );
        assert_eq!(
            validate_envs(&[("A".to_owned(), "1\0".to_owned())].into()),
            Err(CodeError::InvalidEnvs)
        );
    }

    #[test]
    fn registry_lists_default_first_then_creation_order() {
        let mut registry = ContextRegistry::default();
        registry.register(entry("ctx-b")).unwrap();
        registry.register(entry("default")).unwrap();
        registry.register(entry("ctx-a")).unwrap();
        let ids: Vec<String> = registry
            .list()
            .into_iter()
            .map(|info| info.context_id.to_string())
            .collect();
        assert_eq!(ids, vec!["default", "ctx-b", "ctx-a"]);
        assert_eq!(registry.list()[0].language, Language::Python);
        let mut bash = entry("default-bash");
        bash.info.language = Language::Bash;
        registry.register(bash).unwrap();
        assert_eq!(registry.list()[3].language, Language::Bash);
        assert_eq!(registry.list()[3].context_id.as_str(), "default-bash");
    }

    #[test]
    fn registry_caps_at_eight_including_the_default() {
        let mut registry = ContextRegistry::default();
        registry.register(entry("default")).unwrap();
        for index in 1..8 {
            registry.register(entry(&format!("ctx-{index}"))).unwrap();
        }
        assert_eq!(registry.len(), 8);
        assert_eq!(
            registry.register(entry("ctx-9")),
            Err(CodeError::TooManyContexts { max: 8 })
        );
        registry
            .remove(&ContextId::parse("ctx-1").unwrap())
            .unwrap();
        assert!(registry.register(entry("ctx-9")).is_ok());
    }

    #[test]
    fn default_cannot_be_removed_and_unknown_ids_are_not_found() {
        let mut registry = ContextRegistry::default();
        registry.register(entry("default")).unwrap();
        assert_eq!(
            registry.remove(&ContextId::default_context()),
            Err(CodeError::DefaultContextProtected)
        );
        assert_eq!(
            registry.remove(&ContextId::parse("ctx-x").unwrap()),
            Err(CodeError::ContextNotFound)
        );
        assert_eq!(
            registry.get(&ContextId::parse("ctx-x").unwrap()),
            Err(CodeError::ContextNotFound)
        );
        assert_eq!(
            registry.set_state(&ContextId::parse("ctx-x").unwrap(), ContextState::Ready),
            Err(CodeError::ContextNotFound)
        );
    }

    #[test]
    fn state_pid_envs_and_in_flight_are_tracked_and_cleared() {
        let mut registry = ContextRegistry::default();
        registry.register(entry("default")).unwrap();
        registry.register(entry("ctx-1")).unwrap();
        let default = ContextId::default_context();
        let other = ContextId::parse("ctx-1").unwrap();
        registry.set_state(&default, ContextState::Ready).unwrap();
        registry.set_kernel_pid(&default, Some(42)).unwrap();
        registry.set_kernel_pid(&other, Some(43)).unwrap();
        registry
            .set_envs(&default, [("A".to_owned(), "1".to_owned())].into())
            .unwrap();
        registry.begin_execution(&default).unwrap();
        registry.begin_execution(&default).unwrap();
        registry.end_execution(&default);
        let entry = registry.get(&default).unwrap();
        assert_eq!(entry.state, ContextState::Ready);
        assert_eq!(entry.kernel_pid, Some(42));
        assert_eq!(entry.in_flight, 1);
        assert_eq!(entry.envs.get("A").map(String::as_str), Some("1"));
        let mut pids = registry.kernel_pids();
        pids.sort_unstable();
        assert_eq!(pids, vec![42, 43]);
        registry.clear();
        assert!(registry.is_empty());
        assert!(registry.list().is_empty());
        registry.end_execution(&default);
    }
}
