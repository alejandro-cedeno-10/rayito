//! The persistence scope `/run` binds to the sandbox (C-07): the bucket
//! and the operator's base prefix the SDK sent in the `runHookPayload`'s
//! `persist` block. Once bound, every `Checkpoint` and `Restore` must name
//! a location inside it, so an access token reaches only the homes under
//! its own sandbox's prefix even when one execution role covers the whole
//! bucket. The region is not part of the scope: a bucket name is unique in
//! its partition, so the bucket alone says whose data it is.

use std::fmt;

use super::keys::{BucketName, BucketRejection, KeyPrefix, KeyPrefixRejection};

/// Why the `persist` block of the payload was refused; never carries the
/// bucket or the prefix.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BindingRejection {
    MissingBucket,
    MissingKeyPrefix,
    Bucket(BucketRejection),
    KeyPrefix(KeyPrefixRejection),
}

impl fmt::Display for BindingRejection {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::MissingBucket => f.write_str("falta `bucket`"),
            Self::MissingKeyPrefix => f.write_str("falta `key_prefix`"),
            Self::Bucket(rejection) => write!(f, "`bucket` {rejection}"),
            Self::KeyPrefix(rejection) => write!(f, "`key_prefix` {rejection}"),
        }
    }
}

/// A validated bucket plus base prefix; a location is inside it when it
/// names the same bucket and a key prefix equal to the base or below it
/// at a `/` boundary.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PersistBinding {
    bucket: BucketName,
    key_prefix: KeyPrefix,
}

impl PersistBinding {
    /// Both values go through the same D2 rules a request's location does.
    pub fn parse(bucket: Option<&str>, key_prefix: Option<&str>) -> Result<Self, BindingRejection> {
        let bucket = bucket.ok_or(BindingRejection::MissingBucket)?;
        let key_prefix = key_prefix.ok_or(BindingRejection::MissingKeyPrefix)?;
        Ok(Self {
            bucket: BucketName::parse(bucket).map_err(BindingRejection::Bucket)?,
            key_prefix: KeyPrefix::parse(key_prefix).map_err(BindingRejection::KeyPrefix)?,
        })
    }

    /// Validated prefixes have no empty, `.` or `..` component, so a plain
    /// string comparison at a `/` boundary cannot be walked out of.
    #[must_use]
    pub fn admits(&self, bucket: &BucketName, key_prefix: &KeyPrefix) -> bool {
        if bucket != &self.bucket {
            return false;
        }
        let base = self.key_prefix.as_str();
        match key_prefix.as_str().strip_prefix(base) {
            Some(rest) => rest.is_empty() || rest.starts_with('/'),
            None => false,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn binding() -> PersistBinding {
        PersistBinding::parse(Some("amzn-s3-demo-bucket"), Some("tenants/acme")).unwrap()
    }

    fn admits(bucket: &str, key_prefix: &str) -> bool {
        binding().admits(
            &BucketName::parse(bucket).unwrap(),
            &KeyPrefix::parse(key_prefix).unwrap(),
        )
    }

    #[test]
    fn admits_the_base_and_what_lies_below_it() {
        assert!(admits("amzn-s3-demo-bucket", "tenants/acme"));
        assert!(admits("amzn-s3-demo-bucket", "tenants/acme/agent-7"));
        assert!(admits("amzn-s3-demo-bucket", "tenants/acme/a/b"));
    }

    #[test]
    fn refuses_a_sibling_prefix_a_parent_and_another_bucket() {
        assert!(!admits("amzn-s3-demo-bucket", "tenants/acme-evil/agent-7"));
        assert!(!admits("amzn-s3-demo-bucket", "tenants/acmex"));
        assert!(!admits("amzn-s3-demo-bucket", "tenants"));
        assert!(!admits("amzn-s3-demo-bucket", "tenants/other/agent-7"));
        assert!(!admits("amzn-s3-demo-bucket", "x/tenants/acme"));
        assert!(!admits("other-bucket", "tenants/acme/agent-7"));
    }

    #[test]
    fn the_payload_block_needs_both_fields_and_the_d2_rules() {
        assert_eq!(
            PersistBinding::parse(None, Some("p")),
            Err(BindingRejection::MissingBucket)
        );
        assert_eq!(
            PersistBinding::parse(Some("amzn-s3-demo-bucket"), None),
            Err(BindingRejection::MissingKeyPrefix)
        );
        assert!(matches!(
            PersistBinding::parse(Some("B"), Some("p")),
            Err(BindingRejection::Bucket(_))
        ));
        assert!(matches!(
            PersistBinding::parse(Some("amzn-s3-demo-bucket"), Some("a/../b")),
            Err(BindingRejection::KeyPrefix(
                KeyPrefixRejection::DotComponent
            ))
        ));
        let rejection = PersistBinding::parse(Some("Secret-Bucket"), Some("p")).unwrap_err();
        assert!(!rejection.to_string().contains("Secret"));
    }
}
