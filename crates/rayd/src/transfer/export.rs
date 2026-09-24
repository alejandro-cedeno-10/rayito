//! The task of one export (design D10): wait for a byte permit, then send
//! the parts in order, each `PUT` with exactly its length read by offset
//! from the descriptor opened when the export was accepted, in 1 MiB
//! chunks on a blocking thread, four in flight. sha256 runs over the whole
//! file in order: the hasher is cloned at each part's start and the clone
//! is kept only when that part's `PUT` succeeds, so a retried part never
//! hashes twice. A read that ends before the measured size aborts with
//! `file_shrank`; growth past it is never sent. A `/suspend` requeues the
//! export, which restarts from offset 0 after the resume.

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, PoisonError};
use std::time::Instant;

use bytes::Bytes;
use rayd_core::filesystem::SnapshotFile;
use rayd_core::transfer::{
    DoneOutcome, EXPORT_CHUNK_BYTES, EXPORT_CHUNK_QUEUE, ExportTarget, FailureReason, HttpError,
    HttpErrorKind, PartRange, PutOutcome, RequestBody, SignedHttp, TRANSFER_REQUEST_RETRIES,
    TransferDirection, TransferFailure, TransferId, put_part,
};
use sha2::{Digest, Sha256};
use tokio::sync::mpsc;

use super::import::millis;
use super::manager::{Gate, Interrupt, Interrupts, RecordHandle, TransferContext, lower_hex};

pub(crate) struct ExportJob {
    pub(crate) id: TransferId,
    pub(crate) handle: Arc<RecordHandle>,
    pub(crate) target: ExportTarget,
    pub(crate) plan: Vec<PartRange>,
    pub(crate) file: Arc<dyn SnapshotFile>,
    pub(crate) size: u64,
}

enum Ending {
    Done(Box<DoneOutcome>),
    Failed(TransferFailure),
    Cancelled,
    Terminated,
}

/// One pass over every part, from a held permit to the last `PUT`.
enum Pass {
    Done(Box<DoneOutcome>),
    Failed(TransferFailure),
    Suspended,
    Cancelled,
}

