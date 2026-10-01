## 1. [adr] ADR-014 in ARCHITECTURE.md (plan A1)

- [x] 1.1 Read `docs/research/2026-10-e2b-out-of-scope.md` §2 (secrets), §4
      (size), §5 (control-plane paused-by-metadata/events), §6 (OTel), §7
      (custom domain), §8.1 (the ADR-014 proposal and the "rayito-plane"
      grouped-stack idea) end to end
- [x] 1.2 Write `## ADR-014 — Componentes opcionales en la cuenta del
      cliente` after ADR-013 (ends at ARCHITECTURE.md:1618), Contexto /
      Decisión (7 numbered rules) / Consecuencias / Alternativas descartadas
      / Reversible, matching the architect's seven approved decisions
      (never hosted; per-feature CloudFormation templates now,
      `rayito-plane.yaml` deferred; three invariants; the cost rule; the A4
      opt-in convention; phase-1 secrets readable by sandbox code; S3
      volumes deferred)
- [x] 1.3 One-line cross-reference in "### Plano de control (boto3 / SDK JS
      v3)" (ARCHITECTURE.md:1047)
- [x] 1.4 `grep -c "## ADR-014" ARCHITECTURE.md` → 1; the ADR text contains
      "apagad", "opción explícita" and "sin servidor" (the exact test
      markers of `test_optional_features_docs.py`)

## 2. [spec] SPEC.md §4 and openspec/project.md hard rule 3 (plan A2)

- [x] 2.1 SPEC.md §4: replace the "Servicio de plano de control" bullet
      (line 129) with the hosted-vs-customer-account distinction citing
      ADR-014
- [x] 2.2 SPEC.md §4: amend the metadata bullet (118–121) with "sin almacén
      del lado cliente obligatorio; índice DynamoDB opcional ... (ADR-014,
      M14)"
- [x] 2.3 SPEC.md §4: amend the size bullet (122–124) with the `limits.md`
      pointer, no size-resolver promise
- [x] 2.4 SPEC.md §4: add a new bullet approving S3 volumes for a later
      milestone, out of M11–M14
- [x] 2.5 Leave "Templates declarativos" (line 91) untouched — no ADR-015 in
      this campaign
- [x] 2.6 `openspec/project.md` hard rule 3: add the ADR-014 carve-out to its
      "no control-plane service" clause only, nothing else in the rule
      relaxed
- [x] 2.7 `grep -c "Servicio de plano de control: en M1–M5 el SDK llama a AWS directamente" SPEC.md` → 0;
      SPEC.md §4 and hard rule 3 both cite `ADR-014`

## 3. [docs] docs/site/docs/optional-features.md (plan A3)

- [x] 3.1 New page: three-sentence ADR-014 summary, the "Coste y activación"
      docstring-block template, then the main table with the exact 11-column
      header and one row per function (`secrets-injection`, `secrets-crud`,
      `metadata-index`, `otel-sdk`), each with a stable HTML anchor
      (`id="..."`) and Estado `planificado (Mxx)`
- [x] 3.2 "Sin coste AWS" section: the `rayito sandbox proxy` row, written
      complete now since no later group edits this page for it
      (`CreateMicrovmAuthToken` is free, 50 TPS quota; waking a suspended
      sandbox bills its compute, not the proxy)
- [x] 3.3 A short example subsection per row; planned rows say "Llega en
      Mxx", the proxy row gets a real runnable CLI example
- [x] 3.4 `docs/site/mkdocs.yml` nav: add the page next to "Modelo de
      costes"; `docs/site/docs/cost.md` "Reglas prácticas": link to the new
      page; `docs/site/docs/index.md`: one line linking to it
- [x] 3.5 `cd clients/python && uv run --group docs mkdocs build -f ../../docs/site/mkdocs.yml --strict`
      green with the new page and nav entry

## 4. [helpers] The two opt-in helpers (plan A4)

- [x] 4.1 `clients/python/src/rayito/_optional.py`: `require_module(module, *,
      extra, feature)` over `importlib.import_module`; `ImportError` →
      `InvalidArgumentException` naming `pip install rayito[<extra>]`; no
      other error swallowed; no module-level import of anything optional
- [x] 4.2 `clients/python/tests/unit/test_optional.py`: a missing module
      raises with the extra hint and chains the original `ImportError`; an
      existing module resolves; `import rayito` (subprocess-isolated) never
      puts a known optional package in `sys.modules`
