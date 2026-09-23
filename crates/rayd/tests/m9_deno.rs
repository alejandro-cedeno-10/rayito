//! M9 `m9-deno-kernels`, host side of the `code-execution` delta against the
//! stdlib fake sidecar (`--languages`): `typescript` joins the language
//! catalog, so `Execute{language:"typescript"}` creates `default-typescript`
//! lazily (one `create_context` line before the `execute` line) and lists it
//! after `default`; an image that does not ship the Deno kernels answers
//! `UNIMPLEMENTED` naming `rayito-base-poly` for `typescript` and
//! `javascript` without asking the sidecar; `ts` stays an unknown name
//! (aliases are the SDKs' job) whose `INVALID_ARGUMENT` lists the four
//! canonical names; per-execution `envs` are refused on a `typescript`
//! context before any `execute` line.
//!
//! Integration tests are test code, but clippy's `allow-unwrap-in-tests` only
//! recognises `#[test]` functions and `#[cfg(test)]` modules.
#![cfg(unix)]
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod common;

use std::collections::HashMap;

use common::{Harness, Options, authenticated, harness_with};
use rayito_proto::v1::{
    ContextInfo, CreateContextRequest, ExecuteEvent, ExecuteRequest, ListContextsRequest,
    execute_event,
};
use tonic::{Code, Status, Streaming};

const POLY_IMAGE: &str = "rayito-base-poly";
const DENO_CELL: &str = "print hola-desde-deno";
const FOUR_NAMES: &str = "language must be one of python, bash, javascript, typescript";

fn languages_options(languages: &str) -> Options {
    Options {
        fake_flags: vec!["--languages".to_owned(), languages.to_owned()],
        ..Options::default()
    }
}

fn deno_options() -> Options {
    languages_options("python,javascript,typescript")
}

async fn open(
    harness: &Harness,
    code: &str,
    language: &str,
    envs: HashMap<String, String>,
) -> Result<Streaming<ExecuteEvent>, Status> {
    harness
        .code
        .clone()
        .execute(authenticated(ExecuteRequest {
            context_id: None,
            language: Some(language.to_owned()),
            code: code.to_owned(),
            timeout_ms: 10_000,
            envs,
        }))
        .await
        .map(tonic::Response::into_inner)
}

/// Drains the stream and returns the joined stdout and whether `end` came.
async fn drain(mut stream: Streaming<ExecuteEvent>) -> (String, bool) {
    let mut stdout = String::new();
    while let Some(event) = stream.message().await.unwrap() {
        match event.event {
            Some(execute_event::Event::Stdout(chunk)) => stdout.push_str(&chunk.text),
            Some(execute_event::Event::End(_)) => return (stdout, true),
            _ => {}
        }
    }
    (stdout, false)
}

async fn list_contexts(harness: &Harness) -> Vec<ContextInfo> {
    harness
        .code
        .clone()
        .list_contexts(authenticated(ListContextsRequest {}))
        .await
        .unwrap()
        .into_inner()
        .contexts
}

async fn create_context(harness: &Harness, language: &str) -> Result<String, Status> {
    harness
        .code
        .clone()
        .create_context(authenticated(CreateContextRequest {
            language: language.to_owned(),
            cwd: None,
            envs: HashMap::new(),
        }))
        .await
        .map(|response| response.into_inner().context_id)
}

fn position_of(requests: &[serde_json::Value], op: &str, context_id: &str) -> Option<usize> {
    requests
        .iter()
        .position(|request| request["op"] == op && request["context_id"] == context_id)
}

#[tokio::test]
async fn a_typescript_cell_creates_its_default_context_lazily() {
    let harness = harness_with(deno_options()).await;
    assert!(harness.requests_of("create_context").is_empty());
    let stream = open(&harness, DENO_CELL, "typescript", HashMap::new())
        .await
        .unwrap();
    let (stdout, ended) = drain(stream).await;
    assert_eq!(stdout, "hola-desde-deno\n");
    assert!(ended);
    let creates = harness.requests_of("create_context");
    assert_eq!(creates.len(), 1, "{creates:?}");
    assert_eq!(creates[0]["language"], "typescript");
    assert_eq!(creates[0]["context_id"], "default-typescript");
    let requests = harness.requests();
    let create_line = position_of(&requests, "create_context", "default-typescript").unwrap();
    let execute_line = position_of(&requests, "execute", "default-typescript").unwrap();
    assert!(create_line < execute_line, "{requests:?}");
    let listed: Vec<(String, String)> = list_contexts(&harness)
        .await
        .into_iter()
        .map(|info| (info.context_id, info.language))
        .collect();
    assert_eq!(
        listed,
        vec![
            ("default".to_owned(), "python".to_owned()),
            ("default-typescript".to_owned(), "typescript".to_owned())
        ]
    );
}

#[tokio::test]
async fn deno_languages_are_unimplemented_where_the_image_lacks_them() {
    let harness = harness_with(languages_options("python,bash")).await;
    let status = open(&harness, DENO_CELL, "typescript", HashMap::new())
        .await
        .unwrap_err();
    assert_eq!(status.code(), Code::Unimplemented);
    assert!(
        status.message().contains(POLY_IMAGE),
        "{}",
        status.message()
    );
    assert!(
        status.message().contains("typescript"),
        "{}",
        status.message()
    );
    assert!(!status.message().contains("hola"), "{}", status.message());
    let created = create_context(&harness, "javascript").await.unwrap_err();
    assert_eq!(created.code(), Code::Unimplemented);
    assert!(
        created.message().contains(POLY_IMAGE),
        "{}",
        created.message()
    );
    assert!(
        created.message().contains("javascript"),
        "{}",
        created.message()
    );
    assert!(harness.requests_of("create_context").is_empty());
    assert_eq!(list_contexts(&harness).await.len(), 1);
}

#[tokio::test]
async fn the_ts_alias_is_an_unknown_name_for_the_agent() {
    let harness = harness_with(deno_options()).await;
    let status = open(&harness, DENO_CELL, "ts", HashMap::new())
        .await
        .unwrap_err();
    assert_eq!(status.code(), Code::InvalidArgument);
    assert_eq!(status.message(), FOUR_NAMES);
    assert!(harness.requests_of("create_context").is_empty());
    assert!(harness.requests_of("execute").is_empty());
}

#[tokio::test]
async fn per_execution_envs_are_refused_on_typescript() {
    let harness = harness_with(deno_options()).await;
    let envs: HashMap<String, String> = [("K".to_owned(), "1".to_owned())].into();
    let status = open(&harness, DENO_CELL, "typescript", envs)
        .await
        .unwrap_err();
    assert_eq!(status.code(), Code::InvalidArgument);
    assert!(
        status.message().contains("python contexts"),
        "{}",
        status.message()
    );
    assert!(harness.requests_of("execute").is_empty());
    assert!(
        harness.requests_of("create_context").is_empty(),
        "no kernel is started for a refused request"
    );
}
