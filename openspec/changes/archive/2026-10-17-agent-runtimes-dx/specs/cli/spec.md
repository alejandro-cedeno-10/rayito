## ADDED Requirements

### Requirement: rayito agent init scaffolds an HTTP app for custom and a plain graph for langgraph
`rayito agent init --runtime custom` SHALL write `rayito-agent.toml` with `[runtime]` (`kind`, `requirements`, `install = []`) and `[runtime.app]` (`command`, `port`, `health_path`, `invoke_path`, `protocol = "raw"`), a minimal `serve.py` with the declared routes and a `requirements.txt`, and SHALL print a line explaining `raw`. `--runtime langgraph` SHALL write a `create_react_agent` graph. Both CLIs SHALL write the same files, checked against `testdata/cli/agent-scaffold/cases.json`, and the written project SHALL load.

#### Scenario: custom scaffold loads
- **WHEN** `rayito agent init DIR --runtime custom` runs and `AgentProject.load(DIR)` reads the result
- **THEN** the runtime is a `CustomRuntime` with an `HttpApp` of protocol `raw` and `requirements` set

### Requirement: rayito agent deploy can keep a failed sandbox and explains the failure
`rayito agent deploy` SHALL accept `--keep-on-failure`, print the message and notes of a `SandboxException` and exit 1, and print the unknown-usage label `tokens=desconocido` in `run` and `invoke`. The surface golden SHALL list the new option in both CLIs.

#### Scenario: failed startup
- **WHEN** the deployment fails with the host's diagnostics
- **THEN** the CLI prints them to stderr and exits 1

### Requirement: rayito image publish accepts latest as the base image version
`--base-image-version latest` SHALL resolve the newest version `list-managed-microvm-image-versions` returns for the managed base of the region and print `base_image_version=<version>` to stderr before publishing on it. The option SHALL stay required, an explicit value SHALL be used as given with no extra call, and a managed image that lists no version SHALL fail with exit 1 naming the option.

#### Scenario: latest
- **WHEN** `--base-image-version latest` runs and the managed image lists versions 1 and 3
- **THEN** the publish uses `3` and prints `base_image_version=3`
