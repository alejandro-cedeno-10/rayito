//! Presigned transfers in process (`m9-file-transfer` design D23): the real
//! filesystem, process and code managers behind loopback tonic, and a fake
//! `SignedHttp` that plays S3 (objects by path, 404 `NoSuchKey` for a
//! missing one, `ETag`s for every `PUT`) and records every request, so a
//! refusal can be shown to have opened nothing. Imports (happy path, the
//! armed poll, expiry, `too_large`, `checksum_mismatch`, a `/suspend`
//! requeue), exports (single, multipart, a file that shrinks mid-read),
//! the 16-transfer cap, the URL policy, the read-after-upload barrier,
//! gzip only on opt-in, metadata on `Write`, `Stat` and `ListDir`, and the
//! log hygiene of D21.
//!
//! Integration tests are test code, but clippy's `allow-unwrap-in-tests` only
//! recognises `#[test]` functions and `#[cfg(test)]` modules.
#![cfg(unix)]
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::too_many_lines
)]

mod common;

use std::collections::{HashMap, VecDeque};
use std::fs;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use bytes::Bytes;
use common::log_capture::LogCapture;
use common::{Harness, Options, authenticated, data_bytes, harness_with, shell, temp_files};
use http::StatusCode;
use rayd::adapters::OsRandomSource;
use rayd::grpc::TransferServices;
use rayd::transfer::{TransferManager, TransferSettings};
use rayd_core::lifecycle::Hook;
use rayd_core::transfer::{
    HttpError, HttpErrorKind, HttpHead, HttpMethod, OCTET_STREAM, PollSchedule, RequestBody,
    ResponseBody, SignedHttp, SignedRequest,
};
use rayito_proto::v1::{
    CancelTransferRequest, EntryInfo, GetTransferRequest, ListDirRequest, PresignedMultipart,
    PresignedRequest, ReadRequest, S3Object, StartExportRequest, StartImportRequest, StatRequest,
    TransferPhase, TransferState, WatchTransferRequest, WriteRequest, process_event,
    start_export_request, transfer_event,
};
use sha2::{Digest, Sha256};
use tonic::Code;
use tonic::codec::CompressionEncoding;
use tonic::metadata::MetadataValue;

const BUCKET: &str = "amzn-s3-demo-bucket";
const REGION: &str = "us-east-1";
/// The `microvmId` of the harness's `/run` envelope: staging keys must end
/// with it.
const SANDBOX: &str = "mvm-m6";
const PREFIX: &str = "rayito-transfer";
const SIGNATURE: &str = "0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c4b5a69788796a5b4c3d2e1f0";
const MIB: usize = 1024 * 1024;
const BUDGET: Duration = Duration::from_secs(20);

fn host() -> String {
    format!("{BUCKET}.s3.{REGION}.amazonaws.com")
}

fn query() -> String {
    format!(
        "X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=AKIDEXAMPLE%2F20260922%2F{REGION}%2Fs3%2Faws4_request&X-Amz-Date=20260922T000000Z&X-Amz-Expires=3600&X-Amz-SignedHeaders=host&X-Amz-Signature={SIGNATURE}"
    )
}

fn token(seed: u32) -> String {
    format!("{seed:032x}")
}

fn up_key(seed: u32) -> String {
    format!("{PREFIX}/{SANDBOX}/up/{}", token(seed))
}

fn down_key(seed: u32) -> String {
    format!("{PREFIX}/{SANDBOX}/down/{}", token(seed))
}

fn url_for(key: &str) -> String {
    format!("https://{}/{key}?{}", host(), query())
}

fn presigned(url: String) -> PresignedRequest {
    PresignedRequest {
        url,
        headers: HashMap::new(),
    }
}

fn object(key: &str) -> S3Object {
    S3Object {
        bucket: BUCKET.to_owned(),
        key: key.to_owned(),
        region: REGION.to_owned(),
    }
}

fn now_ms() -> i64 {
    i64::try_from(
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_millis(),
    )
    .unwrap()
}

fn import_request(path: &str, key: &str, wait_for_object: bool) -> StartImportRequest {
    StartImportRequest {
        path: path.to_owned(),
        object: Some(object(key)),
        get: Some(presigned(url_for(key))),
        delete: Some(presigned(url_for(key))),
        wait_for_object,
        expires_at_unix_ms: now_ms() + 600_000,
        ..StartImportRequest::default()
    }
}

fn export_request(path: &str, key: &str) -> StartExportRequest {
    StartExportRequest {
        path: path.to_owned(),
        object: Some(object(key)),
        target: Some(start_export_request::Target::Put(presigned(url_for(key)))),
        expires_at_unix_ms: now_ms() + 600_000,
        ..StartExportRequest::default()
    }
}

fn multipart_request(path: &str, key: &str, part_size: u64, parts: u32) -> StartExportRequest {
    let urls = (1..=parts)
        .map(|number| presigned(format!("{}&partNumber={number}&uploadId=U1", url_for(key))))
        .collect();
    StartExportRequest {
        target: Some(start_export_request::Target::Multipart(
            PresignedMultipart {
                part_size,
                parts: urls,
            },
        )),
        ..export_request(path, key)
    }
}

fn sha256_hex(bytes: &[u8]) -> String {
    use std::fmt::Write as _;
    Sha256::digest(bytes)
        .iter()
        .fold(String::new(), |mut hex, byte| {
            write!(hex, "{byte:02x}").unwrap();
            hex
        })
}

/// Bytes that differ at every offset of a part, so a part sent at the wrong
/// offset cannot hash the same.
fn pattern(len: usize, seed: u8) -> Vec<u8> {
    (0..len)
        .map(|index| u8::try_from((index * 31 + usize::from(seed)) % 251).unwrap())
        .collect()
}

/// A poll fast enough for the tests; everything else at its default.
fn fast_settings() -> TransferSettings {
    TransferSettings {
        poll: PollSchedule {
            fast_interval: Duration::from_millis(50),
            slow_interval: Duration::from_millis(50),
            fast_window: Duration::from_secs(600),
        },
        retry_delay: Duration::from_millis(50),
        progress_interval: Duration::from_millis(50),
        gate_poll: Duration::from_millis(20),
        ..TransferSettings::default()
    }
}

/// A poll that never fires within a test: only the barrier's "poll now"
/// makes an armed ticket look again.
fn barrier_settings() -> TransferSettings {
    TransferSettings {
        poll: PollSchedule {
            fast_interval: Duration::from_secs(60),
            slow_interval: Duration::from_secs(60),
            fast_window: Duration::from_secs(600),
        },
        ..fast_settings()
    }
}

