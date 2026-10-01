## Why

The campaign M11–M14 brings four E2B-adjacent features that need AWS calls or
resources beyond `create/connect/kill` against the Lambda MicroVMs control
plane: secret injection and CRUD via Secrets Manager (M13a), a DynamoDB index
for `list()`/`paginate()` over non-`RUNNING` states (M14), and SDK-side
OpenTelemetry spans (M13b). `SPEC.md` §4 currently excludes both "a
client-side store" and "a control-plane service" outright, and no ADR or
document says how a cost-bearing option must be named, defaulted or turned
off. Without that written first, the four following groups (`m13-secrets`,
`m13-otel-sdk`, `m14-metadata-index`, plus `m12-sizes-proxy` in the same
wave) would each invent their own opt-in shape, their own wording for "this
costs money", and their own answer to "is a customer-account CloudFormation
template allowed here" — exactly the divergence `docs/research/2026-10-e2b-out-of-scope.md`
§8.1 warns against ("un ADR por función" as a rejected alternative).

This change is documentation and two helper modules only. It writes ADR-014
(the customer-account-components distinction and its cost rule), amends
`SPEC.md` §4 and `openspec/project.md` hard rule 3 to match, and publishes
the opt-in convention every later group must follow: `docs/site/docs/optional-features.md`
(the one page listing every cost-bearing option, its exact AWS calls, its
approximate cost and how to turn it off) plus the two minimal helpers
(`clients/python/src/rayito/_optional.py`, `clients/typescript/src/optional.ts`)
that let a later group load an optional dependency without ever importing it
when the feature is off.

## What Changes

