## 0. Design (`feat/agent-runtimes-byo`)

- [x] 0.1 ADR-028 in `ARCHITECTURE.md`.
- [x] 0.2 This OpenSpec change (proposal, design, tasks, specs), validated with
      `openspec validate agent-runtimes-byo --strict`.

## 1. Lane A: contract (`feat/agent-byo-contract`)

- [x] 1.1 `InstallableRuntime`, `ServableRuntime`, `InstallTarget`,
      `HostConfig`, `ModelWiring` in `_runtime.py` / `runtime.ts`;
      `is_installable` / `is_servable`.
- [x] 1.2 Extract the v1 decoder from `_deepagents.py` / `deepagents.ts` into
      `_protocol_v1.py` / `protocol-v1.ts`; deepagents vectors unchanged.
- [x] 1.3 OpenCode and deepagents implement both optional ports; their
      install steps move from `_template.py` into the adapters.
- [x] 1.4 `AgentTemplate` composes `install_steps()`; accepts runtime objects;
      new optional `app=` (user source copied to `AGENT_APP_DIR`);
      `testdata/agent/agent-template/cases.json` unchanged for the defaults,
      plus new cases.
- [x] 1.5 Registry names `langgraph`, `strands`, `strands-harness`, `hermes`
      (the last three wired once lane B lands; until then
      `UnimplementedError`).
- [x] 1.6 `limits.json` keys of design D8 and `gen_limits.py` output. The
      three requirements-file digests (`agentStrandsRequirementsSha256`,
      `agentStrandsHarnessRequirementsSha256`, `agentHermesRequirementsSha256`)
      are generated from the hashed files of lane B (task 2.2) and arrive with
      them; their constant names are already reserved in `gen_limits.py`. The
      export base image is two keys, `agentExportBaseImage` and
      `agentExportBaseImageDigest`, so the digest fits the line width.
- [x] 1.7 `AgentProject`, `ServeOptions`, `from_dict`/`to_dict`, `load`
      (`tomllib` / `JSON.parse` / optional peer `smol-toml`);
      `testdata/agent-project/` vectors and `schema.json`.
- [x] 1.8 `testdata/agent/limit-vectors.json` read by the SDK `LimitTracker`
      tests.
- [x] 1.9 Public exports in both SDKs, `check_sdk_parity.py` passes,
      `public-exports.test.ts` updated.

## 2. Lane B: frameworks (`feat/agent-byo-frameworks`)

- [x] 2.1 Spike against the pinned versions: Strands `BedrockModel`
      `endpoint_url` with bearer token, harness `builtin_tools` names, Gemini
      base URL; Hermes `/v1/runs` event schema and Bedrock endpoint override.
      Record fixtures in `testdata/agent/strands-*` and
      `testdata/agent/hermes-v0.22.2-events.jsonl`.
- [x] 2.2 Hashed requirements for strands, strands-harness and hermes, with
      the regeneration command in each header; sha256 in `limits.json`.
- [x] 2.3 `StrandsRuntime` (Python and TS adapters) and host drivers
      `strands` and `strands_ts` (with `strands_bridge.mjs`, Node install
      step).
- [x] 2.4 `HermesRuntime` and the `hermes` host driver.
- [x] 2.5 `CustomRuntime`, `HttpApp`, host drivers `callable` and `command`.
- [x] 2.6 Provider matrix in `provider-catalogue.json`; unsupported
      combinations fail before any RPC in both SDKs.
- [x] 2.7 Safe defaults of design D9, each guarded by a unit test.

## 3. Lane C: host and deploy (`feat/agent-byo-deploy`)

- [x] 3.1 `agent_host` core: task and serve transports, limits, SSE, ready
      marker, content-free logging; `deepagents_runner.py` wrapper with
      unchanged vectors.
- [x] 3.2 Host drivers `deepagents`, `opencode` (shared vectors) and
      `http_app`.
- [x] 3.3 `rayito.agent-service/1` contract tests over a loopback socket.
- [x] 3.4 `AgentProject.build/run/deploy` services (Python sync and async,
      TypeScript) over the existing `Template` and `Sandbox` ports.
- [x] 3.5 `AgentDeployment` / `AsyncAgentDeployment` (`url`, `headers`,
      `invoke`, `stream`, `status`, `stop`, `connect`) with the shared SSE
      parser; 403 re-mint once.
- [x] 3.6 `export()` for `docker` and `agentcore`; file-tree snapshots.
- [x] 3.7 Zero-call tests for every new entry point in both SDKs.
- [x] 3.8 Local e2e (Docker + Floci): custom runtime and project flow, and the
      exported container probed on `/ping` and `/invocations`.

## 4. Lane D: CLI, docs and AWS e2e (`feat/agent-byo-cli-docs`)

- [x] 4.1 `rayito agent init|build|run|deploy|export|invoke|status|stop` in
      both CLIs with scaffolds; surface golden regenerated;
      `check_sdk_parity.py --cli` passes.
- [x] 4.2 Guide `guias/despliega-tu-agente.md` and `.en.md` with tabs per
      framework (Strands, Strands harness, Strands TS, Hermes, LangGraph,
      deepagents, OpenCode, custom) and per SDK; updates to the agent guide,
      agent templates, optional features, pricing and nav; "Coste y
      activación" box; `check_docs_i18n.py` and `mkdocs --strict` pass.
- [x] 4.3 `docs/examples/strands_rayito_sandbox.py` (Strands `Sandbox`
      adapter over Rayito, at most 50 lines).
- [x] 4.4 Cost declaration `agent-project.json`; CHANGELOG entries in both
      packages under `## [Unreleased]`.
- [x] 4.5 AWS e2e per runtime in Python and TypeScript (design test plan):
      `run()` and `deploy()` (`test_agent_project_runs.py`,
      `agent-project.e2e.test.ts`), isolation (the agent cannot read the model
      credential, no telemetry env, tool calls and events, teardown:
      `test_agent_project_isolation.py`, `agent-project-isolation.e2e.test.ts`)
      and `rayito agent` from both CLIs (`test_agent_project_cli.py`,
      `agent-project-cli.e2e.test.ts`).
- [x] 4.6 Found by the AWS run: a deployment could not be reopened from
      another process (the CLI never kept the sandbox access token), so
      `deploy` takes `access_token` / `accessToken` and `--token-file`, and
      `stop` needs no token; the deepagents/LangGraph driver imports the
      entrypoint from `app_dir` like the other runtimes.

## 5. Acceptance and release

- [x] 5.1 Gates of every touched language, OpenSpec `--all`, hygiene, pins,
      limits, licences.
- [ ] 5.2 Full regression tier in AWS on top of lane D, within the budget cap;
      inventory before and after; only the run's resources deleted; measured
      cold starts recorded in `AWS_API_NOTES.md`. Moved to the acceptance of the
      0.14.0 release PR (task 5.3): the agent-runtime e2e of both SDKs already
      ran on this change, and the release PR runs the Python e2e, the TypeScript
      e2e and the E2B corpus on the final tree.
- [ ] 5.3 After 0.13.0 is published: release PR for 0.14.0, CI green, merge,
      publish, verify; archive this change.
