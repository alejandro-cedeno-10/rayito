## MODIFIED Requirements

### Requirement: Execute selects a per-language default context and creates it lazily
`ExecuteRequest` SHALL carry `optional string language = 5` with the canonical values `python`, `bash`, `javascript` and `typescript`. When `context_id` is absent and `language` is absent, empty or `python`, `rayd` SHALL execute on `default`; when `context_id` is absent and `language` is `bash`, `javascript` or `typescript`, `rayd` SHALL execute on the context `default-bash` / `default-javascript` / `default-typescript`, creating it first if it is not live (same `cwd` and `envs` defaults as the rotated `default`: the `/run` payload `workdir`/`envs` or the home) under a per-language lock so that concurrent first executions start exactly one kernel; a lazily created context SHALL count toward the 8-context cap, SHALL be logged as `context created` with `language` and `lazy: true`, and SHALL NOT be started before `/run`. When `context_id` is present and `language` is non-empty, `Execute` SHALL fail with `INVALID_ARGUMENT`. An unknown language name SHALL fail with `INVALID_ARGUMENT`; a known language the running image does not ship (absent from the sidecar's `ready.languages`) SHALL fail with `UNIMPLEMENTED` and a message naming `rayito-base-poly`. `Execute{context_id: "default-bash"}` before the lazy creation SHALL be `NOT_FOUND`. `DestroyContext` on `default-bash` / `default-javascript` / `default-typescript` SHALL be allowed (only `default` is protected) and the next `Execute{language}` SHALL re-create the context. Per-execution `envs` on a context whose language is not `python` SHALL fail with `INVALID_ARGUMENT` before the sidecar is contacted (the set/restore cells are Python source).

#### Scenario: bash cell through the poly image
- **WHEN** the e2e runs `run_code("echo hi", language="bash")` on a sandbox created from `RAYITO_TEMPLATE_POLY`
- **THEN** `"hi"` is in `"".join(execution.logs.stdout)`, `execution.error is None`, and `list_code_contexts()` contains a context with `id == "default-bash"` and `language == "bash"`

#### Scenario: typescript and javascript cells through the poly image
- **WHEN** the e2e runs `run_code("const x: number = 40 + 2; x", language="typescript")`, then `run_code("x + 1", language="typescript")`, then `run_code("let y = 40 + 2; y", language="javascript")` and `run_code("y", language="js")` on a sandbox created from `RAYITO_TEMPLATE_POLY`
- **THEN** the texts are `42`, `43`, `42` and `42`, none contains `\x1b`, the first-cell and second-cell latencies are reported, and `list_code_contexts()` contains `default-typescript` with `language == "typescript"` and `default-javascript` with `language == "javascript"`

#### Scenario: javascript is a known language no image ships
- **WHEN** the e2e runs `run_code("1 + 1", language="javascript")` on a sandbox created from `RAYITO_TEMPLATE_POLY`, the case that M7 answered with `UNIMPLEMENTED` because `ijavascript` could not be built (`AWS_API_NOTES.md` Q57)
- **THEN** the reservation is lifted: the Deno kernel (Q61) answers `execution.text == "2"` with `execution.error is None`, no `UNIMPLEMENTED` is raised, and `javascript` stays `UNIMPLEMENTED` naming `rayito-base-poly` only on images that do not ship it (scenario "Deno languages not shipped by the image")

#### Scenario: one lazy kernel under concurrency
- **WHEN** two `Execute{language: "bash"}` arrive at the same time on a host test against the fake sidecar whose `ready` listed `bash`
- **THEN** exactly one `create_context` line with `"language":"bash"` and `"context_id":"default-bash"` was sent to the sidecar before the two `execute` lines

#### Scenario: language and context are exclusive
- **WHEN** `Execute{context_id: "default", language: "bash"}` is sent
- **THEN** the RPC fails with `INVALID_ARGUMENT` and the SDK raises `InvalidArgumentException` (TypeScript: `InvalidArgumentError`) before any stream event

#### Scenario: language not shipped by the image
- **WHEN** the e2e runs `run_code("echo hi", language="bash")` on a sandbox created from `rayito-base` (`RAYITO_TEMPLATE`)
- **THEN** the SDK raises `UnimplementedError` (not `InvalidArgumentException`) with `rayito-base-poly` in the message, and no context was created

#### Scenario: per-execution envs are python-only
- **WHEN** `run_code("echo $A", language="bash", envs={"A": "1"})` is called
- **THEN** `InvalidArgumentException` is raised and the fake sidecar received no `execute` line

#### Scenario: bash timeout follows the generic rule
- **WHEN** the e2e runs `run_code("sleep 30", language="bash", timeout=2)` and then `run_code("echo back", language="bash")`
- **THEN** the first has `error.name == "ExecutionTimeout"` and the second prints `back`

#### Scenario: Deno languages not shipped by the image
- **WHEN** the e2e runs `run_code("1", language="javascript")` and `run_code("1", language="typescript")` on a sandbox created from `rayito-base` (`RAYITO_TEMPLATE`)
- **THEN** each raises `UnimplementedError` (not `InvalidArgumentException`) with `rayito-base-poly` in the message, and `list_code_contexts()` still lists only `default`

#### Scenario: one lazy typescript kernel on the host
- **WHEN** a host test sends `Execute{language: "typescript"}` to `rayd` whose fake sidecar announced `python,javascript,typescript`
- **THEN** exactly one `create_context` line with `"language":"typescript"` and `"context_id":"default-typescript"` precedes the `execute` line, and `ListContexts` shows `default-typescript` after `default`
