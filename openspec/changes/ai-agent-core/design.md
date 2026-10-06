## Context

The spike (`docs/research/2026-10-agent-spike.md`, PR #131) ran OpenCode
1.18.34 and a deepagents graph inside a `rayito-base-caps` sandbox, with
Bedrock reached through a `SecretGateway` and the egress closed. Three
offline experiments on 2026-10-06 (OpenCode in a container with
`--network none` and a fake Bedrock server) settled the open questions
below. This change keeps the hexagonal split: a pure domain shared by both
SDKs, a pure `AgentRuntime` port, one adapter per runtime and a thin
application layer on `sbx.agent`.

Facts this design relies on (F-numbers are the stage's design notes):

- F1: `opencode serve` loads its config per directory instance, lazily, on
  the first request for that directory; a config rewritten after warm-up is
  picked up by a new `--dir`.
- F2: `PATCH /config` returns 200 but changes nothing when
  `OPENCODE_CONFIG` is set.
- F4: without `--title`, OpenCode makes an extra model call to generate a
  title with a different model.
- F5: on a model 5xx OpenCode retries forever and never exits.
- F6: `run --format json` emits one JSON object per line
  (`step_start`, `step_finish` with `tokens`, `text` once complete,
  `reasoning`, `tool_use`, `error`).
- F7: under `--attach` the exit code does not reflect `session.error`.
- F8: the prompt is read from stdin when it is not a TTY.
- F9: `--auto` answers "once" to every permission request; actions are
  `ask|allow|deny`; agent config takes `steps`, `mode`, `prompt`, `model`
  and `permission`.
- F13/F14: Haiku 4.5 and Sonnet 4.5 per-token prices (price list,
  2026-09-30) and Bedrock prompt caching minimums (4,096 tokens per
  checkpoint for Haiku 4.5, 1,024 for Sonnet 4.5; `inputTokens` excludes
  cached tokens).

## Decisions

- **D1 — Capability names use the `ai-agent-` prefix.** "agente" already
  means `rayd` in this repo (`agent_ready`, `agent-wire-messages`). Specs,
  ADR and docs say "agente de IA".
- **D2 — Credentials only through a gateway.** No type has a key or headers
  field; `AgentModel.gateway` and `McpRemote.gateway` name an entry of
  `sbx.gateways`. `provider` is a reserved `raw_config` key, so a caller
  cannot point the runtime at an upstream outside the gateway. A missing
  gateway raises `InvalidArgumentException` before any RPC
  (`AgentSpec.require_gateways`).
- **D3 — Permissions have no `ask`.** The run is headless: `--auto`
  answers every request, and an `ask` would hang until the timeout. `ask`
  raises `InvalidArgumentException`. `question`, `webfetch` and
  `websearch` are denied below the caller's entries. Permissions are not a
  security boundary (T29).
- **D4 — Hard limits enforced by the SDK, defaults bound the cost.**
  `max_steps=50` (≈ $0.02 per uncached 15k-token Haiku step, F13),
  `timeout=600 s` (10 min of a 2 GB VM ≈ $0.021; mandatory because of F5),
  `max_total_tokens=1_000_000` (worst case ≈ $1.10–5.50 at Haiku regional
  prices; `None` turns it off) and `max_output_bytes=16 MiB` (below
  `COMMAND_OUTPUT_MAX_BYTES`). OpenCode's own `steps` is soft (it forces a
  text answer), so the SDK aborts with `max_steps` when step
  `max_steps + 1` starts. The token budget is checked after each
  `StepFinished`, so it can overshoot by one step.
- **D5 — A closed failure taxonomy with fixed messages.** `reason` is one
  of `model_error, runtime_error, runtime_missing,
  runtime_version_mismatch, protocol_error, timeout, max_steps,
  token_budget, output_limit, aborted, busy`. The message comes from a
  fixed table keyed by `reason`; the only variable part is `detail_code`,
  kept only when it matches `[A-Za-z0-9_.-]{1,64}` (an error class name
  such as `APIError`), so provider text, prompts and content never reach a
  message, a log or a span.
- **D6 — Events are a discriminated union shared with the runner.** `type`
  is one of `text_delta, text, reasoning, tool_call, step_started,
  step_finished, agent_failed, done`; the deepagents runner emits the same
  names. `TokenUsage.input` excludes cached tokens (F14) and `total` sums
  the five counters. OpenCode's own `cost` is not surfaced: it uses
  models.dev prices, which can be wrong for regional profiles.
- **D7 — Gateway presets restrict `allow` to the chosen models.**
  `bedrock_gateway` allows `POST /model/<id>/converse-stream` and
  `/converse` for each model, with the id percent-encoded as the clients
  send it (`:` → `%3A`). Verified in code: `rayd` compares the raw path
  (`AllowRule::matches` is a string compare after `path_is_safe`) and
  `%3A` decodes to `:`, which `path_is_safe` accepts, so no `/model/*`
  fallback is needed. An ARN model id is rejected (its `/` would be `%2F`,
  which `path_is_safe` forbids). `anthropic_gateway` allows
  `POST /v1/messages` with `x-api-key`; `openai_compatible_gateway` allows
  `POST <base_path>/chat/completions` with `authorization`.
- **D8 — Shared constants live in `limits.json`.** The run defaults, the
  event-line and tool-preview caps, the in-VM paths (`DEFAULT_AGENT_WORKDIR`,
  `AGENT_STATE_DIR`, the manifest path and schema), the serve port and
  session title (F4), the protocol version, the minimum memory (2048 MiB,
  spike RSS 575 MiB) and the OpenCode and ripgrep pins (version and the
  sha256 of the release asset, from the spike) are flat keys, so the two
  SDKs and the template share one definition. The deepagents requirements
  pin moves with its file in `ai-agent-deepagents`; `check_pins.py`
  validation of the pins is part of `ai-agent-fast-start`, which bakes
  them. `gen_limits.py` now wraps a TypeScript constant that does not fit
  in 100 columns the way Biome does.
- **D9 — One run at a time per sandbox, attach when a server is warm.** The
  run script takes `flock -n AGENT_STATE_DIR/run.lock` (failure: `busy`),
  attaches to a running `opencode serve` when `attach="auto"` and its
  health check answers, disposes the instance when the config hash changed
  (F1, F2), always passes `--title` (F4), and reads the prompt from stdin
  (F8). `finish()` returns `Done` only on exit 0 without an `error` event
  (F7). The serve password goes through the environment, never argv.
- **D10 — Telemetry only with the existing opt-in tracer.** One span
  `rayito.agent.run` with `gen_ai.*` and `rayito.agent.*` attributes from
  an allow-list; never prompt, text, tool arguments or output.

## Risks / Trade-offs

- Permissions are advisory: `--auto` and `LocalShellBackend` run whatever
  the model asks (T29). The boundary is the MicroVM with egress closed.
- Code in the sandbox can call the gateway directly, outside the SDK token
  budget. Mitigation: `allow` restricted to the model paths and
  `rate_per_minute` (T29).
- The token budget can overshoot by one step (D4); documented.

## Open questions (verified before archive)

- `/instance/dispose?directory=` semantics; fallback: a per-config workdir
  alias.
- Whether a managed background process (serve) survives `close()` of its
  handle and a suspend/resume cycle (measured in the AWS acceptance stage).
