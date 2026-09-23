//! The task of one import (design D9): poll the presigned `GET` on the
//! schedule until the object is there or the ticket expires; with the
//! object found and a byte permit held, the admission checks, then the
//! body through the `Write` sink with its sha256, the checksum, the
//! metadata and the commit. A broken body requeues (the third time ends
//! it); `/suspend` requeues without counting. Every outcome after the
//! object was seen runs the presigned `DELETE` once before the terminal
//! state is published, so a caller that sees `DONE` never finds the
//! staging object.

use std::sync::Arc;
use std::time::{Duration, Instant};

use rayd_core::filesystem::FsIdentity;
use rayd_core::transfer::{
    DeleteOutcome, DoneOutcome, FailureReason, HttpHead, ImportPlan, ImportRequest, PollStep,
    ProbeOutcome, ProbeResponse, ResponseBody, ResponseLog, SignedHttp, TransferDirection,
    TransferFailure, TransferId, attempts_exhausted, delete_object, is_expired, one_shot_verdict,
    probe, verify_checksum,
};
use sha2::{Digest, Sha256};
use tokio::sync::OwnedSemaphorePermit;

use super::manager::{Gate, Interrupt, Interrupts, RecordHandle, TransferContext, lower_hex};

pub(crate) struct ImportJob {
    pub(crate) id: TransferId,
    pub(crate) handle: Arc<RecordHandle>,
    pub(crate) request: ImportRequest,
    pub(crate) identity: FsIdentity,
    pub(crate) admitted_at: Duration,
}

/// How the task ended; `Cancelled` means the cancel RPC already wrote the
/// state, `Terminated` that `/terminate` arrived.
enum Ending {
    Done(Box<DoneOutcome>),
    Failed(TransferFailure),
    Cancelled,
    Terminated,
}

/// How one attempt at writing the body ended.
enum WriteEnd {
    Done(Box<DoneOutcome>),
    Failed(TransferFailure),
    Interrupted,
    Suspended,
    Cancelled,
}

pub(crate) async fn run_import<H: SignedHttp>(context: Arc<TransferContext<H>>, job: ImportJob) {
    let started = Instant::now();
    let ending = drive(&context, &job).await;
    let observed = context
        .hub
        .snapshot(job.id.as_str())
        .is_ok_and(|snapshot| snapshot.object_seen);
    if observed && !matches!(ending, Ending::Terminated) {
        cleanup(&context, &job).await;
    }
    let (phase, reason) = match ending {
        Ending::Done(outcome) => {
            let bytes = outcome.bytes;
            context.hub.apply(&job.id, |registry, now| {
                registry.finish_done(&job.id, *outcome, now)
            });
            tracing::info!(
                transfer_id = %job.id,
                direction = TransferDirection::Import.as_str(),
                phase = "done",
                bytes,
                duration_ms = millis(started.elapsed()),
                "transfer finished"
            );
            return;
        }
        Ending::Failed(failure) => {
            context.hub.apply(&job.id, |registry, now| {
                registry.finish_failed(&job.id, failure, now)
            });
            ("failed", failure.reason.token())
        }
        Ending::Cancelled => ("cancelled", FailureReason::Cancelled.token()),
        Ending::Terminated => {
            context.hub.terminate(&job.id);
            ("cancelled", "terminating")
        }
    };
    tracing::info!(
        transfer_id = %job.id,
        direction = TransferDirection::Import.as_str(),
        phase,
        reason,
        duration_ms = millis(started.elapsed()),
        "transfer finished"
    );
}

