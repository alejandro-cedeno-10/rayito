//! The per-VM payload the SDK passes to `run-microvm` as `runHookPayload`.
//! AWS delivers it verbatim inside the `/run` hook body; it is the only
//! per-MicroVM input channel (ADR-004). Wire shape, versioned by `rayd`:
//!
//! `{"v":1,"token_sha256":"<64 hex>","envs":{...},"user":"user","workdir":"/home/user",
//!  "metadata":{...},"limits":{"cpu_seconds":N}}`
//!
//! `metadata` (client labels echoed by `Health`) and `limits` (per-process
//! `RLIMIT_CPU` for the sandbox's processes and PTYs) are optional; `v`
//! stays 1 because an agent that ignores them still behaves.
//!
//! Errors never quote payload content: the payload may carry a digest and
//! environment values, and hook logs must stay free of both.

use std::collections::BTreeMap;

use serde::Deserialize;
use thiserror::Error;

use crate::auth::{AuthError, TokenDigest};

/// Hard limit of the `runHookPayload` field in the service model.
pub const RUN_HOOK_PAYLOAD_MAX_CHARS: usize = 4096;
/// The only payload version this agent understands.
pub const SUPPORTED_VERSION: u64 = 1;
/// `limits.cpu_seconds` bounds: at least one second, at most the longest
/// life of a `MicroVM` (8 h, ADR-007).
pub const MIN_CPU_SECONDS: u64 = 1;
pub const MAX_CPU_SECONDS: u64 = 28_800;

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum RunPayloadError {
    #[error("runHookPayload absent from the /run body")]
    Missing,
    #[error("payload has {actual} characters, the limit is {RUN_HOOK_PAYLOAD_MAX_CHARS}")]
    TooLarge { actual: usize },
    #[error("payload is not a JSON object (line {line}, column {column})")]
    MalformedJson { line: usize, column: usize },
    #[error("payload version {actual} is not supported (expected {SUPPORTED_VERSION})")]
    UnsupportedVersion { actual: u64 },
    #[error("payload field `{0}` is missing")]
    MissingField(&'static str),
    #[error("payload field `token_sha256` is invalid: {0}")]
    InvalidDigest(AuthError),
    #[error("payload field `limits.cpu_seconds` is outside {MIN_CPU_SECONDS}..={MAX_CPU_SECONDS}")]
    InvalidLimits,
}

/// Sandbox-wide defaults carried by the payload, consumed from M2 on when
/// spawning processes.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct RunDefaults {
    pub envs: BTreeMap<String, String>,
    pub user: Option<String>,
    pub workdir: Option<String>,
    /// `RLIMIT_CPU` soft limit for every process and PTY of the sandbox;
    /// `None` = unlimited. Never applied to the sidecar or the kernels.
    pub cpu_seconds: Option<u64>,
}

#[derive(Debug, PartialEq, Eq)]
pub struct RunPayload {
    pub token_digest: TokenDigest,
    pub defaults: RunDefaults,
    pub metadata: BTreeMap<String, String>,
}

#[derive(Deserialize)]
struct RunPayloadWire {
    v: Option<u64>,
    token_sha256: Option<String>,
    #[serde(default)]
    envs: BTreeMap<String, String>,
    user: Option<String>,
    workdir: Option<String>,
    #[serde(default)]
    metadata: BTreeMap<String, String>,
    limits: Option<LimitsWire>,
}

#[derive(Deserialize)]
struct LimitsWire {
    cpu_seconds: Option<u64>,
}

fn validated_cpu_seconds(limits: Option<LimitsWire>) -> Result<Option<u64>, RunPayloadError> {
    match limits.and_then(|limits| limits.cpu_seconds) {
        None => Ok(None),
        Some(seconds) if (MIN_CPU_SECONDS..=MAX_CPU_SECONDS).contains(&seconds) => {
            Ok(Some(seconds))
        }
        Some(_) => Err(RunPayloadError::InvalidLimits),
    }
}

