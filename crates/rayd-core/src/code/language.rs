//! The kernel languages a context can run: the three canonical wire names,
//! the id of the per-language default context that `Execute{language}`
//! creates lazily, and how the sidecar's `ready.languages` list becomes the
//! set of languages this image ships (design D2).

use std::collections::BTreeSet;
use std::fmt;

use super::context::{ContextId, DEFAULT_CONTEXT_ID};
use super::error::CodeError;

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Language {
    Python,
    Bash,
    Javascript,
}

impl Language {
    pub const ALL: [Self; 3] = [Self::Python, Self::Bash, Self::Javascript];

    /// Empty selects Python; anything but the three canonical names is
    /// `InvalidLanguage` (aliases and capitalisation are the SDKs' job).
    pub fn parse(raw: &str) -> Result<Self, CodeError> {
        match raw {
            "" | "python" => Ok(Self::Python),
            "bash" => Ok(Self::Bash),
            "javascript" => Ok(Self::Javascript),
            _ => Err(CodeError::InvalidLanguage),
        }
    }

    /// Like `parse`, for the `Option` the proto conversion hands over.
    pub fn parse_optional(raw: Option<&str>) -> Result<Self, CodeError> {
        Self::parse(raw.unwrap_or_default())
    }

    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Python => "python",
            Self::Bash => "bash",
            Self::Javascript => "javascript",
        }
    }

    /// `default` for Python, `default-<language>` for the others: the
    /// context an `Execute` without `context_id` runs on.
    #[must_use]
    pub fn default_context_id(self) -> ContextId {
        match self {
            Self::Python => ContextId::default_context(),
            other => ContextId::parse(&format!("{DEFAULT_CONTEXT_ID}-{}", other.as_str()))
                .unwrap_or_else(|_| ContextId::default_context()),
        }
    }

    #[must_use]
    pub fn is_python(self) -> bool {
        matches!(self, Self::Python)
    }
}

impl fmt::Display for Language {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// What the sidecar announced it can run, plus how many names it listed
/// that this agent does not know (logged as a count, never echoed).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AvailableLanguages {
    pub languages: BTreeSet<Language>,
    pub unknown: usize,
}

impl Default for AvailableLanguages {
    /// An older sidecar without the field ships Python only.
    fn default() -> Self {
        Self {
            languages: BTreeSet::from([Language::Python]),
            unknown: 0,
        }
    }
}

impl AvailableLanguages {
    /// Python is always present (it is the sidecar's own interpreter), so
    /// a `ready` that omits it or leaves the list empty still yields it.
    #[must_use]
    pub fn from_ready(names: &[String]) -> Self {
        let mut languages = BTreeSet::from([Language::Python]);
        let mut unknown = 0;
        for name in names {
            match Language::parse(name) {
                Ok(language) if !name.is_empty() => {
                    languages.insert(language);
                }
                _ => unknown += 1,
            }
        }
        Self { languages, unknown }
    }

    #[must_use]
    pub fn contains(&self, language: Language) -> bool {
        self.languages.contains(&language)
    }

    pub fn require(&self, language: Language) -> Result<(), CodeError> {
        if self.contains(language) {
            Ok(())
        } else {
            Err(CodeError::LanguageUnavailable(language))
        }
    }
}

/// Where an `Execute` runs once `context_id` and `language` are read
/// together (design D2 routing table).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ExecuteTarget {
    /// A context the client named; its language is whatever it was
    /// created with.
    Explicit(ContextId),
    /// The per-language default context, `default` for Python; created
    /// lazily by `rayd` for the other languages.
    LanguageDefault(Language),
}

