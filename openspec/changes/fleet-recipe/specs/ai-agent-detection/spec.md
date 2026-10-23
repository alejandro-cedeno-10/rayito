## ADDED Requirements

### Requirement: OpenCode zero-config uses AGENTS.md and opencode.json permissions
When detection resolves framework `opencode` and the project defines no instructions or permissions of its own, the project builder SHALL do two things:

- read the root `AGENTS.md` into `spec.instructions`;
- map the `permission` block of `opencode.json`/`opencode.jsonc` into `spec.permissions` (per-tool `allow`/`ask`/`deny`, plus `bash` patterns).

Because the run is unattended, `ask` SHALL be applied as `deny`. Unknown permission keys SHALL be ignored. Both SHALL produce warning `opencode_permission_ignored`. The detection report SHALL record evidence for both files and SHALL never include their text. Values from `rayito-agent.toml` SHALL win.

#### Scenario: AGENTS.md becomes the instructions
- **WHEN** an OpenCode project with `opencode.json` and `AGENTS.md` is deployed without `rayito-agent.toml`
- **THEN** the served agent's `AGENTS.md` equals the project's and the report lists `file:AGENTS.md`

#### Scenario: permissions honoured
- **WHEN** `opencode.json` has `"permission": {"edit": "deny", "bash": {"*": "ask", "git status": "allow"}}`
- **THEN** the generated `opencode.json` of the runtime carries `"edit": "deny"` and `"bash": {"*": "deny", "git status": "allow"}`, and the report has warning `opencode_permission_ignored` for `bash`

#### Scenario: the project file wins
- **WHEN** `rayito-agent.toml` sets `[agent] instructions` or `[agent.permissions]`
- **THEN** those values are used and `AGENTS.md` or the `permission` block is not applied for that key
