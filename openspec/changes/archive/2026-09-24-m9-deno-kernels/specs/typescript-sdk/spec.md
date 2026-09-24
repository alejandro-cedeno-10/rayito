## MODIFIED Requirements

### Requirement: Code execution surface
`sandbox.runCode(code, { language, context, onStdout, onStderr, onResult, onError, envs, timeoutMs = 300 000, requestTimeoutMs })` SHALL normalise `language` (case-insensitive; `js` → `javascript`, `ts` → `typescript`; `undefined`/`""` → not sent; anything outside `python`, `bash`, `javascript`, `typescript` → `InvalidArgumentError` before any call), throw `InvalidArgumentError` when both `language` and `context` are given, set `ExecuteRequest.language` only when a language was given, open `Execute` on the unary transport with `timeout_ms = round(timeoutMs)` and the stream deadline `timeoutMs + 15 000` (none for `0`/`undefined`; `requestTimeoutMs` replaces it), feed every event into `Execution{ results, logs{ stdout[], stderr[] }, error?, executionCount? }` (`keepalive` ignored; `onStdout`/`onStderr` receive `OutputMessage{ line, timestamp, error }`; `onResult` a `Result`; `onError` an `ExecutionError`), and resolve it; `Execution.text` SHALL be the `text` of the result with `isMainResult`, else `undefined`; kernel errors SHALL be data in `error`, never rejections; `toJSON()` SHALL produce the Python shape. `Result` SHALL expose `text, html, markdown, svg, png, jpeg, pdf, latex, json, javascript, data, chart, isMainResult, extra, raw` and `formats()`; `json` and `data` SHALL be parsed JSON (raw string kept on failure); `chart` SHALL parse into `LineChart`, `ScatterChart`, `BarChart`, `PieChart`, `BoxAndWhiskerChart`, `SuperChart` or a `Chart` of type `"unknown"`. Leaving before `end` (callback throw, abort) SHALL cancel the stream. `createCodeContext({ cwd, language, envs })` SHALL accept the same language names (normalised the same way, `undefined` → `python`), and with `listCodeContexts()`, `removeCodeContext(ctx)` and `restartCodeContext(ctx)` SHALL accept a `CodeContext` or its id, use 90 s default deadlines for create/restart, and map `NotFound` → `NotFoundError`, `FailedPrecondition` → `InvalidArgumentError`, `Unimplemented` → `InvalidArgumentError` (a language the image does not ship, message naming `rayito-base-poly`).

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

#### Scenario: typescript routing against the fake
- **WHEN** `runCode("1", { language: "ts" })`, `runCode("1", { language: "TypeScript" })` and `createCodeContext({ language: "ts" })` run against the fake `rayd`, and then `runCode("1", { language: "tsx" })`
- **THEN** the first three requests carried `language` `"typescript"`, the fake's `ListContexts` shows `default-typescript`, the created context has `language === "typescript"`, and the last rejects with `InvalidArgumentError` with no `Execute` reaching the fake

#### Scenario: Deno language not shipped
- **WHEN** the fake `rayd` answers `Execute{language: "typescript"}` with `Unimplemented` and a message containing `rayito-base-poly`
- **THEN** `runCode` rejects with `InvalidArgumentError` whose message contains `rayito-base-poly`

### Requirement: Poly kernels acceptance through the TypeScript SDK
`clients/typescript/tests/e2e/poly.e2e.test.ts` SHALL run only with `RAYITO_E2E=1`. It SHALL contain two tests.

The poly test runs only when `RAYITO_TEMPLATE_POLY` is set; otherwise it is skipped with a reason naming the variable. It creates one sandbox from the poly image with `timeoutMs ≤ 900 000`, terminates it in `afterAll`, and asserts:
- `runCode("echo hi", { language: "bash" })` resolves with `hi` in `logs.stdout.join("")` and no `error`;
- `runCode("const x: number = 40 + 2; x", { language: "typescript" })` has `text === "42"` with no `\u001b`;
- `runCode("let y = 40 + 2; y", { language: "js" })` has `text === "42"`;
- `console.log('out')` puts `out` in the stdout logs;
- ``Deno.jupyter.html`<b>hi</b>` `` has `results[0].html === "<b>hi</b>"`;
- `throw new Error('boom')` has `error.name === "Error"` and `error.value === "boom"`;
- `createCodeContext({ language: "typescript", envs: { K: "1" } })` followed by `runCode("Deno.env.get('K') === '1'", { context })` has `text === "true"`;
- `runCode("x", { context: "default", language: "bash" })` rejects with `InvalidArgumentError` before any stream event;
- `listCodeContexts()` contains `default-bash`, `default-javascript` and `default-typescript` with their languages.

The base test runs whenever `RAYITO_TEMPLATE` is set. It creates one sandbox from `rayito-base` and asserts that `runCode("1", { language: "javascript" })` and `runCode("1", { language: "typescript" })` reject with `InvalidArgumentError` whose message names `rayito-base-poly`.

The change SHALL be accepted only after these tests and the Python poly and Deno e2e are green against real AWS.

#### Scenario: poly image through TypeScript
- **WHEN** `RAYITO_E2E=1 RAYITO_TEMPLATE=<name> RAYITO_TEMPLATE_POLY=<name> pnpm test:e2e` runs
- **THEN** both tests pass: the bash, JavaScript and TypeScript cells returned their outputs, the exclusive-options call rejected with `InvalidArgumentError`, `rayito-base` rejected both Deno languages naming `rayito-base-poly`, and the final `Sandbox.list` contained neither id

#### Scenario: poly test skipped without the template
- **WHEN** `pnpm test:e2e` runs with `RAYITO_E2E=1` and `RAYITO_TEMPLATE` but no `RAYITO_TEMPLATE_POLY`
- **THEN** the poly test is reported as skipped with a reason naming `RAYITO_TEMPLATE_POLY` and no MicroVM is created for it, while the base test still runs
