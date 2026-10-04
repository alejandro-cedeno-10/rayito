## ADDED Requirements

### Requirement: the docs site has a what's-new page per release
`docs/site/docs/novedades/` SHALL contain an index and one page per minor or patch release from 0.6.0 on, each listed in the `Novedades` section of `docs/site/mkdocs.yml`. Each release page SHALL show every public option or class the release adds with a Python and a TypeScript example, the behaviour changes of its fixes and how to upgrade. Features whose change is not merged SHALL appear only under "En desarrollo", never as available.

#### Scenario: 0.6.x pages exist and build strictly
- **WHEN** `mkdocs build --strict` runs on `docs/site/mkdocs.yml`
- **THEN** `novedades/0.6.0/` and `novedades/0.6.1/` are built, and the volumes and custom-domain options appear only under "En desarrollo"

### Requirement: every documented rayito command exists in the CLI
`scripts/check_docs_examples.py --cli` SHALL check, for every `rayito ...` command in a shell code block (`bash`, `sh`, `shell`, `console`, `zsh`) or in inline code of the docs site, that each subcommand and each `-x`/`--x` option exists at that position in the installed CLI's `typer` tree, accepting synopsis notation (`[--x V]...`, `<marcador>`, `a | b`, `a/b`). CI's docs job SHALL run it.

#### Scenario: a misplaced global option is reported
- **WHEN** a page documents `rayito doctor --json`
- **THEN** the check reports that `rayito doctor` has no option `--json` and exits with 1

#### Scenario: a synopsis is accepted
- **WHEN** a page documents `rayito stack deploy <componente> [--param K=V]... [--yes]`
- **THEN** the check reports no problem
