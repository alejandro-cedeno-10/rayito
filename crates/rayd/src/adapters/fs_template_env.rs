//! Reads `/etc/rayito/template-env.json` once, at boot (ADR-035): the
//! variables a template declared with `set_envs()`, which the image's
//! `ENV` only gave to `rayd` itself. Every failure mode (no file,
//! unreadable, malformed, unknown `version`) answers an empty map rather
//! than an error: an image built before this file existed boots exactly as
//! before. The warnings name the path and the error class only, never a
//! variable or its value.

use std::collections::BTreeMap;
use std::path::Path;

use rayd_core::template::{TEMPLATE_ENV_PATH, TEMPLATE_ENV_VERSION, TemplateEnv};

/// The template variables of this image (`TEMPLATE_ENV_PATH`).
#[must_use]
pub fn read_image_template_envs() -> BTreeMap<String, String> {
    read_template_envs(Path::new(TEMPLATE_ENV_PATH))
}

/// The template variables in `path`; empty when the file is absent or not
/// one this `rayd` understands.
#[must_use]
pub fn read_template_envs(path: &Path) -> BTreeMap<String, String> {
    let raw = match std::fs::read_to_string(path) {
        Ok(raw) => raw,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return BTreeMap::new(),
        Err(error) => {
            tracing::warn!(
                path = %path.display(),
                kind = ?error.kind(),
                "template env file could not be read; starting without template envs"
            );
            return BTreeMap::new();
        }
    };
    let parsed: TemplateEnv = match serde_json::from_str(&raw) {
        Ok(parsed) => parsed,
        Err(error) => {
            tracing::warn!(
                path = %path.display(),
                category = ?error.classify(),
                "template env file is malformed; starting without template envs"
            );
            return BTreeMap::new();
        }
    };
    if parsed.version != TEMPLATE_ENV_VERSION {
        tracing::warn!(
            path = %path.display(),
            expected = TEMPLATE_ENV_VERSION,
            "template env file version is not one this rayd understands; starting without template envs"
        );
        return BTreeMap::new();
    }
    parsed.envs
}

#[cfg(test)]
mod tests {
    use std::io::Write;
    use std::path::PathBuf;

    use super::*;

    fn write(dir: &tempfile::TempDir, contents: &str) -> PathBuf {
        let path = dir.path().join("template-env.json");
        let mut file = std::fs::File::create(&path).expect("create fixture");
        file.write_all(contents.as_bytes()).expect("write fixture");
        path
    }

    #[test]
    fn a_missing_file_has_no_envs() {
        let dir = tempfile::tempdir().expect("tempdir");
        assert!(read_template_envs(&dir.path().join("absent.json")).is_empty());
    }

    #[test]
    fn a_well_formed_file_gives_its_envs() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = write(
            &dir,
            r#"{"version":"rayito.template-env/1","envs":{"MODE":"prod","PATH":"/opt/bin:/usr/bin"}}"#,
        );
        let envs = read_template_envs(&path);
        assert_eq!(envs.get("MODE").map(String::as_str), Some("prod"));
        assert_eq!(
            envs.get("PATH").map(String::as_str),
            Some("/opt/bin:/usr/bin")
        );
    }

    #[test]
    fn malformed_json_has_no_envs() {
        let dir = tempfile::tempdir().expect("tempdir");
        assert!(read_template_envs(&write(&dir, "{not json")).is_empty());
    }

    #[test]
    fn an_unknown_version_has_no_envs() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = write(
            &dir,
            r#"{"version":"rayito.template-env/2","envs":{"A":"1"}}"#,
        );
        assert!(read_template_envs(&path).is_empty());
    }

    #[test]
    fn the_shared_vector_parses_as_the_sdks_write_it() {
        let vector = include_str!("../../../../testdata/templates/template-env.json");
        let cases: serde_json::Value = serde_json::from_str(vector).expect("vector");
        for case in cases["cases"].as_array().expect("cases") {
            let dir = tempfile::tempdir().expect("tempdir");
            let path = write(&dir, case["file"].as_str().expect("file"));
            let expected: BTreeMap<String, String> =
                serde_json::from_value(case["envs"].clone()).expect("envs");
            assert_eq!(read_template_envs(&path), expected, "{}", case["name"]);
        }
    }
}
