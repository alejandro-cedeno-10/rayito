## Why

`sbx.agent` (`ai-agent-core`, `ai-agent-deepagents`) is on `main` with unit tests over fakes only. Nothing runs it end to end against a real model with egress closed, so the properties the design relies on (the gateway as the only exit, the credential unreadable, abort and limits that really stop the agent, the event stream shape, telemetry off by default) are untested for both runtimes. A first local run against Amazon Bedrock found four defects the fakes could not show:

1. `abort()` killed the runtime's process group, but OpenCode and deepagents start every shell-tool command in its own session (`setsid`), so a `sleep` or a server launched by the agent survived the abort.
2. `max_steps` and `token_budget` ended the stream but left the runtime working (and spending tokens) in the background, holding the run lock.
3. A timeout enforced by `rayd` ended the stream as `runtime_error`: iterating a `CommandHandle` does not raise on the timeout end status, only `wait()` does, so the `TimeoutException` branch never ran outside the fakes.
4. The deepagents runner built `LocalShellBackend(root_dir=workdir)` in its default virtual mode, so `write_file("/home/user/x")` wrote `/home/user/home/user/x` while the shell tool and OpenCode saw the real path.

## What Changes

- New `local`-marked tests in both SDKs (`clients/python/tests/local/test_local_agents.py`, `clients/typescript/tests/local/agents.local.test.ts`) that drive `sbx.agent` with `bedrock_gateway`/`bedrockGateway` against the agent guest and a real Bedrock model, for OpenCode and deepagents: tool use and session continuation, the event stream, `abort()`, `max_steps`, `token_budget` and `timeout` leaving no runtime (and, for abort and limits, no tool) process behind, direct egress closed while the model answers, no telemetry variables and no credential in the agent's environment, and the credential readable nowhere in the sandbox. A strict expected failure records that the timeout does not yet reach the shell tool's processes.
- `make local-agent-up` and `make local-bedrock-key`; the runner declares `RAYITO_LOCAL_BEDROCK_KEY_FILE` and passes `RAYITO_E2E_BEDROCK_MODEL`/`RAYITO_E2E_BEDROCK_REGION`. The agent guest installs the deepagents runner where `AgentTemplate` does. Without the key file the tests skip, so `make local-e2e` and the `local-e2e` workflow stay credential-less.
- SDK fixes (Python sync and async, TypeScript, same behaviour): `stop_tree_command`/`stopTreeCommand` before killing the handle on abort and on an SDK limit; drain the handle after an SDK limit; map the timeout end status to `reason="timeout"`. Runner: `LocalShellBackend(root_dir=workdir, virtual_mode=False)`. The requirement text lives in the still-open `ai-agent-core` and `ai-agent-deepagents` changes.
- Docs: the local testing guide and the agent guide.

## Impact

- Specs: `local-testing` (ADDED); `ai-agent-runtime` through the open `ai-agent-core` and `ai-agent-deepagents` changes.
- Cost: only when a maintainer runs `make local-bedrock-key`: some sixty Bedrock Haiku 4.5 calls per run of both suites (cents). No AWS resource is created; the key is a locally presigned URL.
- No `rayd` or proto change. Follow-up: `rayd` should kill the whole process tree on a command timeout (today only the group).
