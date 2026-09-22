## Why

The internal security audit of 2026-09-22 (`docs/SECURITY_AUDIT.md`) closed with
**0 blockers**, but its triage table (§8) marks **11 rows as "arreglar ahora"**,
i.e. before the repository is published — "porque es lo que otros copiarán o
porque la documentación publicada afirma algo que el código no hace".

Five of those eleven rows are exactly that second case: `SECURITY.md`,
`ARCHITECTURE.md`, `infra/README.md` and the docs site state things the code does
not do. Verbatim, today:

- **C-01 / C-02** — T2 scopes the hook threat to "alcanzables desde **fuera** con
  un JWE `allPorts`", never lists `/terminate` (irreversible: `rayd` is the image
  `CMD`) or `/validate` among the forgeable hooks, and claims "el kernel no se
  reinicia" although a forged `/validate` restarts the `default` context
  (`code/validate.rs:23`). `rayd` binds `0.0.0.0:9000` in the sandbox's own
  network namespace, so a process at uid 1000 inside the VM reaches all six hook
  routes over loopback: the port control bounds the external origin only.
- **C-03** — `ARCHITECTURE.md:299-300` and T2 say "cada hook se audita"; `audit()`
  is called from four of six handlers (`hooks/mod.rs:282,306,452,506`), so
  `/ready` and `/validate` are never audited and their counters cannot move.
- **C-04** — T4 and `docs/site/docs/security.md:23-29` describe the readers of
  `metadata` as IAM principals ("cualquier principal que pueda acuñar un JWE"),
  while `Health` is anonymous on `0.0.0.0:8080` (ADR-004): the sandbox's own
  workload reads `sandbox_id` and the whole map with no credential. The operator
  decides what goes into `metadata` by reading that sentence.
- **C-07** — T15 ("El rol sólo alcanza `<bucket>/<prefix>/*`") and
  `docs/site/docs/persistence.md` read as if the prefix were a boundary.
  `resolve_location()` (`rayd-core/src/persistence/mod.rs:77`) never binds the
  `S3Location` to the sandbox, so the prefix does not separate tenants.
- **C-08 / C-09 / H-06** — `concepts.md:115` recommends exporting
  `RAYITO_ACCESS_TOKEN` in a paragraph about `connect()`, without saying
  `create()` reads it too (one secret for the whole process, which T14 rejects);
  `SECURITY.md:85` calls `CallerPolicy` "la máquina que ejecuta el SDK" although
  it is the **publisher** policy, so someone can hang it on an application
  server; `infra/README.md:122` states "otra rama, otro environment u otro
  repositorio no pueden asumirlo" although the OIDC `sub` of a job with an
  environment carries no branch component.

Publishing the repository with these sentences ships a threat model that is
wrong in the operator's favour. All of them are verifiable locally.

## What Changes

Documentation only: the audit rows **C-01, C-02, C-03, C-04, C-07, C-08 (prose),
C-09 (prose) and H-06**, each following the recommendation written in
`docs/SECURITY_AUDIT.md` §4–§5 and its triage row, not a redesign.

- `SECURITY.md` T2: name the in-VM origin (uid 1000 reaching `0.0.0.0:9000` over
  loopback, shared netns, no seccomp/cgroups, the M6 policy route covering only
  `169.254.169.254/32`), add `/terminate` and `/validate` to the forgeable hooks
  with their real effect, drop "el kernel no se reinicia", and say "cada hook de
  **runtime**" naming `/ready` and `/validate` as unaudited.
- `ARCHITECTURE.md`: the same two corrections in the "Origen de los hooks"
  paragraph (`:296-306`) and in the ADR-006 "Consecuencia".
- `SECURITY.md` T4 + `docs/site/docs/security.md`: the sandbox workload itself
  reads `Health.metadata` with no credential (accepted, not fixed: splitting the
  response breaks the readiness probe and the 0.2.0 `.proto`).
- `SECURITY.md` T15 + `docs/site/docs/persistence.md` (+ the T15 summary of
  `docs/site/docs/security.md`): the S3 prefix does **not** separate tenants; one
  role and one prefix per tenant. The quickstart of that same page stops
  publishing `prefix="rayito"`: `rayito/` is the image-artifact namespace H-01
  removes from `spike/m0/iam.yaml`, so the recipe now passes the
  `PersistencePrefix` default `rayito-home` explicitly in both snippets and the
  `prefix` bullet says why the first segment can never be `rayito`.
