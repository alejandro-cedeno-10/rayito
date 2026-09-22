//! The `ObjectStore` port over `aws-sdk-s3` (design D2/D5/D7): one
//! credentials provider built at startup (`IMDSv2` as root by default, the
//! SDK's default chain only for `make dev-run`), one client per region
//! built on first use, `BehaviorVersion::latest()`, the SDK's default
//! retries, no custom endpoint, no checksum, encryption, storage-class or
//! ACL parameters (`AWS_API_NOTES.md` §17 is the contract). Multipart
//! uploads send sequential 8 MiB parts and are aborted on every error,
//! interruption and drop path within a 5 s budget. Log lines carry the S3
//! error code, the request id, counts and sizes: never a bucket, a key or
//! a credential.

use std::collections::HashMap;
use std::sync::{Mutex, PoisonError};
use std::time::Duration;

use aws_config::imds::credentials::ImdsCredentialsProvider;
use aws_sdk_s3::config::http::HttpResponse;
use aws_sdk_s3::config::retry::RetryConfig;
use aws_sdk_s3::config::{
    BehaviorVersion, ProvideCredentials, Region, RequestChecksumCalculation,
    ResponseChecksumValidation, SharedCredentialsProvider,
};
use aws_sdk_s3::error::{ProvideErrorMetadata, SdkError};
use aws_sdk_s3::operation::RequestId;
use aws_sdk_s3::primitives::ByteStream;
use aws_sdk_s3::types::{CompletedMultipartUpload, CompletedPart};
use bytes::Bytes;
use rayd_core::persistence::{
    ObjectBody, ObjectStore, PartSource, PutSummary, StoreError, StoreErrorKind, StoreTarget,
};

/// How long an abort may take before the upload is left to the bucket's
/// lifecycle rule.
pub const ABORT_BUDGET: Duration = Duration::from_secs(5);
/// The `IMDSv2` profile the platform serves (`AWS_API_NOTES.md` §9).
pub const EXECUTION_ROLE_PROFILE: &str = "execution_role";

/// Which credentials the store resolves; `Imds` is what the image runs.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CredentialsSource {
    Imds,
    Default,
}

impl CredentialsSource {
    #[must_use]
    pub fn parse(raw: &str) -> Option<Self> {
        match raw {
            "imds" => Some(Self::Imds),
            "default" => Some(Self::Default),
            _ => None,
        }
    }

    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Imds => "imds",
            Self::Default => "default",
        }
    }
}

pub struct S3ObjectStore {
    provider: SharedCredentialsProvider,
    default_region: Option<String>,
    clients: Mutex<HashMap<String, aws_sdk_s3::Client>>,
    source: CredentialsSource,
}

impl S3ObjectStore {
    /// `default_region` is the platform's `AWS_REGION`; a request may name
    /// another one.
    pub async fn new(source: CredentialsSource, default_region: Option<String>) -> Self {
        let provider = match source {
            CredentialsSource::Imds => SharedCredentialsProvider::new(
                ImdsCredentialsProvider::builder()
                    .profile(EXECUTION_ROLE_PROFILE)
                    .build(),
            ),
            CredentialsSource::Default => SharedCredentialsProvider::new(
                aws_config::default_provider::credentials::DefaultCredentialsChain::builder()
                    .build()
                    .await,
            ),
        };
        Self {
            provider,
            default_region,
            clients: Mutex::new(HashMap::new()),
            source,
        }
    }

    #[must_use]
    pub fn credentials_source(&self) -> CredentialsSource {
        self.source
    }

    fn client_for(&self, target: &StoreTarget) -> Result<aws_sdk_s3::Client, StoreError> {
        let region = target
            .region
            .clone()
            .or_else(|| self.default_region.clone())
            .ok_or_else(|| StoreError::new(StoreErrorKind::Other))?;
        let mut clients = self.clients.lock().unwrap_or_else(PoisonError::into_inner);
        Ok(clients
            .entry(region.clone())
            .or_insert_with(|| self.build_client(region))
            .clone())
    }