- [x] 4.3 `clients/typescript/src/optional.ts`: `loadOptionalPeer<T>(specifier,
      feature)` over dynamic `import()`; a resolution failure
      (`ERR_MODULE_NOT_FOUND`/`MODULE_NOT_FOUND`) → `InvalidArgumentError`
      naming `npm install <specifier>`, chaining the cause; any other error
      propagates as-is
- [x] 4.4 `clients/typescript/tests/unit/optional.test.ts`: a missing
      specifier rejects with the npm hint and the chained cause; an existing
      specifier (`node:util`) resolves
- [x] 4.5 `cd clients/python && uv run pytest tests/unit -q` and `cd
      clients/typescript && pnpm test` both green with the new tests; `ruff
      check`/`ruff format --check`/`mypy` on `_optional.py`; `tsc --noEmit`
      and `biome check` on `optional.ts`

## 5. [milestones] MILESTONES.md M11–M14 (plan A5)

- [x] 5.1 `## M11 — ADR-014 y convención de opt-in`: docs-only scope, "sin
      opciones = comportamiento de 0.4.0" (trivially true, no SDK option
      ships), $0 budget
- [x] 5.2 `## M12 — Tamaños e2e y proxy local`: `rayito sandbox proxy` e2e
      criterion (serves a guest port on localhost through a simulated JWE
      refresh cycle), ≤ $0.10 budget
- [x] 5.3 `## M13a — Secretos: inyección y CRUD`: real e2e, CRUD + injection,
      one `GetSecretValue` call for N commands inside the cache TTL, ≤ $0.10
- [x] 5.4 `## M13b — Trazas OpenTelemetry del SDK`: unit tests with an
      in-memory exporter as the gate, e2e optional, ≤ $0.10
- [x] 5.5 `## M14 — Índice de metadatos (DynamoDB)`: real e2e against a table
      deployed from `infra/metadata-index.yaml`, `list(metadata=,
      states=[SUSPENDED])` with no VM woken and no `CreateMicrovmAuthToken`
      call, ≤ $0.10
- [x] 5.6 `grep -c "^## M1[1-4]" MILESTONES.md` → 4

## 6. [gate] scripts/tests/test_optional_features_docs.py (plan A1–A5 combined)

- [x] 6.1 Write the module: `REPO_ROOT = Path(__file__).resolve().parents[2]`
      (the established pattern), a generic markdown-table parser so later
      groups extend the page without editing this file
- [x] 6.2 One test per surface: ADR-014 wording in `ARCHITECTURE.md`;
      `SPEC.md` §4 cites ADR-014 and drops the retired sentence;
      `openspec/project.md` hard rule 3 cites ADR-014; the page exists and is
      in nav; the table header matches the contract exactly; one row per
      function with its anchor; the "Sin coste AWS" section lists the proxy;
      any "disponible" row's option symbols are cited next to "Coste y
      activación" in its named SDK file(s); `MILESTONES.md` has the four
      headers
- [x] 6.3 `cd clients/python && uv run pytest ../../scripts/tests -q` green:
      218 passed total (209 pre-existing + 9 new in
      `test_optional_features_docs.py`)
- [x] 6.4 `uvx ruff check scripts`, `python3 scripts/check_pins.py`, `python3
      scripts/check_hygiene.py` all green

## 7. [gates] Everything else still passes

- [x] 7.1 `cd clients/python && uv run pytest tests/unit -q`, `uv run ruff
      check . && uv run ruff format --check . && uv run mypy src tests`
- [x] 7.2 `cd clients/typescript && npx -y pnpm@9.15.4 install
      --frozen-lockfile && pnpm lint && pnpm typecheck && pnpm build &&
      pnpm test && pnpm pack:check`
- [x] 7.3 Confirm no Rust, `.proto`, CloudFormation, `Cargo.lock` or
      `pnpm-lock.yaml`-dependency-adding change (only source/test/doc files)
- [x] 7.4 `openspec validate m11-optin-adr --strict --no-interactive` passes

## Notes

- Out of scope by design, named here so nobody re-opens them inside this
  change: `infra/secrets-access.yaml`, `infra/metadata-index.yaml` and
  `infra/rayito-plane.yaml` (each feature group's own deliverable, or
  deferred entirely); any edit to `e2b-parity.md`, `e2b-compat.md`,
  `SECURITY.md`, `AWS_API_NOTES.md` or any `CHANGELOG.md` (release-please's,
  per the campaign's hard exclusions); `ConfigureSandbox`, `SuspendBudget`,
  the secrets loopback gateway (§8.2 foundations); anything in `crates/`.
