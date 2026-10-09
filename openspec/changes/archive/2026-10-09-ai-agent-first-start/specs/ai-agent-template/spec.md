## MODIFIED Requirements

### Requirement: AgentTemplate builds the agent image from pinned, hash-checked runtimes
Both SDKs SHALL export `AgentTemplate(name="rayito-agent", base="rayito-base-caps", runtimes=("opencode", "deepagents"), prefetch=True, kernel_warmup=False, memory_mib=2048, base_version=None)` (TypeScript `kernelWarmup`; Python also `AsyncAgentTemplate` with an `async build`). `to_template()` / `toTemplate()` SHALL return a plain `Template` from `base` that installs OpenCode and ripgrep from their release assets checked with `sha256sum -c` against `AGENT_OPENCODE_SHA256` and `AGENT_RIPGREP_SHA256`, and, for `deepagents`, copies the package-data requirements file, checks it against `AGENT_DEEPAGENTS_REQUIREMENTS_SHA256` and installs it into `/opt/agents/deepagents` with `pip install --require-hashes --no-deps --only-binary=:all:`, and copies the package-data runner `deepagents_runner.py` to `DEEPAGENTS_RUNNER_PATH` (`/opt/agents/rayito/deepagents_runner.py`) with mode 0755; everything under `/opt/agents` SHALL be root-owned and not writable by group or others; with `kernel_warmup=False` it SHALL write `AGENT_KERNEL_WARMUP_SLIM_VALUE` (`slim`) to `AGENT_KERNEL_WARMUP_MARKER_PATH` (the sidecar's `warmup_variant` marker under `/opt/rayito/sidecar/ipython/startup/`) with mode 0644, and with `kernel_warmup=True` it SHALL NOT touch that file; the OpenCode flag envs (including `OPENCODE_DISABLE_CLAUDE_CODE=1`) SHALL be set; a smoke test SHALL run each runtime as `user`. `memory_mib` below `AGENT_MIN_MEMORY_MIB`, an empty or unknown or repeated runtime, or an empty `name`/`base` SHALL raise `InvalidArgumentException` / `InvalidArgumentError` with no network call. `build(bucket=...)` SHALL write the build context to a temporary directory, call `Template.build` with `memory_mb=memory_mib` and that `context_dir`, and remove the directory afterwards. Both SDKs SHALL produce the Dockerfile, manifest, start command, ready command and context file names of `testdata/agent/agent-template/cases.json`.

#### Scenario: pins come from limits.json
- **WHEN** a unit test renders `AgentTemplate().to_dockerfile()`
- **THEN** it contains the OpenCode, ripgrep and requirements sha256 values of `limits.json` and `--require-hashes`

#### Scenario: kernel warm-up off by default
- **WHEN** a unit test renders `AgentTemplate().to_dockerfile()` and `AgentTemplate(kernel_warmup=True).to_dockerfile()`
- **THEN** only the first writes the `slim` marker to `AGENT_KERNEL_WARMUP_MARKER_PATH`, which is where `image/Dockerfile` copies the sidecar's `ipython/startup/` and the value its warm-up script compares against

#### Scenario: too little memory
- **WHEN** a caller builds `AgentTemplate(memory_mib=1024)`
- **THEN** it raises `InvalidArgumentException` and no AWS client is created

#### Scenario: build passes the context and cleans up
- **WHEN** a unit test builds `AgentTemplate(name="mi-agente", memory_mib=4096)` with `Template.build` replaced by a spy
- **THEN** the spy saw `memory_mb=4096` and a context with `deepagents_runner.py`, `rayito-agent.json`, `requirements-deepagents.txt` and `rayito-agent-prefetch`, and the directory no longer exists after the call

### Requirement: The prefetch daemon warms the page cache after a snapshot restore
With `prefetch=True` the template SHALL install `rayito-agent-prefetch` (package data) at `/opt/agents/bin/` and set it as `start_cmd` with the manifest path, `AGENT_PREFETCH_RESTORE_JUMP_SECONDS` (30), `AGENT_PREFETCH_INTERVAL_SECONDS` (2) and `AGENT_PREFETCH_BUILD_MARKER_PATH`, with a `ready_cmd` that waits for that marker file for at most `AGENT_PREFETCH_BUILD_TIMEOUT_SECONDS` (300). When started with the marker argument (before the build snapshot) the daemon SHALL read every `prefetch_paths` entry once, without waiting for the guest to settle, and then create the marker, so the memory snapshot already holds those pages. The daemon SHALL then sleep the interval in a loop and, when the wall clock jumped more than the threshold, first wait until the guest has had no I/O in flight (`/proc/diskstats`) for one second in a row, at most 60 s, so its reads never compete with the boot that `create()` waits for, and then read every `prefetch_paths` entry with `nice -n 19` and, if the deepagents venv exists, import its modules once; apart from the build marker it SHALL NOT write files, and it SHALL NOT open network connections. With `prefetch=False` there SHALL be no `start_cmd` and no script in the context.

#### Scenario: a clock jump triggers the prefetch
- **WHEN** a unit test runs the script with a fake `date` that jumps by 100 s
- **THEN** it reads the manifest's path through `nice`

#### Scenario: the prefetch waits for the boot to go quiet
- **WHEN** a unit test runs the script after the jump with a `diskstats` file that shows I/O in flight, and later one that shows none
- **THEN** it reads nothing while I/O is in flight and reads the manifest's path once the guest is quiet

#### Scenario: the build snapshot waits for the first read
- **WHEN** a unit test runs the script with the marker argument and a `diskstats` file that shows I/O in flight
- **THEN** it reads the manifest's path at once and then creates the marker, and the template's `ready_cmd` is `test -e <marker>` with a 300 s timeout

### Requirement: rayito agent template build is the CLI over AgentTemplate
The CLI SHALL provide `rayito agent template build --bucket B [--name] [--base] [--memory-mb] [--deepagents/--no-deepagents] [--prefetch/--no-prefetch] [--kernel-warmup/--no-kernel-warmup] [--force] [--timeout]`, with defaults taken from the SDK constants (`--no-kernel-warmup` by default), printing `template_id=` and `build_id=` (or JSON with `--json`), and failing with a message on `InvalidArgumentException` or `BuildException`.

#### Scenario: flags map onto AgentTemplate
- **WHEN** a unit test invokes `template build --bucket amzn-s3-demo-bucket --no-deepagents --no-prefetch --kernel-warmup` with `AgentTemplate.build` replaced by a spy
- **THEN** the spy received `runtimes == ("opencode",)`, `prefetch is False` and `kernel_warmup is True`, and the output has `template_id=`
