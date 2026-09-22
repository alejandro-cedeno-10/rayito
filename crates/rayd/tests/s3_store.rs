//! `S3ObjectStore` against a real bucket with the developer's credential
//! chain (`--persistence-credentials default` semantics): a 20 MiB
//! multipart round trip, a single-part `PutObject`, `NoSuchKey` as
//! `NotFound` and a cleanup with the developer's own `DeleteObject`. Run
//! by hand: `RAYITO_PERSIST_BUCKET=<bucket> AWS_REGION=us-east-1 cargo
//! test -p rayd --test s3_store -- --ignored`.
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::cast_precision_loss,
    clippy::unused_async_trait_impl
)]

use std::time::Instant;

use bytes::Bytes;
use rayd::adapters::{CredentialsSource, S3ObjectStore};
use rayd_core::persistence::{
    BucketName, ObjectBody, ObjectStore, PART_BYTES, PartSource, StoreError, StoreErrorKind,
    StoreTarget,
};

const PREFIX_ENV: &str = "RAYITO_PERSIST_PREFIX";
const BUCKET_ENV: &str = "RAYITO_PERSIST_BUCKET";
const ROUND_TRIP_BYTES: usize = 20 * 1024 * 1024;

struct VecParts {
    parts: Vec<Bytes>,
}

impl PartSource for VecParts {
    async fn next_part(&mut self) -> Result<Option<Bytes>, StoreError> {
        if self.parts.is_empty() {
            return Ok(None);
        }
        Ok(Some(self.parts.remove(0)))
    }
}

fn target() -> Option<(StoreTarget, String)> {
    let bucket = std::env::var(BUCKET_ENV).ok()?;
    let prefix = std::env::var(PREFIX_ENV).unwrap_or_else(|_| "rayito-e2e".to_owned());
    let region = std::env::var("AWS_REGION").ok();
    Some((
        StoreTarget {
            bucket: BucketName::parse(&bucket).unwrap(),
            region,
        },
        format!("{prefix}/s3-store-test-{}", std::process::id()),
    ))
}

async fn read_all<B: ObjectBody>(mut body: B) -> Vec<u8> {
    let mut out = Vec::new();
    while let Some(chunk) = body.next_chunk().await.unwrap() {
        out.extend_from_slice(&chunk);
    }
    out
}

async fn delete(target: &StoreTarget, key: &str) {
    let config = aws_config::defaults(aws_config::BehaviorVersion::latest())
        .load()
        .await;
    let client = aws_sdk_s3::Client::new(&config);
    client
        .delete_object()
        .bucket(target.bucket.as_str())
        .key(key)
        .send()
        .await
        .unwrap();
}

#[tokio::test]
#[ignore = "needs RAYITO_PERSIST_BUCKET and real credentials"]
async fn twenty_mib_round_trip_and_not_found() {
    let Some((target, prefix)) = target() else {
        eprintln!("{BUCKET_ENV} not set; skipping");
        return;
    };
    let store = S3ObjectStore::new(CredentialsSource::Default, target.region.clone()).await;
    store.probe_credentials().await.unwrap();

    let payload: Vec<u8> = (0..ROUND_TRIP_BYTES)
        .map(|index| u8::try_from(index % 251).unwrap())
        .collect();
    let parts: Vec<Bytes> = payload
        .chunks(PART_BYTES)
        .map(Bytes::copy_from_slice)
        .collect();
    let archive_key = format!("{prefix}/home.tar.gz");
    let started = Instant::now();
    let summary = store
        .put_multipart(
            &target,
            &archive_key,
            "application/gzip",
            VecParts { parts },
        )
        .await
        .unwrap();
    let upload_secs = started.elapsed().as_secs_f64();
    assert_eq!(summary.parts, 3);
    assert_eq!(summary.bytes, ROUND_TRIP_BYTES as u64);

    let started = Instant::now();
    let body = store.get(&target, &archive_key).await.unwrap();
    assert_eq!(body.content_length(), Some(ROUND_TRIP_BYTES as u64));
    let downloaded = read_all(body).await;
    let download_secs = started.elapsed().as_secs_f64();
    assert_eq!(downloaded, payload);
    eprintln!(
        "20 MiB multipart: upload {upload_secs:.2} s ({:.2} MB/s), download {download_secs:.2} s ({:.2} MB/s)",
        ROUND_TRIP_BYTES as f64 / 1e6 / upload_secs,
        ROUND_TRIP_BYTES as f64 / 1e6 / download_secs
    );

    let manifest_key = format!("{prefix}/manifest.json");
    store
        .put(
            &target,
            &manifest_key,
            Bytes::from_static(b"{\"version\":1}"),
            "application/json",
        )
        .await
        .unwrap();
    let small = read_all(store.get(&target, &manifest_key).await.unwrap()).await;
    assert_eq!(small, b"{\"version\":1}");

    let single_key = format!("{prefix}/single.bin");
    let single = store
        .put_multipart(
            &target,
            &single_key,
            "application/octet-stream",
            VecParts {
                parts: vec![Bytes::from_static(b"one part only")],
            },
        )
        .await
        .unwrap();
    assert_eq!(single.parts, 1);

    let Err(missing) = store.get(&target, &format!("{prefix}/missing")).await else {
        panic!("a missing key must not answer a body")
    };
    assert_eq!(missing.kind, StoreErrorKind::NotFound);
    assert_eq!(missing.s3_error_code.as_deref(), Some("NoSuchKey"));

    for key in [&archive_key, &manifest_key, &single_key] {
        delete(&target, key).await;
    }
}
