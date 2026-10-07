## MODIFIED Requirements

### Requirement: E2B exception names
`rayito.e2b.exceptions` SHALL re-export these native classes under their names:

- `SandboxException`, `TimeoutException`, `NotFoundException`, `FileNotFoundException`, `SandboxNotFoundException`
- `AuthenticationException`, `InvalidArgumentException`, `RateLimitException`, `CommandExitException`
- `FileUploadException` (a subclass of `TransferException`, itself a `SandboxException` carrying `code` and `reason`, raised when a transfer import fails)
- `GitAuthException` (subclass of `AuthenticationException`) and `GitUpstreamException` (subclass of `SandboxException`)
- `TemplateException` and `BuildException`, the classes `Template.build()` raises, so `except rayito.e2b.BuildException` catches a failed build
- `UnimplementedError`

It SHALL bind the following aliases to the native classes Rayito actually raises:

- `NotEnoughSpaceException` to `DiskFullException`, which is raised for `disk_reserve`/`disk_full`
- `ServiceBusyException` to `CapacityException`, which is raised for `InsufficientCapacityException`

`RayitoCompatWarning` SHALL subclass `UserWarning`.

#### Scenario: except clauses keep compiling
- **WHEN** an E2B program wraps `sbx.commands.run("exit 3")` in `except CommandExitException as e`, imported from `rayito.e2b.exceptions`
- **THEN** the clause catches it with `e.exit_code == 3`, and `rayito.e2b.BuildException` is `rayito.BuildException` and `rayito.e2b.TemplateException` is `rayito.TemplateException`

#### Scenario: capacity and disk errors use the E2B names
- **WHEN** the stubbed control plane raises `InsufficientCapacityException` on `run-microvm`, and the fake `rayd` rejects a write with `RESOURCE_EXHAUSTED` and `disk_reserve`
- **THEN** the first surfaces as an instance of `ServiceBusyException` and the second as an instance of `NotEnoughSpaceException`
