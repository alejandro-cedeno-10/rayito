//! File metadata (design D17): a small map stored as `user.rayito.<key>`
//! extended attributes on the file itself, set through the temp file's
//! descriptor before the rename so content and metadata appear together.
//! Keys are HTTP token characters kept lowercased, values printable ASCII,
//! and the whole set is bounded so it always fits one xattr block. Errors
//! never quote a key or a value.

use std::collections::BTreeMap;

use thiserror::Error;

pub const METADATA_XATTR_PREFIX: &str = "user.rayito.";
pub const METADATA_MAX_KEYS: usize = 64;
/// Bound on `Σ(len(prefix) + len(key) + len(value))` over the whole set.
pub const METADATA_MAX_BYTES: usize = 4000;
pub const METADATA_KEY_MAX_BYTES: usize = 255;

#[derive(Debug, Error, Clone, Copy, PartialEq, Eq)]
#[error("metadatos inválidos")]
pub struct InvalidMetadata;

/// A validated metadata set, keys lowercased and sorted.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct FileMetadata(BTreeMap<String, String>);

impl FileMetadata {
    /// Validates a whole set as a client sent it: every key a token, no two
    /// keys equal once lowercased, every value printable ASCII, at most
    /// `METADATA_MAX_KEYS` keys and `METADATA_MAX_BYTES` in total.
    pub fn parse<I, K, V>(entries: I) -> Result<Self, InvalidMetadata>
    where
        I: IntoIterator<Item = (K, V)>,
        K: AsRef<str>,
        V: AsRef<str>,
    {
        let mut map = BTreeMap::new();
        let mut total = 0usize;
        for (key, value) in entries {
            let (key, value) = (key.as_ref(), value.as_ref());
            if !is_valid_key(key) || !is_valid_value(value) {
                return Err(InvalidMetadata);
            }
            total = total.saturating_add(entry_bytes(key, value));
            if map
                .insert(key.to_ascii_lowercase(), value.to_owned())
                .is_some()
            {
                return Err(InvalidMetadata);
            }
        }
        if map.len() > METADATA_MAX_KEYS || total > METADATA_MAX_BYTES {
            return Err(InvalidMetadata);
        }
        Ok(Self(map))
    }

    /// Rebuilds the set from the `user.*` attributes of a file. Anything a
    /// sandbox process stored under the prefix that the write rules would
    /// refuse is skipped rather than failing the listing that reads it.
    pub fn from_xattrs<I>(attributes: I) -> Self
    where
        I: IntoIterator<Item = (String, Vec<u8>)>,
    {
        let map = attributes
            .into_iter()
            .filter_map(|(name, value)| {
                let key = name.strip_prefix(METADATA_XATTR_PREFIX)?;
                let value = String::from_utf8(value).ok()?;
                (is_valid_key(key) && is_valid_value(&value))
                    .then(|| (key.to_ascii_lowercase(), value))
            })
            .take(METADATA_MAX_KEYS)
            .collect();
        Self(map)
    }

    /// The attribute name a key is stored under.
    #[must_use]
    pub fn xattr_name(key: &str) -> String {
        format!("{METADATA_XATTR_PREFIX}{key}")
    }

    /// Whether an attribute name belongs to this namespace.
    #[must_use]
    pub fn is_metadata_attribute(name: &str) -> bool {
        name.starts_with(METADATA_XATTR_PREFIX)
    }

    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }

    #[must_use]
    pub fn len(&self) -> usize {
        self.0.len()
    }

    pub fn iter(&self) -> impl Iterator<Item = (&str, &str)> {
        self.0
            .iter()
            .map(|(key, value)| (key.as_str(), value.as_str()))
    }

    #[must_use]
    pub fn into_map(self) -> BTreeMap<String, String> {
        self.0
    }
}

fn entry_bytes(key: &str, value: &str) -> usize {
    METADATA_XATTR_PREFIX.len() + key.len() + value.len()
}

/// RFC 9110 `token`: letters, digits and ``!#$%&'*+-.^_`|~``.
fn is_valid_key(key: &str) -> bool {
    (1..=METADATA_KEY_MAX_BYTES).contains(&key.len()) && key.bytes().all(is_token_byte)
}

