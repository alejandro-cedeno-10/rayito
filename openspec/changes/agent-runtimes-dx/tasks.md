## 1. Host (`agent_host`)

- [x] 1.1 `diagnostics.py`: `sanitize_diagnostics`, `bounded_tail`, bound from
      `limits.json` (`agentStartupDiagnosticsMaxBytes`).
- [x] 1.2 `ChildProcess(capture_stderr_tail=True)` with a bounded reader thread.
- [x] 1.3 `child_environment` and `child_argv` for `http-app` and `command`; the
      `http-app` working directory is `app_dir`.
- [x] 1.4 Failed marker: code line plus diagnostics; `AgentFailure.diagnostics`.
- [x] 1.5 `usage_reported` in the limit tracker and the service JSON; unknown
      usage from `http-app`, `callable` and `command` text.
- [x] 1.6 `agent` node in the deepagents driver translator.
- [x] 1.7 Regenerate the TypeScript host assets.

## 2. SDKs

- [x] 2.1 `CustomRuntime` host options, `HttpApp.hides_own_routes` (Python, TS).
- [x] 2.2 Marker parsing, `keep_on_failure` / `keepOnFailure`.
- [x] 2.3 `AgentDeployment.access_token`, `pause()`, `resume()` (sync, async, TS).
- [x] 2.4 `usage_reported` on `Done` and `AgentResult`, v1 decoder, service body.
- [x] 2.5 CLI: `agent deploy --keep-on-failure`, routes warning, `tokens=desconocido`,
      `init` scaffolds, `image publish --base-image-version latest`.
- [x] 2.6 Shared vectors: `framework-host-config.json`, protocol goldens,
      scaffold cases, CLI surface.

## 3. Docs and release notes

- [x] 3.1 Guide (es, en): prerequisites, LangGraph and custom tabs, token, pause,
      failed startup, protocol table, usage, contract, key table.
- [x] 3.2 CLI and Configure AWS pages (es, en); CHANGELOG of both packages.

## 4. Acceptance

- [x] 4.1 Unit tests in both SDKs and the real LangGraph smoke
      (`dev/local/agent/runner_smoke.py`).
- [x] 4.2 AWS: the demo agent deployed with the documented minimal config, `/health`
      and `/invoke` from a laptop, pause/resume and reconnect with the returned token.