    fn build_client(&self, region: String) -> aws_sdk_s3::Client {
        let config = aws_sdk_s3::Config::builder()
            .behavior_version(BehaviorVersion::latest())
            .region(Region::new(region))
            .credentials_provider(self.provider.clone())
            .retry_config(RetryConfig::standard())
            .request_checksum_calculation(RequestChecksumCalculation::WhenRequired)
            .response_checksum_validation(ResponseChecksumValidation::WhenRequired)
            .build();
        aws_sdk_s3::Client::from_conf(config)
    }
}

pub struct S3Body {
    body: ByteStream,
    content_length: Option<u64>,
}

impl ObjectBody for S3Body {
    fn content_length(&self) -> Option<u64> {
        self.content_length
    }

    async fn next_chunk(&mut self) -> Result<Option<Bytes>, StoreError> {
        match self.body.next().await {
            Some(Ok(chunk)) => Ok(Some(chunk)),
            Some(Err(error)) => {
                tracing::warn!(reason = %error, "s3 body read failed");
                Err(StoreError::new(StoreErrorKind::Interrupted))
            }
            None => Ok(None),
        }
    }
}

impl ObjectStore for S3ObjectStore {
    type Body = S3Body;

    async fn probe_credentials(&self) -> Result<(), StoreError> {
        match self.provider.provide_credentials().await {
            Ok(_) => Ok(()),
            Err(error) => {
                tracing::warn!(
                    credentials = self.source.as_str(),
                    reason = %error,
                    "execution role credentials unavailable"
                );
                Err(StoreError::new(StoreErrorKind::NoCredentials))
            }
        }
    }

    async fn get(&self, target: &StoreTarget, key: &str) -> Result<S3Body, StoreError> {
        let client = self.client_for(target)?;
        let output = client
            .get_object()
            .bucket(target.bucket.as_str())
            .key(key)
            .send()
            .await
            .map_err(|error| classify("GetObject", &error))?;
        Ok(S3Body {
            content_length: output
                .content_length
                .and_then(|len| u64::try_from(len).ok()),
            body: output.body,
        })
    }

    async fn put(
        &self,
        target: &StoreTarget,
        key: &str,
        body: Bytes,
        content_type: &str,
    ) -> Result<(), StoreError> {
        let client = self.client_for(target)?;
        put_object(&client, target, key, body, content_type).await
    }

    async fn put_multipart<P: PartSource>(
        &self,
        target: &StoreTarget,
        key: &str,
        content_type: &str,
        mut parts: P,
    ) -> Result<PutSummary, StoreError> {
        let client = self.client_for(target)?;
        let first = parts.next_part().await?.unwrap_or_default();
        let Some(second) = parts.next_part().await? else {
            let bytes = first.len() as u64;
            put_object(&client, target, key, first, content_type).await?;
            return Ok(PutSummary { parts: 1, bytes });
        };
        let mut upload = MultipartUpload::create(client, target, key, content_type).await?;
        upload.send_part(first).await?;
        upload.send_part(second).await?;
        loop {
            match parts.next_part().await {
                Ok(Some(part)) => upload.send_part(part).await?,
                Ok(None) => break,
                Err(error) => {
                    upload.abort().await;
                    return Err(error);
                }
            }
        }
        upload.complete().await
    }
}

async fn put_object(
    client: &aws_sdk_s3::Client,
    target: &StoreTarget,
    key: &str,
    body: Bytes,
    content_type: &str,
) -> Result<(), StoreError> {
    let len = i64::try_from(body.len()).map_err(|_| StoreError::new(StoreErrorKind::Other))?;
    client
        .put_object()
        .bucket(target.bucket.as_str())
        .key(key)
        .body(ByteStream::from(body))
        .content_length(len)
        .content_type(content_type)
        .send()
        .await
        .map(|_| ())
        .map_err(|error| classify("PutObject", &error))
}

/// One multipart upload in flight; dropping it before `complete` aborts
/// it on a detached task so a cancelled stream never leaves parts behind.
struct MultipartUpload {
    client: aws_sdk_s3::Client,
    bucket: String,
    key: String,
    upload_id: String,
    parts: Vec<CompletedPart>,
    bytes: u64,
    armed: bool,
}

