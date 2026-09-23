//! JSON lines between `rayd` and the kernel sidecar, protocol version 1
//! (design D2). Requests go down the sidecar's stdin as
//! `{"id", "op", ...}`, events come back on its stdout as `{"event", ...}`.
//! The Python codec (`rayito_kernel_sidecar.protocol`) agrees byte for byte
//! on `kernel-sidecar/tests/fixtures/protocol_v1.jsonl`: compact
//! separators, this field order, nested objects with sorted keys.

use std::collections::BTreeMap;

use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use thiserror::Error;

pub const SIDECAR_PROTOCOL_VERSION: u64 = 1;

const KNOWN_EVENTS: [&str; 9] = [
    "ready",
    "reply",
    "started",
    "stdout",
    "stderr",
    "result",
    "error",
    "end",
    "kernel_died",
];

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SidecarRequest {
    pub id: u64,
    #[serde(flatten)]
    pub op: SidecarOp,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
pub enum SidecarOp {
    Ping,
    CreateContext {
        context_id: String,
        language: String,
        cwd: String,
        envs: BTreeMap<String, String>,
    },
    Execute {
        context_id: String,
        execution_id: String,
        code: String,
        envs: BTreeMap<String, String>,
    },
    Interrupt {
        context_id: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        execution_id: Option<String>,
    },
    DestroyContext {
        context_id: String,
    },
    RestartContext {
        context_id: String,
        envs: BTreeMap<String, String>,
    },
    ListContexts,
    Reseed,
    Quiesce,
    Resume,
}

impl SidecarOp {
    #[must_use]
    pub fn name(&self) -> &'static str {
        match self {
            Self::Ping => "ping",
            Self::CreateContext { .. } => "create_context",
            Self::Execute { .. } => "execute",
            Self::Interrupt { .. } => "interrupt",
            Self::DestroyContext { .. } => "destroy_context",
            Self::RestartContext { .. } => "restart_context",
            Self::ListContexts => "list_contexts",
            Self::Reseed => "reseed",
            Self::Quiesce => "quiesce",
            Self::Resume => "resume",
        }
    }

    /// An advisory op is best effort: a reply that never comes is logged
    /// and never counts towards the sidecar's kill switch. Only `reseed`
    /// (queued behind whatever cell is running at `/resume`) qualifies.
    #[must_use]
    pub fn is_advisory(&self) -> bool {
        matches!(self, Self::Reseed)
    }

    /// Layers the local egress proxy's variables (ADR-012) under the envs
    /// of every op that starts a kernel, so each context create, restart,
    /// `/run` rotation and post-resume restart gets them whatever its
    /// language; the context's own envs still win. `Execute` envs are
    /// user-supplied per cell and stay untouched.
    #[must_use]
    pub fn with_egress_env(self, egress_env: &BTreeMap<String, String>) -> Self {
        if egress_env.is_empty() {
            return self;
        }
        let layered = |envs: BTreeMap<String, String>| {
            let mut merged = egress_env.clone();
            merged.extend(envs);
            merged
        };
        match self {
            Self::CreateContext {
                context_id,
                language,
                cwd,
                envs,
            } => Self::CreateContext {
                context_id,
                language,
                cwd,
                envs: layered(envs),
            },
            Self::RestartContext { context_id, envs } => Self::RestartContext {
                context_id,
                envs: layered(envs),
            },
            other => other,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SidecarErrorCode {
    NotFound,
    InvalidArgument,
    KernelDead,
    Busy,
    Internal,
    #[serde(other)]
    Unknown,
}

impl SidecarErrorCode {
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::NotFound => "not_found",
            Self::InvalidArgument => "invalid_argument",
            Self::KernelDead => "kernel_dead",
            Self::Busy => "busy",
            Self::Internal => "internal",
            Self::Unknown => "unknown",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ReplyError {
    pub code: SidecarErrorCode,
    pub message: String,
}

/// The `payload` object of a successful reply, decoded on demand into the
/// shape the caller expects.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct ReplyPayload(pub serde_json::Value);

impl ReplyPayload {
    pub fn decode<T: DeserializeOwned>(&self) -> Result<T, ProtocolError> {
        serde_json::from_value(self.0.clone()).map_err(|error| classify(&error))
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct KernelPidPayload {
    pub kernel_pid: Option<u32>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PingPayload {
    pub kernel_ready: bool,
    pub contexts: u64,
}

/// The `reseed` reply: contexts reseeded inline, contexts whose reseed was
/// deferred behind a running cell (older sidecars omit the field),
/// contexts that could not be reseeded and non-Python contexts the sidecar
/// never touches (`skipped`, absent before the language catalog).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ReseedPayload {
    pub reseeded: Vec<String>,
    #[serde(default)]
    pub deferred: Vec<String>,
    pub failed: Vec<String>,
    #[serde(default)]
    pub skipped: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "event", rename_all = "snake_case")]
pub enum SidecarEvent {
    Ready {
        v: u64,
        default_context_id: String,
        kernel_pid: Option<u32>,
        warmup_ms: u64,
        /// Canonical names the image can run; absent from an older sidecar
        /// (then Python only).
        #[serde(default)]
        languages: Vec<String>,
    },
    Reply {
        id: u64,
        ok: bool,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        payload: Option<serde_json::Value>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        error: Option<ReplyError>,
    },
    Started {
        id: u64,
        execution_id: String,
        execution_count: u64,
    },
    Stdout {
        id: u64,
        execution_id: String,
        text: String,
        timestamp_unix_ns: i64,
    },
    Stderr {
        id: u64,
        execution_id: String,
        text: String,
        timestamp_unix_ns: i64,
    },
    Result {
        id: u64,
        execution_id: String,
        is_main_result: bool,
        mime: BTreeMap<String, String>,
    },
    Error {
        id: u64,
        execution_id: String,
        name: String,
        value: String,
        traceback: Vec<String>,
    },
    End {
        id: u64,
        execution_id: String,
        execution_count: u64,
    },
    KernelDied {
        context_id: String,
        exit_code: Option<i32>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        execution_id: Option<String>,
    },
}

impl SidecarEvent {
    /// The request `id` an execution event or reply answers, `None` for
    /// `ready` and `kernel_died`.
    #[must_use]
    pub fn request_id(&self) -> Option<u64> {
        match self {
            Self::Reply { id, .. }
            | Self::Started { id, .. }
            | Self::Stdout { id, .. }
            | Self::Stderr { id, .. }
            | Self::Result { id, .. }
            | Self::Error { id, .. }
            | Self::End { id, .. } => Some(*id),
            Self::Ready { .. } | Self::KernelDied { .. } => None,
        }
    }

    #[must_use]
    pub fn name(&self) -> &'static str {
        match self {
            Self::Ready { .. } => "ready",
            Self::Reply { .. } => "reply",
            Self::Started { .. } => "started",
            Self::Stdout { .. } => "stdout",
            Self::Stderr { .. } => "stderr",
            Self::Result { .. } => "result",
            Self::Error { .. } => "error",
            Self::End { .. } => "end",
            Self::KernelDied { .. } => "kernel_died",
        }
    }
}

/// Never quotes the line: a malformed line may carry cell output.
#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum ProtocolError {
    #[error("sidecar line is not a JSON object")]
    Malformed,
    #[error("unknown sidecar event `{0}`")]
    UnknownEvent(String),
    #[error("sidecar event is missing field `{0}`")]
    MissingField(String),
    #[error("sidecar line of {bytes} bytes exceeds the limit")]
    LineTooLong { bytes: usize },
}

/// One line without its trailing newline; the adapter appends it.
#[must_use]
pub fn encode_request(request: &SidecarRequest) -> String {
    serde_json::to_string(request).unwrap_or_else(|_| format!("{{\"id\":{}}}", request.id))
}

pub fn decode_event(line: &str) -> Result<SidecarEvent, ProtocolError> {
    let value: serde_json::Value =
        serde_json::from_str(line).map_err(|_| ProtocolError::Malformed)?;
    let object = value.as_object().ok_or(ProtocolError::Malformed)?;
    let event = object
        .get("event")
        .and_then(serde_json::Value::as_str)
        .ok_or_else(|| ProtocolError::MissingField("event".to_owned()))?;
    if !KNOWN_EVENTS.contains(&event) {
        return Err(ProtocolError::UnknownEvent(event.to_owned()));
    }
    serde_json::from_value(value).map_err(|error| classify(&error))
}

fn classify(error: &serde_json::Error) -> ProtocolError {
    let message = error.to_string();
    message
        .strip_prefix("missing field `")
        .and_then(|rest| rest.split('`').next())
        .map_or(ProtocolError::Malformed, |field| {
            ProtocolError::MissingField(field.to_owned())
        })
}

#[cfg(test)]
mod tests {
    use super::*;

    const FIXTURE: &str = include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../kernel-sidecar/tests/fixtures/protocol_v1.jsonl"
    ));

    fn lines() -> Vec<&'static str> {
        FIXTURE.lines().filter(|line| !line.is_empty()).collect()
    }

    fn envs(pairs: &[(&str, &str)]) -> BTreeMap<String, String> {
        pairs
            .iter()
            .map(|(key, value)| ((*key).to_owned(), (*value).to_owned()))
            .collect()
    }

    #[test]
    fn the_egress_env_is_layered_under_kernel_starting_ops_only() {
        let egress = envs(&[("HTTPS_PROXY", "http://127.0.0.1:4000"), ("FOO", "egress")]);
        let created = SidecarOp::CreateContext {
            context_id: "c1".to_owned(),
            language: "javascript".to_owned(),
            cwd: "/home/user".to_owned(),
            envs: envs(&[("FOO", "context")]),
        }
        .with_egress_env(&egress);
        let SidecarOp::CreateContext { envs: merged, .. } = created else {
            panic!("still a create");
        };
        assert_eq!(merged["HTTPS_PROXY"], "http://127.0.0.1:4000");
        assert_eq!(merged["FOO"], "context");
        let restarted = SidecarOp::RestartContext {
            context_id: "default".to_owned(),
            envs: BTreeMap::new(),
        }
        .with_egress_env(&egress);
        assert_eq!(
            restarted,
            SidecarOp::RestartContext {
                context_id: "default".to_owned(),
                envs: egress.clone(),
            }
        );
        let execute = SidecarOp::Execute {
            context_id: "c1".to_owned(),
            execution_id: "e1".to_owned(),
            code: "1".to_owned(),
            envs: BTreeMap::new(),
        };
        assert_eq!(execute.clone().with_egress_env(&egress), execute);
        let untouched = SidecarOp::RestartContext {
            context_id: "c1".to_owned(),
            envs: envs(&[("A", "1")]),
        };
        assert_eq!(
            untouched.clone().with_egress_env(&BTreeMap::new()),
            untouched
        );
    }

    fn request_lines() -> Vec<&'static str> {
        lines()
            .into_iter()
            .filter(|line| line.contains("\"op\""))
            .collect()
    }

    fn event_lines() -> Vec<&'static str> {
        lines()
            .into_iter()
            .filter(|line| line.contains("\"event\""))
            .collect()
    }

    #[test]
    fn every_op_and_event_has_a_golden_line() {
        let ops: std::collections::BTreeSet<&str> = request_lines()
            .into_iter()
            .map(|line| {
                serde_json::from_str::<SidecarRequest>(line)
                    .unwrap()
                    .op
                    .name()
            })
            .collect();
        assert_eq!(ops.len(), 10);
        let events: std::collections::BTreeSet<&str> = event_lines()
            .into_iter()
            .map(|line| decode_event(line).unwrap().name())
            .collect();
        assert_eq!(events.len(), KNOWN_EVENTS.len());
    }

    #[test]
    fn request_lines_round_trip_byte_for_byte() {
        for line in request_lines() {
            let request: SidecarRequest = serde_json::from_str(line).unwrap();
            assert_eq!(encode_request(&request), line, "{line}");
        }
    }

    #[test]
    fn event_lines_round_trip_byte_for_byte() {
        for line in event_lines() {
            let event = decode_event(line).unwrap();
            assert_eq!(serde_json::to_string(&event).unwrap(), line, "{line}");
        }
    }

    #[test]
    fn encode_request_matches_the_python_field_order() {
        let request = SidecarRequest {
            id: 7,
            op: SidecarOp::RestartContext {
                context_id: "default".to_owned(),
                envs: [("M4".to_owned(), "1".to_owned())].into(),
            },
        };
        assert_eq!(
            encode_request(&request),
            "{\"id\":7,\"op\":\"restart_context\",\"context_id\":\"default\",\"envs\":{\"M4\":\"1\"}}"
        );
        let ping = SidecarRequest {
            id: 1,
            op: SidecarOp::Ping,
        };
        assert_eq!(encode_request(&ping), "{\"id\":1,\"op\":\"ping\"}");
    }

    #[test]
    fn unknown_events_are_rejected_and_unknown_fields_ignored() {
        assert_eq!(
            decode_event("{\"event\":\"frobnicate\",\"id\":1}"),
            Err(ProtocolError::UnknownEvent("frobnicate".to_owned()))
        );
        let event = decode_event(
            "{\"event\":\"end\",\"id\":3,\"execution_id\":\"exec-1\",\"execution_count\":2,\"future\":true}",
        )
        .unwrap();
        assert_eq!(
            event,
            SidecarEvent::End {
                id: 3,
                execution_id: "exec-1".to_owned(),
                execution_count: 2
            }
        );
    }

    #[test]
    fn missing_fields_and_malformed_lines_are_classified() {
        assert_eq!(
            decode_event("{\"event\":\"end\",\"id\":3}"),
            Err(ProtocolError::MissingField("execution_id".to_owned()))
        );
        assert_eq!(decode_event("not json"), Err(ProtocolError::Malformed));
        assert_eq!(decode_event("[1]"), Err(ProtocolError::Malformed));
        assert_eq!(
            decode_event("{\"id\":1}"),
            Err(ProtocolError::MissingField("event".to_owned()))
        );
    }

    #[test]
    fn reply_error_codes_decode_with_an_unknown_fallback() {
        let event = decode_event(
            "{\"event\":\"reply\",\"id\":1,\"ok\":false,\"error\":{\"code\":\"made_up\",\"message\":\"x\"}}",
        )
        .unwrap();
        match event {
            SidecarEvent::Reply { error, payload, .. } => {
                assert_eq!(error.unwrap().code, SidecarErrorCode::Unknown);
                assert_eq!(payload, None);
            }
            other => panic!("unexpected {other:?}"),
        }
    }

    #[test]
    fn reply_payloads_decode_into_typed_shapes() {
        let payload = ReplyPayload(serde_json::json!({"kernel_pid": 4243}));
        assert_eq!(
            payload.decode::<KernelPidPayload>().unwrap(),
            KernelPidPayload {
                kernel_pid: Some(4243)
            }
        );
        let payload = ReplyPayload(serde_json::json!({"reseeded": ["default"], "failed": []}));
        let reseed = payload.decode::<ReseedPayload>().unwrap();
        assert_eq!(reseed.reseeded, vec!["default"]);
        assert!(reseed.deferred.is_empty(), "older replies omit deferred");
        let deferred = ReplyPayload(serde_json::json!({
            "reseeded": [], "deferred": ["default"], "failed": ["ctx-1"]
        }));
        let decoded = deferred.decode::<ReseedPayload>().unwrap();
        assert_eq!(decoded.deferred, vec!["default"]);
        assert!(decoded.skipped.is_empty(), "older replies omit skipped");
        let skipped = ReplyPayload(serde_json::json!({
            "reseeded": ["default"], "deferred": [], "failed": [], "skipped": ["default-bash"]
        }));
        assert_eq!(
            skipped.decode::<ReseedPayload>().unwrap().skipped,
            vec!["default-bash"]
        );
        let bad = ReplyPayload(serde_json::json!({"contexts": 1}));
        assert_eq!(
            bad.decode::<PingPayload>(),
            Err(ProtocolError::MissingField("kernel_ready".to_owned()))
        );
    }

    #[test]
    fn ready_without_languages_decodes_as_an_older_sidecar() {
        let event = decode_event(
            "{\"event\":\"ready\",\"v\":1,\"default_context_id\":\"default\",\"kernel_pid\":1,\"warmup_ms\":2}",
        )
        .unwrap();
        match event {
            SidecarEvent::Ready { languages, .. } => assert!(languages.is_empty()),
            other => panic!("unexpected {other:?}"),
        }
        let request = SidecarRequest {
            id: 12,
            op: SidecarOp::CreateContext {
                context_id: "default-bash".to_owned(),
                language: "bash".to_owned(),
                cwd: "/home/user".to_owned(),
                envs: BTreeMap::new(),
            },
        };
        assert_eq!(
            encode_request(&request),
            "{\"id\":12,\"op\":\"create_context\",\"context_id\":\"default-bash\",\"language\":\"bash\",\"cwd\":\"/home/user\",\"envs\":{}}"
        );
    }

    #[test]
    fn only_reseed_is_advisory() {
        assert!(SidecarOp::Reseed.is_advisory());
        assert!(!SidecarOp::Ping.is_advisory());
        assert!(!SidecarOp::Quiesce.is_advisory());
        assert!(!SidecarOp::Resume.is_advisory());
    }

    #[test]
    fn error_messages_never_quote_the_line() {
        let error = decode_event("{\"event\":\"stdout\",\"text\":\"secret output\"}").unwrap_err();
        assert!(!error.to_string().contains("secret"));
    }
}