fn is_token_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || b"!#$%&'*+-.^_`|~".contains(&byte)
}

fn is_valid_value(value: &str) -> bool {
    value.bytes().all(|byte| (0x20..=0x7e).contains(&byte))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(entries: &[(&str, &str)]) -> Result<FileMetadata, InvalidMetadata> {
        FileMetadata::parse(entries.iter().copied())
    }

    #[test]
    fn keys_are_lowercased_and_sorted() {
        let metadata = parse(&[("Owner", "alice"), ("a-b.c_d", "")]).unwrap();
        assert_eq!(
            metadata.iter().collect::<Vec<_>>(),
            vec![("a-b.c_d", ""), ("owner", "alice")]
        );
        assert_eq!(metadata.len(), 2);
        assert!(parse(&[]).unwrap().is_empty());
    }

    #[test]
    fn token_characters_are_accepted_and_the_rest_refused() {
        assert!(parse(&[("!#$%&'*+-.^_`|~Az09", "v")]).is_ok());
        for key in ["a b", "", "a:b", "a/b", "ñ", "a\"b", "a(b)", "a\tb", "a=b"] {
            assert_eq!(parse(&[(key, "v")]), Err(InvalidMetadata), "{key:?}");
        }
        assert!(parse(&[(&"k".repeat(255), "v")]).is_ok());
        assert_eq!(parse(&[(&"k".repeat(256), "v")]), Err(InvalidMetadata));
    }

    #[test]
    fn values_must_be_printable_ascii() {
        assert!(parse(&[("k", " ~printable 123")]).is_ok());
        for value in ["tab\there", "new\nline", "ñ", "\u{7f}"] {
            assert_eq!(parse(&[("k", value)]), Err(InvalidMetadata), "{value:?}");
        }
    }

    #[test]
    fn duplicate_keys_after_lowercasing_are_refused() {
        assert_eq!(
            parse(&[("Owner", "a"), ("owner", "b")]),
            Err(InvalidMetadata)
        );
    }

    #[test]
    fn the_key_count_and_byte_budget_are_bounded() {
        let keys: Vec<String> = (0..METADATA_MAX_KEYS).map(|i| format!("k{i}")).collect();
        assert!(FileMetadata::parse(keys.iter().map(|k| (k.as_str(), "v"))).is_ok());
        let one_more: Vec<String> = (0..=METADATA_MAX_KEYS).map(|i| format!("k{i}")).collect();
        assert_eq!(
            FileMetadata::parse(one_more.iter().map(|k| (k.as_str(), "v"))),
            Err(InvalidMetadata)
        );
        let fits = "v".repeat(METADATA_MAX_BYTES - METADATA_XATTR_PREFIX.len() - 1);
        assert!(parse(&[("k", &fits)]).is_ok());
        let over = format!("{fits}v");
        assert_eq!(parse(&[("k", &over)]), Err(InvalidMetadata));
    }

    #[test]
    fn xattrs_are_read_back_skipping_foreign_and_invalid_entries() {
        let metadata = FileMetadata::from_xattrs(vec![
            ("user.rayito.owner".to_owned(), b"alice".to_vec()),
            ("user.rayito.Team".to_owned(), b"core".to_vec()),
            ("user.other".to_owned(), b"x".to_vec()),
            ("user.rayito.bin".to_owned(), vec![0xff, 0x00]),
            ("user.rayito.a b".to_owned(), b"x".to_vec()),
        ]);
        assert_eq!(
            metadata.into_map().into_iter().collect::<Vec<_>>(),
            vec![
                ("owner".to_owned(), "alice".to_owned()),
                ("team".to_owned(), "core".to_owned())
            ]
        );
        assert_eq!(FileMetadata::xattr_name("owner"), "user.rayito.owner");
        assert!(FileMetadata::is_metadata_attribute("user.rayito.x"));
        assert!(!FileMetadata::is_metadata_attribute("user.other"));
    }

    #[test]
    fn the_error_never_quotes_input() {
        assert_eq!(InvalidMetadata.to_string(), "metadatos inválidos");
    }
}
