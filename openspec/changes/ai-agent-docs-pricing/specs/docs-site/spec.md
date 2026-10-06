## MODIFIED Requirements

### Requirement: the docs site has a what's-new page per release

`docs/site/docs/novedades/` SHALL contain an index and one page per minor
or patch release from 0.6.0 on, each listed in the `Novedades` section of
`docs/site/mkdocs.yml`. Each release page SHALL show every public option
or class the release adds with a Python and a TypeScript example, the
behaviour changes of its fixes and how to upgrade. Features whose change
is not merged SHALL appear only under "En desarrollo", never as available.
A draft page for an unreleased, unmerged version SHALL carry an explicit
warning that it is a draft describing an accepted design, not a published
release, and SHALL NOT be reachable from `novedades/index.md`'s version
grid in a way that could be mistaken for the current version (it SHALL be
visually distinct — e.g. "borrador" instead of a version tag with "actual").

#### Scenario: 0.6.x pages exist and build strictly

- **WHEN** `mkdocs build --strict` runs on `docs/site/mkdocs.yml`
- **THEN** `novedades/0.6.0/` and `novedades/0.6.1/` are built, and the
  volumes and custom-domain options appear only under "En desarrollo"

#### Scenario: an unmerged version's draft is marked as a draft

- **WHEN** `docs/site/docs/novedades/0.8.0.md` documents the design of
  `ai-agent-core`/`ai-agent-fast-start`/`ai-agent-deepagents` before any of
  the three is merged
- **THEN** the page's own content states it is an unpublished draft of an
  accepted design, and `novedades/index.md` shows it with a "borrador"
  label distinct from the "actual" label of the real current release

### Requirement: a feature whose runtime code is unmerged is documented without claiming it works

A docs page for a feature whose runtime implementation has not merged
SHALL carry a visible warning stating that the page documents an accepted
design, not shipped behaviour, and every code example that calls the
unmerged API SHALL be excluded from automated execution-style checking
with an HTML comment naming the reason (the existing `noqa` convention of
`scripts/check_docs_examples.py`).

#### Scenario: the agent guide is marked as a draft and its examples are excluded

- **WHEN** `scripts/check_docs_examples.py --ruff --mypy` runs over
  `docs/site/docs/guias/agente-en-el-sandbox.md`
- **THEN** every Python/TypeScript code block calling `sbx.agent` is
  preceded by a `noqa` comment naming the unmerged change, and the check
  does not fail on an attribute or import that does not exist in the
  installed package
