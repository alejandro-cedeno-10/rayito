## ADDED Requirements

### Requirement: rayito agent deploy deploys a project with no project file
`rayito agent deploy [PATH]` SHALL detect the project when `rayito-agent.toml` is absent, print the detection summary before building, and accept `--framework`, `--language`, `--entry`, `--protocol`, `--memory`, `--max-sessions`, `--sessions`, `--min-vms`, `--max-vms`, `--warm`, `--idle`, `--queue`, `--queue-timeout`, `--env K=V`, `--allow-out`, `--region`, `--bucket`, `--secret provider=name`, `--create-secret`, `--yes`, `--dry-run` and `--write`. Help text and messages SHALL be in Spanish and defaults SHALL be the SDK constants. `--dry-run` SHALL make no AWS call.

#### Scenario: dry run prints plan and cost
- **WHEN** `rayito agent deploy PATH --dry-run` runs
- **THEN** it prints the detection, the plan and a cost estimate, exits 0 and creates nothing

#### Scenario: flag overrides detection
- **WHEN** `--max-sessions 2 --min-vms 1 --max-vms 3 --warm 1 --memory 4096` are passed
- **THEN** the resolved plan shows those values

### Requirement: The CLI surface golden lists the new commands and options
`testdata/cli/surface.json` SHALL list `agent detect` and the options above, and both the Python CLI and the TypeScript CLI that exists for agents SHALL match it.

#### Scenario: surface
- **WHEN** the surface test runs
- **THEN** the listed options match the CLI
