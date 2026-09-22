## MODIFIED Requirements

### Requirement: Docs site skeleton builds strictly
`docs/site/mkdocs.yml` SHALL configure `mkdocs-material` with the `mkdocstrings` python handler pointed at `clients/python/src` and a nav under `docs/site/docs` that contains, in this relative order (other M7 changes may insert their own pages between them), `index.md`, `quickstart.md`, `cli.md` (the `rayito` command-line tool: install, global options, `image`, `sandbox`, `doctor`, `make` equivalences, non-goals), `concepts.md`, `api.md` (mkdocstrings for `rayito.Sandbox`, `rayito.AsyncSandbox`, the sub-clients, the models and `rayito.e2b`), `security.md`, `limits.md` (including the `## Compatibilidad SDK ↔ rayd ↔ imagen` table kept equal to `rayito.cli._compat.COMPATIBILITY`), `e2b-compat.md` (what works unchanged, what maps with a note, what raises `UnimplementedError` and why, the `timeout`/`set_timeout` difference, the metadata cost), `cost.md` (the measured numbers with their source file: $0.126/h at 2 GB/1 vCPU, ≈ $0.011 per suspend/resume cycle, $0.037/week per image version, ≈ $0.03 per e2e run, 2.36 s to `kernel_ready`, ≈ 1.4 s `pause()`, 0.7–1.6 s auto-resume, 0.65 MB/s write, 6.71 MB/s read, the measured `list(metadata=)` cost per running sandbox) and `verify.md`. `make docs` and a CI `docs` job SHALL build it with `mkdocs build --strict` and no deploy.

#### Scenario: strict build
- **WHEN** CI runs `cd clients/python && uv run --group docs mkdocs build -f ../../docs/site/mkdocs.yml --strict`
- **THEN** the build succeeds with no warnings, the API page renders `rayito.Sandbox.create` and `rayito.e2b.Sandbox` signatures, and `cli/index.html` is produced

#### Scenario: CLI page in the nav
- **WHEN** a reader opens the built site's navigation
- **THEN** "CLI" appears right after "Quickstart" and before "Conceptos"

#### Scenario: compatibility table in the docs
- **WHEN** `docs/site/docs/limits.md` is parsed by `tests/unit/cli/test_compat.py`
- **THEN** the rows under `## Compatibilidad SDK ↔ rayd ↔ imagen` equal `rayito.cli._compat.COMPATIBILITY`