- `docs/site/docs/concepts.md:113-115` + `SECURITY.md` T4:
  `RAYITO_ACCESS_TOKEN` is read by `create()` too, so exporting it shares one
  secret across every sandbox of the process; the MCP server has no opt-out.
- `SECURITY.md:85`: `CallerPolicy` is the **publisher** policy, not the policy of
  an application server; `infra/ci-oidc-role.yaml` is the runtime-only shape.
- `infra/README.md:122` and its post-deploy checklist: the `sub` has no branch
  component; pin the `e2e` environment's deployment branches to `main` at
  creation time.
- New `scripts/tests/test_security_docs.py`: one test per audit row asserting the
  corrected wording is present and the retired wording is gone. It runs inside
  the existing `pytest ../../scripts/tests` gate, so no `Makefile`, workflow or
  runtime file changes.

**Not in this change.** The other six "arreglar ahora" rows — H-01, H-02, C-05,
C-13, H-03, H-04, H-05, C-11 and the one-time `logger.warning` of C-08 — belong
to `m8-security-fixes` (code, IAM and CI); this change touches no production
code, no template and no workflow, and shares no capability and no requirement
with it. The one file both changes edit is `infra/README.md`, in disjoint
sections (H-01 rewrites the persistence recipe at `:159-160`; this change
rewrites the OIDC paragraph at `:122` and the post-deploy checklist), so either
change can land first. Everything
the triage marks **M8** (per-peer-uid hook authentication, the two `audit()`
calls, the `runHookPayload` persistence binding, the MCP opt-out, the
runtime/publisher policy split, build/publish separation, `--require-hashes`) and
the single **accepted** row (C-06) stay out; where this change has to mention
them it names them as pending work, never as existing controls.

## Capabilities

### New Capabilities
- `security-docs`: the sentences the audit corrected that no existing capability
  owns — the `RAYITO_ACCESS_TOKEN` sharing semantics in `concepts.md` and T4, the
  `CallerPolicy` publisher wording in the IAM section of `SECURITY.md` — plus the
  `scripts/tests/test_security_docs.py` gate that pins every corrected sentence
  of this change.

### Modified Capabilities
- `hook-defense`: the hook-origin requirement gains the in-VM origin, the two
  extra forgeable hooks and the ban on the "kernel no se reinicia" claim; the
  runtime-audit requirement gains the "cada hook de runtime" wording with
  `/ready` and `/validate` named as unaudited.
- `sandbox-metadata`: the "metadata is not secret" requirement gains the in-VM
  anonymous reader of `Health`.
- `sdk-persistence`: the `S3Prefix` requirement gains the statement that the
  prefix is not a tenant boundary and the ban on publishing a recipe whose
  first prefix segment is `rayito`.
- `e2e-workflow`: the OIDC role requirement gains what `infra/README.md` must say
  about the missing branch component, and its false "another branch cannot assume
  the role" scenario is replaced.

## Impact

- New file: `scripts/tests/test_security_docs.py`.
- Edited: `SECURITY.md` (T2, T4, T15, the `CallerPolicy` bullet of the IAM
  section), `ARCHITECTURE.md` ("Origen de los hooks", ADR-006 "Consecuencia"),
  `infra/README.md` (the OIDC section and the post-deploy steps),
  `docs/site/docs/security.md`, `docs/site/docs/persistence.md`,
  `docs/site/docs/concepts.md`.
- Behaviour: none. No Rust, Python, TypeScript, CloudFormation or workflow file
  changes; no `.proto`, no image, no AWS call. `AWS_API_NOTES.md` does not
  change.
- Gates: every existing gate unchanged; `cd clients/python && uv run pytest
  ../../scripts/tests` grows eight tests; `uvx ruff check scripts` covers the new
  module; `mkdocs build --strict` stays green with no nav change.
- Acceptance is local (`docs/SECURITY_AUDIT.md` §8 chose rows verifiable without
  AWS): no image publish and no e2e run for this change.
