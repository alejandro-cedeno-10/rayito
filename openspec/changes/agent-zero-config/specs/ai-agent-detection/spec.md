## ADDED Requirements

### Requirement: Detection reads project files and never runs user code
`AgentProject.detect(path, overrides)` (Python and TypeScript) SHALL return an `AgentProject` and a `DetectionReport` (schema `rayito.agent-detection/1`) computed only from the files under `path`: manifests, lockfiles, configuration files and line patterns of source files. It SHALL NOT import, execute or evaluate any file of the project, SHALL NOT read the values of `.env*` files, SHALL NOT call AWS and SHALL NOT load an AWS peer. It SHALL be bounded by the file, depth and file-size limits of `limits.json`.

#### Scenario: nothing is executed
- **WHEN** a project whose `agent.py` raises on import is detected
- **THEN** detection succeeds and reports the framework found in the manifest

#### Scenario: environment files are not read
- **WHEN** a project has a `.env` file with a value
- **THEN** no part of the report, the warnings or the evidence contains that value

#### Scenario: zero calls
- **WHEN** detection runs with fake AWS clients that fail on any call
- **THEN** it succeeds and no client was created

### Requirement: Detection rules are one shared data file
The detection rules (manifest names, dependency names per framework, entrypoint and route patterns, request and response field candidates, provider tables and the framework priority) SHALL live in `agent-detection.json` and be generated into both SDKs by `scripts/gen_agent_detection.py`, whose `--check` mode SHALL run in the root gates and in CI. Both SDKs SHALL pass the same vectors in `testdata/agent-detection/`.

#### Scenario: a generated file drifts
- **WHEN** a generated rules file differs from the output of the generator
- **THEN** `gen_agent_detection.py --check` exits non-zero

#### Scenario: same vectors
- **WHEN** the Python and the TypeScript test suites read a vector directory
- **THEN** both produce the `expected.json` report of that directory

### Requirement: Detected values obey a fixed order of precedence
For every key, a value in the project file SHALL win over a flag or argument, a flag or argument over detection, and detection over a named default. A project file that omits `[runtime]` SHALL have it filled by detection. Unknown keys SHALL still be rejected.

#### Scenario: flag over detection
- **WHEN** detection finds Strands and the caller passes `framework="deepagents"`
- **THEN** the project uses deepagents and the report lists the detected value as an alternative

#### Scenario: file over flag
- **WHEN** the project file sets `[serve] max_concurrency = 2` and the flag says 4
- **THEN** the project uses 2

### Requirement: Detection prefers an error to a guess
When the evidence supports more than one framework, language or entrypoint, detection SHALL raise `AgentProjectException(code="ambiguous")`; when it finds none it SHALL raise `code="not_detected"`; for a combination that cannot reach the gateway without a code change it SHALL raise `code="unsupported"`. Each message SHALL be in Spanish and SHALL name the one flag or key that resolves it, and SHALL NOT contain file contents.

#### Scenario: two frameworks
- **WHEN** a project depends on two agent frameworks and neither has an entrypoint match
- **THEN** detection raises `ambiguous` naming `--framework` and `--entry`

#### Scenario: nothing found
- **WHEN** a directory has no manifest
- **THEN** detection raises `not_detected`

### Requirement: A framework object wins over an HTTP app
When a project holds both a framework agent object and an HTTP app, detection SHALL choose the framework driver and list the HTTP app in `alternatives` with a warning; `--entry http` (or `[runtime] kind = "custom"` with `[runtime.app]`) SHALL serve the app instead and choose the `json` protocol when it has a POST route and no AgentCore routes.

#### Scenario: LangGraph with FastAPI
- **WHEN** a project has a compiled graph and a FastAPI app
- **THEN** the report's framework is LangGraph and `alternatives` lists the app

### Requirement: Detection resolves the install plan and runtime versions from lockfiles
The report SHALL carry an install plan chosen by lockfile (`uv.lock`, `requirements*.txt`, `pyproject.toml`, `package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`), with a warning when the plan is not reproducible, and Python and Node versions taken from the project's declared constraint and resolved to the highest pinned build in `limits.json` that satisfies it. Nothing unpinned SHALL be downloaded.

#### Scenario: lockfile choice
- **WHEN** a project has `uv.lock` and `requirements.txt`
- **THEN** the plan is the frozen `uv` sync

#### Scenario: no constraint
- **WHEN** a project declares no version
- **THEN** the report uses the named default Python minor and Node major

### Requirement: Detection infers the model provider without reading values
The report SHALL name the provider found from dependencies and from environment variable names referenced in code, `.env.example` or the README, and map it to an existing gateway preset. It SHALL never include a value.

#### Scenario: Bedrock from a dependency
- **WHEN** a project depends on `langchain-aws`
- **THEN** the report's provider is the Bedrock preset

### Requirement: rayito agent detect prints the report without calling AWS
`rayito agent detect [path] [--json] [--write]` SHALL print the report and the resolved project file, SHALL write `rayito-agent.toml` only with `--write`, SHALL refuse to overwrite an existing file without `--force`, and SHALL make no AWS call.

#### Scenario: json output
- **WHEN** `rayito agent detect PATH --json` runs
- **THEN** stdout is a single JSON document with schema `rayito.agent-detection/1`