/// One request the fake received; the URL is kept whole so the tests can
/// tell which object and which part it addressed.
#[derive(Debug, Clone)]
struct Call {
    method: HttpMethod,
    url: String,
    content_type: Option<&'static str>,
    content_length: Option<u64>,
}

impl Call {
    fn path(&self) -> &str {
        let rest = self.url.split_once("://").map_or("", |(_, rest)| rest);
        let path = rest.find('/').map_or("", |start| &rest[start..]);
        path.split('?').next().unwrap_or_default()
    }

    fn part_number(&self) -> Option<u32> {
        self.url
            .split(['?', '&'])
            .find_map(|param| param.strip_prefix("partNumber="))
            .and_then(|number| number.parse().ok())
    }
}

/// A canned answer for the next `GET`s, before the object store is asked.
#[derive(Debug, Clone, Copy)]
enum Scripted {
    Status(u16, &'static str),
}

type PutHook = Arc<dyn Fn() + Send + Sync>;

#[derive(Default)]
struct S3State {
    objects: HashMap<String, Vec<u8>>,
    parts: HashMap<u32, Vec<u8>>,
    calls: Vec<Call>,
    scripted_gets: VecDeque<Scripted>,
    stall_gets: bool,
    on_first_put_chunk: Option<PutHook>,
}

/// The fake S3 behind `SignedHttp`: `GET` serves the stored object in
/// 64 KiB chunks (or a stalled body, or 404 `NoSuchKey`), `PUT` stores the
/// whole body (a part by its number) and answers an `ETag`, `DELETE`
/// removes the object.
#[derive(Clone, Default)]
struct FakeS3 {
    state: Arc<Mutex<S3State>>,
}

impl FakeS3 {
    fn state(&self) -> std::sync::MutexGuard<'_, S3State> {
        self.state.lock().unwrap()
    }

    fn store(&self, key: &str, bytes: &[u8]) {
        self.state()
            .objects
            .insert(format!("/{key}"), bytes.to_vec());
    }

    fn object(&self, key: &str) -> Option<Vec<u8>> {
        self.state().objects.get(&format!("/{key}")).cloned()
    }

    fn part(&self, number: u32) -> Option<Vec<u8>> {
        self.state().parts.get(&number).cloned()
    }

    fn calls(&self) -> Vec<Call> {
        self.state().calls.clone()
    }

    fn calls_of(&self, method: HttpMethod) -> Vec<Call> {
        self.calls()
            .into_iter()
            .filter(|call| call.method == method)
            .collect()
    }

    fn script_gets(&self, answers: &[Scripted]) {
        self.state().scripted_gets.extend(answers.iter().copied());
    }

    fn stall_gets(&self, stall: bool) {
        self.state().stall_gets = stall;
    }

    fn on_first_put_chunk(&self, hook: PutHook) {
        self.state().on_first_put_chunk = Some(hook);
    }

    fn get(&self, path: &str) -> (HttpHead, FakeBody) {
        let mut state = self.state();
        if let Some(Scripted::Status(status, code)) = state.scripted_gets.pop_front() {
            return error_response(status, code);
        }
        match state.objects.get(path) {
            Some(bytes) => {
                let head = HttpHead {
                    status: 200,
                    content_length: Some(bytes.len() as u64),
                    etag: Some("\"object\"".to_owned()),
                    request_id: Some("REQ-GET".to_owned()),
                };
                (head, FakeBody::chunked(bytes, state.stall_gets))
            }
            None => error_response(404, "NoSuchKey"),
        }
    }

    async fn put<B: RequestBody>(&self, call: &Call, mut body: B) -> Result<HttpHead, HttpError> {
        let hook = self.state().on_first_put_chunk.take();
        let mut received = Vec::new();
        let mut first = true;
        while let Some(chunk) = body.next_chunk().await? {
            received.extend_from_slice(&chunk);
            if first {
                first = false;
                if let Some(hook) = &hook {
                    hook();
                }
            }
        }
        let mut state = self.state();
        let etag = if let Some(number) = call.part_number() {
            state.parts.insert(number, received);
            format!("\"etag-{number}\"")
        } else {
            state.objects.insert(call.path().to_owned(), received);
            "\"etag-put\"".to_owned()
        };
        Ok(HttpHead {
            status: 200,
            content_length: Some(0),
            etag: Some(etag),
            request_id: Some("REQ-PUT".to_owned()),
        })
    }
}

fn error_response(status: u16, code: &str) -> (HttpHead, FakeBody) {
    let body = format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<Error><Code>{code}</Code><Message>fake</Message><RequestId>REQ-ERR</RequestId></Error>"
    );
    let head = HttpHead {
        status,
        content_length: Some(body.len() as u64),
        etag: None,
        request_id: Some("REQ-ERR".to_owned()),
    };
    (head, FakeBody::chunked(body.as_bytes(), false))
}

/// A response body; a stalled one sends its first chunk and then never
/// another, like a connection that went quiet mid-download.
struct FakeBody {
    chunks: VecDeque<Bytes>,
    stall_after_first: bool,
    sent: usize,
}

impl FakeBody {
    fn chunked(bytes: &[u8], stall_after_first: bool) -> Self {
        Self {
            chunks: bytes
                .chunks(64 * 1024)
                .map(Bytes::copy_from_slice)
                .collect(),
            stall_after_first,
            sent: 0,
        }
    }
}

impl ResponseBody for FakeBody {
    async fn next_chunk(&mut self) -> Result<Option<Bytes>, HttpError> {
        if self.stall_after_first && self.sent == 1 {
            std::future::pending::<()>().await;
        }
        self.sent += 1;
        Ok(self.chunks.pop_front())
    }
}

impl SignedHttp for FakeS3 {
    type Body = FakeBody;

    async fn send<B: RequestBody>(
        &self,
        request: SignedRequest,
        body: Option<B>,
    ) -> Result<(HttpHead, FakeBody), HttpError> {
        let call = Call {
            method: request.method,
            url: request.url.as_str().to_owned(),
            content_type: request.content_type,
            content_length: request.content_length,
        };
        self.state().calls.push(call.clone());
        match call.method {
            HttpMethod::Get => Ok(self.get(call.path())),
            HttpMethod::Put => {
                let body = body.ok_or(HttpError::new(HttpErrorKind::Io))?;
                let head = self.put(&call, body).await?;
                Ok((head, FakeBody::chunked(&[], false)))
            }
            HttpMethod::Delete => {
                self.state().objects.remove(call.path());
                let head = HttpHead {
                    status: 204,
                    request_id: Some("REQ-DELETE".to_owned()),
                    ..HttpHead::default()
                };
                Ok((head, FakeBody::chunked(&[], false)))
            }
        }
    }
}

