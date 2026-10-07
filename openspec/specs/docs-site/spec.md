# docs-site Specification

## Purpose
What the published docs site (`docs/site`) must contain per release and how its examples and commands are checked in CI.

## Requirements

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

### Requirement: every documented rayito command exists in the CLI
`scripts/check_docs_examples.py --cli` SHALL check, for every `rayito ...` command in a shell code block (`bash`, `sh`, `shell`, `console`, `zsh`) or in inline code of the docs site, that each subcommand and each `-x`/`--x` option exists at that position in the installed CLI's `typer` tree, accepting synopsis notation (`[--x V]...`, `<marcador>`, `a | b`, `a/b`). CI's docs job SHALL run it.

#### Scenario: a misplaced global option is reported
- **WHEN** a page documents `rayito doctor --json`
- **THEN** the check reports that `rayito doctor` has no option `--json` and exits with 1

#### Scenario: a synopsis is accepted
- **WHEN** a page documents `rayito stack deploy <componente> [--param K=V]... [--yes]`
- **THEN** the check reports no problem

### Requirement: a feature whose runtime code is unmerged is documented without claiming it works

A docs page for a feature whose runtime implementation has not merged
SHALL carry a visible warning stating that the page documents an accepted
design, not shipped behaviour, and every code example that calls the
unmerged API SHALL be excluded from automated execution-style checking
with an HTML comment naming the reason (the existing `noqa` convention of
`scripts/check_docs_examples.py`).

#### Scenario: a page for an unmerged feature is marked and its examples are excluded

- **WHEN** a docs page documents a feature whose runtime change is not
  merged yet (as the agent guide did before `ai-agent-core`,
  `ai-agent-fast-start` and `ai-agent-deepagents` merged)
- **THEN** the page carries the warning, every Python/TypeScript code block
  calling the unmerged API is preceded by a `noqa` comment naming the
  change, and `scripts/check_docs_examples.py --ruff --mypy` does not fail
  on an attribute or import that does not exist in the installed package;
  once the change merges, the warning and the `noqa` comments are removed
