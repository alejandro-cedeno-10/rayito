## Decisions

- **D1 — One page per version under `novedades/`.** Diátaxis keeps
  reference (CHANGELOG, included verbatim in `referencia/changelog.md`)
  separate from the user-facing "what changes for you" pages, as E2B,
  Vercel and Stripe do with changelog vs. upgrade notes.
- **D2 — Unmerged features are only "En desarrollo".** EFS volumes and the
  custom domain keep their pages (owned by their own changes) under an "En
  desarrollo" nav group and are never listed as available.
- **D3 — The CLI check reads the real `typer` tree.** `load_cli_tree()`
  walks `typer.main.get_command(app)`; no hand-written list of commands.
  Synopses (`[--x V]...`, `<marcador>`, `a | b`, `a/b`) are accepted; only
  subcommands and `-x`/`--x` options are checked, global options only at
  the root position (where `typer` accepts them). Output text
  (`rayito doctor: 9 OK`) is skipped because its first word ends in `:`.
- **D4 — Off by default in the script, on in CI.** `--cli` needs the CLI
  installed (`rayito[cli]` or the `dev` group); without it the flag exits
  with 2 like `--ruff`/`--mypy`, and CI's docs job passes it.
