//! `TemplateSpecSource` (m15-templates, investigación §3.5): reads
//! `/etc/rayito/template.json` once, at boot. Every failure mode (no file,
//! unreadable, malformed, future/unknown `version`) answers `None` rather
//! than an error: an image built before `Template.build()` baked this file
//! in, or baked an incompatible one, must boot exactly like 0.5.x
//! (ADR-022: "ningún `Sandbox.create()` existente cambia de
//! comportamiento"), not refuse to start.

use std::path::{Path, PathBuf};

use rayd_core::template::{StartSpec, TEMPLATE_SPEC_PATH, TEMPLATE_SPEC_VERSION};

pub trait TemplateSpecSource: Send + Sync {
    /// `None` on anything other than "a well-formed, known-version spec is
    /// present"; `fs_template_spec`'s own `tracing::warn!` is the only
    /// place that distinguishes "absent" from "present but rejected" (the
    /// caller does not need to).
    fn read(&self) -> Option<StartSpec>;
}

/// Reads `path` (`TEMPLATE_SPEC_PATH` by default) with `std::fs::read_to_string`:
/// synchronous and quick enough for a one-shot boot read that nothing else
/// depends on starting.
pub struct FsTemplateSpecSource {
    path: PathBuf,
}

impl FsTemplateSpecSource {
    #[must_use]
    pub fn new(path: impl Into<PathBuf>) -> Self {
        Self { path: path.into() }
    }

    #[must_use]
    pub fn at_image_path() -> Self {
        Self::new(TEMPLATE_SPEC_PATH)
    }
}

impl Default for FsTemplateSpecSource {
    fn default() -> Self {
        Self::at_image_path()
    }
}

impl TemplateSpecSource for FsTemplateSpecSource {
    fn read(&self) -> Option<StartSpec> {
        read_spec(&self.path)
    }
}

fn read_spec(path: &Path) -> Option<StartSpec> {
    let raw = match std::fs::read_to_string(path) {
        Ok(raw) => raw,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return None,
        Err(error) => {
            tracing::warn!(
                path = %path.display(),
                error = %error,
                "template spec could not be read; booting as a 0.5.x image"
            );
            return None;
        }
    };
    let spec: StartSpec = match serde_json::from_str(&raw) {
        Ok(spec) => spec,
        Err(error) => {
            tracing::warn!(
                path = %path.display(),
                error = %error,
                "template spec is malformed JSON; booting as a 0.5.x image"
            );
            return None;
        }
    };
    if spec.version != TEMPLATE_SPEC_VERSION {
        tracing::warn!(
            path = %path.display(),
            version = %spec.version,
            expected = TEMPLATE_SPEC_VERSION,
            "template spec version is not one this rayd understands; booting as a 0.5.x image"
        );
        return None;
    }
    Some(spec)
}

#[cfg(test)]
mod tests {
    use std::io::Write;

    use super::*;

    fn write(dir: &tempfile::TempDir, name: &str, contents: &str) -> PathBuf {
        let path = dir.path().join(name);
        let mut file = std::fs::File::create(&path).expect("create fixture");
        file.write_all(contents.as_bytes()).expect("write fixture");
        path
    }

    #[test]
    fn a_missing_file_is_absent_without_a_warning() {
        let dir = tempfile::tempdir().expect("tempdir");
        let source = FsTemplateSpecSource::new(dir.path().join("does-not-exist.json"));
        assert_eq!(source.read(), None);
    }

    #[test]
    fn a_well_formed_spec_is_parsed() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = write(
            &dir,
            "template.json",
            r#"{"version":"rayito.template/1","start_cmd":"python app.py","ready_cmd":null,
               "user":"1000","workdir":null,"envs":{},"ready_poll":null}"#,
        );
        let spec = FsTemplateSpecSource::new(path).read().expect("parsed");
        assert_eq!(spec.start_cmd, "python app.py");
        assert_eq!(spec.user, "1000");
    }

    #[test]
    fn malformed_json_is_absent() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = write(&dir, "template.json", "{not json");
        assert_eq!(FsTemplateSpecSource::new(path).read(), None);
    }

    #[test]
    fn an_unknown_version_is_absent() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = write(
            &dir,
            "template.json",
            r#"{"version":"rayito.template/2","start_cmd":"x","ready_cmd":null,
               "user":"1000","workdir":null,"envs":{},"ready_poll":null}"#,
        );
        assert_eq!(FsTemplateSpecSource::new(path).read(), None);
    }

    #[test]
    fn the_default_source_points_at_the_image_path() {
        assert_eq!(
            FsTemplateSpecSource::default().path,
            PathBuf::from(TEMPLATE_SPEC_PATH)
        );
    }
}
