## ADDED Requirements

### Requirement: the SDKs expose no gateway-specific exception

Neither SDK SHALL export a gateway-specific exception type
(`GatewayException` in Python, `GatewayError`/`GatewayErrorOptions` in
TypeScript). A request the gateway refuses SHALL reach the sandbox process
only as an HTTP response, and a gateway config `rayd` rejects SHALL surface
as `SandboxException`/`SandboxError`.

#### Scenario: Python public API has no GatewayException

- **WHEN** a caller inspects `rayito.__all__` and `rayito.exceptions`
- **THEN** neither contains `GatewayException`

#### Scenario: TypeScript public API has no GatewayError

- **WHEN** a caller imports the package entry point
- **THEN** it has no `GatewayError` export
