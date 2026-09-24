//! A scripted `SignedHttp` for the host tests: each `send` pops the next
//! canned response, records the method, the URL, the declared length and
//! the full request body, and never touches the network. `block_on` drives
//! the (always ready) futures without an executor.

use std::collections::VecDeque;
use std::future::Future;
use std::pin::pin;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::task::{Context, Poll, Waker};

use bytes::Bytes;

use super::ports::{
    HttpError, HttpErrorKind, HttpHead, HttpMethod, RequestBody, ResponseBody, SignedHttp,
    SignedRequest,
};

/// Polls a future that never waits on anything external to completion.
pub fn block_on<F: Future>(future: F) -> F::Output {
    let mut future = pin!(future);
    let mut context = Context::from_waker(Waker::noop());
    loop {
        if let Poll::Ready(output) = future.as_mut().poll(&mut context) {
            return output;
        }
    }
}

pub struct FakeBody {
    chunks: VecDeque<Vec<u8>>,
}

impl FakeBody {
    pub fn new(chunks: Vec<Vec<u8>>) -> Self {
        Self {
            chunks: chunks.into(),
        }
    }

    pub fn remaining(&self) -> usize {
        self.chunks.len()
    }
}

impl ResponseBody for FakeBody {
    fn next_chunk(&mut self) -> impl Future<Output = Result<Option<Bytes>, HttpError>> + Send {
        std::future::ready(Ok(self.chunks.pop_front().map(Bytes::from)))
    }
}

#[derive(Debug, Clone)]
pub enum FakeResponse {
    Status {
        status: u16,
        etag: Option<String>,
        content_length: Option<u64>,
        body: Vec<u8>,
    },
    Error(HttpErrorKind),
}

impl FakeResponse {
    pub fn ok(body: &[u8]) -> Self {
        Self::Status {
            status: 200,
            etag: None,
            content_length: Some(body.len() as u64),
            body: body.to_vec(),
        }
    }

    pub fn etag(etag: &str) -> Self {
        Self::Status {
            status: 200,
            etag: Some(etag.to_owned()),
            content_length: Some(0),
            body: Vec::new(),
        }
    }

    pub fn error(status: u16, code: &str, message: &str) -> Self {
        let body = format!("<Error><Code>{code}</Code><Message>{message}</Message></Error>");
        Self::Status {
            status,
            etag: None,
            content_length: Some(body.len() as u64),
            body: body.into_bytes(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecordedCall {
    pub method: HttpMethod,
    pub url: String,
    pub content_type: Option<&'static str>,
    pub content_length: Option<u64>,
    pub body: Vec<u8>,
}

#[derive(Clone, Default)]
pub struct FakeHttp {
    responses: Arc<Mutex<VecDeque<FakeResponse>>>,
    calls: Arc<Mutex<Vec<RecordedCall>>>,
}

impl FakeHttp {
    pub fn with(responses: Vec<FakeResponse>) -> Self {
        Self {
            responses: Arc::new(Mutex::new(responses.into())),
            calls: Arc::default(),
        }
    }

    pub fn calls(&self) -> Vec<RecordedCall> {
        lock(&self.calls).clone()
    }

    fn next_response(&self) -> FakeResponse {
        lock(&self.responses)
            .pop_front()
            .unwrap_or(FakeResponse::Error(HttpErrorKind::Connect))
    }
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

impl SignedHttp for FakeHttp {
    type Body = FakeBody;

    async fn send<B: RequestBody>(
        &self,
        request: SignedRequest,
        body: Option<B>,
    ) -> Result<(HttpHead, Self::Body), HttpError> {
        let mut sent = Vec::new();
        if let Some(mut body) = body {
            while let Some(chunk) = body.next_chunk().await? {
                sent.extend_from_slice(&chunk);
            }
        }
        lock(&self.calls).push(RecordedCall {
            method: request.method,
            url: request.url.to_string(),
            content_type: request.content_type,
            content_length: request.content_length,
            body: sent,
        });
        match self.next_response() {
            FakeResponse::Error(kind) => Err(HttpError::new(kind)),
            FakeResponse::Status {
                status,
                etag,
                content_length,
                body,
            } => Ok((
                HttpHead {
                    status,
                    content_length,
                    etag,
                    request_id: Some("FAKEREQUESTID".to_owned()),
                },
                FakeBody::new(if body.is_empty() {
                    Vec::new()
                } else {
                    vec![body]
                }),
            )),
        }
    }
}
