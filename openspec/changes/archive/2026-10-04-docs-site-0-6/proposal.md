## Why

0.6.0 and 0.6.1 shipped six opt-in features, the `OptionalStack`
convention, the orphan reaper and the `reincarnate()` replay, but the docs
site (`docs/site`, published to GitHub Pages) still surfaced only the 0.5
capabilities on its home page, had no per-version "what's new" page, kept
stale notes ("llega con m15-sizes-catalog", "TemplateException nunca se
lanza"), lacked the receiver side of the webhook signature, background
builds and logs for templates, and the 0.6 options in the TypeScript
reference. `scripts/check_docs_examples.py` checked Python and TypeScript
examples but not the `rayito ...` command lines, so a documented flag that
does not exist (`rayito doctor --json`) went unnoticed.

## What Changes

- New `novedades/` section: an index (versions, how to upgrade, "En
  desarrollo" for EFS volumes and custom domain, never as available) and
  one page per version (0.6.0, 0.6.1) with Python + TypeScript examples.
- Home page, navigation, optional-features summary, E2B parity, images,
  errors, Python and TypeScript references updated for 0.6.x; the feature
  guides gain the missing sections (webhook signature verification in
  Python and Node, background builds/logs and the E2B mapping for
  templates, per-component parameters and `parameter_changes` for stacks).
- `check_docs_examples.py --cli` checks every `rayito ...` command in shell
  blocks and inline code against the installed CLI (`typer` tree); CI and
  `make docs-examples` run it.

## Impact

- Docs: `docs/site/docs/**`, `docs/site/mkdocs.yml`.
- Tooling: `scripts/check_docs_examples.py` (+ tests), `.github/workflows/ci.yml`,
  `Makefile`, the gates reference of the engineering skill.
- No runtime change in the SDKs or `rayd`.
