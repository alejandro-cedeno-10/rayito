## Why

`ai-agent-core`, `ai-agent-fast-start` and `ai-agent-deepagents` design an
agent runtime (`sbx.agent`) that runs OpenCode or deepagents inside a
sandbox, a template that bakes them in, and a pool warmup/serve mechanism
that avoids paying OpenCode's first `exec` on every take
(`docs/research/2026-10-agent-spike.md`). None of the three is merged yet.
Documentation-site convention (`openspec/specs/docs-site`) requires that a
feature whose change is not merged appear only under "En desarrollo", never
as available; this change writes the docs site pages for the agent surface
under that rule — a full guide, a template page, reference updates, a
pricing worked example and a pool section — so a reader can evaluate the
accepted design, its cost model and its security posture before the code
lands, and so the real pages need only a search-and-replace of the warning
boxes once `ai-agent-core`/`ai-agent-fast-start`/`ai-agent-deepagents`
merge.

## What Changes

- **New guide**: `docs/site/docs/guias/agente-en-el-sandbox.md` — a
  "Definir el `AgentSpec`" section (Python, Python async, TS tabs) with a
  full end-to-end example: `SecretStore.create`, a `bedrock_gateway`/
  `anthropic_gateway`/`openai_compatible_gateway` preset instead of a
  hand-built `allow=`, `AgentModel`/`AgentSpec`/`AgentPermissions`/
  `AgentLimits`, a field table (including `RESERVED_CONFIG_KEYS` and where
  `instructions` is written, `AGENT_STATE_DIR/<sha>/AGENTS.md`), then
  gateway setup, `create(allow_internet_access=False, gateways=...)`,
  `run`/`stream`, sessions, `abort()`, limits,
  permissions-are-not-a-boundary, the four fast-start options (A–D) with
  their measured/estimated cost, the deepagents entry point, and the E2B
  shim note. Python and TypeScript tabs throughout; every code block that
  calls the not-yet-merged API is marked `<!-- noqa: example: ... -->` for
  `check_docs_examples.py`. The flagship example restricts the gateway
  allowlist to the exact models used (`bedrock_gateway(..., models=[...])`),
  never `/model/*`, matching `security.md` T29.
- **New template page**: `docs/site/docs/funciones-opcionales/templates-de-agente.md`
  — `AgentTemplate`, its pins, prefetch, `--no-deepagents`/`--no-prefetch`,
  and a "Coste y activación" box with the build and per-version storage
  cost from the spike's measured sizes. Every CLI example passes
  `--bucket`/`--name` to match `rayito template build`'s required option.
- **`cost.md`**: rewritten in place, nav label renamed to "Precios
  (MicroVMs, pool, agentes)". A "Componentes del precio" section (compute,
  snapshot read/write, storage with its one-week minimum, the 8 h
  `maximumDuration` including suspended time and why the pool recycles at
  ≈ 7 h, data transfer), each price tagged List or Measured, an "Referencias
  oficiales" box (aws.amazon.com/lambda/pricing, the `AWSLambda` price-list
  offer dated 2026-10-01, and the microvms-images-snapshots/
  microvms-how-it-works/gettingstarted-limits doc pages, consulted
  2026-10-06) alongside the exact price-list read/write/storage values. A
  new "Coste de un agente: VM frente a modelo" section with dated Bedrock
  prices (Haiku 4.5 / Sonnet 4.5, regional and global, consulted
  2026-10-06), the prompt-caching break-even, and the worked 10-step
  example with the per-row arithmetic spelled out (tokens written/read per
  step, the VM formula `300 s × $0,1261/h + launch $0,0014`) showing the
  model costs ≈ 7–21× the VM.
- **`pool.md`**: a new "Calentamiento (`warmup`) y servidor residente"
  section with the C/D cost table and the per-slot/per-cycle numbers,
  plus the two open design gates (G1 prefetch-by-default, G2
  serve-recommended) pending AWS measurement.
- **`referencia/errores.md`**: `AgentException`/`AgentError` added to both
  hierarchy trees and the full table, plus a dedicated section with the
  closed `reason` list.
- **`optional-features.md`**: rows for the agent template, `sbx.agent`
  itself and pool warmup/serve, each marked "(borrador)".
- **`security.md`**: a new "Agente de código dentro del sandbox" section
  describing the accepted T29/T30 content (permissions are not a security
  boundary; the runtime supply-chain pins recorded, not SDK-verified) as a
  draft, since those identifiers do not exist yet in `SECURITY.md` — that
  file is `ai-agent-core`'s to write, not this change's.
- **`e2b-compat.md`**: `agent` is *not* a row of the `UnimplementedError`
  table (the E2B shim does not change for this design: `sbx.agent` simply
  doesn't exist on `rayito.e2b.Sandbox`, an `AttributeError`, not an
  `UnimplementedError`) — a note box outside that table instead, pointing
  at `rayito.Sandbox.connect()` as the workaround.
- **`novedades/0.8.0.md`** (new, explicitly marked as an unpublished draft)
  and a draft card in `novedades/index.md`, plus a separate "Diseñado, sin
  código todavía" list (not the "En desarrollo" table, which claims every
  row raises `UnimplementedError` today).
- **`mkdocs.yml`**: nav entries for the two new pages and the draft
  novedades page, each labelled "(borrador)".
- **Gates**: `mkdocs build --strict` and
  `check_docs_examples.py --ruff --mypy --cli` green on this branch;
  `openspec validate --all --strict` green for this change.

## Non-blocking follow-ups

- `referencia/python/*.md` and `referencia/typescript.md` are
  mkdocstrings-generated from the installed package; they cannot document
  `sbx.agent` until `ai-agent-core` actually lands the code. The guide page
  is the complete API reference until then.
- `e2b-parity.md`'s 113-row numbered table is not touched: inserting a row
  without renumbering risks breaking its existing cross-references: left
  for whoever lands `ai-agent-core` to renumber once the real row count is
  known.
- `SECURITY.md` T29/T30 and `ARCHITECTURE.md` ADR-025 are `ai-agent-core`'s
  files per the design's file ownership; this change only links to them
  (anchors validated as `warn`, not `error`, in `mkdocs.yml`) and inlines
  their content on the docs site so the site is self-contained today.

## Impact

- Affected specs: `docs-site` (MODIFIED), `optional-features` (MODIFIED).
- Affected code: none. Only `docs/site/**` and `docs/site/mkdocs.yml`.