impl MultipartUpload {
    async fn create(
        client: aws_sdk_s3::Client,
        target: &StoreTarget,
        key: &str,
        content_type: &str,
    ) -> Result<Self, StoreError> {
        let output = client
            .create_multipart_upload()
            .bucket(target.bucket.as_str())
            .key(key)
            .content_type(content_type)
            .send()
            .await
            .map_err(|error| classify("CreateMultipartUpload", &error))?;
        let upload_id = output
            .upload_id
            .ok_or_else(|| StoreError::new(StoreErrorKind::Other))?;
        Ok(Self {
            client,
            bucket: target.bucket.as_str().to_owned(),
            key: key.to_owned(),
            upload_id,
            parts: Vec::new(),
            bytes: 0,
            armed: true,
        })
    }

    async fn send_part(&mut self, part: Bytes) -> Result<(), StoreError> {
        let number = i32::try_from(self.parts.len() + 1)
            .map_err(|_| StoreError::new(StoreErrorKind::Other))?;
        let len = i64::try_from(part.len()).map_err(|_| StoreError::new(StoreErrorKind::Other))?;
        let uploaded = self
            .client
            .upload_part()
            .bucket(&self.bucket)
            .key(&self.key)
            .upload_id(&self.upload_id)
            .part_number(number)
            .body(ByteStream::from(part))
            .content_length(len)
            .send()
            .await;
        let output = match uploaded {
            Ok(output) => output,
            Err(error) => {
                let classified = classify("UploadPart", &error);
                self.abort().await;
                return Err(classified);
            }
        };
        self.bytes += u64::try_from(len).unwrap_or(0);
        self.parts.push(
            CompletedPart::builder()
                .set_e_tag(output.e_tag)
                .part_number(number)
                .build(),
        );
        Ok(())
    }

    async fn complete(mut self) -> Result<PutSummary, StoreError> {
        let parts = u32::try_from(self.parts.len()).unwrap_or(u32::MAX);
        let completed = self
            .client
            .complete_multipart_upload()
            .bucket(&self.bucket)
            .key(&self.key)
            .upload_id(&self.upload_id)
            .multipart_upload(
                CompletedMultipartUpload::builder()
                    .set_parts(Some(std::mem::take(&mut self.parts)))
                    .build(),
            )
            .send()
            .await;
        match completed {
            Ok(_) => {
                self.armed = false;
                tracing::debug!(parts, bytes = self.bytes, "multipart upload completed");
                Ok(PutSummary {
                    parts,
                    bytes: self.bytes,
                })
            }
            Err(error) => {
                let classified = classify("CompleteMultipartUpload", &error);
                self.abort().await;
                Err(classified)
            }
        }
    }

    /// Best effort within `ABORT_BUDGET`; the outcome is logged, never
    /// surfaced (the caller's own error is the one that matters).
    async fn abort(&mut self) {
        if !self.armed {
            return;
        }
        self.armed = false;
        let aborted = tokio::time::timeout(
            ABORT_BUDGET,
            abort_upload(
                self.client.clone(),
                self.bucket.clone(),
                self.key.clone(),
                self.upload_id.clone(),
            ),
        )
        .await;
        match aborted {
            Ok(Ok(())) => tracing::info!(parts = self.parts.len(), "multipart upload aborted"),
            Ok(Err(error)) => tracing::warn!(
                s3_error_code = error.s3_error_code.as_deref().unwrap_or(""),
                s3_request_id = error.request_id.as_deref().unwrap_or(""),
                "multipart upload abort failed"
            ),
            Err(_) => tracing::warn!("multipart upload abort timed out"),
        }
    }
}

impl Drop for MultipartUpload {
    fn drop(&mut self) {
        if !self.armed {
            return;
        }
        self.armed = false;
        let abort = abort_upload(
            self.client.clone(),
            self.bucket.clone(),
            self.key.clone(),
            self.upload_id.clone(),
        );
        if let Ok(handle) = tokio::runtime::Handle::try_current() {
            handle.spawn(async move {
                if tokio::time::timeout(ABORT_BUDGET, abort).await.is_err() {
                    tracing::warn!("multipart upload abort timed out after drop");
                }
            });
        }
    }
}

