//! Where a persisted home lives in S3 (design D2): a validated bucket
//! name, a validated key prefix and the two object keys `rayd` derives
//! from them. Both parsers refuse before any network call and never quote
//! the offending input in their errors.

use std::fmt;

use super::{ARCHIVE_KEY, MANIFEST_KEY, PERSIST_KEY_PREFIX_MAX_BYTES};

pub const BUCKET_NAME_MIN: usize = 3;
pub const BUCKET_NAME_MAX: usize = 63;

/// Why a bucket name was refused; never carries the name.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BucketRejection {
    Length,
    Charset,
    Edge,
    DoubleDot,
    Ipv4Literal,
}

impl fmt::Display for BucketRejection {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Length => "must be 3 to 63 characters",
            Self::Charset => "may only contain lowercase letters, digits, `.` and `-`",
            Self::Edge => "must start and end with a letter or a digit",
            Self::DoubleDot => "must not contain `..`",
            Self::Ipv4Literal => "must not look like an IPv4 address",
        })
    }
}

/// Why a key prefix was refused; never carries the prefix.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum KeyPrefixRejection {
    Empty,
    TooLong,
    LeadingSlash,
    TrailingSlash,
    EmptyComponent,
    DotComponent,
    Charset,
}

impl fmt::Display for KeyPrefixRejection {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Empty => "is empty",
            Self::TooLong => "exceeds 900 bytes",
            Self::LeadingSlash => "starts with `/`",
            Self::TrailingSlash => "ends with `/`",
            Self::EmptyComponent => "contains an empty component",
            Self::DotComponent => "contains a `.` or `..` component",
            Self::Charset => "contains a character outside the S3 safe set",
        })
    }
}

/// An S3 bucket name that passed the D2 rules.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct BucketName(String);

impl BucketName {
    pub fn parse(raw: &str) -> Result<Self, BucketRejection> {
        if !(BUCKET_NAME_MIN..=BUCKET_NAME_MAX).contains(&raw.len()) {
            return Err(BucketRejection::Length);
        }
        if !raw.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'.' || byte == b'-'
        }) {
            return Err(BucketRejection::Charset);
        }
        if !starts_and_ends_alphanumeric(raw) {
            return Err(BucketRejection::Edge);
        }
        if raw.contains("..") {
            return Err(BucketRejection::DoubleDot);
        }
        if looks_like_ipv4(raw) {
            return Err(BucketRejection::Ipv4Literal);
        }
        Ok(Self(raw.to_owned()))
    }

    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for BucketName {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

fn starts_and_ends_alphanumeric(raw: &str) -> bool {
    let first = raw.bytes().next();
    let last = raw.bytes().next_back();
    matches!((first, last), (Some(a), Some(b)) if a.is_ascii_alphanumeric() && b.is_ascii_alphanumeric())
}

fn looks_like_ipv4(raw: &str) -> bool {
    let octets: Vec<&str> = raw.split('.').collect();
    octets.len() == 4
        && octets
            .iter()
            .all(|octet| !octet.is_empty() && octet.bytes().all(|byte| byte.is_ascii_digit()))
}

/// A key prefix that passed the D2 rules: `rayd` appends `/home.tar.gz`
/// and `/manifest.json` to it.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct KeyPrefix(String);

impl KeyPrefix {
    pub fn parse(raw: &str) -> Result<Self, KeyPrefixRejection> {
        if raw.is_empty() {
            return Err(KeyPrefixRejection::Empty);
        }
        if raw.len() > PERSIST_KEY_PREFIX_MAX_BYTES {
            return Err(KeyPrefixRejection::TooLong);
        }
        if raw.starts_with('/') {
            return Err(KeyPrefixRejection::LeadingSlash);
        }
        if raw.ends_with('/') {
            return Err(KeyPrefixRejection::TrailingSlash);
        }
        for component in raw.split('/') {
            if component.is_empty() {
                return Err(KeyPrefixRejection::EmptyComponent);
            }
            if component == "." || component == ".." {
                return Err(KeyPrefixRejection::DotComponent);
            }
        }
        if !raw.bytes().all(is_safe_key_byte) {
            return Err(KeyPrefixRejection::Charset);
        }
        Ok(Self(raw.to_owned()))
    }

    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }

    #[must_use]
    pub fn object_keys(&self) -> ObjectKeys {
        ObjectKeys {
            archive: format!("{}/{ARCHIVE_KEY}", self.0),
            manifest: format!("{}/{MANIFEST_KEY}", self.0),
        }
    }
}

