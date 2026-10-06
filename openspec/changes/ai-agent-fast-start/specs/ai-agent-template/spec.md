## ADDED Requirements

### Requirement: AgentTemplate builds the agent image from pinned, hash-checked runtimes
Both SDKs SHALL export `AgentTemplate(name="rayito-agent", base="rayito-base-caps", runtimes=("opencode", "deepagents"), prefetch=True, memory_mib=2048, base_version=None)` (Python also `AsyncAgentTemplate` with an `async build`). `to_template()` / `toTemplate()` SHALL return a plain `Template` from `base` that installs OpenCode and ripgrep from their release assets checked with `sha256sum -c` against `AGENT_OPENCODE_SHA256` and `AGENT_RIPGREP_SHA256`, and, for `deepagents`, copies the package-data requirements file, checks it against `AGENT_DEEPAGENTS_REQUIREMENTS_SHA256` and installs it into `/opt/agents/deepagents` with `pip install --require-hashes --no-deps --only-binary=:all:`; everything under `/opt/agents` SHALL be root-owned and not writable by group or others; the OpenCode flag envs (including `OPENCODE_DISABLE_CLAUDE_CODE=1`) SHALL be set; a smoke test SHALL run each runtime as `user`. `memory_mib` below `AGENT_MIN_MEMORY_MIB`, an empty or unknown or repeated runtime, or an empty `name`/`base` SHALL raise `InvalidArgumentException` / `InvalidArgumentError` with no network call. `build(bucket=...)` SHALL write the build context to a temporary directory, call `Template.build` with `memory_mb=memory_mib` and that `context_dir`, and remove the directory afterwards. Both SDKs SHALL produce the Dockerfile, manifest, start command and context file names of `testdata/agent/agent-template/cases.json`.

#### Scenario: pins come from limits.json
- **WHEN** a unit test renders `AgentTemplate().to_dockerfile()`
- **THEN** it contains the OpenCode, ripgrep and requirements sha256 values of `limits.json` and `--require-hashes`

#### Scenario: too little memory
- **WHEN** a caller builds `AgentTemplate(memory_mib=1024)`
- **THEN** it raises `InvalidArgumentException` and no AWS client is created

#### Scenario: build passes the context and cleans up
- **WHEN** a unit test builds `AgentTemplate(name="mi-agente", memory_mib=4096)` with `Template.build` replaced by a spy
- **THEN** the spy saw `memory_mb=4096` and a context with `rayito-agent.json`, `requirements-deepagents.txt` and `rayito-agent-prefetch`, and the directory no longer exists after the call

### Requirement: The image carries a rayito.agent-template/1 manifest
The template SHALL bake `AGENT_TEMPLATE_MANIFEST_PATH` (`/opt/agents/rayito-agent.json`) with `{schema: "rayito.agent-template/1", protocol: AGENT_PROTOCOL_VERSION, opencode: {version, sha256} | null, deepagents: {requirements_sha256} | null, runner_sha256: null, prefetch_paths: [...]}`, where `prefetch_paths` lists the OpenCode and ripgrep binaries when OpenCode is installed.

#### Scenario: opencode only
- **WHEN** a unit test reads `AgentTemplate(runtimes=("opencode",)).manifest()`
- **THEN** `deepagents` is null and `prefetch_paths` is `["/opt/agents/bin/opencode", "/opt/agents/bin/rg"]`

### Requirement: The prefetch daemon warms the page cache after a snapshot restore
With `prefetch=True` the template SHALL install `rayito-agent-prefetch` (package data) at `/opt/agents/bin/` and set it as `start_cmd` with the manifest path, `AGENT_PREFETCH_RESTORE_JUMP_SECONDS` (30) and `AGENT_PREFETCH_INTERVAL_SECONDS` (2). The daemon SHALL sleep the interval in a loop and, when the wall clock jumped more than the threshold, read every `prefetch_paths` entry with `nice -n 19` and, if the deepagents venv exists, import its modules once; it SHALL NOT write files nor open network connections. With `prefetch=False` there SHALL be no `start_cmd` and no script in the context.

#### Scenario: a clock jump triggers the prefetch
- **WHEN** a unit test runs the script with a fake `date` that jumps by 100 s
- **THEN** it reads the manifest's path through `nice`

### Requirement: rayito agent template build is the CLI over AgentTemplate
The CLI SHALL provide `rayito agent template build --bucket B [--name] [--base] [--memory-mb] [--deepagents/--no-deepagents] [--prefetch/--no-prefetch] [--force] [--timeout]`, with defaults taken from the SDK constants, printing `template_id=` and `build_id=` (or JSON with `--json`), and failing with a message on `InvalidArgumentException` or `BuildException`.

#### Scenario: flags map onto AgentTemplate
- **WHEN** a unit test invokes `template build --bucket amzn-s3-demo-bucket --no-deepagents --no-prefetch` with `AgentTemplate.build` replaced by a spy
- **THEN** the spy received `runtimes == ("opencode",)` and `prefetch is False`, and the output has `template_id=`

### Requirement: agent_pool_warmup prepares pool slots for the agent
Both SDKs SHALL export `agent_pool_warmup(runtime="opencode", *, serve=False)` / `agentPoolWarmup(runtime, { serve })` returning the runtime's `warmup_steps(serve=...)`; with `serve=True` (OpenCode only, otherwise `InvalidArgumentException`) it SHALL prepend a step that writes a placeholder config at `OPENCODE_CONFIG` only if none exists and append a foreground step that waits for the serve secret and `/global/health` (basic auth with the in-VM password, never placed in a step's text) and then warms an instance with `GET /config?directory=<AGENT_STATE_DIR>/warm`, within `DEFAULT_WARMUP_STEP_TIMEOUT_SECONDS`. The serve password SHALL be generated inside each VM, so every slot and every recycled slot has its own. Both SDKs SHALL produce the steps of `testdata/agent/pool-warmup.json`.

#### Scenario: serve steps order
- **WHEN** a unit test reads `agent_pool_warmup("opencode", serve=True)`
- **THEN** the tags are the placeholder config, the binary warm-up, the background `rayito-agent-serve` and the ready step, in that order, and the ready step calls `/global/health` and the warm `/config` URL

### Requirement: agent.prepare() leaves background steps running
`sbx.agent.prepare()` SHALL start background warm-up steps with no timeout, so a resident server started by `prepare(serve=True)` is not killed after `timeout_seconds`.

#### Scenario: prepare with serve
- **WHEN** a caller runs `sbx.agent.prepare(serve=True)`
- **THEN** the `opencode serve` step is started with `background=True` and no timeout