async fn drive<H: SignedHttp>(context: &TransferContext<H>, job: &ImportJob) -> Ending {
    let mut permit: Option<OwnedSemaphorePermit> = None;
    let mut interrupted = 0u32;
    let mut retries = 0u32;
    loop {
        let mut interrupts = match arm(context, job).await {
            Ok(interrupts) => interrupts,
            Err(ending) => return ending,
        };
        if is_expired(context.hub.wall_ms(), job.request.expires_at_unix_ms) {
            return Ending::Failed(TransferFailure::of(FailureReason::Expired));
        }
        let (response, log) = tokio::select! {
            probed = probe(&context.http, &job.request.get, job.request.wait_for_object) => probed,
            interrupt = interrupts.fired() => match interrupt {
                Interrupt::Cancelled => return Ending::Cancelled,
                Interrupt::Suspended => {
                    permit = None;
                    continue;
                }
            },
        };
        let found = matches!(response, ProbeResponse::Found { .. });
        let probes = context
            .hub
            .apply(&job.id, |registry, now| {
                registry.record_probe(&job.id, found, now)
            })
            .map_or(0, |snapshot| snapshot.probes);
        log_probe(&job.id, probes, found, &log);
        match response {
            ProbeResponse::Found { head, body } => {
                let Some(held) = permit
                    .take()
                    .or_else(|| context.hub.permits.clone().try_acquire_owned().ok())
                else {
                    drop(body);
                    match wait_for_permit(context, &mut interrupts).await {
                        Ok(acquired) => {
                            permit = Some(acquired);
                            continue;
                        }
                        Err(Interrupt::Cancelled) => return Ending::Cancelled,
                        Err(Interrupt::Suspended) => continue,
                    }
                };
                match write_object(context, job, head, body, held, &mut interrupts).await {
                    WriteEnd::Done(outcome) => return Ending::Done(outcome),
                    WriteEnd::Failed(failure) => return Ending::Failed(failure),
                    WriteEnd::Cancelled => return Ending::Cancelled,
                    WriteEnd::Suspended => requeue(context, job, "suspending", interrupted),
                    WriteEnd::Interrupted => {
                        interrupted += 1;
                        if attempts_exhausted(interrupted) {
                            return Ending::Failed(TransferFailure::of(
                                FailureReason::S3Unavailable,
                            ));
                        }
                        requeue(context, job, "interrupted", interrupted);
                    }
                }
            }
            ProbeResponse::Answer(outcome) => {
                permit = None;
                let wait = match next_wait(context, job, outcome, &mut retries) {
                    Ok(wait) => wait,
                    Err(failure) => return Ending::Failed(failure),
                };
                tokio::select! {
                    () = tokio::time::sleep(wait) => {}
                    () = job.handle.poll_now.notified() => {}
                    interrupt = interrupts.fired() => {
                        if interrupt == Interrupt::Cancelled {
                            return Ending::Cancelled;
                        }
                    }
                }
            }
        }
    }
}

/// Waits for the gate, then subscribes; a `/terminate` or a cancel while
/// parked ends the task.
async fn arm<H>(context: &TransferContext<H>, job: &ImportJob) -> Result<Interrupts, Ending> {
    loop {
        match context.hub.wait_for_gate(&job.handle.cancel).await {
            Gate::Open => {}
            Gate::Terminated => return Err(Ending::Terminated),
            Gate::Cancelled => return Err(Ending::Cancelled),
        }
        if let Some(interrupts) = context.hub.interrupts(&context.suspend, &job.handle.cancel) {
            return Ok(interrupts);
        }
    }
}

/// The wait before the next probe: the poll schedule for an armed ticket
/// (1 s, then 5 s after ten minutes, never past the expiry), the retry
/// delay for a one-shot import that got a transient answer.
fn next_wait<H>(
    context: &TransferContext<H>,
    job: &ImportJob,
    outcome: ProbeOutcome,
    retries: &mut u32,
) -> Result<Duration, TransferFailure> {
    let verdict = if job.request.wait_for_object {
        outcome
    } else {
        one_shot_verdict(outcome, *retries)
    };
    match verdict {
        ProbeOutcome::Failed(failure) => Err(failure),
        ProbeOutcome::Transient if !job.request.wait_for_object => {
            *retries += 1;
            Ok(context.hub.settings.retry_delay)
        }
        ProbeOutcome::Found | ProbeOutcome::Pending | ProbeOutcome::Transient => {
            let since = context.hub.now().saturating_sub(job.admitted_at);
            match context.hub.settings.poll.next(
                since,
                context.hub.wall_ms(),
                job.request.expires_at_unix_ms,
            ) {
                PollStep::Wait(wait) => Ok(wait),
                PollStep::Expired => Err(TransferFailure::of(FailureReason::Expired)),
            }
        }
    }
}

async fn wait_for_permit<H>(
    context: &TransferContext<H>,
    interrupts: &mut Interrupts,
) -> Result<OwnedSemaphorePermit, Interrupt> {
    tokio::select! {
        acquired = context.hub.permits.clone().acquire_owned() => {
            acquired.map_err(|_| Interrupt::Cancelled)
        }
        interrupt = interrupts.fired() => Err(interrupt),
    }
}

fn requeue<H>(context: &TransferContext<H>, job: &ImportJob, reason: &'static str, attempt: u32) {
    context
        .hub
        .apply(&job.id, |registry, now| registry.requeue(&job.id, now));
    tracing::warn!(
        transfer_id = %job.id,
        direction = TransferDirection::Import.as_str(),
        phase = "waiting",
        reason,
        attempt,
        "transfer requeued"
    );
}

