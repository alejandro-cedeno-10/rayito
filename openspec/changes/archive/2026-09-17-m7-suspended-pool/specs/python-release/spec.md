## MODIFIED Requirements

### Requirement: Docs site skeleton builds strictly
`docs/site/mkdocs.yml` SHALL configure `mkdocs-material` with the `mkdocstrings` python handler pointed at `clients/python/src` and a nav of twelve pages under `docs/site/docs`, in this order: `index.md`, `quickstart.md`, `cli.md` (`m7-cli`), `concepts.md`, `api.md` (mkdocstrings for `rayito.Sandbox`, `rayito.AsyncSandbox`, the sub-clients, `rayito.SandboxPool`, `rayito.AsyncSandboxPool`, `rayito.PoolConfig`, the pool stats and backends, the models and `rayito.e2b`), `security.md`, `limits.md`, `e2b-compat.md` (what works unchanged, what maps with a note, what raises `UnimplementedError` and why, the `timeout`/`set_timeout` difference, the metadata cost), `mcp.md` (`m7-mcp-server`), `cost.md` (the measured numbers with their source file: $0.126/h at 2 GB/1 vCPU, ≈ $0.0049 per suspend/resume cycle, $0.037/week per image version, ≈ $0.03 per e2e run, the measured create/resume/first-cell timings, 0.65 MB/s write, 6.71 MB/s read, the measured `list(metadata=)` cost per running sandbox, and one row for an idle pool slot linking `pool.md`), `pool.md` ("Pool de sandboxes": the suspended-slot pool, its API, semantics, backends, custody of the secret, the measured `T_take`/`T_create` and the per-slot cost table) and `verify.md` (release verification). `make docs` and a CI `docs` job SHALL build it with `mkdocs build --strict` and no deploy.

#### Scenario: strict build
- **WHEN** CI runs `cd clients/python && uv run --group docs mkdocs build -f ../../docs/site/mkdocs.yml --strict`
- **THEN** the build succeeds with no warnings, and the API page renders `rayito.Sandbox.create`, `rayito.SandboxPool.take` and `rayito.e2b.Sandbox` signatures

#### Scenario: pool page in the nav
- **WHEN** a reader opens the built site's navigation
- **THEN** "Pool de sandboxes" appears right after "Modelo de costes" and before "Verificar una release", and `cost.md` links to `pool.md` from its pool row