- **New ADR-014** in `ARCHITECTURE.md` ("Componentes opcionales en la cuenta
  del cliente"): never a Rayito-hosted service; optional components in the
  customer's own AWS account are allowed, shipped as independent
  CloudFormation templates under `infra/` (this campaign:
  `infra/secrets-access.yaml`, `infra/metadata-index.yaml`; a single grouped
  stack `infra/rayito-plane.yaml` is named as a future target, not created
  now); three invariants (create/connect/kill work without them, `list-microvms`
  stays the only source of truth for sandbox state, no data leaves the
  account); the cost rule (every AWS-quota-or-money feature is off by
  default and turned on only by an explicit SDK option — never an
  environment variable, a config file or a global setter — with zero extra
  AWS resources/calls with no options set, matching 0.4.0); the opt-in
  convention (below); phase-1 secrets are readable by sandbox code
  (documented, with the untrusted-code recommendation); S3 volumes approved
  for a later milestone, out of scope here. One-line cross-reference from
  "Plano de control (boto3 / SDK JS v3)".
- **`SPEC.md` §4 amendment**: "Servicio de plano de control" bullet now
  distinguishes "hospedado por Rayito: nunca" from "componentes opcionales
  ... permitidos según ADR-014"; the metadata bullet gains "sin almacén del
  lado cliente obligatorio; índice DynamoDB opcional ... (ADR-014, M14)"; the
  size bullet gains the `limits.md` pointer and drops any promise of a
  per-sandbox size resolver; a new bullet approves S3 volumes for a later
  milestone. The "Templates declarativos" bullet is untouched (no ADR-015 in
  this campaign). `openspec/project.md` hard rule 3 gains the same ADR-014
  carve-out for its "no control-plane service" clause, nothing else relaxed.
- **New page `docs/site/docs/optional-features.md`** ("Funciones opcionales y
  su coste"), in `mkdocs.yml` nav next to "Modelo de costes", linked from
  `cost.md` and `index.md`: the three-sentence ADR-014 summary, the
  "Coste y activación" docstring-block template, one table row per
  cost-bearing function (`secrets-injection`, `secrets-crud`,
  `metadata-index`, `otel-sdk`, all starting "planificado (Mxx)") and a
  separate "Sin coste AWS" section for `rayito sandbox proxy` (written once,
  complete, because no later group touches this page for that row).
- **Two opt-in helpers, the only code in this change**:
  `clients/python/src/rayito/_optional.py` (`require_module`, importlib-based,
  raises `InvalidArgumentException` naming the `pip install rayito[<extra>]`
  hint on `ImportError`) and `clients/typescript/src/optional.ts`
  (`loadOptionalPeer`, dynamic `import()`, rejects with `InvalidArgumentError`
  naming the npm package on a resolution failure). Neither is wired into any
  sandbox module; neither imports anything optional at module load time.
- **`MILESTONES.md`**: acceptance sections for M11–M14, each stating "sin
  opciones = comportamiento de 0.4.0" as an explicit criterion, with an e2e
  cost budget (≤ $0.10 each, M11 at $0 since it ships no SDK option).
- **New gate `scripts/tests/test_optional_features_docs.py`**: pins the
  ADR-014 wording in `ARCHITECTURE.md`, the `SPEC.md` §4 and
  `openspec/project.md` amendments, the new page's presence in nav and its
  table header, one row per function with a stable anchor, and — for any row
  a later group marks "disponible" — that the option symbol it cites appears
  next to the "Coste y activación" marker in the SDK file its "Dónde" column
  names. This lets later groups (`m13-secrets`, `m13-otel-sdk`,
  `m14-metadata-index`) extend the same page without touching this test
  file.

**Not in this change.** No sandbox module (`create`, `list`, `commands`,
`run_code`, …) changes; no `infra/*.yaml` is created (that is each
feature group's own deliverable); `docs/site/docs/e2b-parity.md`,
`e2b-compat.md`, `SECURITY.md` and `AWS_API_NOTES.md` are untouched (owned by
`m12-sizes-proxy`, `m13-secrets`, `m14-metadata-index`, `m13-otel-sdk` in
their own waves); no `ConfigureSandbox` RPC, `SuspendBudget` or credential
broker (§8.2 foundations, deferred); nothing in `crates/`.

## Capabilities

### New Capabilities
- `optional-features`: the opt-in convention itself (ADR-014's cost rule and
  five-part convention), the `optional-features.md` page contract (table
  shape, per-row docstring-marker gate), and the two helper functions
  (`require_module`, `loadOptionalPeer`). Later groups add a `Requirement`
  here only if they change the convention itself; flipping their own table
  row to "disponible" is covered by the existing per-row requirement, not a
  new one.

### Modified Capabilities
None — ADR-011/012/013 and the other existing capabilities are unaffected;
`SPEC.md` §4's non-goals list and `openspec/project.md` hard rule 3 are
project governance, not tracked as a capability spec in this repo (see
`architecture-docs` for the precedent of ADR text living outside a
dedicated capability, and `m9-egress-policy`/`m10-*` for §4 edits that
likewise carry no capability delta).

## Impact

- New files: `docs/site/docs/optional-features.md`,
  `clients/python/src/rayito/_optional.py`,
  `clients/python/tests/unit/test_optional.py`,
  `clients/typescript/src/optional.ts`,
  `clients/typescript/tests/unit/optional.test.ts`,
  `scripts/tests/test_optional_features_docs.py`.
- Edited: `ARCHITECTURE.md` (new ADR-014, one-line cross-reference),
  `SPEC.md` (§4), `openspec/project.md` (hard rule 3), `MILESTONES.md`
  (M11–M14 sections), `docs/site/mkdocs.yml` (nav), `docs/site/docs/cost.md`
  ("Reglas prácticas"), `docs/site/docs/index.md` (one line).
- Behaviour: none. No Rust, no `.proto`, no AWS call, no runtime dependency
  added (both helpers use only the standard library plus the SDK's existing
  error classes).
- Gates: `cd clients/python && uv run pytest tests/unit -q` grows by 5 tests
  (`test_optional.py`); `cd clients/typescript && pnpm test` grows by 3
  (`optional.test.ts`); `cd clients/python && uv run pytest ../../scripts/tests -q`
  grows by 9 (`test_optional_features_docs.py`); `uvx ruff check scripts`,
  `python3 scripts/check_pins.py`, `python3 scripts/check_hygiene.py` and
  `mkdocs build --strict` stay green.
- Acceptance is local: no image publish, no e2e, no AWS credentials — this
  group ships no SDK option to exercise against real AWS.
