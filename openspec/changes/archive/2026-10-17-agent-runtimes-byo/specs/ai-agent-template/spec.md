## MODIFIED Requirements

### Requirement: AgentTemplate builds the agent image from pinned, hash-checked runtimes
Both SDKs SHALL export `AgentTemplate(name="rayito-agent", base="rayito-base-caps", runtimes=("opencode", "deepagents"), prefetch=True, kernel_warmup=False, memory_mib=2048, base_version=None, app=None, app_exclude=())` (TypeScript `kernelWarmup`, `appExclude`; Python also `AsyncAgentTemplate` with an `async build`). `runtimes` SHALL accept registry names and runtime objects that implement `InstallableRuntime`, and the template SHALL be composed from each runtime's `install_steps()`, `context_files()`, `smoke_checks()` and `prefetch_paths()`. `app`, when set, SHALL be a local directory copied to `AGENT_APP_DIR` (root-owned, not writable by `user`) honouring its own `.dockerignore` and then `app_exclude`/`appExclude` (dockerignore patterns). `to_template()` / `toTemplate()` SHALL return a plain `Template` from `base` that installs OpenCode and ripgrep from their release assets checked with `sha256sum -c` against `AGENT_OPENCODE_SHA256` and `AGENT_RIPGREP_SHA256`, and, for `deepagents`, copies the package-data requirements file, checks it against `AGENT_DEEPAGENTS_REQUIREMENTS_SHA256` and installs it into `/opt/agents/deepagents` with `pip install --require-hashes --no-deps --only-binary=:all:`, and copies the package-data runner `deepagents_runner.py` to `DEEPAGENTS_RUNNER_PATH` (`/opt/agents/rayito/deepagents_runner.py`) with mode 0755; it SHALL also copy the agent host to `/opt/agents/rayito/agent_host/`; everything under `/opt/agents` SHALL be root-owned and not writable by group or others; with `kernel_warmup=False` it SHALL write `AGENT_KERNEL_WARMUP_SLIM_VALUE` (`slim`) to `AGENT_KERNEL_WARMUP_MARKER_PATH` (the sidecar's `warmup_variant` marker under `/opt/rayito/sidecar/ipython/startup/`) with mode 0644, and with `kernel_warmup=True` it SHALL NOT touch that file; the OpenCode flag envs (including `OPENCODE_DISABLE_CLAUDE_CODE=1`) SHALL be set; a smoke test SHALL run each runtime as `user`. Every new runtime SHALL install only pinned artefacts whose versions and sha256 (or commit) are in `limits.json`. `memory_mib` below `AGENT_MIN_MEMORY_MIB`, an empty or unknown or repeated runtime, a runtime object that does not implement `InstallableRuntime`, or an empty `name`/`base` SHALL raise `InvalidArgumentException` / `InvalidArgumentError` with no network call. `build(bucket=...)` SHALL write the build context to a temporary directory, call `Template.build` with `memory_mb=memory_mib` and that `context_dir`, and remove the directory afterwards. Both SDKs SHALL produce the Dockerfile, manifest, start command, ready command and context file names of `testdata/agent/agent-template/cases.json`; the cases that existed before this change SHALL keep their expected Dockerfile apart from the added agent host copy step.

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
- **THEN** the spy saw `memory_mb=4096` and a context with `deepagents_runner.py`, `rayito-agent.json`, `requirements-deepagents.txt`, `rayito-agent-prefetch` and the agent host, and the directory no longer exists after the call

#### Scenario: a Strands and Hermes image
- **WHEN** a unit test renders `AgentTemplate(runtimes=("strands", "hermes")).to_dockerfile()`
- **THEN** it installs the Strands venv with `--require-hashes` from the requirements whose sha256 is in `limits.json`, clones Hermes at the pinned tag, fails unless `git rev-parse HEAD` equals the pinned commit, and does not install OpenCode or deepagents

#### Scenario: a runtime object without install support
- **WHEN** a caller passes an object that implements only `AgentRuntime` in `runtimes`
- **THEN** it raises `InvalidArgumentException` naming `InstallableRuntime`

### Requirement: The image carries a rayito.agent-template/1 manifest
The template SHALL bake `AGENT_TEMPLATE_MANIFEST_PATH` (`/opt/agents/rayito-agent.json`) with `{schema: "rayito.agent-template/1", protocol: AGENT_PROTOCOL_VERSION, opencode: {version, sha256} | null, deepagents: {requirements_sha256} | null, runner_sha256: <sha256 of the runner> | null, prefetch_paths: [...], runtimes: {<name>: <manifest_entry()>}, host_sha256: <sha256 of the agent host files>}`, where `runner_sha256` is set exactly when deepagents is installed and `prefetch_paths` is the concatenation of each runtime's `prefetch_paths()` (for OpenCode, the OpenCode and ripgrep binaries). `runtimes` and `host_sha256` are additive: the prefetch daemon and older SDKs that read only the earlier keys SHALL keep working.

#### Scenario: deepagents runner is installed
- **WHEN** a unit test renders `AgentTemplate()`
- **THEN** the context carries `deepagents_runner.py`, the Dockerfile copies it to `/opt/agents/rayito/deepagents_runner.py` and makes it 0755, and the manifest's `runner_sha256` is the sha256 of that file (`DEEPAGENTS_RUNNER_SHA256` in TypeScript)

#### Scenario: opencode only
- **WHEN** a unit test reads `AgentTemplate(runtimes=("opencode",)).manifest()`
- **THEN** `deepagents` and `runner_sha256` are null, `prefetch_paths` is `["/opt/agents/bin/opencode", "/opt/agents/bin/rg"]` and `runtimes` has only the `opencode` entry
