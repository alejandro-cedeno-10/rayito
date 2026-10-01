## Context

`docs/research/2026-10-e2b-out-of-scope.md` measured four E2B-adjacent
features (secrets, a metadata index, SDK-side OpenTelemetry, plus a
control-plane pattern shared by several out-of-scope rows) and, in §8.1,
proposed a single grouped optional stack (`infra/rayito-plane.yaml`) plus an
ADR distinguishing "hosted by Rayito" from "optional in the customer's
account". The architect's cross-group plan narrows that proposal for this
campaign: per-feature templates now (`infra/secrets-access.yaml`,
`infra/metadata-index.yaml`), the grouped stack named only as a future
target — because a shared "extension point" ahead of its first two adapters
would be a speculative abstraction (the repo's hard rule: introduce a port
only in the milestone that ships its adapter, applied here to
infrastructure, not just to Rust ports).

## Goals / Non-Goals

- **Goal**: one written rule for "off by default, explicit option, zero
  extra AWS calls with no options", so `m13-secrets`, `m13-otel-sdk` and
  `m14-metadata-index` do not each invent their own.
- **Goal**: one page (`optional-features.md`) and one gate
  (`test_optional_features_docs.py`) that those three groups extend by
  editing their own row, without touching this change's files again.
- **Non-goal**: implementing any of the three features. This change ships no
  SDK option; `require_module`/`loadOptionalPeer` are called by nobody yet.
- **Non-goal**: the grouped `infra/rayito-plane.yaml` stack. Named as a
  future target in ADR-014, not built.

## Decisions

### D1 — Per-feature CloudFormation templates now, not one grouped stack

The research's own "Matiz propio" already recommended not building the
grouped stack in one shot; the architect's plan goes further and gives each
of the two features that need infrastructure in this campaign (secrets,
metadata index) its own template. Rationale: two independent, single-purpose
templates are easier to review and to deploy selectively than one
multi-parameter stack with pieces most users will not enable; and Secrets
Manager and DynamoDB access have no shared IAM shape worth factoring out at
two data points. `infra/rayito-plane.yaml` stays named in ADR-014 as where a
third or fourth optional component would justify consolidating.

### D2 — `optional-features.md` is a shared page, not a per-group page

One table beats four, because the whole point of ADR-014's cost rule is that
every option looks the same to a user scanning for "what costs money". The
alternative (a page per feature) was rejected: it would hide the fact that
turning on nothing means zero extra cost, which is the one sentence that
matters most and belongs at the top of a single page.

### D3 — The docs gate is data-driven, not row-hardcoded

`test_optional_features_docs.py` parses the table generically (header match,
then one dict per row) instead of asserting on four hardcoded row objects.
This means `m13-secrets`, `m13-otel-sdk` and `m14-metadata-index` can flip
their own "Estado" cell to "disponible (0.5.0)" and the existing
`test_available_rows_cite_their_docstring_marker_in_the_named_sdk_file` test
starts checking their row automatically, with no edit to this test file.
Rejected alternative: one test function per row, added by each group — more
code, and a group could add a passing test without the generic invariant
(symbol next to the docstring marker) actually holding for a sibling row.

### D4 — `require_module` catches only `ImportError`

A module that exists but fails for its own reasons (a genuine bug inside an
already-installed optional package) should propagate as-is; masking it as
"install the extra" would be misleading and would hide a real defect behind
an installation instruction. Same reasoning for `loadOptionalPeer`, checking
the resolution-failure error codes (`ERR_MODULE_NOT_FOUND` /
`MODULE_NOT_FOUND`) rather than catching every rejection.

### D5 — No `describe_optin` helper

The plan explicitly names this as unnecessary: two helpers is the complete
surface this convention needs from code. A third helper describing the
convention in prose would duplicate `optional-features.md` and the ADR
without adding behavior.

## Risks / Trade-offs

- **Four planned rows with no working example yet.** A reader of
  `optional-features.md` today sees "Llega en Mxx" for every cost-bearing
  function. Accepted: the alternative (not publishing the page until the
  first feature ships) would let `m13-secrets` design its opt-in shape
  without a written contract to follow, defeating the point of this change.
- **The generic table parser is stricter than a hand-written one.** Any
  group that reformats a row outside the 11-column shape breaks
  `parse_markdown_table`'s column-count assertion immediately, which is the
  intended fail-fast behavior, not a foot-gun: the table's whole value is
  that every row has the same shape.

## Migration Plan

None: no existing SDK behavior changes. `require_module` and
`loadOptionalPeer` are new, unused surface until a later group calls them.

## Open Questions

None blocking. `infra/rayito-plane.yaml`'s trigger condition ("a third or
fourth optional component") is left for the architect to revisit after M14.