impl fmt::Display for KeyPrefix {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

/// The S3 "safe" character set plus `/` as the separator.
fn is_safe_key_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || b"!_.*'()/-".contains(&byte)
}

/// The two objects one persisted home is made of.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ObjectKeys {
    pub archive: String,
    pub manifest: String,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bucket_rules() {
        assert_eq!(
            BucketName::parse("amzn-s3-demo-bucket-123456789012-us-east-1")
                .unwrap()
                .as_str(),
            "amzn-s3-demo-bucket-123456789012-us-east-1"
        );
        assert!(BucketName::parse("a.b").is_ok());
        assert_eq!(BucketName::parse("ab"), Err(BucketRejection::Length));
        assert_eq!(
            BucketName::parse(&"a".repeat(64)),
            Err(BucketRejection::Length)
        );
        assert_eq!(BucketName::parse("Bucket"), Err(BucketRejection::Charset));
        assert_eq!(
            BucketName::parse("my_bucket"),
            Err(BucketRejection::Charset)
        );
        assert_eq!(BucketName::parse("-abc"), Err(BucketRejection::Edge));
        assert_eq!(BucketName::parse("abc."), Err(BucketRejection::Edge));
        assert_eq!(BucketName::parse("a..b"), Err(BucketRejection::DoubleDot));
        assert_eq!(
            BucketName::parse("192.168.0.1"),
            Err(BucketRejection::Ipv4Literal)
        );
        assert!(BucketName::parse("192.168.0.a").is_ok());
        assert_eq!(
            BucketRejection::Ipv4Literal.to_string(),
            "must not look like an IPv4 address"
        );
    }

    #[test]
    fn key_prefix_rules() {
        let prefix = KeyPrefix::parse("rayito/e2e-1").unwrap();
        assert_eq!(prefix.as_str(), "rayito/e2e-1");
        let keys = prefix.object_keys();
        assert_eq!(keys.archive, "rayito/e2e-1/home.tar.gz");
        assert_eq!(keys.manifest, "rayito/e2e-1/manifest.json");
        assert!(KeyPrefix::parse("a!b_c.d*e'f(g)h-i").is_ok());
        assert_eq!(KeyPrefix::parse(""), Err(KeyPrefixRejection::Empty));
        assert_eq!(
            KeyPrefix::parse(&"a".repeat(901)),
            Err(KeyPrefixRejection::TooLong)
        );
        assert!(KeyPrefix::parse(&"a".repeat(900)).is_ok());
        assert_eq!(
            KeyPrefix::parse("/rayito"),
            Err(KeyPrefixRejection::LeadingSlash)
        );
        assert_eq!(
            KeyPrefix::parse("rayito/"),
            Err(KeyPrefixRejection::TrailingSlash)
        );
        assert_eq!(
            KeyPrefix::parse("rayito//x"),
            Err(KeyPrefixRejection::EmptyComponent)
        );
        assert_eq!(
            KeyPrefix::parse("rayito/../x"),
            Err(KeyPrefixRejection::DotComponent)
        );
        assert_eq!(
            KeyPrefix::parse("rayito/./x"),
            Err(KeyPrefixRejection::DotComponent)
        );
        assert_eq!(
            KeyPrefix::parse("ray ito"),
            Err(KeyPrefixRejection::Charset)
        );
        assert_eq!(KeyPrefix::parse("rayitó"), Err(KeyPrefixRejection::Charset));
        assert_eq!(KeyPrefix::parse("a&b"), Err(KeyPrefixRejection::Charset));
        assert_eq!(
            KeyPrefixRejection::Charset.to_string(),
            "contains a character outside the S3 safe set"
        );
    }
}
