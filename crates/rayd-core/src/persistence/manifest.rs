//! `manifest.json` v1 (design D2): written after the archive is complete
//! in S3, so its presence means the archive is whole. Unknown keys are
//! ignored on read; `home` and `user` are informative only.

use serde::{Deserialize, Serialize};

use super::error::{ManifestRejection, PersistenceError};
use super::{ARCHIVE_KEY, MANIFEST_VERSION};

/// Largest manifest `rayd` reads; a bigger object is not one it wrote.
pub const MANIFEST_MAX_BYTES: usize = 64 * 1024;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Manifest {
    pub version: u32,
    pub archive: String,
    pub compression: String,
    pub sha256: String,
    pub archive_bytes: u64,
    pub files: u64,
    pub bytes: u64,
    pub skipped: u64,
    pub home: String,
    pub user: String,
    pub sandbox_id: String,
    pub agent_version: String,
    pub created_at: String,
    #[serde(default)]
    pub excluded: Vec<String>,
}

/// What a finished archive reports, from which the manifest is built.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ManifestInput<'a> {
    pub sha256: &'a str,
    pub archive_bytes: u64,
    pub files: u64,
    pub bytes: u64,
    pub skipped: u64,
    pub home: &'a str,
    pub user: &'a str,
    pub sandbox_id: &'a str,
    pub agent_version: &'a str,
    pub created_at: &'a str,
    pub excluded: &'a [String],
}

impl Manifest {
    #[must_use]
    pub fn v1(input: &ManifestInput<'_>) -> Self {
        Self {
            version: MANIFEST_VERSION,
            archive: ARCHIVE_KEY.to_owned(),
            compression: "gzip".to_owned(),
            sha256: input.sha256.to_owned(),
            archive_bytes: input.archive_bytes,
            files: input.files,
            bytes: input.bytes,
            skipped: input.skipped,
            home: input.home.to_owned(),
            user: input.user.to_owned(),
            sandbox_id: input.sandbox_id.to_owned(),
            agent_version: input.agent_version.to_owned(),
            created_at: input.created_at.to_owned(),
            excluded: input.excluded.to_vec(),
        }
    }

    pub fn parse(bytes: &[u8]) -> Result<Self, PersistenceError> {
        if bytes.len() > MANIFEST_MAX_BYTES {
            return Err(PersistenceError::InvalidManifest(
                ManifestRejection::TooLarge,
            ));
        }
        let manifest: Self = serde_json::from_slice(bytes)
            .map_err(|_| PersistenceError::InvalidManifest(ManifestRejection::Malformed))?;
        manifest.validate()?;
        Ok(manifest)
    }

    pub fn validate(&self) -> Result<(), PersistenceError> {
        if self.version != MANIFEST_VERSION {
            return Err(PersistenceError::InvalidManifest(
                ManifestRejection::Version,
            ));
        }
        if self.sha256.is_empty() || !self.sha256.bytes().all(|byte| byte.is_ascii_hexdigit()) {
            return Err(PersistenceError::InvalidManifest(
                ManifestRejection::Checksum,
            ));
        }
        if self.archive != ARCHIVE_KEY || self.compression != "gzip" {
            return Err(PersistenceError::InvalidManifest(
                ManifestRejection::Archive,
            ));
        }
        Ok(())
    }

    pub fn to_json(&self) -> Result<Vec<u8>, PersistenceError> {
        serde_json::to_vec(self)
            .map_err(|_| PersistenceError::InvalidManifest(ManifestRejection::Malformed))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample() -> Manifest {
        Manifest::v1(&ManifestInput {
            sha256: "ab".repeat(32).as_str(),
            archive_bytes: 10,
            files: 3,
            bytes: 7,
            skipped: 1,
            home: "/home/user",
            user: "user",
            sandbox_id: "mvm-1",
            agent_version: "0.2.0",
            created_at: "2026-09-16T12:00:00Z",
            excluded: &["skipme".to_owned()],
        })
    }

    #[test]
    fn golden_round_trip() {
        let manifest = sample();
        let json = manifest.to_json().unwrap();
        let text = String::from_utf8(json.clone()).unwrap();
        assert!(
            text.starts_with("{\"version\":1,\"archive\":\"home.tar.gz\",\"compression\":\"gzip\"")
        );
        assert!(text.contains("\"excluded\":[\"skipme\"]"));
        assert_eq!(Manifest::parse(&json).unwrap(), manifest);
    }

    #[test]
    fn unknown_keys_are_ignored_and_bad_manifests_refused() {
        let mut value: serde_json::Value =
            serde_json::from_slice(&sample().to_json().unwrap()).unwrap();
        value["future"] = serde_json::json!({"x": 1});
        value.as_object_mut().unwrap().remove("excluded");
        let parsed = Manifest::parse(value.to_string().as_bytes()).unwrap();
        assert!(parsed.excluded.is_empty());
        let mut wrong_version = value.clone();
        wrong_version["version"] = serde_json::json!(2);
        assert_eq!(
            Manifest::parse(wrong_version.to_string().as_bytes()).unwrap_err(),
            PersistenceError::InvalidManifest(ManifestRejection::Version)
        );
        let mut empty_sha = value.clone();
        empty_sha["sha256"] = serde_json::json!("");
        assert_eq!(
            Manifest::parse(empty_sha.to_string().as_bytes()).unwrap_err(),
            PersistenceError::InvalidManifest(ManifestRejection::Checksum)
        );
        let mut other_archive = value.clone();
        other_archive["archive"] = serde_json::json!("home.tar.zst");
        assert_eq!(
            Manifest::parse(other_archive.to_string().as_bytes()).unwrap_err(),
            PersistenceError::InvalidManifest(ManifestRejection::Archive)
        );
        assert_eq!(
            Manifest::parse(b"not json").unwrap_err(),
            PersistenceError::InvalidManifest(ManifestRejection::Malformed)
        );
        assert_eq!(
            Manifest::parse(&vec![b'{'; MANIFEST_MAX_BYTES + 1]).unwrap_err(),
            PersistenceError::InvalidManifest(ManifestRejection::TooLarge)
        );
    }
}
