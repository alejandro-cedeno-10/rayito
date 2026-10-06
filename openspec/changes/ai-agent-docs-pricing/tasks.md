## 1. Guide and template page

- [x] 1.1 `docs/site/docs/guias/agente-en-el-sandbox.md`: gateway, `create`,
      `run`/`stream`/`prepare`, sessions, `abort`, limits, permissions,
      fast-start table (A–D), deepagents entry point, E2B shim note. Python
      and TypeScript tabs; a top "borrador, sin publicar" warning; every
      code block against the unmerged API marked `noqa`.
- [x] 1.2 `docs/site/docs/funciones-opcionales/templates-de-agente.md`:
      `AgentTemplate`, pins table, prefetch, `--no-deepagents`/
      `--no-prefetch`, "Coste y activación" box.

## 2. Pricing and pool

- [x] 2.1 `cost.md`: "Coste de un agente: VM frente a modelo" — dated
      Bedrock prices, prompt-caching break-even, worked 10-step example.
- [x] 2.2 `pool.md`: "Calentamiento (`warmup`) y servidor residente" —
      the C/D cost table, how D's port-at-take works, the two open gates.

## 3. Reference and security

- [x] 3.1 `referencia/errores.md`: `AgentException`/`AgentError` in both
      hierarchy trees, the full table and a dedicated reasons section.
- [x] 3.2 `optional-features.md`: rows for the template, `sbx.agent` and
      pool warmup/serve, marked "(borrador)".
- [x] 3.3 `security.md`: "Agente de código dentro del sandbox" section
      (T29/T30 content, marked as draft; no edit to `SECURITY.md` itself).
- [x] 3.4 `e2b-compat.md`: `agent` row in the `UnimplementedError` table.

## 4. Nav and novedades

- [x] 4.1 `novedades/0.8.0.md` (new, explicit unpublished-draft warning) and
      the draft card + "En desarrollo" row in `novedades/index.md`.
- [x] 4.2 `mkdocs.yml`: nav entries for the two new pages and the draft
      novedades page, each labelled "(borrador)".

## 5. Gates

- [x] 5.1 `mkdocs build -f docs/site/mkdocs.yml --strict` green.
- [x] 5.2 `check_docs_examples.py --ruff --mypy --cli` green (every
      agent-API code block is `noqa`-marked with a reason).
- [x] 5.3 `openspec validate --all --strict --no-interactive` green.
- [x] 5.4 `check_hygiene.py` on the changed/new files.