pub fn parse_run_payload(raw: &str) -> Result<RunPayload, RunPayloadError> {
    let chars = raw.chars().count();
    if chars > RUN_HOOK_PAYLOAD_MAX_CHARS {
        return Err(RunPayloadError::TooLarge { actual: chars });
    }
    let wire: RunPayloadWire =
        serde_json::from_str(raw).map_err(|error| RunPayloadError::MalformedJson {
            line: error.line(),
            column: error.column(),
        })?;
    let version = wire.v.ok_or(RunPayloadError::MissingField("v"))?;
    if version != SUPPORTED_VERSION {
        return Err(RunPayloadError::UnsupportedVersion { actual: version });
    }
    let digest_hex = wire
        .token_sha256
        .ok_or(RunPayloadError::MissingField("token_sha256"))?;
    let token_digest =
        TokenDigest::from_hex(&digest_hex).map_err(RunPayloadError::InvalidDigest)?;
    let cpu_seconds = validated_cpu_seconds(wire.limits)?;
    Ok(RunPayload {
        token_digest,
        defaults: RunDefaults {
            envs: wire.envs,
            user: wire.user,
            workdir: wire.workdir,
            cpu_seconds,
        },
        metadata: wire.metadata,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    const DIGEST_HEX: &str = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

    fn payload(fields: &str) -> String {
        format!("{{\"v\":1,\"token_sha256\":\"{DIGEST_HEX}\"{fields}}}")
    }

    #[test]
    fn parses_the_minimal_payload() {
        let parsed = parse_run_payload(&payload("")).unwrap();
        assert_eq!(
            parsed.token_digest,
            TokenDigest::from_hex(DIGEST_HEX).unwrap()
        );
        assert_eq!(parsed.defaults, RunDefaults::default());
    }

    #[test]
    fn parses_envs_user_and_workdir() {
        let parsed = parse_run_payload(&payload(
            ",\"envs\":{\"A\":\"1\"},\"user\":\"user\",\"workdir\":\"/home/user\"",
        ))
        .unwrap();
        assert_eq!(parsed.defaults.envs.get("A").map(String::as_str), Some("1"));
        assert_eq!(parsed.defaults.user.as_deref(), Some("user"));
        assert_eq!(parsed.defaults.workdir.as_deref(), Some("/home/user"));
    }

    #[test]
    fn rejects_payloads_over_the_model_limit() {
        let padding = "x".repeat(RUN_HOOK_PAYLOAD_MAX_CHARS);
        let raw = payload(&format!(",\"note\":\"{padding}\""));
        assert!(matches!(
            parse_run_payload(&raw),
            Err(RunPayloadError::TooLarge { actual }) if actual > RUN_HOOK_PAYLOAD_MAX_CHARS
        ));
    }

    #[test]
    fn rejects_malformed_json_without_quoting_it() {
        let error = parse_run_payload("{\"v\":1,").unwrap_err();
        assert!(matches!(error, RunPayloadError::MalformedJson { .. }));
        assert!(!error.to_string().contains("token"));
    }

    #[test]
    fn rejects_missing_version_and_missing_digest() {
        assert_eq!(
            parse_run_payload(&format!("{{\"token_sha256\":\"{DIGEST_HEX}\"}}")),
            Err(RunPayloadError::MissingField("v"))
        );
        assert_eq!(
            parse_run_payload("{\"v\":1}"),
            Err(RunPayloadError::MissingField("token_sha256"))
        );
    }

    #[test]
    fn rejects_unknown_versions() {
        let raw = format!("{{\"v\":2,\"token_sha256\":\"{DIGEST_HEX}\"}}");
        assert_eq!(
            parse_run_payload(&raw),
            Err(RunPayloadError::UnsupportedVersion { actual: 2 })
        );
    }

    #[test]
    fn rejects_a_malformed_digest() {
        let raw = "{\"v\":1,\"token_sha256\":\"deadbeef\"}";
        assert_eq!(
            parse_run_payload(raw),
            Err(RunPayloadError::InvalidDigest(AuthError::MalformedDigest {
                actual: 8
            }))
        );
    }

    #[test]
    fn limits_are_optional_and_bounded() {
        let absent = parse_run_payload(&payload("")).unwrap();
        assert_eq!(absent.defaults.cpu_seconds, None);
        let empty = parse_run_payload(&payload(",\"limits\":{}")).unwrap();
        assert_eq!(empty.defaults.cpu_seconds, None);
        let present = parse_run_payload(&payload(",\"limits\":{\"cpu_seconds\":2}")).unwrap();
        assert_eq!(present.defaults.cpu_seconds, Some(2));
        let max = parse_run_payload(&payload(",\"limits\":{\"cpu_seconds\":28800}")).unwrap();
        assert_eq!(max.defaults.cpu_seconds, Some(28_800));
        for bad in [
            ",\"limits\":{\"cpu_seconds\":0}",
            ",\"limits\":{\"cpu_seconds\":28801}",
        ] {
            assert_eq!(
                parse_run_payload(&payload(bad)),
                Err(RunPayloadError::InvalidLimits),
                "{bad}"
            );
        }
        let wrong_type = parse_run_payload(&payload(",\"limits\":{\"cpu_seconds\":\"2\"}"));
        assert!(matches!(
            wrong_type,
            Err(RunPayloadError::MalformedJson { .. })
        ));
        assert!(!RunPayloadError::InvalidLimits.to_string().contains("token"));
    }

    #[test]
    fn metadata_is_optional_and_ignores_unknown_keys() {
        let absent = parse_run_payload(&payload("")).unwrap();
        assert!(absent.metadata.is_empty());
        let empty = parse_run_payload(&payload(",\"metadata\":{}")).unwrap();
        assert!(empty.metadata.is_empty());
        let present = parse_run_payload(&payload(
            ",\"metadata\":{\"team\":\"data\",\"job\":\"7\"},\"future\":true",
        ))
        .unwrap();
        assert_eq!(present.metadata.len(), 2);
        assert_eq!(present.metadata.get("job").map(String::as_str), Some("7"));
        let non_string = parse_run_payload(&payload(",\"metadata\":{\"n\":1}"));
        assert!(matches!(
            non_string,
            Err(RunPayloadError::MalformedJson { .. })
        ));
    }
}
