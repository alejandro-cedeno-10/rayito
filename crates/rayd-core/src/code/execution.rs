//! One execution as the client sees it: `execution_id`, the counted `seq`
//! of its events, the mime bundle mapping of design D5 and the tracker
//! that rewrites sidecar events under a server timeout (design D7) or
//! ends them synthetically when the sidecar cannot.

use std::collections::BTreeMap;
use std::fmt;

use super::context::hex;
use super::error::CodeError;
use super::ports::RandomSource;
use super::protocol::SidecarEvent;

const GENERATED_ID_BYTES: usize = 8;
const EXECUTION_ID_PREFIX: &str = "exec-";

#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct ExecutionId(String);

impl ExecutionId {
    /// `exec-<16 hex>` from eight random bytes, generated at request time.
    pub fn generate(random: &dyn RandomSource) -> Result<Self, CodeError> {
        let mut bytes = [0u8; GENERATED_ID_BYTES];
        random
            .fill(&mut bytes)
            .map_err(|error| CodeError::Internal(error.to_string()))?;
        Ok(Self(format!("exec-{}", hex(&bytes))))
    }

    #[must_use]
    pub fn from_raw(raw: &str) -> Self {
        Self(raw.to_owned())
    }

    /// `Reattach.execution_id` must have the generated shape.
    pub fn parse(raw: &str) -> Result<Self, CodeError> {
        let hex_part = raw
            .strip_prefix(EXECUTION_ID_PREFIX)
            .ok_or(CodeError::InvalidExecutionId)?;
        let valid = hex_part.len() == GENERATED_ID_BYTES * 2
            && hex_part.bytes().all(|byte| byte.is_ascii_hexdigit());
        if valid {
            Ok(Self(raw.to_owned()))
        } else {
            Err(CodeError::InvalidExecutionId)
        }
    }

    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for ExecutionId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

/// The closed set of error names `rayd` or the sidecar produce themselves.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SyntheticError {
    ExecutionTimeout,
    KernelDied,
    KernelRestarted,
    ContextDestroyed,
    ExecutionAborted,
    OutputTruncated,
}

impl SyntheticError {
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::ExecutionTimeout => "ExecutionTimeout",
            Self::KernelDied => "KernelDied",
            Self::KernelRestarted => "KernelRestarted",
            Self::ContextDestroyed => "ContextDestroyed",
            Self::ExecutionAborted => "ExecutionAborted",
            Self::OutputTruncated => "OutputTruncated",
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ResultBundle {
    pub is_main_result: bool,
    pub text: Option<String>,
    pub html: Option<String>,
    pub markdown: Option<String>,
    pub latex: Option<String>,
    pub json: Option<String>,
    pub javascript: Option<String>,
    pub png: Option<String>,
    pub jpeg: Option<String>,
    pub svg: Option<String>,
    pub pdf: Option<String>,
    pub chart: Option<String>,
    pub data: Option<String>,
    pub extra: BTreeMap<String, String>,
}

impl ResultBundle {
    #[must_use]
    pub fn from_mime(is_main_result: bool, mime: BTreeMap<String, String>) -> Self {
        let mut bundle = Self {
            is_main_result,
            ..Self::default()
        };
        for (mime_type, value) in mime {
            let slot = match mime_type.as_str() {
                "text/plain" => &mut bundle.text,
                "text/html" => &mut bundle.html,
                "text/markdown" => &mut bundle.markdown,
                "text/latex" => &mut bundle.latex,
                "application/json" => &mut bundle.json,
                "application/javascript" => &mut bundle.javascript,
                "image/png" => &mut bundle.png,
                "image/jpeg" => &mut bundle.jpeg,
                "image/svg+xml" => &mut bundle.svg,
                "application/pdf" => &mut bundle.pdf,
                "e2b/chart" => &mut bundle.chart,
                "e2b/data" => &mut bundle.data,
                _ => {
                    bundle.extra.insert(mime_type, value);
                    continue;
                }
            };
            *slot = Some(value);
        }
        bundle
    }

