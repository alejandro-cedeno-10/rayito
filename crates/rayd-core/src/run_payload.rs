//! The per-VM payload the SDK passes to `run-microvm` as `runHookPayload`.
//! AWS delivers it verbatim inside the `/run` hook body; it is the only
//! per-MicroVM input channel (ADR-004). Wire shape, versioned by `rayd`:
//!
//! `{"v":1,"token_sha256":"<64 hex>","envs":{...},"user":"user","workdir":"/home/user",
//!  "metadata":{...},"limits":{"cpu_seconds":N},
//!  "lifecycle":{"auto_resume":false,"cap_s":900,"on_timeout":"kill","timeout_s":60},
//!  "network":{"enforce":true}}`
//!
//! `network.enforce` (ADR-012) asks for deny-all before `/run` is
//! answered; absent means `false`, anything but a JSON bool is an error,
//! and unknown keys inside `network` are ignored.
//!
//! `metadata` (client labels echoed by `Health`), `limits` (per-process
//! `RLIMIT_CPU` for the sandbox's processes and PTYs) and `lifecycle` (the
//! logical deadline of ADR-011) are optional; `v` stays 1 because an agent
//! that ignores them still behaves, and the SDK recognises an agent that
//! ignored `lifecycle` by its absence from `Health`. Inside `lifecycle` all
//! four keys are required: `timeout_s` in `1..=28800`, `cap_s` in
//! `120..=28800`, `timeout_s <= cap_s`, `on_timeout` `"kill"` or `"pause"`,
//! and `auto_resume: true` only with `"pause"`.
//!
//! Errors never quote payload content: the payload may carry a digest and
//! environment values, and hook logs must stay free of both.

use std::collections::BTreeMap;
use std::time::Duration;

use serde::Deserialize;
use thiserror::Error;

use crate::auth::{AuthError, TokenDigest};
use crate::sandbox_timeout::{
    LifecycleSpec, MAX_LIFETIME_SECONDS, MIN_CAP_SECONDS, MIN_TIMEOUT_SECONDS, TimeoutAction,
    TimeoutPolicy,
};

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
    /// Names the violated rule, never a value.
    #[error("payload field `lifecycle` is invalid: {0}")]
    InvalidLifecycle(&'static str),
    #[error("payload field `network.enforce` is not a boolean")]
    InvalidNetwork,
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
    /// `None`: no lifecycle block, the sandbox life is the platform's.
    pub lifecycle: Option<LifecycleSpec>,
    /// `network.enforce`: install deny-all before answering `/run`
    /// (ADR-012). The payload never carries rules or credentials.
    pub network_enforce: bool,
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
    lifecycle: Option<LifecycleWire>,
    network: Option<NetworkWire>,
}

/// `enforce` is read as any JSON value so a wrong type is this field's
/// own error instead of a whole-payload parse failure; unknown keys are
/// ignored.
#[derive(Deserialize)]
struct NetworkWire {
    enforce: Option<serde_json::Value>,
}

#[derive(Deserialize)]
struct LimitsWire {
    cpu_seconds: Option<u64>,
}

