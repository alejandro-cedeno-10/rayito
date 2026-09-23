//! M7 `m7-poly-kernels`, host side of the `code-execution` delta against the
//! stdlib fake sidecar (`--languages`): the `Execute{language}` routing
//! table, the lazily created per-language default (one kernel under two
//! concurrent first cells), `language` + `context_id` refused,
//! `UNIMPLEMENTED` naming `rayito-base-poly` when the image does not ship
//! the language (or the sidecar predates the field), per-execution `envs`
//! refused on a bash context before any sidecar line, `ListContexts`
//! reporting `default-bash`/`bash`, destroy-and-recreate, the explicit
//! `CreateContext{bash}` request line, the `reseed` reply with `skipped`,
//! and the lazy-creation log line free of cell text.
//!
//! Integration tests are test code, but clippy's `allow-unwrap-in-tests` only
//! recognises `#[test]` functions and `#[cfg(test)]` modules.
#![cfg(unix)]
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod common;

use std::collections::HashMap;
use std::time::Duration;

use common::log_capture::log_capture;
use common::{Harness, Options, authenticated, harness_with};
use rayd_core::lifecycle::Hook;
use rayito_proto::v1::{
    ContextInfo, CreateContextRequest, DestroyContextRequest, ExecuteEvent, ExecuteRequest,
    ListContextsRequest, execute_event,
};
use tonic::{Code, Status, Streaming};

const POLY_IMAGE: &str = "rayito-base-poly";
const BASH_CELL: &str = "print hola-desde-bash";
const REQUEST_BUDGET: Duration = Duration::from_secs(5);

fn poly_options() -> Options {
    Options {
        fake_flags: vec!["--languages".to_owned(), "python,bash".to_owned()],
        ..Options::default()
    }
}

fn python_only_options(announce: bool) -> Options {
    let languages = if announce { "python" } else { "none" };
    Options {
        fake_flags: vec!["--languages".to_owned(), languages.to_owned()],
        ..Options::default()
    }
}

fn execute_request(
    code: &str,
    context_id: Option<&str>,
    language: Option<&str>,
    envs: HashMap<String, String>,
) -> ExecuteRequest {
    ExecuteRequest {
        context_id: context_id.map(str::to_owned),
        language: language.map(str::to_owned),
        code: code.to_owned(),
        timeout_ms: 10_000,
        envs,
    }
}

async fn open(
    harness: &Harness,
    code: &str,
    context_id: Option<&str>,
    language: Option<&str>,
    envs: HashMap<String, String>,
) -> Result<Streaming<ExecuteEvent>, Status> {
    harness
        .code
        .clone()
        .execute(authenticated(execute_request(
            code, context_id, language, envs,
        )))
        .await
        .map(tonic::Response::into_inner)
}

/// Drains the stream and returns the joined stdout and the `end` count.
async fn drain(mut stream: Streaming<ExecuteEvent>) -> (String, Option<u64>) {
    let mut stdout = String::new();
    let mut end = None;
    while let Some(event) = stream.message().await.unwrap() {
        match event.event {
            Some(execute_event::Event::Stdout(chunk)) => stdout.push_str(&chunk.text),
            Some(execute_event::Event::End(done)) => {
                end = Some(done.execution_count);
                break;
            }
            _ => {}
        }
    }
    (stdout, end)
}