    /// Mime type names only, for logs.
    #[must_use]
    pub fn mime_types(&self) -> Vec<&'static str> {
        let mut names = Vec::new();
        let fields: [(&'static str, &Option<String>); 12] = [
            ("text/plain", &self.text),
            ("text/html", &self.html),
            ("text/markdown", &self.markdown),
            ("text/latex", &self.latex),
            ("application/json", &self.json),
            ("application/javascript", &self.javascript),
            ("image/png", &self.png),
            ("image/jpeg", &self.jpeg),
            ("image/svg+xml", &self.svg),
            ("application/pdf", &self.pdf),
            ("e2b/chart", &self.chart),
            ("e2b/data", &self.data),
        ];
        for (name, value) in fields {
            if value.is_some() {
                names.push(name);
            }
        }
        names
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExecutionErrorInfo {
    pub name: String,
    pub value: String,
    pub traceback: Vec<String>,
}

impl ExecutionErrorInfo {
    #[must_use]
    pub fn synthetic(name: SyntheticError, value: impl Into<String>) -> Self {
        Self {
            name: name.as_str().to_owned(),
            value: value.into(),
            traceback: Vec::new(),
        }
    }
}

/// What the gRPC adapter turns into `ExecuteEvent`s; `seq` counts from 1.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ExecuteOutput {
    Started {
        seq: u64,
        execution_id: ExecutionId,
        execution_count: u64,
    },
    Stdout {
        seq: u64,
        text: String,
        timestamp_unix_ns: i64,
    },
    Stderr {
        seq: u64,
        text: String,
        timestamp_unix_ns: i64,
    },
    Result {
        seq: u64,
        bundle: Box<ResultBundle>,
    },
    Error {
        seq: u64,
        error: ExecutionErrorInfo,
    },
    End {
        seq: u64,
        execution_count: u64,
    },
}

impl ExecuteOutput {
    #[must_use]
    pub fn seq(&self) -> u64 {
        match self {
            Self::Started { seq, .. }
            | Self::Stdout { seq, .. }
            | Self::Stderr { seq, .. }
            | Self::Result { seq, .. }
            | Self::Error { seq, .. }
            | Self::End { seq, .. } => *seq,
        }
    }

    #[must_use]
    pub fn is_end(&self) -> bool {
        matches!(self, Self::End { .. })
    }
}

/// Sequences one execution's events, guarantees exactly one `Started` and
/// one `End`, and applies the timeout rewrite: after `mark_timed_out` the
/// kernel's own errors are dropped and the `End` is preceded by one
/// `ExecutionTimeout`.
#[derive(Debug)]
pub struct ExecutionTracker {
    execution_id: ExecutionId,
    next_seq: u64,
    started: bool,
    ended: bool,
    timed_out: Option<u64>,
}

impl ExecutionTracker {
    #[must_use]
    pub fn new(execution_id: ExecutionId) -> Self {
        Self {
            execution_id,
            next_seq: 1,
            started: false,
            ended: false,
            timed_out: None,
        }
    }

    #[must_use]
    pub fn execution_id(&self) -> &ExecutionId {
        &self.execution_id
    }

    #[must_use]
    pub fn ended(&self) -> bool {
        self.ended
    }

    #[must_use]
    pub fn started(&self) -> bool {
        self.started
    }

    pub fn mark_timed_out(&mut self, timeout_ms: u64) {
        if self.timed_out.is_none() {
            self.timed_out = Some(timeout_ms);
        }
    }

    pub fn on_event(&mut self, event: &SidecarEvent) -> Vec<ExecuteOutput> {
        if self.ended {
            return Vec::new();
        }
        match event {
            SidecarEvent::Started {
                execution_count, ..
            } => self.started_output(*execution_count),
            SidecarEvent::Stdout {
                text,
                timestamp_unix_ns,
                ..
            } => self.counted(|seq| ExecuteOutput::Stdout {
                seq,
                text: text.clone(),
                timestamp_unix_ns: *timestamp_unix_ns,
            }),
            SidecarEvent::Stderr {
                text,
                timestamp_unix_ns,
                ..
            } => self.counted(|seq| ExecuteOutput::Stderr {
                seq,
                text: text.clone(),
                timestamp_unix_ns: *timestamp_unix_ns,
            }),
            SidecarEvent::Result {
                is_main_result,
                mime,
                ..
            } => {
                let bundle = Box::new(ResultBundle::from_mime(*is_main_result, mime.clone()));
                self.counted(|seq| ExecuteOutput::Result { seq, bundle })
            }
            SidecarEvent::Error {
                name,
                value,
                traceback,
                ..
            } => {
                if self.timed_out.is_some() {
                    return Vec::new();
                }
                let error = ExecutionErrorInfo {
                    name: name.clone(),
                    value: value.clone(),
                    traceback: traceback.clone(),
                };
                self.counted(|seq| ExecuteOutput::Error { seq, error })
            }
            SidecarEvent::End {
                execution_count, ..
            } => self.end_output(*execution_count),
            SidecarEvent::Ready { .. }
            | SidecarEvent::Reply { .. }
            | SidecarEvent::KernelDied { .. } => Vec::new(),
        }
    }

