## ADDED Requirements

### Requirement: `rayito dashboard` is off by default and read-only by default
The command SHALL bind to `127.0.0.1` on a random port, use the user's AWS credentials, and make no AWS call and load no optional peer unless it is run. Actions SHALL require `--allow-actions` and UI confirmation.

#### Scenario: no activation, no calls
- **WHEN** the SDK and CLI are used without running `rayito dashboard`
- **THEN** the golden call trace contains no dashboard-related AWS call

#### Scenario: actions refused without the flag
- **WHEN** a kill request reaches the server started without `--allow-actions`
- **THEN** it is rejected and no AWS call is made

### Requirement: The local server defends against browser attacks
The server SHALL check `Host` and `Origin`, protect state-changing requests with a CSRF token, and require a random session token delivered in the start URL.

#### Scenario: rebinding and cross-origin requests are rejected
- **WHEN** a request arrives with a foreign `Host` or `Origin`, or without the session token
- **THEN** it is rejected

### Requirement: The SPA needs no build step
The SPA SHALL be ES modules and web components served from the wheel, use the docs-site design tokens, and support light and dark themes.

#### Scenario: installed wheel serves the SPA
- **WHEN** the dashboard starts from an installed wheel
- **THEN** the SPA is served without Node or a build step

### Requirement: Bulk actions respect platform TPS
Bulk kill, suspend and resume SHALL be queued at or below the applied TPS of each operation and show progress.

#### Scenario: suspend queue honours 2 TPS
- **WHEN** 10 sandboxes are suspended in bulk with a 2 TPS limit
- **THEN** no more than 2 suspend calls start per second