/// Admission checks against the destination as it is now, then the body
/// into the temp file with its sha256; the permit is held until the end.
async fn write_object<H: SignedHttp>(
    context: &TransferContext<H>,
    job: &ImportJob,
    head: HttpHead,
    mut body: H::Body,
    _permit: OwnedSemaphorePermit,
    interrupts: &mut Interrupts,
) -> WriteEnd {
    if context
        .hub
        .apply(&job.id, |registry, now| {
            registry.start_running(&job.id, now)
        })
        .is_none()
    {
        return WriteEnd::Interrupted;
    }
    let files = &context.files;
    let destination = match files
        .import_destination(&job.identity, &job.request.path)
        .await
    {
        Ok(destination) => destination,
        Err(error) => return WriteEnd::Failed(TransferFailure::from_filesystem(&error)),
    };
    let free = match files
        .import_free_bytes(&job.identity, &destination.dir)
        .await
    {
        Ok(free) => free,
        Err(error) => return WriteEnd::Failed(TransferFailure::from_filesystem(&error)),
    };
    let length = match ImportPlan::admit(head.content_length, job.request.max_bytes, free) {
        Ok(length) => length,
        Err(failure) => return WriteEnd::Failed(failure),
    };
    context.hub.apply(&job.id, |registry, now| {
        registry.set_total(&job.id, length, now)
    });
    let mut sink = match files
        .begin_import(&job.identity, &destination, job.request.mode)
        .await
    {
        Ok(sink) => sink,
        Err(error) => return WriteEnd::Failed(TransferFailure::from_filesystem(&error)),
    };
    let mut hasher = Sha256::new();
    let mut written = 0u64;
    let mut progress = Progress::new(context.hub.settings.progress_interval);
    loop {
        let chunk = tokio::select! {
            chunk = body.next_chunk() => chunk,
            interrupt = interrupts.fired() => return interrupted_by(interrupt),
        };
        let bytes = match chunk {
            Ok(Some(bytes)) => bytes,
            Ok(None) if written == length => break,
            Ok(None) | Err(_) => return WriteEnd::Interrupted,
        };
        written = written.saturating_add(bytes.len() as u64);
        if written > length {
            return WriteEnd::Interrupted;
        }
        hasher.update(&bytes);
        let stored = tokio::select! {
            stored = sink.write(bytes) => stored,
            interrupt = interrupts.fired() => return interrupted_by(interrupt),
        };
        if let Err(error) = stored {
            return WriteEnd::Failed(TransferFailure::from_filesystem(&error));
        }
        if progress.due() {
            context.hub.apply(&job.id, |registry, now| {
                registry.set_progress(&job.id, written, now)
            });
        }
    }
    let sha256 = lower_hex(&hasher.finalize());
    if let Err(failure) = verify_checksum(job.request.expected_sha256.as_deref(), &sha256) {
        return WriteEnd::Failed(failure);
    }
    if job.handle.cancel.is_cancelled() {
        return WriteEnd::Cancelled;
    }
    match sink.commit(job.request.metadata.clone()).await {
        Ok(entry) => WriteEnd::Done(Box::new(DoneOutcome {
            entry: Some(entry),
            sha256,
            bytes: length,
            part_etags: Vec::new(),
        })),
        Err(error) => WriteEnd::Failed(TransferFailure::from_filesystem(&error)),
    }
}

fn interrupted_by(interrupt: Interrupt) -> WriteEnd {
    match interrupt {
        Interrupt::Cancelled => WriteEnd::Cancelled,
        Interrupt::Suspended => WriteEnd::Suspended,
    }
}

/// The presigned `DELETE`, retried once on a transient answer; its outcome
/// is logged and never changes the transfer's result.
async fn cleanup<H: SignedHttp>(context: &TransferContext<H>, job: &ImportJob) {
    let Some(delete) = job.request.delete.as_ref() else {
        return;
    };
    for attempt in 1..=2u32 {
        let (outcome, log) = delete_object(&context.http, delete).await;
        tracing::info!(
            transfer_id = %job.id,
            direction = TransferDirection::Import.as_str(),
            phase = "cleanup",
            attempt,
            http_status = log.http_status,
            s3_error_code = log.s3_error_code.as_deref(),
            s3_request_id = log.s3_request_id.as_deref(),
            outcome = delete_outcome(outcome),
            "transfer cleanup"
        );
        if outcome != DeleteOutcome::Transient {
            return;
        }
    }
}

fn delete_outcome(outcome: DeleteOutcome) -> &'static str {
    match outcome {
        DeleteOutcome::Deleted => "deleted",
        DeleteOutcome::Transient => "transient",
        DeleteOutcome::Failed => "failed",
    }
}

fn log_probe(id: &TransferId, probes: u32, found: bool, log: &ResponseLog) {
    tracing::debug!(
        transfer_id = %id,
        direction = TransferDirection::Import.as_str(),
        probes,
        http_status = log.http_status,
        s3_error_code = log.s3_error_code.as_deref(),
        s3_request_id = log.s3_request_id.as_deref(),
        outcome = if found { "found" } else { "absent" },
        "transfer probe"
    );
}

/// Progress goes to the registry at most once per interval.
pub(crate) struct Progress {
    interval: Duration,
    last: Instant,
}

impl Progress {
    pub(crate) fn new(interval: Duration) -> Self {
        Self {
            interval,
            last: Instant::now(),
        }
    }

    pub(crate) fn due(&mut self) -> bool {
        if self.last.elapsed() >= self.interval {
            self.last = Instant::now();
            true
        } else {
            false
        }
    }
}

pub(crate) fn millis(duration: Duration) -> u64 {
    u64::try_from(duration.as_millis()).unwrap_or(u64::MAX)
}