async fn run(harness: &Harness, code: &str, language: Option<&str>) -> (String, Option<u64>) {
    let stream = open(harness, code, None, language, HashMap::new())
        .await
        .unwrap();
    drain(stream).await
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

async fn destroy_context(harness: &Harness, context_id: &str) -> Result<(), Status> {
    harness
        .code
        .clone()
        .destroy_context(authenticated(DestroyContextRequest {
            context_id: context_id.to_owned(),
        }))
        .await
        .map(|_| ())
}

fn create_lines_for(harness: &Harness, context_id: &str) -> Vec<serde_json::Value> {
    harness
        .requests_of("create_context")
        .into_iter()
        .filter(|request| request["context_id"] == context_id)
        .collect()
}

#[tokio::test]
async fn a_bash_cell_creates_the_language_default_lazily_and_lists_it() {
    let capture = log_capture();
    let harness = harness_with(poly_options()).await;
    assert!(
        harness.requests_of("create_context").is_empty(),
        "nothing is created before the first bash cell"
    );
    let (stdout, end) = run(&harness, BASH_CELL, Some("bash")).await;
    assert_eq!(stdout, "hola-desde-bash\n");
    assert!(end.is_some());
    let creates = create_lines_for(&harness, "default-bash");
    assert_eq!(creates.len(), 1, "{creates:?}");
    assert_eq!(creates[0]["language"], "bash");
    assert_eq!(creates[0]["cwd"], "/tmp", "the /run payload workdir");
    assert_eq!(creates[0]["envs"]["M6"], "1", "the /run payload envs");
    let executes = harness.requests_of("execute");
    assert_eq!(executes.last().unwrap()["context_id"], "default-bash");
    let listed = list_contexts(&harness).await;
    let ids: Vec<(String, String)> = listed
        .iter()
        .map(|info| (info.context_id.clone(), info.language.clone()))
        .collect();
    assert_eq!(
        ids,
        vec![
            ("default".to_owned(), "python".to_owned()),
            ("default-bash".to_owned(), "bash".to_owned())
        ]
    );
    let (again, _) = run(&harness, BASH_CELL, Some("bash")).await;
    assert_eq!(again, "hola-desde-bash\n");
    assert_eq!(create_lines_for(&harness, "default-bash").len(), 1);
    let (python, _) = run(&harness, "print(x)", Some("python")).await;
    assert!(python.contains("42") || python.is_empty());
    assert_eq!(
        harness.requests_of("execute").last().unwrap()["context_id"],
        "default",
        "language python selects the default context"
    );
    let log = capture.text();
    let lazy_line = log
        .lines()
        .find(|line| line.contains("context created") && line.contains("\"lazy\":true"))
        .unwrap_or_else(|| panic!("no lazy creation line in {log}"));
    assert!(lazy_line.contains("\"language\":\"bash\""), "{lazy_line}");
    assert!(
        lazy_line.contains("\"context_id\":\"default-bash\""),
        "{lazy_line}"
    );
    assert!(!lazy_line.contains("hola-desde-bash"), "{lazy_line}");
    assert!(
        !log.contains("hola-desde-bash"),
        "cell text leaked into the log"
    );
}

#[tokio::test]
async fn two_concurrent_first_bash_cells_start_one_kernel() {
    let harness = harness_with(poly_options()).await;
    let first = open(&harness, BASH_CELL, None, Some("bash"), HashMap::new());
    let second = open(&harness, BASH_CELL, None, Some("bash"), HashMap::new());
    let (first, second) = tokio::join!(first, second);
    let (out_a, _) = drain(first.unwrap()).await;
    let (out_b, _) = drain(second.unwrap()).await;
    assert_eq!(out_a, "hola-desde-bash\n");
    assert_eq!(out_b, "hola-desde-bash\n");
    let requests = harness.requests();
    let create_index = requests
        .iter()
        .position(|request| {
            request["op"] == "create_context" && request["context_id"] == "default-bash"
        })
        .expect("one create_context for default-bash");
    let execute_indexes: Vec<usize> = requests
        .iter()
        .enumerate()
        .filter(|(_, request)| {
            request["op"] == "execute" && request["context_id"] == "default-bash"
        })
        .map(|(index, _)| index)
        .collect();
    assert_eq!(create_lines_for(&harness, "default-bash").len(), 1);
    assert_eq!(execute_indexes.len(), 2);
    assert!(execute_indexes.iter().all(|index| *index > create_index));
}

#[tokio::test]
async fn language_and_context_are_exclusive() {
    let harness = harness_with(poly_options()).await;
    let executes_before = harness.requests_of("execute").len();
    let status = open(&harness, "x", Some("default"), Some("bash"), HashMap::new())
        .await
        .unwrap_err();
    assert_eq!(status.code(), Code::InvalidArgument);
    assert_eq!(
        status.message(),
        "language cannot be combined with context_id"
    );
    let status = open(
        &harness,
        "x",
        Some("default"),
        Some("python"),
        HashMap::new(),
    )
    .await
    .unwrap_err();
    assert_eq!(status.code(), Code::InvalidArgument);
    let unknown = open(&harness, "x", None, Some("r"), HashMap::new())
        .await
        .unwrap_err();
    assert_eq!(unknown.code(), Code::InvalidArgument);
    assert_eq!(
        unknown.message(),
        "language must be one of python, bash, javascript, typescript"
    );
    assert_eq!(harness.requests_of("execute").len(), executes_before);
    assert!(create_lines_for(&harness, "default-bash").is_empty());
}

#[tokio::test]
async fn a_language_the_image_does_not_ship_is_unimplemented() {
    let harness = harness_with(python_only_options(true)).await;
    let status = open(&harness, BASH_CELL, None, Some("bash"), HashMap::new())
        .await
        .unwrap_err();
    assert_eq!(status.code(), Code::Unimplemented);
    assert!(
        status.message().contains(POLY_IMAGE),
        "{}",
        status.message()
    );
    assert!(status.message().contains("bash"), "{}", status.message());
    assert!(!status.message().contains("hola"), "{}", status.message());
    let created = create_context(&harness, "javascript").await.unwrap_err();
    assert_eq!(created.code(), Code::Unimplemented);
    assert!(created.message().contains(POLY_IMAGE));
    assert!(created.message().contains("javascript"));
    assert!(harness.requests_of("create_context").is_empty());
    assert_eq!(list_contexts(&harness).await.len(), 1);
    let (stdout, _) = run(&harness, "print(x)", None).await;
    assert!(stdout.is_empty() || stdout.contains("42"));
}

#[tokio::test]
async fn an_older_sidecar_without_languages_means_python_only() {
    let harness = harness_with(python_only_options(false)).await;
    harness
        .wait_for_request("restart_context", REQUEST_BUDGET)
        .await;
    let status = open(&harness, BASH_CELL, None, Some("bash"), HashMap::new())
        .await
        .unwrap_err();
    assert_eq!(status.code(), Code::Unimplemented);
    assert!(status.message().contains(POLY_IMAGE));
    let (_, end) = run(&harness, "x = 42", Some("python")).await;
    assert!(end.is_some());
    assert!(create_context(&harness, "python").await.is_ok());
    assert!(create_context(&harness, "").await.is_ok());
}

#[tokio::test]
async fn per_execution_envs_are_python_only() {
    let harness = harness_with(poly_options()).await;
    let envs: HashMap<String, String> = [("A".to_owned(), "1".to_owned())].into();
    let status = open(&harness, "print $A", None, Some("bash"), envs.clone())
        .await
        .unwrap_err();
    assert_eq!(status.code(), Code::InvalidArgument);
    assert!(
        status.message().contains("python contexts"),
        "{}",
        status.message()
    );
    assert!(
        create_lines_for(&harness, "default-bash").is_empty(),
        "no kernel is started for a refused request"
    );
    assert!(harness.requests_of("execute").is_empty());
    let (stdout, _) = run(&harness, BASH_CELL, Some("bash")).await;
    assert_eq!(stdout, "hola-desde-bash\n");
    let status = open(
        &harness,
        "print $A",
        Some("default-bash"),
        None,
        envs.clone(),
    )
    .await
    .unwrap_err();
    assert_eq!(status.code(), Code::InvalidArgument);
    assert_eq!(harness.requests_of("execute").len(), 1);
    let stream = open(&harness, "envs", Some("default"), None, envs)
        .await
        .unwrap();
    drain(stream).await;
    assert_eq!(harness.requests_of("execute").len(), 2);
}

#[tokio::test]
async fn the_language_default_can_be_destroyed_and_comes_back() {
    let harness = harness_with(poly_options()).await;
    let missing = open(
        &harness,
        BASH_CELL,
        Some("default-bash"),
        None,
        HashMap::new(),
    )
    .await
    .unwrap_err();
    assert_eq!(missing.code(), Code::NotFound);
    run(&harness, BASH_CELL, Some("bash")).await;
    destroy_context(&harness, "default-bash").await.unwrap();
    assert_eq!(list_contexts(&harness).await.len(), 1);
    let (stdout, _) = run(&harness, BASH_CELL, Some("bash")).await;
    assert_eq!(stdout, "hola-desde-bash\n");
    assert_eq!(create_lines_for(&harness, "default-bash").len(), 2);
    assert_eq!(harness.requests_of("destroy_context").len(), 1);
    let protected = destroy_context(&harness, "default").await.unwrap_err();
    assert_eq!(protected.code(), Code::FailedPrecondition);
}

#[tokio::test]
async fn explicit_bash_contexts_carry_the_language_and_count_toward_the_cap() {
    let harness = harness_with(poly_options()).await;
    let context_id = create_context(&harness, "bash").await.unwrap();
    assert!(context_id.starts_with("ctx-"));
    assert_eq!(context_id.len(), "ctx-".len() + 12);
    let create = create_lines_for(&harness, &context_id);
    assert_eq!(create.len(), 1);
    assert_eq!(create[0]["language"], "bash");
    let listed = list_contexts(&harness).await;
    assert_eq!(listed[0].context_id, "default");
    assert_eq!(listed[1].context_id, context_id);
    assert_eq!(listed[1].language, "bash");
    let (stdout, _) = drain(
        open(&harness, BASH_CELL, Some(&context_id), None, HashMap::new())
            .await
            .unwrap(),
    )
    .await;
    assert_eq!(stdout, "hola-desde-bash\n");
    for _ in 0..5 {
        create_context(&harness, "python").await.unwrap();
    }
    run(&harness, BASH_CELL, Some("bash")).await;
    assert_eq!(list_contexts(&harness).await.len(), 8);
    let full = create_context(&harness, "bash").await.unwrap_err();
    assert_eq!(full.code(), Code::ResourceExhausted);
    destroy_context(&harness, "default-bash").await.unwrap();
    let refused = open(&harness, BASH_CELL, None, Some("bash"), HashMap::new()).await;
    assert!(refused.is_ok(), "the freed slot lets the default come back");
}

#[tokio::test]
async fn resume_reseed_skips_the_bash_context() {
    let capture = log_capture();
    let harness = harness_with(poly_options()).await;
    run(&harness, BASH_CELL, Some("bash")).await;
    let reseeds_before = harness.requests_of("reseed").len();
    harness.post(Hook::Suspend, None).await;
    harness.post(Hook::Resume, None).await;
    let deadline = std::time::Instant::now() + REQUEST_BUDGET;
    while harness.requests_of("reseed").len() <= reseeds_before {
        assert!(
            std::time::Instant::now() < deadline,
            "no reseed after /resume"
        );
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    let deadline = std::time::Instant::now() + REQUEST_BUDGET;
    let reseed_line = loop {
        let log = capture.text();
        if let Some(line) = log
            .lines()
            .find(|line| line.contains("kernels reseeded") && line.contains("\"skipped\":1"))
        {
            break line.to_owned();
        }
        assert!(
            std::time::Instant::now() < deadline,
            "no reseed log with skipped in {log}"
        );
        tokio::time::sleep(Duration::from_millis(20)).await;
    };
    assert!(reseed_line.contains("\"reseeded\":1"), "{reseed_line}");
    assert!(reseed_line.contains("\"deferred\":0"), "{reseed_line}");
    assert!(reseed_line.contains("\"failed\":0"), "{reseed_line}");
    let (stdout, _) = run(&harness, BASH_CELL, Some("bash")).await;
    assert_eq!(stdout, "hola-desde-bash\n");
}