/// Every log line of this binary, `rayd`'s own at debug so the probe and
/// part lines are covered too. Installed before any harness initialises
/// the stderr subscriber.
fn capture() -> LogCapture {
    static CAPTURE: OnceLock<LogCapture> = OnceLock::new();
    CAPTURE
        .get_or_init(|| {
            let capture = LogCapture::default();
            let _ = tracing_subscriber::fmt()
                .json()
                .with_env_filter(tracing_subscriber::EnvFilter::new(
                    "info,rayd=debug,rayd_core=debug",
                ))
                .with_writer(capture.clone())
                .with_target(false)
                .try_init();
            capture
        })
        .clone()
}

fn options_with(fake: &FakeS3, settings: TransferSettings) -> Options {
    let http = fake.clone();
    Options {
        transfers: Some(Box::new(move |session, files, suspend| {
            let manager = TransferManager::new(
                session.clone(),
                files.clone(),
                suspend.clone(),
                http,
                Arc::new(OsRandomSource),
                settings,
            );
            TransferServices {
                barrier: manager.barrier(),
                backend: manager,
            }
        })),
        ..Options::default()
    }
}

async fn transfer_harness(fake: &FakeS3, settings: TransferSettings) -> Harness {
    capture();
    harness_with(options_with(fake, settings)).await
}

async fn start_import(
    harness: &Harness,
    request: StartImportRequest,
) -> Result<String, tonic::Status> {
    harness
        .files
        .clone()
        .start_import(authenticated(request))
        .await
        .map(|response| response.into_inner().transfer_id)
}

async fn start_export(
    harness: &Harness,
    request: StartExportRequest,
) -> Result<String, tonic::Status> {
    harness
        .files
        .clone()
        .start_export(authenticated(request))
        .await
        .map(|response| response.into_inner().transfer_id)
}

async fn get_transfer(harness: &Harness, id: &str) -> Result<TransferState, tonic::Status> {
    harness
        .files
        .clone()
        .get_transfer(authenticated(GetTransferRequest {
            transfer_id: id.to_owned(),
        }))
        .await
        .map(tonic::Response::into_inner)
}

async fn cancel_transfer(harness: &Harness, id: &str) -> Result<(), tonic::Status> {
    harness
        .files
        .clone()
        .cancel_transfer(authenticated(CancelTransferRequest {
            transfer_id: id.to_owned(),
        }))
        .await
        .map(|_| ())
}

/// `WatchTransfer` until its terminal snapshot; the stream must end right
/// after it.
async fn wait_terminal(harness: &Harness, id: &str) -> TransferState {
    let mut stream = harness
        .files
        .clone()
        .watch_transfer(authenticated(WatchTransferRequest {
            transfer_id: id.to_owned(),
        }))
        .await
        .unwrap()
        .into_inner();
    loop {
        let event = tokio::time::timeout(BUDGET, stream.message())
            .await
            .expect("transfer never finished")
            .unwrap()
            .expect("stream ended before a terminal state");
        if let Some(transfer_event::Event::State(state)) = event.event
            && is_terminal(&state)
        {
            let after = tokio::time::timeout(BUDGET, stream.message())
                .await
                .unwrap()
                .unwrap();
            assert!(after.is_none(), "the stream ends after the terminal state");
            return state;
        }
    }
}

fn is_terminal(state: &TransferState) -> bool {
    matches!(
        state.phase(),
        TransferPhase::Done | TransferPhase::Failed | TransferPhase::Cancelled
    )
}

