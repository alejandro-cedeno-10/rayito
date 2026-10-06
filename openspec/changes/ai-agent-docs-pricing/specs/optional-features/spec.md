## ADDED Requirements

### Requirement: a row for a feature whose runtime code is unmerged is marked as a draft, never as available

`docs/site/docs/optional-features.md` SHALL mark any row for a feature
whose SDK runtime code has not merged into `main` with a label containing
"borrador" next to the feature's name, and SHALL NOT state or imply, for
that row, that the option is usable today. Such a row MAY still state its
projected cost and IAM, labelled as an estimate from the accepted design.

#### Scenario: the agent rows are marked as drafts

- **WHEN** `docs/site/docs/optional-features.md` is read while
  `ai-agent-core`/`ai-agent-fast-start`/`ai-agent-deepagents` are not yet
  merged
- **THEN** the rows for the agent template, `sbx.agent` and pool
  warmup/serve each carry "(borrador)" next to their name, and no row
  claims the feature is available
