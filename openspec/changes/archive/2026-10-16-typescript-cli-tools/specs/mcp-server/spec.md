## ADDED Requirements

### Requirement: The TypeScript package ships the same MCP server
The npm package `rayito` SHALL declare a `rayito-mcp` executable that exposes the same six tools (`run_code`, `run_command`, `read_file`, `write_file`, `list_files`, `list_sandboxes`) with the same `readOnly` and `idempotent` annotations, reads the same `RAYITO_MCP_*` variables, keeps one sandbox per process through the same lease semantics, and logs only to stderr and never payloads. The tool names and input schemas SHALL be listed in `testdata/mcp/tools.json`, read by the tests of both SDKs.

#### Scenario: both servers expose the same tools
- **WHEN** each server is listed through an in-memory MCP client
- **THEN** the tool names and input schemas equal `testdata/mcp/tools.json`

### Requirement: The MCP SDK is an optional peer in TypeScript
`@modelcontextprotocol/server` and `zod` SHALL be optional peers loaded with `loadOptionalPeer`; the library entry points SHALL NOT import them. When a peer is missing, the error SHALL carry the exact command that runs the server with the peers.

#### Scenario: a missing peer is explained
- **WHEN** `rayito-mcp` starts without `@modelcontextprotocol/server` installed
- **THEN** it exits non-zero with a message naming the `npx -y -p rayito -p @modelcontextprotocol/server -p zod rayito-mcp` command
