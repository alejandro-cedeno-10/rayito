## ADDED Requirements

### Requirement: AgentDeployment exposes its access token and can pause and resume
`AgentDeployment` and `AsyncAgentDeployment` (and the TypeScript `AgentDeployment`) SHALL expose `access_token` / `accessToken`, the access token of the deployment's sandbox (the one passed to `deploy()`, `RAYITO_ACCESS_TOKEN` or the generated one), and SHALL provide `pause(wait=True)` and `resume(wait=True)` (`pause({ wait })`, `resume({ wait })`) that call the sandbox's `pause` and `resume` and keep the sandbox alive. `pause()` SHALL return `False` when the sandbox was already suspended. The token SHALL never be written to a log, an error or a CLI output.

#### Scenario: reconnect with the returned token
- **WHEN** a caller deploys without `access_token`, reads `deployment.access_token` and calls `AgentDeployment.connect(sandbox_id, access_token=token)` from another process
- **THEN** the second client can invoke the agent and the first can still stop it

#### Scenario: pause and resume
- **WHEN** a caller calls `pause()` then `resume()`
- **THEN** the sandbox's `pause` and `resume` ran once each with `wait` forwarded and no `kill` was called

### Requirement: deploy can keep the sandbox of a failed startup
`AgentProject.deploy` SHALL accept `keep_on_failure` / `keepOnFailure` (`False` by default). With it, a failure after the sandbox was created SHALL NOT terminate the sandbox and SHALL annotate the error with the sandbox id and how to terminate it (never the token); a `BaseException` that is not an `Exception` SHALL still terminate it. It SHALL raise `InvalidArgumentException` before creating anything when no `access_token` (argument or `RAYITO_ACCESS_TOKEN`) is known. `rayito agent deploy --keep-on-failure` SHALL pass it.

#### Scenario: kept on failure
- **WHEN** `deploy(access_token=..., keep_on_failure=True)` fails because the host left a failed marker
- **THEN** no `kill()` ran and the error names the sandbox id

#### Scenario: no token to come back with
- **WHEN** `deploy(keep_on_failure=True)` runs with no access token and no `RAYITO_ACCESS_TOKEN`
- **THEN** it raises `InvalidArgumentException` and creates no sandbox

### Requirement: An HttpApp tells when its own routes are not exposed
`HttpApp` SHALL provide `hides_own_routes` / `hidesOwnRoutes`: `true` when the protocol is not `raw` and `health_path` or `invoke_path` differ from `AGENT_SERVICE_HEALTH_PATH` and `AGENT_SERVICE_INVOKE_PATH`. `rayito agent deploy` SHALL print a warning naming the protocol and the routes, and pointing to `protocol = "raw"`, when it is `true`; the default protocol SHALL NOT change.

#### Scenario: custom routes with the default protocol
- **WHEN** an app with `health_path="/health"` and the default protocol is deployed from the CLI
- **THEN** the CLI prints a warning mentioning `/health` and `protocol = "raw"`

#### Scenario: raw
- **WHEN** the same app uses `protocol="raw"`
- **THEN** no warning is printed