async fn abort_upload(
    client: aws_sdk_s3::Client,
    bucket: String,
    key: String,
    upload_id: String,
) -> Result<(), StoreError> {
    client
        .abort_multipart_upload()
        .bucket(bucket)
        .key(key)
        .upload_id(upload_id)
        .send()
        .await
        .map(|_| ())
        .map_err(|error| classify("AbortMultipartUpload", &error))
}

/// The D8 classification of an SDK error, with the log line D13 asks for.
fn classify<E>(operation: &'static str, error: &SdkError<E, HttpResponse>) -> StoreError
where
    E: ProvideErrorMetadata + std::error::Error,
    SdkError<E, HttpResponse>: RequestId,
{
    let code = error.code().map(str::to_owned);
    let request_id = error.request_id().map(str::to_owned);
    let status = match error {
        SdkError::ServiceError(context) => Some(context.raw().status().as_u16()),
        SdkError::ResponseError(context) => Some(context.raw().status().as_u16()),
        _ => None,
    };
    let kind = kind_for(code.as_deref(), status);
    tracing::warn!(
        operation,
        outcome = ?kind,
        s3_error_code = code.as_deref().unwrap_or(""),
        s3_request_id = request_id.as_deref().unwrap_or(""),
        http_status = status.unwrap_or(0),
        "s3 request failed"
    );
    StoreError {
        kind,
        s3_error_code: code,
        request_id,
    }
}

fn kind_for(code: Option<&str>, status: Option<u16>) -> StoreErrorKind {
    match code {
        Some("NoSuchKey" | "NoSuchBucket" | "NotFound") => StoreErrorKind::NotFound,
        Some(
            "AccessDenied" | "AllAccessDisabled" | "InvalidAccessKeyId" | "SignatureDoesNotMatch",
        ) => StoreErrorKind::AccessDenied,
        Some("ExpiredToken" | "ExpiredTokenException" | "InvalidToken") => {
            StoreErrorKind::CredentialsRejected
        }
        Some(
            "PermanentRedirect"
            | "AuthorizationHeaderMalformed"
            | "IllegalLocationConstraintException",
        ) => StoreErrorKind::WrongRegion,
        Some(_) => StoreErrorKind::Other,
        None => match status {
            Some(404) => StoreErrorKind::NotFound,
            Some(403) => StoreErrorKind::AccessDenied,
            Some(301) => StoreErrorKind::WrongRegion,
            _ => StoreErrorKind::Other,
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn s3_error_codes_map_to_the_d8_kinds() {
        assert_eq!(
            kind_for(Some("NoSuchKey"), Some(404)),
            StoreErrorKind::NotFound
        );
        assert_eq!(
            kind_for(Some("AccessDenied"), Some(403)),
            StoreErrorKind::AccessDenied
        );
        assert_eq!(
            kind_for(Some("ExpiredToken"), Some(400)),
            StoreErrorKind::CredentialsRejected
        );
        assert_eq!(
            kind_for(Some("PermanentRedirect"), Some(301)),
            StoreErrorKind::WrongRegion
        );
        assert_eq!(
            kind_for(Some("InternalError"), Some(500)),
            StoreErrorKind::Other
        );
        assert_eq!(kind_for(None, Some(404)), StoreErrorKind::NotFound);
        assert_eq!(kind_for(None, Some(403)), StoreErrorKind::AccessDenied);
        assert_eq!(kind_for(None, Some(301)), StoreErrorKind::WrongRegion);
        assert_eq!(kind_for(None, None), StoreErrorKind::Other);
    }

    #[test]
    fn credentials_source_parses_the_flag_values() {
        assert_eq!(
            CredentialsSource::parse("imds"),
            Some(CredentialsSource::Imds)
        );
        assert_eq!(
            CredentialsSource::parse("default"),
            Some(CredentialsSource::Default)
        );
        assert_eq!(CredentialsSource::parse("env"), None);
        assert_eq!(CredentialsSource::Imds.as_str(), "imds");
    }
}
