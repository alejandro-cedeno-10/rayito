## Context

The host (`agent_host`) is stdlib-only Python copied into the agent image; the
Node bridge is a package asset next to it. Both SDKs generate the same image
steps from the same pins, so each rule is written once in `limits.json` and
checked against shared vectors.

## Decisions

### D1. Supervision lives in the host, in `process.py`
`RestartBudget` is pure (a clock passed in, `record_death(now)` returns the wait
or `None`); `Supervisor` owns one child, a monitor thread and the states
`running`, `restarting`, `failed` and `stopped`. The first start is synchronous
and a failure is the existing `app_exited` / `app_not_ready` with diagnostics:
a mis-configured app is not restarted. A restarted child that does not become
ready counts as another death. The driver exposes `availability()` and
`add_failure_listener()`; the service never imports a driver.

### D2. The queue is a ticket FIFO on a `Condition`
`Service.acquire()` returns `acquired`, `busy` (no slot and the queue is full or
off) or `queue_timeout`. A new request never overtakes one that waits. `/ping`
stays `HealthyBusy` while requests wait because capacity is what it reports.
`host.json` values are clamped to `limits.json` ceilings.

### D3. `json` forwards, `agentcore` does not
`HttpApp.hides_own_routes` is false for `json` (and `raw`). Only detection will
choose `json`; an explicit `HttpApp` keeps `agentcore`, so nothing changes for
existing projects.

### D4. One Node bridge, adapters by data
`config.adapter` selects the adapter (`strands`, `langgraph`, `vercel-ai`,
`callable`); `strands-ts` stays as a host alias for `node-callable`. Entrypoints
are `path` or `path#export`. Adapters read duck-typed shapes (LangChain messages,
AI SDK stream parts) so they work with LangGraph.js, deepagents-js and the AI SDK
without importing them. The bridge is a process per request, so a framework
without its own memory (the AI SDK, a graph with no checkpointer) gets a
session file of `{ role, content }` turns, bounded by `HISTORY_MAX_MESSAGES`.
Tool permissions are only enforced by the `strands` adapter: the other
frameworks run their tools inside user code.

### D5. HTTP/1.1 for the AWS SDK behind the gateway
The JavaScript AWS SDK client of Bedrock always builds an HTTP/2 handler and the
gateway listener only serves HTTP/1.1. When `AWS_ENDPOINT_URL_BEDROCK_RUNTIME`
is set (the host sets it for a gateway), the bridge replaces `NodeHttp2Handler`
by `NodeHttpHandler` in every `@smithy/node-http-handler` copy that loads, the
recipe the Strands documentation gives for proxies. Without a gateway nothing
changes.

### D6. The install plan is pure and shared
`_install_plan.py` and `install-plan.ts` compose `TemplateStep`s from pinned
builds in `limits.json`. A project's ecosystem comes from `package_manager`.
With a venv that already has Rayito's pins (deepagents, Strands) dependencies are
added without removing anything (`create_venv=False`, `uv sync --inexact`).
Hermes uses the same CPython download. `testdata/agent/install-plans/` is read by
both SDKs.