    /// Ends the execution on `rayd`'s own initiative (`Started` first if
    /// the sidecar never sent one, then the error, then `End{0}`).
    pub fn synthetic_end(
        &mut self,
        name: SyntheticError,
        value: impl Into<String>,
    ) -> Vec<ExecuteOutput> {
        if self.ended {
            return Vec::new();
        }
        let mut outputs = self.ensure_started(0);
        let error = ExecutionErrorInfo::synthetic(name, value);
        let seq = self.take_seq();
        outputs.push(ExecuteOutput::Error { seq, error });
        let seq = self.take_seq();
        outputs.push(ExecuteOutput::End {
            seq,
            execution_count: 0,
        });
        self.ended = true;
        outputs
    }

    fn started_output(&mut self, execution_count: u64) -> Vec<ExecuteOutput> {
        if self.started {
            return Vec::new();
        }
        self.ensure_started(execution_count)
    }

    fn end_output(&mut self, execution_count: u64) -> Vec<ExecuteOutput> {
        let mut outputs = self.ensure_started(execution_count);
        if let Some(timeout_ms) = self.timed_out {
            let error = ExecutionErrorInfo::synthetic(
                SyntheticError::ExecutionTimeout,
                format!("execution exceeded {timeout_ms} ms"),
            );
            let seq = self.take_seq();
            outputs.push(ExecuteOutput::Error { seq, error });
        }
        let seq = self.take_seq();
        outputs.push(ExecuteOutput::End {
            seq,
            execution_count,
        });
        self.ended = true;
        outputs
    }

    fn counted(&mut self, make: impl FnOnce(u64) -> ExecuteOutput) -> Vec<ExecuteOutput> {
        let mut outputs = self.ensure_started(0);
        let seq = self.take_seq();
        outputs.push(make(seq));
        outputs
    }

    fn ensure_started(&mut self, execution_count: u64) -> Vec<ExecuteOutput> {
        if self.started {
            return Vec::new();
        }
        self.started = true;
        let seq = self.take_seq();
        vec![ExecuteOutput::Started {
            seq,
            execution_id: self.execution_id.clone(),
            execution_count,
        }]
    }