impl ExecuteTarget {
    /// Empty strings count as absent, so `context_id: ""` still selects the
    /// default and `language: ""` never conflicts with a context.
    pub fn resolve(context_id: Option<&str>, language: Option<&str>) -> Result<Self, CodeError> {
        let context_id = context_id.filter(|id| !id.is_empty());
        let language = language.filter(|name| !name.is_empty());
        match (context_id, language) {
            (Some(_), Some(_)) => Err(CodeError::LanguageWithContext),
            (Some(id), None) => Ok(Self::Explicit(ContextId::parse(id)?)),
            (None, Some(name)) => Ok(Self::LanguageDefault(Language::parse(name)?)),
            (None, None) => Ok(Self::LanguageDefault(Language::Python)),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn execute_target_follows_the_routing_table() {
        let default = ExecuteTarget::LanguageDefault(Language::Python);
        assert_eq!(ExecuteTarget::resolve(None, None), Ok(default.clone()));
        assert_eq!(
            ExecuteTarget::resolve(Some(""), Some("")),
            Ok(default.clone())
        );
        assert_eq!(ExecuteTarget::resolve(None, Some("python")), Ok(default));
        assert_eq!(
            ExecuteTarget::resolve(None, Some("bash")),
            Ok(ExecuteTarget::LanguageDefault(Language::Bash))
        );
        assert_eq!(
            ExecuteTarget::resolve(Some("ctx-1"), None),
            Ok(ExecuteTarget::Explicit(ContextId::parse("ctx-1").unwrap()))
        );
        assert_eq!(
            ExecuteTarget::resolve(Some("ctx-1"), Some("")),
            Ok(ExecuteTarget::Explicit(ContextId::parse("ctx-1").unwrap()))
        );
        assert_eq!(
            ExecuteTarget::resolve(Some("default"), Some("bash")),
            Err(CodeError::LanguageWithContext)
        );
        assert_eq!(
            ExecuteTarget::resolve(Some("default"), Some("python")),
            Err(CodeError::LanguageWithContext)
        );
        assert_eq!(
            ExecuteTarget::resolve(None, Some("r")),
            Err(CodeError::InvalidLanguage)
        );
        assert_eq!(
            ExecuteTarget::resolve(Some("has space"), None),
            Err(CodeError::InvalidContextId)
        );
    }

    #[test]
    fn parse_accepts_the_canonical_names_and_empty() {
        assert_eq!(Language::parse(""), Ok(Language::Python));
        assert_eq!(Language::parse("python"), Ok(Language::Python));
        assert_eq!(Language::parse("bash"), Ok(Language::Bash));
        assert_eq!(Language::parse("javascript"), Ok(Language::Javascript));
        assert_eq!(Language::parse_optional(None), Ok(Language::Python));
        assert_eq!(Language::parse_optional(Some("bash")), Ok(Language::Bash));
        for raw in ["Python", "js", "JS", "r", "java", " bash", "bash "] {
            assert_eq!(
                Language::parse(raw),
                Err(CodeError::InvalidLanguage),
                "{raw}"
            );
        }
    }

    #[test]
    fn names_and_default_context_ids() {
        for language in Language::ALL {
            assert_eq!(Language::parse(language.as_str()), Ok(language));
            assert_eq!(language.to_string(), language.as_str());
        }
        assert!(Language::Python.default_context_id().is_default());
        assert_eq!(Language::Bash.default_context_id().as_str(), "default-bash");
        assert_eq!(
            Language::Javascript.default_context_id().as_str(),
            "default-javascript"
        );
        assert!(Language::Python.is_python());
        assert!(!Language::Bash.is_python());
    }

    #[test]
    fn available_languages_from_ready_keep_python_and_count_unknowns() {
        let absent = AvailableLanguages::default();
        assert_eq!(absent.languages, BTreeSet::from([Language::Python]));
        assert_eq!(absent, AvailableLanguages::from_ready(&[]));
        let listed = AvailableLanguages::from_ready(&[
            "bash".to_owned(),
            "python".to_owned(),
            "r".to_owned(),
            String::new(),
        ]);
        assert_eq!(
            listed.languages,
            BTreeSet::from([Language::Python, Language::Bash])
        );
        assert_eq!(listed.unknown, 2);
        assert!(listed.contains(Language::Bash));
        assert!(!listed.contains(Language::Javascript));
        assert_eq!(listed.require(Language::Bash), Ok(()));
        assert_eq!(
            listed.require(Language::Javascript),
            Err(CodeError::LanguageUnavailable(Language::Javascript))
        );
        assert_eq!(
            AvailableLanguages::from_ready(&["javascript".to_owned()]).languages,
            BTreeSet::from([Language::Python, Language::Javascript])
        );
    }
}