async fn wait_until<F: Fn() -> bool>(what: &str, condition: F) {
    let deadline = Instant::now() + BUDGET;
    while !condition() {
        assert!(Instant::now() < deadline, "timed out waiting for {what}");
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
}

async fn read_file(harness: &Harness, path: &str) -> Result<Vec<u8>, tonic::Status> {
    let mut stream = harness
        .files
        .clone()
        .read(authenticated(ReadRequest {
            path: path.to_owned(),
            user: None,
        }))
        .await?
        .into_inner();
    let mut bytes = Vec::new();
    while let Some(response) = stream.message().await? {
        bytes.extend_from_slice(&response.chunk);
    }
    Ok(bytes)
}

async fn stat(harness: &Harness, path: &str) -> EntryInfo {
    harness
        .files
        .clone()
        .stat(authenticated(StatRequest {
            path: path.to_owned(),
            user: None,
        }))
        .await
        .unwrap()
        .into_inner()
        .entry
        .unwrap()
}

/// The stdout of one shell command, to its end.
async fn run_stdout(harness: &Harness, script: &str) -> Vec<u8> {
    let (_, mut stream) = harness.start(shell(script, 10_000)).await.unwrap();
    let mut stdout = Vec::new();
    loop {
        let event = tokio::time::timeout(BUDGET, stream.message())
            .await
            .expect("process stalled")
            .unwrap();
        match event.and_then(|event| event.event) {
            Some(process_event::Event::Data(data)) => stdout.extend_from_slice(data_bytes(&data)),
            Some(process_event::Event::End(_)) | None => return stdout,
            Some(_) => {}
        }
    }
}

fn owner_metadata() -> HashMap<String, String> {
    HashMap::from([("Owner".to_owned(), "alice".to_owned())])
}

fn lowercase_owner() -> HashMap<String, String> {
    HashMap::from([("owner".to_owned(), "alice".to_owned())])
}

#[tokio::test]
async fn an_import_writes_the_object_as_the_user_with_mode_metadata_and_sha256_then_deletes_it() {
    let fake = FakeS3::default();
    let harness = transfer_harness(&fake, fast_settings()).await;
    let path = harness.path("imported.bin");
    let bytes = pattern(300 * 1024 + 7, 1);
    fake.store(&up_key(1), &bytes);
    let id = start_import(
        &harness,
        StartImportRequest {
            mode: Some(0o600),
            metadata: owner_metadata(),
            ..import_request(&path, &up_key(1), true)
        },
    )
    .await
    .unwrap();
    assert_eq!(id.len(), 32);
    let state = wait_terminal(&harness, &id).await;
    assert_eq!(state.phase(), TransferPhase::Done, "{state:?}");
    assert_eq!(state.sha256, sha256_hex(&bytes));
    assert_eq!(state.bytes_done, bytes.len() as u64);
    assert_eq!(state.bytes_total, bytes.len() as u64);
    assert!(state.probes >= 1);
    let entry = state.entry.unwrap();
    assert_eq!(entry.path, path);
    assert_eq!(entry.size, bytes.len() as u64);
    assert_eq!(entry.metadata, lowercase_owner());
    assert_eq!(fs::read(&path).unwrap(), bytes);
    let on_disk = fs::metadata(&path).unwrap();
    assert_eq!(on_disk.permissions().mode() & 0o7777, 0o600);
    assert_eq!(on_disk.uid(), nix::unistd::geteuid().as_raw());
    assert_eq!(stat(&harness, &path).await.metadata, lowercase_owner());
    assert_eq!(temp_files(&harness.root), 0);
    let deletes = fake.calls_of(HttpMethod::Delete);
    assert_eq!(deletes.len(), 1, "one cleanup DELETE");
    assert_eq!(deletes[0].path(), format!("/{}", up_key(1)));
    assert!(
        fake.object(&up_key(1)).is_none(),
        "the staging object is gone"
    );
    let gets = fake.calls_of(HttpMethod::Get);
    assert!(
        gets.iter()
            .all(|call| call.content_length.is_none() && call.content_type.is_none())
    );
    let replay = get_transfer(&harness, &id).await.unwrap();
    assert_eq!(
        replay.phase(),
        TransferPhase::Done,
        "finished transfers stay readable"
    );
}

#[tokio::test]
async fn an_armed_ticket_polls_through_404_and_403_until_the_object_appears() {
    let fake = FakeS3::default();
    let harness = transfer_harness(&fake, fast_settings()).await;
    let path = harness.path("late.bin");
    fake.script_gets(&[Scripted::Status(403, "AccessDenied")]);
    let id = start_import(&harness, import_request(&path, &up_key(2), true))
        .await
        .unwrap();
    wait_until("three probes", || fake.calls_of(HttpMethod::Get).len() >= 3).await;
    let waiting = get_transfer(&harness, &id).await.unwrap();
    assert_eq!(waiting.phase(), TransferPhase::Waiting);
    assert!(!fs::exists(&path).unwrap());
    let bytes = pattern(4096, 2);
    fake.store(&up_key(2), &bytes);
    let state = wait_terminal(&harness, &id).await;
    assert_eq!(state.phase(), TransferPhase::Done, "{state:?}");
    assert!(state.probes >= 4, "{}", state.probes);
    assert_eq!(
        u32::try_from(fake.calls_of(HttpMethod::Get).len()).unwrap(),
        state.probes
    );
    assert_eq!(fs::read(&path).unwrap(), bytes);
}

#[tokio::test]
async fn an_armed_ticket_without_an_object_fails_deadline_exceeded_at_its_expiry() {
    let fake = FakeS3::default();
    let harness = transfer_harness(&fake, fast_settings()).await;
    let path = harness.path("never.bin");
    let started = Instant::now();
    let id = start_import(
        &harness,
        StartImportRequest {
            expires_at_unix_ms: now_ms() + 1_500,
            ..import_request(&path, &up_key(3), true)
        },
    )
    .await
    .unwrap();
    let state = wait_terminal(&harness, &id).await;
    let elapsed = started.elapsed();
    assert_eq!(state.phase(), TransferPhase::Failed);
    let error = state.error.unwrap();
    assert_eq!(error.code, "deadline_exceeded");
    assert!(error.message.starts_with("expired: "), "{}", error.message);
    assert!(elapsed >= Duration::from_millis(1_300), "{elapsed:?}");
    assert!(elapsed < Duration::from_secs(6), "{elapsed:?}");
    assert!(
        fake.calls_of(HttpMethod::Delete).is_empty(),
        "no object was ever seen"
    );
    assert!(!fs::exists(&path).unwrap());
}

#[tokio::test]
async fn an_object_above_max_bytes_is_refused_before_any_byte_and_still_deleted() {
    let fake = FakeS3::default();
    let harness = transfer_harness(&fake, fast_settings()).await;
    let path = harness.path("capped.bin");
    fake.store(&up_key(4), &pattern(2048, 4));
    let id = start_import(
        &harness,
        StartImportRequest {
            max_bytes: 1024,
            ..import_request(&path, &up_key(4), true)
        },
    )
    .await
    .unwrap();
    let state = wait_terminal(&harness, &id).await;
    assert_eq!(state.phase(), TransferPhase::Failed);
    let error = state.error.unwrap();
    assert_eq!(error.code, "invalid_argument");
    assert!(
        error.message.starts_with("too_large: "),
        "{}",
        error.message
    );
    assert!(!fs::exists(&path).unwrap(), "no file written");
    assert_eq!(temp_files(&harness.root), 0, "no temporary opened or left");
    assert_eq!(fake.calls_of(HttpMethod::Delete).len(), 1);
    assert!(fake.object(&up_key(4)).is_none());
}

#[tokio::test]
async fn a_checksum_mismatch_discards_the_temporary_and_deletes_the_object() {
    let fake = FakeS3::default();
    let harness = transfer_harness(&fake, fast_settings()).await;
    let path = harness.path("mismatch.bin");
    fs::write(&path, b"previous").unwrap();
    fake.store(&up_key(5), &pattern(100_000, 5));
    let id = start_import(
        &harness,
        StartImportRequest {
            expected_sha256: sha256_hex(b"something else"),
            ..import_request(&path, &up_key(5), false)
        },
    )
    .await
    .unwrap();
    let state = wait_terminal(&harness, &id).await;
    assert_eq!(state.phase(), TransferPhase::Failed);
    let error = state.error.unwrap();
    assert_eq!(error.code, "failed_precondition");
    assert!(
        error.message.starts_with("checksum_mismatch: "),
        "{}",
        error.message
    );
    assert_eq!(fs::read(&path).unwrap(), b"previous", "the old file stays");
    assert_eq!(temp_files(&harness.root), 0);
    assert_eq!(fake.calls_of(HttpMethod::Delete).len(), 1);
}

#[tokio::test]
async fn a_one_shot_import_of_a_missing_object_fails_not_found_without_polling() {
    let fake = FakeS3::default();
    let harness = transfer_harness(&fake, fast_settings()).await;
    let path = harness.path("absent.bin");
    let id = start_import(&harness, import_request(&path, &up_key(6), false))
        .await
        .unwrap();
    let state = wait_terminal(&harness, &id).await;
    let error = state.error.unwrap();
    assert_eq!(error.code, "not_found");
    assert!(
        error.message.starts_with("no_object: "),
        "{}",
        error.message
    );
    assert_eq!(fake.calls_of(HttpMethod::Get).len(), 1);
    assert!(fake.calls_of(HttpMethod::Delete).is_empty());
}

#[tokio::test]
async fn suspend_requeues_a_running_import_and_resume_completes_it() {
    let fake = FakeS3::default();
    let harness = transfer_harness(&fake, fast_settings()).await;
    let path = harness.path("suspended.bin");
    let bytes = pattern(512 * 1024, 7);
    fake.store(&up_key(7), &bytes);
    fake.stall_gets(true);
    let id = start_import(&harness, import_request(&path, &up_key(7), true))
        .await
        .unwrap();
    let root = harness.root.clone();
    wait_until("the import's temporary", || temp_files(&root) == 1).await;
    let running = get_transfer(&harness, &id).await.unwrap();
    assert_eq!(running.phase(), TransferPhase::Running);
    let mut watch = harness
        .files
        .clone()
        .watch_transfer(authenticated(WatchTransferRequest {
            transfer_id: id.clone(),
        }))
        .await
        .unwrap()
        .into_inner();
    let (status, reply) = harness.post(Hook::Suspend, None).await;
    assert_eq!(status, StatusCode::OK, "/suspend always answers 200");
    assert_eq!(reply.phase, "suspending");
    let closed = loop {
        match tokio::time::timeout(BUDGET, watch.message()).await.unwrap() {
            Ok(Some(_)) => {}
            Ok(None) => panic!("the watch ended without the suspend status"),
            Err(status) => break status,
        }
    };
    assert_eq!(closed.code(), Code::Unavailable);
    assert_eq!(closed.message(), "suspending");
    let requeued = get_transfer(&harness, &id).await.unwrap();
    assert_eq!(requeued.phase(), TransferPhase::Waiting, "{requeued:?}");
    assert_eq!(
        temp_files(&harness.root),
        0,
        "the temporary went with the sink"
    );
    assert!(!fs::exists(&path).unwrap());
    let gets_while_suspended = fake.calls_of(HttpMethod::Get).len();
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert_eq!(
        fake.calls_of(HttpMethod::Get).len(),
        gets_while_suspended,
        "nothing is issued while suspended"
    );
    fake.stall_gets(false);
    harness.clock.jump(Duration::from_secs(30));
    let (status, _) = harness.post(Hook::Resume, None).await;
    assert_eq!(status, StatusCode::OK);
    let state = wait_terminal(&harness, &id).await;
    assert_eq!(state.phase(), TransferPhase::Done, "{state:?}");
    assert_eq!(state.sha256, sha256_hex(&bytes));
    assert_eq!(fs::read(&path).unwrap(), bytes);
    assert!(fake.calls_of(HttpMethod::Get).len() > gets_while_suspended);
}

#[tokio::test]
async fn an_export_puts_exactly_the_file_with_its_length_and_octet_stream() {
    let fake = FakeS3::default();
    let harness = transfer_harness(&fake, fast_settings()).await;
    let path = harness.path("export.bin");
    let bytes = pattern(3 * MIB + 17, 8);
    fs::write(&path, &bytes).unwrap();
    let id = start_export(&harness, export_request(&path, &down_key(8)))
        .await
        .unwrap();
    let state = wait_terminal(&harness, &id).await;
    assert_eq!(state.phase(), TransferPhase::Done, "{state:?}");
    assert_eq!(state.sha256, sha256_hex(&bytes));
    assert_eq!(state.bytes_done, bytes.len() as u64);
    assert_eq!(state.bytes_total, bytes.len() as u64);
    assert!(state.part_etags.is_empty());
    assert_eq!(state.entry.unwrap().size, bytes.len() as u64);
    assert_eq!(fake.object(&down_key(8)).unwrap(), bytes);
    let puts = fake.calls_of(HttpMethod::Put);
    assert_eq!(puts.len(), 1);
    assert_eq!(puts[0].content_length, Some(bytes.len() as u64));
    assert_eq!(puts[0].content_type, Some(OCTET_STREAM));
}

#[tokio::test]
async fn a_multipart_export_sends_every_part_in_order_and_returns_their_etags() {
    let fake = FakeS3::default();
    let harness = transfer_harness(&fake, fast_settings()).await;
    let path = harness.path("parts.bin");
    let part_size = 5 * MIB;
    let bytes = pattern(2 * part_size + 2 * MIB + 5, 9);
    fs::write(&path, &bytes).unwrap();
    let id = start_export(
        &harness,
        multipart_request(&path, &down_key(9), part_size as u64, 3),
    )
    .await
    .unwrap();
    let state = wait_terminal(&harness, &id).await;
    assert_eq!(state.phase(), TransferPhase::Done, "{state:?}");
    assert_eq!(
        state.part_etags,
        vec!["\"etag-1\"", "\"etag-2\"", "\"etag-3\""]
    );
    assert_eq!(state.sha256, sha256_hex(&bytes));
    let lengths: Vec<Option<u64>> = fake
        .calls_of(HttpMethod::Put)
        .iter()
        .map(|call| call.content_length)
        .collect();
    assert_eq!(
        lengths,
        vec![
            Some(part_size as u64),
            Some(part_size as u64),
            Some((bytes.len() - 2 * part_size) as u64)
        ]
    );
    let joined: Vec<u8> = (1..=3)
        .flat_map(|number| fake.part(number).unwrap())
        .collect();
    assert_eq!(joined, bytes);
    let changed = start_export(
        &harness,
        multipart_request(&path, &down_key(10), part_size as u64, 2),
    )
    .await
    .unwrap_err();
    assert_eq!(
        changed.code(),
        Code::FailedPrecondition,
        "a plan for another size"
    );
    assert!(changed.message().starts_with("file_changed"));
}

#[tokio::test]
async fn an_export_aborts_when_the_file_shrinks_mid_read() {
    let fake = FakeS3::default();
    let harness = transfer_harness(&fake, fast_settings()).await;
    let path = harness.path("shrinking.bin");
    fs::write(&path, pattern(16 * MIB, 11)).unwrap();
    let truncate = path.clone();
    fake.on_first_put_chunk(Arc::new(move || {
        fs::OpenOptions::new()
            .write(true)
            .open(&truncate)
            .unwrap()
            .set_len(2 * MIB as u64)
            .unwrap();
    }));
    let id = start_export(&harness, export_request(&path, &down_key(11)))
        .await
        .unwrap();
    let state = wait_terminal(&harness, &id).await;
    assert_eq!(state.phase(), TransferPhase::Failed, "{state:?}");
    let error = state.error.unwrap();
    assert_eq!(error.code, "failed_precondition");
    assert!(
        error.message.starts_with("file_shrank: "),
        "{}",
        error.message
    );
    assert!(fake.object(&down_key(11)).is_none(), "no object was stored");
}

#[tokio::test]
async fn the_seventeenth_active_transfer_is_resource_exhausted_and_cancel_frees_a_slot() {
    let fake = FakeS3::default();
    let harness = transfer_harness(&fake, barrier_settings()).await;
    let mut ids = Vec::new();
    for seed in 0..16u32 {
        let path = harness.path(&format!("slot-{seed}.bin"));
        ids.push(
            start_import(&harness, import_request(&path, &up_key(100 + seed), true))
                .await
                .unwrap(),
        );
    }
    let extra = harness.path("slot-extra.bin");
    let refused = start_import(&harness, import_request(&extra, &up_key(200), true))
        .await
        .unwrap_err();
    assert_eq!(refused.code(), Code::ResourceExhausted);
    cancel_transfer(&harness, &ids[0]).await.unwrap();
    let cancelled = get_transfer(&harness, &ids[0]).await.unwrap();
    assert_eq!(cancelled.phase(), TransferPhase::Cancelled);
    assert_eq!(cancelled.error.unwrap().code, "cancelled");
    cancel_transfer(&harness, &ids[0])
        .await
        .expect("cancel is idempotent");
    assert!(
        start_import(&harness, import_request(&extra, &up_key(200), true))
            .await
            .is_ok()
    );
    assert_eq!(
        get_transfer(&harness, "").await.unwrap_err().code(),
        Code::NotFound,
        "the SDK's capability probe"
    );
    assert_eq!(
        get_transfer(&harness, &token(999))
            .await
            .unwrap_err()
            .code(),
        Code::NotFound
    );
    assert_eq!(
        cancel_transfer(&harness, &token(999))
            .await
            .unwrap_err()
            .code(),
        Code::NotFound
    );
    for id in &ids[1..] {
        cancel_transfer(&harness, id).await.unwrap();
    }
}

/// Every URL-policy and request-rule refusal is `INVALID_ARGUMENT` with a
/// value-free message, and the fake records no request at all: nothing
/// was dialled.
#[tokio::test]
async fn policy_refusals_are_invalid_argument_and_open_no_connection() {
    let fake = FakeS3::default();
    let harness = transfer_harness(&fake, fast_settings()).await;
    let path = harness.path("policy.bin");
    let key = up_key(12);
    let with_url = |url: String| StartImportRequest {
        get: Some(presigned(url)),
        delete: None,
        ..import_request(&path, &key, true)
    };
    let tail = format!("/{key}?{}", query());
    let mut cases = vec![
        ("imds", with_url(format!("https://169.254.169.254{tail}"))),
        ("ip literal", with_url(format!("https://10.0.0.5{tail}"))),
        ("ipv6 literal", with_url(format!("https://[::1]{tail}"))),
        ("http", with_url(format!("http://{}{tail}", host()))),
        (
            "global host",
            with_url(format!("https://{BUCKET}.s3.amazonaws.com{tail}")),
        ),
        (
            "path style",
            with_url(format!("https://s3.{REGION}.amazonaws.com/{BUCKET}{tail}")),
        ),
        (
            "accelerate",
            with_url(format!(
                "https://{BUCKET}.s3-accelerate.amazonaws.com{tail}"
            )),
        ),
        (
            "other bucket",
            with_url(format!(
                "https://other-bucket.s3.{REGION}.amazonaws.com{tail}"
            )),
        ),
        (
            "other region",
            with_url(format!("https://{BUCKET}.s3.eu-west-1.amazonaws.com{tail}")),
        ),
        ("other key", with_url(url_for(&up_key(13)))),
        (
            "userinfo",
            with_url(format!("https://user:pw@{}{tail}", host())),
        ),
        (
            "port 8443",
            with_url(format!("https://{}:8443{tail}", host())),
        ),
        (
            "sigv2",
            with_url(format!(
                "https://{}/{key}?AWSAccessKeyId=AKIDEXAMPLE&Signature=abc&Expires=1",
                host()
            )),
        ),
        (
            "no signature",
            with_url(url_for(&key).replace(&format!("&X-Amz-Signature={SIGNATURE}"), "")),
        ),
        (
            "mixed get and delete",
            StartImportRequest {
                delete: Some(presigned(format!(
                    "https://{BUCKET}.s3.dualstack.{REGION}.amazonaws.com{tail}"
                ))),
                ..import_request(&path, &key, true)
            },
        ),
        (
            "extra header",
            StartImportRequest {
                get: Some(PresignedRequest {
                    url: url_for(&key),
                    headers: HashMap::from([("x-amz-meta-a".to_owned(), "b".to_owned())]),
                }),
                ..import_request(&path, &key, true)
            },
        ),
        (
            "wrong direction",
            import_request(&path, &down_key(12), true),
        ),
        (
            "another sandbox",
            import_request(&path, &format!("{PREFIX}/mvm-other/up/{}", token(12)), true),
        ),
        (
            "dotted bucket",
            StartImportRequest {
                object: Some(S3Object {
                    bucket: "amzn.s3.demo".to_owned(),
                    ..object(&key)
                }),
                get: Some(presigned(format!(
                    "https://amzn.s3.demo.s3.{REGION}.amazonaws.com{tail}"
                ))),
                ..import_request(&path, &key, true)
            },
        ),
        (
            "expired",
            StartImportRequest {
                expires_at_unix_ms: now_ms() - 1,
                ..import_request(&path, &key, true)
            },
        ),
        (
            "bad sha256",
            StartImportRequest {
                expected_sha256: "ABC".to_owned(),
                ..import_request(&path, &key, true)
            },
        ),
        (
            "metadata key with a space",
            StartImportRequest {
                metadata: HashMap::from([("a b".to_owned(), "x".to_owned())]),
                ..import_request(&path, &key, true)
            },
        ),
    ];
    for (label, request) in cases.drain(..) {
        let status = start_import(&harness, request).await.unwrap_err();
        assert_eq!(status.code(), Code::InvalidArgument, "{label}: {status:?}");
        for leaked in ["amazonaws", "169.254", "http", BUCKET, SIGNATURE] {
            assert!(
                !status.message().contains(leaked),
                "{label}: {}",
                status.message()
            );
        }
    }
    let export_path = harness.path("export-policy.bin");
    fs::write(&export_path, b"x").unwrap();
    let mut mixed = multipart_request(&export_path, &down_key(12), 5 * MIB as u64, 1);
    if let Some(start_export_request::Target::Multipart(multipart)) = &mut mixed.target {
        multipart.parts[0].url = multipart.parts[0]
            .url
            .replace("partNumber=1", "partNumber=2");
    }
    let status = start_export(&harness, mixed).await.unwrap_err();
    assert_eq!(status.code(), Code::InvalidArgument);
    let status = start_export(
        &harness,
        StartExportRequest {
            target: Some(start_export_request::Target::Put(presigned(format!(
                "https://169.254.169.254/{}?{}",
                down_key(12),
                query()
            )))),
            ..export_request(&export_path, &down_key(12))
        },
    )
    .await
    .unwrap_err();
    assert_eq!(status.code(), Code::InvalidArgument);
    assert!(
        fake.calls().is_empty(),
        "no request left rayd: {:?}",
        fake.calls()
    );
}

#[tokio::test]
async fn an_agent_without_the_transfer_client_answers_unimplemented() {
    capture();
    let harness = harness_with(Options::default()).await;
    let path = harness.path("none.bin");
    assert_eq!(
        get_transfer(&harness, "").await.unwrap_err().code(),
        Code::Unimplemented
    );
    assert_eq!(
        start_import(&harness, import_request(&path, &up_key(15), true))
            .await
            .unwrap_err()
            .code(),
        Code::Unimplemented
    );
}

/// The barrier: a `Read` right after the object appears returns the new
/// content although the ticket's own poll would not look again for a
/// minute.
#[tokio::test]
async fn a_read_right_after_the_object_appears_returns_the_new_content() {
    let fake = FakeS3::default();
    let harness = transfer_harness(&fake, barrier_settings()).await;
    let path = harness.path("barrier-read.bin");
    fs::write(&path, b"old content").unwrap();
    let id = start_import(&harness, import_request(&path, &up_key(16), true))
        .await
        .unwrap();
    wait_until("the first probe", || {
        !fake.calls_of(HttpMethod::Get).is_empty()
    })
    .await;
    let bytes = pattern(200_000, 16);
    fake.store(&up_key(16), &bytes);
    let started = Instant::now();
    assert_eq!(read_file(&harness, &path).await.unwrap(), bytes);
    assert!(started.elapsed() < Duration::from_secs(10));
    assert_eq!(
        get_transfer(&harness, &id).await.unwrap().phase(),
        TransferPhase::Done
    );
}

#[tokio::test]
async fn stat_and_list_dir_wait_for_an_import_they_touch() {
    let fake = FakeS3::default();
    let harness = transfer_harness(&fake, barrier_settings()).await;
    let dir = harness.path("landing");
    fs::create_dir(&dir).unwrap();
    let stat_path = format!("{dir}/stat.bin");
    let listed_path = format!("{dir}/listed.bin");
    start_import(&harness, import_request(&stat_path, &up_key(17), true))
        .await
        .unwrap();
    start_import(&harness, import_request(&listed_path, &up_key(18), true))
        .await
        .unwrap();
    wait_until("both first probes", || {
        fake.calls_of(HttpMethod::Get).len() >= 2
    })
    .await;
    fake.store(&up_key(17), &pattern(1234, 17));
    assert_eq!(stat(&harness, &stat_path).await.size, 1234);
    fake.store(&up_key(18), &pattern(4321, 18));
    let entries = harness
        .files
        .clone()
        .list_dir(authenticated(ListDirRequest {
            path: harness.root.clone(),
            depth: 2,
            user: None,
        }))
        .await
        .unwrap()
        .into_inner()
        .entries;
    let listed = entries
        .iter()
        .find(|entry| entry.path == listed_path)
        .expect("the landed file is listed");
    assert_eq!(listed.size, 4321);
}

#[tokio::test]
async fn a_new_process_waits_for_an_armed_import() {
    let fake = FakeS3::default();
    let harness = transfer_harness(&fake, barrier_settings()).await;
    let path = harness.path("barrier-start.txt");
    start_import(&harness, import_request(&path, &up_key(19), true))
        .await
        .unwrap();
    wait_until("the first probe", || {
        !fake.calls_of(HttpMethod::Get).is_empty()
    })
    .await;
    fake.store(&up_key(19), b"landed before the spawn\n");
    let stdout = run_stdout(&harness, &format!("cat '{path}'")).await;
    assert_eq!(stdout, b"landed before the spawn\n");
}

/// With nothing armed the barrier is one atomic load: no probe leaves
/// `rayd` for a `Read`, `Stat`, `ListDir` or `Start`, not even after a
/// finished import; an armed ticket elsewhere is not probed by a `Read`
/// of another path.
#[tokio::test]
async fn the_barrier_issues_nothing_without_a_concerned_armed_ticket() {
    let fake = FakeS3::default();
    let harness = transfer_harness(&fake, barrier_settings()).await;
    let path = harness.path("plain.txt");
    fs::write(&path, b"plain").unwrap();
    assert_eq!(read_file(&harness, &path).await.unwrap(), b"plain");
    stat(&harness, &path).await;
    run_stdout(&harness, "true").await;
    assert!(fake.calls().is_empty(), "{:?}", fake.calls());
    let done_path = harness.path("done.bin");
    fake.store(&up_key(20), b"done");
    let id = start_import(&harness, import_request(&done_path, &up_key(20), false))
        .await
        .unwrap();
    assert_eq!(
        wait_terminal(&harness, &id).await.phase(),
        TransferPhase::Done
    );
    let after_import = fake.calls().len();
    assert_eq!(read_file(&harness, &done_path).await.unwrap(), b"done");
    run_stdout(&harness, "true").await;
    assert_eq!(fake.calls().len(), after_import);
    let elsewhere = harness.path("elsewhere.bin");
    start_import(&harness, import_request(&elsewhere, &up_key(21), true))
        .await
        .unwrap();
    wait_until("the armed probe", || fake.calls().len() == after_import + 1).await;
    assert_eq!(read_file(&harness, &path).await.unwrap(), b"plain");
    assert_eq!(
        fake.calls().len(),
        after_import + 1,
        "a Read of another path probes nothing"
    );
}

/// gzip responses only for calls that opt in with `rayito-compress: gzip`;
/// gzip requests are always accepted.
#[tokio::test]
async fn filesystem_responses_are_gzip_only_with_the_opt_in_header() {
    capture();
    let harness = harness_with(Options::default()).await;
    let path = harness.path("compressible.txt");
    let text = "rayito gzip ".repeat(50_000).into_bytes();
    let mut compressing = harness
        .files
        .clone()
        .send_compressed(CompressionEncoding::Gzip)
        .accept_compressed(CompressionEncoding::Gzip);
    compressing
        .write(authenticated(tokio_stream::iter(vec![WriteRequest {
            path: Some(path.clone()),
            user: None,
            mode: None,
            chunk: text.clone(),
            metadata: HashMap::new(),
        }])))
        .await
        .expect("a gzip request is accepted");
    assert_eq!(fs::read(&path).unwrap(), text);
    let plain = compressing
        .read(authenticated(ReadRequest {
            path: path.clone(),
            user: None,
        }))
        .await
        .unwrap();
    let plain_encoding = plain
        .metadata()
        .get("grpc-encoding")
        .map(|value| value.to_str().unwrap().to_owned());
    assert!(
        plain_encoding.is_none() || plain_encoding.as_deref() == Some("identity"),
        "{plain_encoding:?}"
    );
    let mut opted = authenticated(ReadRequest {
        path: path.clone(),
        user: None,
    });
    opted
        .metadata_mut()
        .insert("rayito-compress", MetadataValue::from_static("gzip"));
    let response = compressing.read(opted).await.unwrap();
    assert_eq!(
        response
            .metadata()
            .get("grpc-encoding")
            .map(|value| value.to_str().unwrap()),
        Some("gzip")
    );
    let mut stream = response.into_inner();
    let mut received = Vec::new();
    while let Some(message) = stream.message().await.unwrap() {
        received.extend_from_slice(&message.chunk);
    }
    assert_eq!(received, text);
}

fn write_request(
    path: Option<&str>,
    chunk: &[u8],
    metadata: HashMap<String, String>,
) -> WriteRequest {
    WriteRequest {
        path: path.map(str::to_owned),
        user: None,
        mode: None,
        chunk: chunk.to_vec(),
        metadata,
    }
}

/// One `Write` stream; the entries it committed, in order.
async fn write(
    harness: &Harness,
    requests: Vec<WriteRequest>,
) -> Result<Vec<EntryInfo>, tonic::Status> {
    harness
        .files
        .clone()
        .write(authenticated(tokio_stream::iter(requests)))
        .await
        .map(|response| response.into_inner().entries)
}

async fn list_dir(harness: &Harness, path: &str) -> Vec<EntryInfo> {
    harness
        .files
        .clone()
        .list_dir(authenticated(ListDirRequest {
            path: path.to_owned(),
            depth: 1,
            user: None,
        }))
        .await
        .unwrap()
        .into_inner()
        .entries
}

/// Design D17 on the `Write` path: the first message's metadata lands on
/// the file with its keys lowercased, `Stat` and `ListDir` read it back,
/// an overwrite without metadata clears it, and a key that is not an HTTP
/// token or metadata on a message without `path` is `INVALID_ARGUMENT`
/// with nothing committed and no key echoed. The `Write` log line counts
/// the keys and never carries a value.
#[tokio::test]
async fn a_write_stores_metadata_that_stat_and_list_dir_read_back_until_an_overwrite() {
    let logs = capture();
    let harness = harness_with(Options::default()).await;
    let path = harness.path("tagged.txt");
    let written = write(
        &harness,
        vec![write_request(Some(&path), b"v1", owner_metadata())],
    )
    .await
    .unwrap();
    assert_eq!(written[0].metadata, lowercase_owner());
    assert_eq!(stat(&harness, &path).await.metadata, lowercase_owner());
    let listed = list_dir(&harness, &harness.root).await;
    let tagged = listed
        .iter()
        .find(|entry| entry.path == path)
        .expect("the tagged file is listed");
    assert_eq!(tagged.metadata, lowercase_owner());
    let overwritten = write(
        &harness,
        vec![write_request(Some(&path), b"v2", HashMap::new())],
    )
    .await
    .unwrap();
    assert!(overwritten[0].metadata.is_empty());
    assert!(stat(&harness, &path).await.metadata.is_empty());
    assert_eq!(fs::read(&path).unwrap(), b"v2");
    let refused = harness.path("refused.txt");
    let spaced = write(
        &harness,
        vec![write_request(
            Some(&refused),
            b"x",
            HashMap::from([("a b".to_owned(), "x".to_owned())]),
        )],
    )
    .await
    .unwrap_err();
    assert_eq!(spaced.code(), Code::InvalidArgument);
    assert!(!spaced.message().contains("a b"), "{}", spaced.message());
    let late = write(
        &harness,
        vec![
            write_request(Some(&refused), b"x", HashMap::new()),
            write_request(None, b"y", owner_metadata()),
        ],
    )
    .await
    .unwrap_err();
    assert_eq!(late.code(), Code::InvalidArgument);
    assert!(!fs::exists(&refused).unwrap());
    assert_eq!(temp_files(&harness.root), 0);
    let text = logs.text();
    assert!(
        text.contains("\"metadata_keys\":1"),
        "the write line counts keys"
    );
    assert!(
        !text.contains("alice"),
        "a log line carries a metadata value"
    );
}

/// Design D21: a full import and export leave no URL, signature,
/// credential scope, bucket, key, path or file name in any log line.
#[tokio::test]
async fn transfer_logs_never_carry_signatures_or_names() {
    let logs = capture();
    let fake = FakeS3::default();
    let harness = transfer_harness(&fake, fast_settings()).await;
    let name = "log-hygiene-7f3a91.bin";
    let path = harness.path(name);
    let bytes = pattern(70_000, 22);
    fake.store(&up_key(22), &bytes);
    let import = start_import(
        &harness,
        StartImportRequest {
            metadata: owner_metadata(),
            ..import_request(&path, &up_key(22), true)
        },
    )
    .await
    .unwrap();
    assert_eq!(
        wait_terminal(&harness, &import).await.phase(),
        TransferPhase::Done
    );
    let export = start_export(&harness, export_request(&path, &down_key(22)))
        .await
        .unwrap();
    assert_eq!(
        wait_terminal(&harness, &export).await.phase(),
        TransferPhase::Done
    );
    let refused = start_import(
        &harness,
        StartImportRequest {
            get: Some(presigned(format!(
                "https://169.254.169.254/{}?{}",
                up_key(23),
                query()
            ))),
            ..import_request(&path, &up_key(23), true)
        },
    )
    .await;
    assert!(refused.is_err());
    let text = logs.text();
    assert!(text.contains(&import), "the capture saw the import");
    assert!(text.contains(&export), "the capture saw the export");
    assert!(text.contains("transfer finished"));
    assert!(text.contains("\"phase\":\"cleanup\""));
    for leaked in [
        "X-Amz-Signature",
        "X-Amz-Credential",
        SIGNATURE,
        "AKIDEXAMPLE",
        BUCKET,
        "amazonaws",
        "169.254",
        &token(22),
        &token(23),
        name,
        path.as_str(),
        "alice",
    ] {
        assert!(!text.contains(leaked), "a log line carries {leaked}");
    }
}

#[tokio::test]
async fn a_cancelled_waiting_import_stops_polling_and_writes_nothing() {
    let fake = FakeS3::default();
    let harness = transfer_harness(&fake, fast_settings()).await;
    let path = harness.path("cancelled.bin");
    let id = start_import(&harness, import_request(&path, &up_key(24), true))
        .await
        .unwrap();
    wait_until("a probe", || !fake.calls_of(HttpMethod::Get).is_empty()).await;
    cancel_transfer(&harness, &id).await.unwrap();
    let state = wait_terminal(&harness, &id).await;
    assert_eq!(state.phase(), TransferPhase::Cancelled);
    let probes = fake.calls_of(HttpMethod::Get).len();
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert_eq!(
        fake.calls_of(HttpMethod::Get).len(),
        probes,
        "the poller stopped"
    );
    fake.store(&up_key(24), b"too late");
    tokio::time::sleep(Duration::from_millis(200)).await;
    assert!(!fs::exists(&path).unwrap());
}