    fn take_seq(&mut self) -> u64 {
        let seq = self.next_seq;
        self.next_seq += 1;
        seq
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::code::ports::RandomError;

    struct FixedRandom(u8);

    impl RandomSource for FixedRandom {
        fn fill(&self, buf: &mut [u8]) -> Result<(), RandomError> {
            buf.fill(self.0);
            Ok(())
        }
    }

    fn tracker() -> ExecutionTracker {
        ExecutionTracker::new(ExecutionId::from_raw("exec-1"))
    }

    fn started(count: u64) -> SidecarEvent {
        SidecarEvent::Started {
            id: 1,
            execution_id: "exec-1".to_owned(),
            execution_count: count,
        }
    }

    fn stdout(text: &str) -> SidecarEvent {
        SidecarEvent::Stdout {
            id: 1,
            execution_id: "exec-1".to_owned(),
            text: text.to_owned(),
            timestamp_unix_ns: 7,
        }
    }

    fn error(name: &str) -> SidecarEvent {
        SidecarEvent::Error {
            id: 1,
            execution_id: "exec-1".to_owned(),
            name: name.to_owned(),
            value: "v".to_owned(),
            traceback: vec!["t".to_owned()],
        }
    }

    fn end(count: u64) -> SidecarEvent {
        SidecarEvent::End {
            id: 1,
            execution_id: "exec-1".to_owned(),
            execution_count: count,
        }
    }

    fn result(main: bool, mime: &[(&str, &str)]) -> SidecarEvent {
        SidecarEvent::Result {
            id: 1,
            execution_id: "exec-1".to_owned(),
            is_main_result: main,
            mime: mime
                .iter()
                .map(|(k, v)| ((*k).to_owned(), (*v).to_owned()))
                .collect(),
        }
    }

    fn seqs(outputs: &[ExecuteOutput]) -> Vec<u64> {
        outputs.iter().map(ExecuteOutput::seq).collect()
    }

    #[test]
    fn execution_ids_are_exec_plus_sixteen_hex() {
        let id = ExecutionId::generate(&FixedRandom(0x0f)).unwrap();
        assert_eq!(id.as_str(), "exec-0f0f0f0f0f0f0f0f");
        assert_eq!(ExecutionId::parse("exec-0f0f0f0f0f0f0f0f").unwrap(), id);
        assert!(ExecutionId::parse("exec-0F0F0F0F0F0F0F0F").is_ok());
        for bad in [
            "",
            "exec-",
            "exec-0f0f",
            "exec-0f0f0f0f0f0f0f0g",
            "ctx-0f0f0f0f0f0f0f0f",
            "exec-0f0f0f0f0f0f0f0f0f",
        ] {
            assert_eq!(
                ExecutionId::parse(bad),
                Err(CodeError::InvalidExecutionId),
                "{bad}"
            );
        }
    }

    #[test]
    fn from_mime_maps_every_known_type_and_keeps_the_rest_in_extra() {
        let mime: BTreeMap<String, String> = [
            ("text/plain", "t"),
            ("text/html", "h"),
            ("text/markdown", "m"),
            ("text/latex", "l"),
            ("application/json", "j"),
            ("application/javascript", "js"),
            ("image/png", "p"),
            ("image/jpeg", "jp"),
            ("image/svg+xml", "s"),
            ("application/pdf", "pdf"),
            ("e2b/chart", "c"),
            ("e2b/data", "d"),
            ("rayito/omitted", "image/png: 9000000 bytes"),
            ("application/vnd.custom", "x"),
        ]
        .into_iter()
        .map(|(k, v)| (k.to_owned(), v.to_owned()))
        .collect();
        let bundle = ResultBundle::from_mime(true, mime);
        assert!(bundle.is_main_result);
        assert_eq!(bundle.text.as_deref(), Some("t"));
        assert_eq!(bundle.html.as_deref(), Some("h"));
        assert_eq!(bundle.markdown.as_deref(), Some("m"));
        assert_eq!(bundle.latex.as_deref(), Some("l"));
        assert_eq!(bundle.json.as_deref(), Some("j"));
        assert_eq!(bundle.javascript.as_deref(), Some("js"));
        assert_eq!(bundle.png.as_deref(), Some("p"));
        assert_eq!(bundle.jpeg.as_deref(), Some("jp"));
        assert_eq!(bundle.svg.as_deref(), Some("s"));
        assert_eq!(bundle.pdf.as_deref(), Some("pdf"));
        assert_eq!(bundle.chart.as_deref(), Some("c"));
        assert_eq!(bundle.data.as_deref(), Some("d"));
        assert_eq!(bundle.extra.len(), 2);
        assert_eq!(
            bundle.extra.get("rayito/omitted").map(String::as_str),
            Some("image/png: 9000000 bytes")
        );
        assert_eq!(bundle.mime_types().len(), 12);
        assert_eq!(
            ResultBundle::from_mime(false, BTreeMap::new()).mime_types(),
            Vec::<&str>::new()
        );
    }

    #[test]
    fn events_are_numbered_from_one_and_stop_after_end() {
        let mut tracker = tracker();
        let mut outputs = tracker.on_event(&started(1));
        outputs.extend(tracker.on_event(&stdout("x")));
        outputs.extend(tracker.on_event(&result(true, &[("text/plain", "42")])));
        outputs.extend(tracker.on_event(&end(1)));
        assert_eq!(seqs(&outputs), vec![1, 2, 3, 4]);
        assert!(matches!(
            outputs[0],
            ExecuteOutput::Started {
                execution_count: 1,
                ..
            }
        ));
        assert!(
            matches!(&outputs[2], ExecuteOutput::Result { bundle, .. } if bundle.is_main_result)
        );
        assert!(tracker.ended());
        assert!(tracker.on_event(&stdout("late")).is_empty());
        assert!(tracker.on_event(&end(1)).is_empty());
        assert!(
            tracker
                .synthetic_end(SyntheticError::KernelDied, "x")
                .is_empty()
        );
    }

    #[test]
    fn a_missing_started_is_synthesised_and_a_second_one_ignored() {
        let mut tracker = tracker();
        let outputs = tracker.on_event(&stdout("x"));
        assert_eq!(seqs(&outputs), vec![1, 2]);
        assert!(matches!(
            outputs[0],
            ExecuteOutput::Started {
                execution_count: 0,
                ..
            }
        ));
        assert!(tracker.on_event(&started(5)).is_empty());
        let mut fresh = ExecutionTracker::new(ExecutionId::from_raw("exec-2"));
        let outputs = fresh.on_event(&end(3));
        assert!(matches!(
            outputs[0],
            ExecuteOutput::Started {
                execution_count: 3,
                ..
            }
        ));
        assert!(matches!(
            outputs[1],
            ExecuteOutput::End {
                execution_count: 3,
                ..
            }
        ));
    }

    #[test]
    fn timeout_drops_kernel_errors_and_prepends_execution_timeout_to_end() {
        let mut tracker = tracker();
        tracker.on_event(&started(2));
        tracker.mark_timed_out(2_000);
        tracker.mark_timed_out(9_999);
        assert!(tracker.on_event(&error("KeyboardInterrupt")).is_empty());
        assert!(tracker.on_event(&error("KernelRestarted")).is_empty());
        let outputs = tracker.on_event(&end(2));
        assert_eq!(seqs(&outputs), vec![2, 3]);
        match &outputs[0] {
            ExecuteOutput::Error { error, .. } => {
                assert_eq!(error.name, "ExecutionTimeout");
                assert_eq!(error.value, "execution exceeded 2000 ms");
                assert!(error.traceback.is_empty());
            }
            other => panic!("unexpected {other:?}"),
        }
        assert!(matches!(
            outputs[1],
            ExecuteOutput::End {
                execution_count: 2,
                ..
            }
        ));
    }

    #[test]
    fn kernel_errors_pass_through_before_a_timeout() {
        let mut tracker = tracker();
        tracker.on_event(&started(1));
        let outputs = tracker.on_event(&error("ZeroDivisionError"));
        match &outputs[0] {
            ExecuteOutput::Error { seq, error } => {
                assert_eq!(*seq, 2);
                assert_eq!(error.name, "ZeroDivisionError");
                assert_eq!(error.traceback, vec!["t"]);
            }
            other => panic!("unexpected {other:?}"),
        }
    }

    #[test]
    fn synthetic_end_produces_started_error_and_end_zero() {
        let mut tracker = tracker();
        let outputs =
            tracker.synthetic_end(SyntheticError::KernelDied, "the kernel sidecar exited");
        assert_eq!(seqs(&outputs), vec![1, 2, 3]);
        assert!(matches!(outputs[0], ExecuteOutput::Started { .. }));
        match &outputs[1] {
            ExecuteOutput::Error { error, .. } => assert_eq!(error.name, "KernelDied"),
            other => panic!("unexpected {other:?}"),
        }
        assert!(matches!(
            outputs[2],
            ExecuteOutput::End {
                execution_count: 0,
                ..
            }
        ));
        assert!(tracker.ended());
    }

    #[test]
    fn non_execution_events_produce_nothing() {
        let mut tracker = tracker();
        assert!(
            tracker
                .on_event(&SidecarEvent::KernelDied {
                    context_id: "default".to_owned(),
                    exit_code: Some(1),
                    execution_id: None,
                })
                .is_empty()
        );
        assert!(!tracker.started());
    }

    #[test]
    fn synthetic_names_are_the_closed_set() {
        let names: Vec<&str> = [
            SyntheticError::ExecutionTimeout,
            SyntheticError::KernelDied,
            SyntheticError::KernelRestarted,
            SyntheticError::ContextDestroyed,
            SyntheticError::ExecutionAborted,
            SyntheticError::OutputTruncated,
        ]
        .into_iter()
        .map(SyntheticError::as_str)
        .collect();
        assert_eq!(
            names,
            vec![
                "ExecutionTimeout",
                "KernelDied",
                "KernelRestarted",
                "ContextDestroyed",
                "ExecutionAborted",
                "OutputTruncated"
            ]
        );
    }
}