/// `on_timeout` is read as a plain string and matched, so an unknown value
/// is a rule violation that never reaches an error message.
#[derive(Deserialize)]
struct LifecycleWire {
    timeout_s: Option<u64>,
    cap_s: Option<u64>,
    on_timeout: Option<String>,
    auto_resume: Option<bool>,
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

fn validated_lifecycle(
    lifecycle: Option<LifecycleWire>,
) -> Result<Option<LifecycleSpec>, RunPayloadError> {
    lifecycle.map(LifecycleWire::validated).transpose()
}

impl LifecycleWire {
    fn validated(self) -> Result<LifecycleSpec, RunPayloadError> {
        let invalid = RunPayloadError::InvalidLifecycle;
        let (Some(timeout_s), Some(cap_s), Some(on_timeout), Some(auto_resume)) = (
            self.timeout_s,
            self.cap_s,
            self.on_timeout,
            self.auto_resume,
        ) else {
            return Err(invalid(
                "timeout_s, cap_s, on_timeout and auto_resume are all required",
            ));
        };
        if !(MIN_TIMEOUT_SECONDS..=MAX_LIFETIME_SECONDS).contains(&timeout_s) {
            return Err(invalid("timeout_s is outside 1..=28800"));
        }
        if !(MIN_CAP_SECONDS..=MAX_LIFETIME_SECONDS).contains(&cap_s) {
            return Err(invalid("cap_s is outside 120..=28800"));
        }
        if timeout_s > cap_s {
            return Err(invalid("timeout_s is greater than cap_s"));
        }
        let on_timeout = TimeoutAction::parse(&on_timeout)
            .ok_or_else(|| invalid("on_timeout is neither \"kill\" nor \"pause\""))?;
        if auto_resume && on_timeout != TimeoutAction::Pause {
            return Err(invalid("auto_resume requires on_timeout \"pause\""));
        }
        Ok(LifecycleSpec {
            timeout: Duration::from_secs(timeout_s),
            cap: Duration::from_secs(cap_s),
            policy: TimeoutPolicy {
                on_timeout,
                auto_resume,
            },
        })
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
    let lifecycle = validated_lifecycle(wire.lifecycle)?;
    let network_enforce = validated_network_enforce(wire.network)?;
    Ok(RunPayload {
        token_digest,
        defaults: RunDefaults {
            envs: wire.envs,
            user: wire.user,
            workdir: wire.workdir,
            cpu_seconds,
        },
        metadata: wire.metadata,
        lifecycle,
        network_enforce,
    })
}

fn validated_network_enforce(network: Option<NetworkWire>) -> Result<bool, RunPayloadError> {
    match network.and_then(|network| network.enforce) {
        None => Ok(false),
        Some(serde_json::Value::Bool(enforce)) => Ok(enforce),
        Some(_) => Err(RunPayloadError::InvalidNetwork),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const DIGEST_HEX: &str = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

    fn payload(fields: &str) -> String {
        format!("{{\"v\":1,\"token_sha256\":\"{DIGEST_HEX}\"{fields}}}")
    }

    #[test]
    fn network_enforce_is_an_optional_boolean() {
        let enforce = |fields: &str| parse_run_payload(&payload(fields)).map(|p| p.network_enforce);
        assert_eq!(enforce(""), Ok(false));
        assert_eq!(enforce(",\"network\":{}"), Ok(false));
        assert_eq!(enforce(",\"network\":{\"enforce\":true}"), Ok(true));
        assert_eq!(enforce(",\"network\":{\"enforce\":false}"), Ok(false));
        assert_eq!(
            enforce(",\"network\":{\"enforce\":true,\"rules\":[\"x\"],\"future\":1}"),
            Ok(true)
        );
        for bad in [
            ",\"network\":{\"enforce\":\"yes\"}",
            ",\"network\":{\"enforce\":1}",
            ",\"network\":{\"enforce\":[true]}",
        ] {
            assert_eq!(enforce(bad), Err(RunPayloadError::InvalidNetwork), "{bad}");
        }
        assert!(!RunPayloadError::InvalidNetwork.to_string().contains("yes"));
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

    fn lifecycle_block(
        timeout_s: &str,
        cap_s: &str,
        on_timeout: &str,
        auto_resume: &str,
    ) -> String {
        format!(
            ",\"lifecycle\":{{\"auto_resume\":{auto_resume},\"cap_s\":{cap_s},\"on_timeout\":{on_timeout},\"timeout_s\":{timeout_s}}}"
        )
    }

    #[test]
    fn lifecycle_is_optional_and_validated() {
        assert_eq!(parse_run_payload(&payload("")).unwrap().lifecycle, None);
        let kill = parse_run_payload(&payload(&lifecycle_block("60", "900", "\"kill\"", "false")))
            .unwrap();
        assert_eq!(
            kill.lifecycle,
            Some(LifecycleSpec {
                timeout: Duration::from_secs(60),
                cap: Duration::from_mins(15),
                policy: TimeoutPolicy {
                    on_timeout: TimeoutAction::Kill,
                    auto_resume: false,
                },
            })
        );
        let pause = parse_run_payload(&payload(&lifecycle_block("60", "900", "\"pause\"", "true")))
            .unwrap();
        assert_eq!(
            pause.lifecycle.map(|spec| spec.policy),
            Some(TimeoutPolicy {
                on_timeout: TimeoutAction::Pause,
                auto_resume: true,
            })
        );
        let edges =
            parse_run_payload(&payload(&lifecycle_block("1", "120", "\"kill\"", "false"))).unwrap();
        assert_eq!(edges.lifecycle.map(|spec| spec.cap.as_secs()), Some(120));
        let widest = parse_run_payload(&payload(&lifecycle_block(
            "28800",
            "28800",
            "\"pause\"",
            "false",
        )))
        .unwrap();
        assert_eq!(
            widest.lifecycle.map(|spec| spec.timeout.as_secs()),
            Some(28_800)
        );
        let rejected = [
            lifecycle_block("0", "900", "\"kill\"", "false"),
            lifecycle_block("60", "119", "\"kill\"", "false"),
            lifecycle_block("60", "28801", "\"kill\"", "false"),
            lifecycle_block("28801", "28801", "\"kill\"", "false"),
            lifecycle_block("901", "900", "\"kill\"", "false"),
            lifecycle_block("60", "900", "\"freeze\"", "false"),
            lifecycle_block("60", "900", "\"kill\"", "true"),
            ",\"lifecycle\":{\"cap_s\":900,\"on_timeout\":\"kill\",\"auto_resume\":false}"
                .to_owned(),
            ",\"lifecycle\":{\"timeout_s\":60,\"on_timeout\":\"kill\",\"auto_resume\":false}"
                .to_owned(),
            ",\"lifecycle\":{\"timeout_s\":60,\"cap_s\":900,\"auto_resume\":false}".to_owned(),
            ",\"lifecycle\":{\"timeout_s\":60,\"cap_s\":900,\"on_timeout\":\"kill\"}".to_owned(),
        ];
        for block in &rejected {
            let error = parse_run_payload(&payload(block)).unwrap_err();
            assert!(
                matches!(error, RunPayloadError::InvalidLifecycle(_)),
                "{block}: {error:?}"
            );
            let message = error.to_string();
            assert!(!message.contains("freeze"), "{message}");
            assert!(!message.contains("token"), "{message}");
            assert!(!message.contains(DIGEST_HEX), "{message}");
        }
        let wrong_type = parse_run_payload(&payload(&lifecycle_block(
            "\"60\"", "900", "\"kill\"", "false",
        )));
        assert!(matches!(
            wrong_type,
            Err(RunPayloadError::MalformedJson { .. })
        ));
    }
}
