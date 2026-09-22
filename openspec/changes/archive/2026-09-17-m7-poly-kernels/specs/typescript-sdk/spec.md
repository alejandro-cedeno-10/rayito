## ADDED Requirements

### Requirement: Poly kernels acceptance through the TypeScript SDK
`clients/typescript/tests/e2e/poly.e2e.test.ts` SHALL run only with `RAYITO_E2E=1` and `RAYITO_TEMPLATE_POLY` (skipped with a reason naming the variable otherwise), create one sandbox from the poly image with `timeoutMs ≤ 900 000`, terminate it in `afterAll`, and assert: `runCode("echo hi", { language: "bash" })` resolves with `hi` in `logs.stdout.join("")` and no `error`; `runCode("1 + 1", { language: "javascript" })` rejects with `InvalidArgumentError` whose message names `rayito-base-poly` (`Unimplemented`: no image ships `ijavascript`, design D5 fallback, AWS_API_NOTES.md Q57); `runCode("x", { context: "default", language: "bash" })` rejects with `InvalidArgumentError` before any stream event; `listCodeContexts()` contains `default-bash` with `language === "bash"`. The change SHALL be accepted only after this test and the Python poly e2e are green against real AWS.

#### Scenario: poly image through TypeScript
- **WHEN** `RAYITO_E2E=1 RAYITO_TEMPLATE_POLY=<arn> pnpm test:e2e` runs
- **THEN** the poly test passes, the bash cell returned its output, the javascript cell and the exclusive-options call rejected with `InvalidArgumentError`, and the final `Sandbox.list` did not contain the id

#### Scenario: poly test skipped without the template
- **WHEN** `pnpm test:e2e` runs with `RAYITO_E2E=1` and `RAYITO_TEMPLATE` but no `RAYITO_TEMPLATE_POLY`
- **THEN** the poly test is reported as skipped with a reason naming `RAYITO_TEMPLATE_POLY` and no MicroVM is created for it

## MODIFIED Requirements

### Requirement: Code execution surface
`sandbox.runCode(code, { language, context, onStdout, onStderr, onResult, onError, envs, timeoutMs = 300 000, requestTimeoutMs })` SHALL normalise `language` (case-insensitive; `js` → `javascript`; `undefined`/`""` → not sent; anything outside `python`, `bash`, `javascript` → `InvalidArgumentError` before any call), throw `InvalidArgumentError` when both `language` and `context` are given, set `ExecuteRequest.language` only when a language was given, open `Execute` on the unary transport with `timeout_ms = round(timeoutMs)` and the stream deadline `timeoutMs + 15 000` (none for `0`/`undefined`; `requestTimeoutMs` replaces it), feed every event into `Execution{ results, logs{ stdout[], stderr[] }, error?, executionCount? }` (`keepalive` ignored; `onStdout`/`onStderr` receive `OutputMessage{ line, timestamp, error }`; `onResult` a `Result`; `onError` an `ExecutionError`), and resolve it; `Execution.text` SHALL be the `text` of the result with `isMainResult`, else `undefined`; kernel errors SHALL be data in `error`, never rejections; `toJSON()` SHALL produce the Python shape. `Result` SHALL expose `text, html, markdown, svg, png, jpeg, pdf, latex, json, javascript, data, chart, isMainResult, extra, raw` and `formats()`; `json` and `data` SHALL be parsed JSON (raw string kept on failure); `chart` SHALL parse into `LineChart`, `ScatterChart`, `BarChart`, `PieChart`, `BoxAndWhiskerChart`, `SuperChart` or a `Chart` of type `"unknown"`. Leaving before `end` (callback throw, abort) SHALL cancel the stream. `createCodeContext({ cwd, language, envs })` SHALL accept the same language names (normalised the same way, `undefined` → `python`), and with `listCodeContexts()`, `removeCodeContext(ctx)` and `restartCodeContext(ctx)` SHALL accept a `CodeContext` or its id, use 90 s default deadlines for create/restart, and map `NotFound` → `NotFoundError`, `FailedPrecondition` → `InvalidArgumentError`, `Unimplemented` → `InvalidArgumentError` (a language the image does not ship, message naming `rayito-base-poly`).

#### Scenario: acceptance sequence against the fake
- **WHEN** the SDK runs `x = 42`, `x`, `print(x)`, the plot cell, `1/0` and `slow 10` with `timeoutMs 2000`
- **THEN** `runCode("x").text === "42"`, `"42"` is in `logs.stdout.join("")` of `print(x)`, the plot's `results[0].png` and `chart` are defined and `formats()` equals `["png", "chart"]`, `error.name === "ZeroDivisionError"` with `executionCount` set, and the last `error.name === "ExecutionTimeout"`

#### Scenario: callbacks receive typed messages
- **WHEN** `print('a'); print('b')` runs with `onStdout: seen.push`
- **THEN** every element of `seen` has `error === false`, a positive `timestamp`, and their `line`s joined contain `a` and `b`

#### Scenario: contexts
- **WHEN** `ctx = await createCodeContext({ cwd: "/tmp" })`, `runCode("2*2", { context: ctx })`, `listCodeContexts()`, `removeCodeContext(ctx.id)`, then `removeCodeContext("default")`
- **THEN** the result text is `"4"`, the list had `default` first and `ctx` after, removal resolved, and removing `default` rejects with `InvalidArgumentError`

#### Scenario: language routing against the fake
- **WHEN** `runCode("echo hi", { language: "Bash" })`, `runCode("1 + 1", { language: "js" })`, `runCode("x")` and `createCodeContext({ language: "bash" })` run against the fake `rayd`
- **THEN** the first two requests carried `language` `"bash"` and `"javascript"`, the third carried no `language`, the fake's `ListContexts` shows `default-bash` and `default-javascript` with their languages after the cells, and the created context has `language === "bash"`

#### Scenario: invalid language combinations
- **WHEN** `runCode("1", { language: "r" })` and `runCode("1", { language: "bash", context: "default" })` are called
- **THEN** both reject with `InvalidArgumentError` and the fake received no `Execute`

#### Scenario: language not shipped
- **WHEN** the fake `rayd` answers `Execute{language: "javascript"}` with `Unimplemented` and a message containing `rayito-base-poly`
- **THEN** `runCode` rejects with `InvalidArgumentError` whose message contains `rayito-base-poly`
