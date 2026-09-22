//! The `/validate` work (design D9): the same default-kernel restart `/run`
//! performs, then a pandas + matplotlib cell with a 60 s server timeout
//! consumed to its end, so Lambda's throwaway VM touches every page a
//! fresh sandbox needs (the kernel start reads the Python stack from the
//! lazily restored disk; the cell exercises the acceptance path) and
//! prefetches them on `run-microvm`. It never fails the build: the outcome
//! is logged and reported in the hook body.

use std::sync::Arc;
use std::time::Duration;

use rayd_core::code::{DEFAULT_CONTEXT_ID, ExecuteOutput, VALIDATE_CELL, ValidationOutcome};
use tokio_stream::StreamExt;

use super::execute::ExecutionSubscriberStream;
use super::manager::{CodeManager, ExecuteInput};

pub const VALIDATE_TIMEOUT: Duration = Duration::from_secs(60);
/// Bound on the whole run, after the server timeout's own restart branch.
const VALIDATE_DEADLINE: Duration = Duration::from_secs(90);

pub async fn run_validation(manager: &Arc<CodeManager>) -> ValidationOutcome {
    if let Err(error) = manager.restart_context(DEFAULT_CONTEXT_ID).await {
        tracing::warn!(reason = %error, "validate could not restart the default kernel");
    }
    let input = ExecuteInput {
        context_id: None,
        language: None,
        code: VALIDATE_CELL.to_owned(),
        timeout_ms: u64::try_from(VALIDATE_TIMEOUT.as_millis()).unwrap_or(60_000),
        envs: std::collections::BTreeMap::new(),
    };
    let stream = match manager.execute_unchecked(input).await {
        Ok(stream) => stream,
        Err(error) => {
            return ValidationOutcome::Failed {
                error_name: error.to_string(),
            };
        }
    };
    let outcome = tokio::time::timeout(VALIDATE_DEADLINE, consume(stream)).await;
    match outcome {
        Ok(outcome) => outcome,
        Err(_) => ValidationOutcome::Failed {
            error_name: "ValidateDeadline".to_owned(),
        },
    }
}

async fn consume(mut stream: ExecutionSubscriberStream) -> ValidationOutcome {
    let mut error_name: Option<String> = None;
    let mut results = 0usize;
    while let Some(output) = stream.next().await {
        match output {
            ExecuteOutput::Error { error, .. } => error_name = Some(error.name),
            ExecuteOutput::Result { .. } => results += 1,
            ExecuteOutput::End { .. } => break,
            ExecuteOutput::Started { .. }
            | ExecuteOutput::Stdout { .. }
            | ExecuteOutput::Stderr { .. } => {}
        }
    }
    tracing::info!(results, "validate cell consumed");
    match error_name {
        Some(error_name) => ValidationOutcome::Failed { error_name },
        None => ValidationOutcome::Validated,
    }
}