pub(crate) async fn run_export<H: SignedHttp>(context: Arc<TransferContext<H>>, job: ExportJob) {
    let started = Instant::now();
    let (phase, reason) = match drive(&context, &job).await {
        Ending::Done(outcome) => {
            context.hub.apply(&job.id, |registry, now| {
                registry.finish_done(&job.id, *outcome, now)
            });
            ("done", "none")
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
        direction = TransferDirection::Export.as_str(),
        phase,
        reason,
        bytes = job.size,
        duration_ms = millis(started.elapsed()),
        "transfer finished"
    );
}

async fn drive<H: SignedHttp>(context: &TransferContext<H>, job: &ExportJob) -> Ending {
    loop {
        match context.hub.wait_for_gate(&job.handle.cancel).await {
            Gate::Open => {}
            Gate::Terminated => return Ending::Terminated,
            Gate::Cancelled => return Ending::Cancelled,
        }
        let Some(mut interrupts) = context.hub.interrupts(&context.suspend, &job.handle.cancel)
        else {
            continue;
        };
        let permit = tokio::select! {
            acquired = context.hub.permits.clone().acquire_owned() => match acquired {
                Ok(permit) => permit,
                Err(_) => return Ending::Cancelled,
            },
            interrupt = interrupts.fired() => match interrupt {
                Interrupt::Cancelled => return Ending::Cancelled,
                Interrupt::Suspended => continue,
            },
        };
        if context
            .hub
            .apply(&job.id, |registry, now| {
                registry.start_running(&job.id, now)
            })
            .is_none()
        {
            return Ending::Cancelled;
        }
        let pass = upload(context, job, &mut interrupts).await;
        drop(permit);
        match pass {
            Pass::Done(outcome) => return Ending::Done(outcome),
            Pass::Failed(failure) => return Ending::Failed(failure),
            Pass::Cancelled => return Ending::Cancelled,
            Pass::Suspended => {
                context
                    .hub
                    .apply(&job.id, |registry, now| registry.requeue(&job.id, now));
                tracing::warn!(
                    transfer_id = %job.id,
                    direction = TransferDirection::Export.as_str(),
                    phase = "waiting",
                    reason = "suspending",
                    "transfer requeued"
                );
            }
        }
    }
}

async fn upload<H: SignedHttp>(
    context: &TransferContext<H>,
    job: &ExportJob,
    interrupts: &mut Interrupts,
) -> Pass {
    let mut hasher = Sha256::new();
    let mut part_etags = Vec::new();
    let mut completed = 0u64;
    for part in &job.plan {
        let Some(url) = job.target.url(part.number) else {
            return Pass::Failed(TransferFailure::of(FailureReason::UnexpectedResponse));
        };
        let mut retries = 0u32;
        loop {
            let body = SnapshotBody::spawn(job.file.clone(), *part, hasher.clone());
            let (part_hasher, shrank, sent) =
                (body.hasher.clone(), body.shrank.clone(), body.sent.clone());
            let put = put_part(
                &context.http,
                url,
                body,
                part.len,
                job.target.is_multipart(),
            );
            tokio::pin!(put);
            let mut ticker = tokio::time::interval(context.hub.settings.progress_interval);
            let (outcome, log) = loop {
                tokio::select! {
                    answered = &mut put => break answered,
                    _ = ticker.tick() => {
                        let done = completed + sent.load(Ordering::Relaxed);
                        context.hub.apply(&job.id, |registry, now| registry.set_progress(&job.id, done, now));
                    }
                    interrupt = interrupts.fired() => return match interrupt {
                        Interrupt::Cancelled => Pass::Cancelled,
                        Interrupt::Suspended => Pass::Suspended,
                    },
                }
            };
            if shrank.load(Ordering::SeqCst) {
                return Pass::Failed(TransferFailure::of(FailureReason::FileShrank));
            }
            tracing::debug!(
                transfer_id = %job.id,
                direction = TransferDirection::Export.as_str(),
                part = part.number,
                attempt = retries + 1,
                http_status = log.http_status,
                s3_error_code = log.s3_error_code.as_deref(),
                s3_request_id = log.s3_request_id.as_deref(),
                "transfer part"
            );
            match outcome {
                PutOutcome::Stored { etag } => {
                    hasher = part_hasher
                        .lock()
                        .unwrap_or_else(PoisonError::into_inner)
                        .clone();
                    if job.target.is_multipart() {
                        part_etags.push(etag.unwrap_or_default());
                    }
                    completed += part.len;
                    break;
                }
                PutOutcome::Failed(failure) => return Pass::Failed(failure),
                PutOutcome::Transient if retries >= TRANSFER_REQUEST_RETRIES => {
                    return Pass::Failed(TransferFailure::of(FailureReason::S3Unavailable));
                }
                PutOutcome::Transient => {
                    retries += 1;
                    tokio::select! {
                        () = tokio::time::sleep(context.hub.settings.retry_delay) => {}
                        interrupt = interrupts.fired() => return match interrupt {
                            Interrupt::Cancelled => Pass::Cancelled,
                            Interrupt::Suspended => Pass::Suspended,
                        },
                    }
                }
            }
        }
    }
    if job.handle.cancel.is_cancelled() {
        return Pass::Cancelled;
    }
    Pass::Done(Box::new(DoneOutcome {
        entry: None,
        sha256: lower_hex(&hasher.finalize()),
        bytes: job.size,
        part_etags,
    }))
}

/// One part's bytes, read with `read_at` on a blocking thread into a
/// bounded channel; each chunk updates the part's hasher as it goes out.
struct SnapshotBody {
    chunks: mpsc::Receiver<Result<Bytes, ()>>,
    hasher: Arc<Mutex<Sha256>>,
    shrank: Arc<AtomicBool>,
    sent: Arc<AtomicU64>,
}

impl SnapshotBody {
    fn spawn(file: Arc<dyn SnapshotFile>, part: PartRange, hasher: Sha256) -> Self {
        let (sender, chunks) = mpsc::channel(EXPORT_CHUNK_QUEUE);
        let shrank = Arc::new(AtomicBool::new(false));
        let flag = shrank.clone();
        tokio::task::spawn_blocking(move || read_part(file.as_ref(), part, &sender, &flag));
        Self {
            chunks,
            hasher: Arc::new(Mutex::new(hasher)),
            shrank,
            sent: Arc::new(AtomicU64::new(0)),
        }
    }
}

/// Exactly `part.len` bytes from `part.offset`; an end of file before that
/// sets `shrank` and fails the body, which aborts the `PUT`.
fn read_part(
    file: &dyn SnapshotFile,
    part: PartRange,
    sender: &mpsc::Sender<Result<Bytes, ()>>,
    shrank: &AtomicBool,
) {
    let end = part.offset.saturating_add(part.len);
    let mut offset = part.offset;
    while offset < end {
        let want = usize::try_from(end - offset)
            .unwrap_or(usize::MAX)
            .min(EXPORT_CHUNK_BYTES);
        let mut buffer = vec![0u8; want];
        let chunk = match file.read_at(&mut buffer, offset) {
            Ok(0) => {
                shrank.store(true, Ordering::SeqCst);
                Err(())
            }
            Ok(read) => {
                buffer.truncate(read);
                offset = offset.saturating_add(read as u64);
                Ok(Bytes::from(buffer))
            }
            Err(_) => Err(()),
        };
        let failed = chunk.is_err();
        if sender.blocking_send(chunk).is_err() || failed {
            return;
        }
    }
}

impl RequestBody for SnapshotBody {
    async fn next_chunk(&mut self) -> Result<Option<Bytes>, HttpError> {
        match self.chunks.recv().await {
            Some(Ok(bytes)) => {
                self.hasher
                    .lock()
                    .unwrap_or_else(PoisonError::into_inner)
                    .update(&bytes);
                self.sent.fetch_add(bytes.len() as u64, Ordering::Relaxed);
                Ok(Some(bytes))
            }
            Some(Err(())) => Err(HttpError::new(HttpErrorKind::Io)),
            None => Ok(None),
        }
    }
}
