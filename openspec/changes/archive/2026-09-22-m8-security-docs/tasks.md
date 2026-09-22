## 0. [pre-flight] The sentences are still the ones the audit read

- [x] 0.1 Confirm the retired wording is still present, exactly once each: `grep -n "el kernel no se reinicia" SECURITY.md`, `grep -n "cada hook se audita" ARCHITECTURE.md`, `grep -n "otra rama, otro environment u otro repositorio no pueden asumirlo" infra/README.md`, `grep -n "la máquina que ejecuta el SDK" SECURITY.md`, `grep -n "o exporta \`RAYITO_ACCESS_TOKEN\`" docs/site/docs/concepts.md`. If any differs, stop and record the new text in `design.md` "Context" before editing
- [x] 0.2 Confirm the code facts the new sentences assert are still true: `0.0.0.0:9000` in `crates/rayd/src/main.rs`, the four `audit()` call sites in `crates/rayd/src/hooks/mod.rs`, `restart_context(DEFAULT_CONTEXT_ID)` in `crates/rayd/src/code/validate.rs`, the anonymous `Health` path in `crates/rayd-core/src/auth.rs`, `resolve_location` in `crates/rayd-core/src/persistence/mod.rs`, the `RAYITO_ACCESS_TOKEN` read in `clients/python/src/rayito/_sandbox_base.py` and `clients/typescript/src/sandbox/launch.ts`, and the `sub` condition in `infra/ci-oidc-role.yaml`
- [x] 0.3 Confirm the file split with `m8-security-fixes` (design D1 table): this change owns `SECURITY.md`, `ARCHITECTURE.md`, `docs/site/docs/{security,persistence,concepts}.md` and `scripts/tests/test_security_docs.py`; `infra/README.md` is shared, so edit only the OIDC section (`:116-129`) and the post-deploy steps (`:193-200`) and leave the persistence recipe at `:159-160` to H-01

## 1. [hooks] T2 and ARCHITECTURE.md tell the truth about the hooks (design D2, D3 — audit C-01, C-02, C-03)

- [x] 1.1 `SECURITY.md` T2 threat column: add the in-VM origin clause (shared netns, `0.0.0.0:9000`, no seccomp/cgroups, the M6 policy route covering only `169.254.169.254/32`, uid 1000 reaching the six routes over loopback) and extend the forged-effects list with `/terminate` (takes the VM: `rayd` is the image `CMD`) and `/validate` (restarts the `default` kernel context). The row stays on one line
- [x] 1.2 `SECURITY.md` T2 mitigation column: "de cada hook tras el primer `/run`" → "de cada hook de runtime (`/run`, `/suspend`, `/resume`, `/terminate`) tras el primer `/run`", naming `/ready` and `/validate` as build hooks that are never audited, and state that the port bounds the external origin only, with peer-uid authentication of `/terminate` and `/validate` named as pending
- [x] 1.3 `SECURITY.md` T2 residual list: delete "y el kernel no se reinicia"; add the `/terminate` entry (irreversible, the one hook effect the operator cannot undo) and the `/validate` entry (restarts the `default` context once per boot, runs the validation cell past the `stream_gate`, no `hook_audit` line)
- [x] 1.4 `ARCHITECTURE.md` "Origen de los hooks" (`:296-306`): "cada hook se audita" → "cada hook de runtime se audita", plus the sentences of D3 (the port bounds the external origin only; `/terminate` and `/validate` reachable from inside; `/ready` and `/validate` not audited; peer-uid auth pending)
- [x] 1.5 `ARCHITECTURE.md` ADR-006 "Consecuencia" (`:1101-1103`): append the same two facts in ADR voice, so the ADR no longer leaves "no acuñar `allPorts`" as the whole mitigation
- [x] 1.6 `grep -c "el kernel no se reinicia" SECURITY.md` → 0; `grep -c "cada hook se audita" ARCHITECTURE.md` → 0; both files still render (no broken table row: `grep -n "^| T2 |" SECURITY.md` returns exactly one line)

## 2. [metadata] T4 and the docs site name the reader inside the VM (design D4 — audit C-04)

- [x] 2.1 `SECURITY.md` T4: after the "cualquier principal que pueda acuñar un JWE" clause, add that the sandbox workload itself reads `sandbox_id` and the whole `metadata` map with no credential (`0.0.0.0:8080`, `Health` anonymous by ADR-004), and that this is accepted, not fixed (splitting the response breaks the readiness probe and the 0.2.0 `.proto`), so the only control is what goes into `metadata`
- [x] 2.2 `docs/site/docs/security.md`, section "Qué no poner en `envs` ni en `metadata`": add the same fact in user voice, right after the `lambda:CreateMicrovmAuthToken` sentence
- [x] 2.3 Read both paragraphs end to end: neither now describes the readers of `metadata` as IAM principals only

## 3. [persistence] The S3 prefix is not a tenant boundary (design D5 — audit C-07)

- [x] 3.1 `SECURITY.md` T15: after "El rol sólo alcanza `<bucket>/<prefix>/*` (sin `DeleteObject`)", add that this is a limit of the role and not a tenant boundary (`resolve_location()` validates syntax only, the manifest `sandbox_id` is informative, the access token of one sandbox reaches every `name` under the prefix), that isolation takes one role and one prefix per tenant, and that binding the destination in the `runHookPayload` is pending
- [x] 3.2 `docs/site/docs/persistence.md`, end of the `## S3Prefix` section (before "Bajo el prefijo hay dos objetos"): a short paragraph "El prefijo no separa inquilinos." with the three facts in user voice
- [x] 3.3 `docs/site/docs/security.md`, section "Persistencia en S3 (T15)": one clause with the same fact, so the public summary does not re-create the corrected claim
- [x] 3.4 Re-read `docs/site/docs/persistence.md` `## IAM y bucket`: it still describes what the role can do and now no longer reads as if the prefix separated tenants
- [x] 3.5 `docs/site/docs/persistence.md` quickstart (`:21`, `:25`, `:38`): the published recipe must not teach the namespace H-01 forbids — use `prefix="rayito-home"` / `prefix: "rayito-home"` (the `PersistencePrefix` default of `spike/m0/iam.yaml`) instead of the SDK default `prefix="rayito"`, and fix the `s3://mi-bucket/rayito/…` comment
- [x] 3.6 `docs/site/docs/persistence.md` `prefix` bullet (`:49-50`): the value must match `PersistencePrefix` (default `rayito-home`) or every `checkpoint_files()` returns `permission_denied`, and its first segment must never be `rayito` because that namespace holds `rayito/images/*` and the IAM `*` crosses `/`. The SDK default stays documented as-is in the signature line (changing it is code, `m8-security-fixes` territory)
- [x] 3.7 `test_persistence_quickstart_stays_out_of_the_artifact_namespace` pins both halves; proven failing against the pre-change quickstart

## 4. [tokens] `RAYITO_ACCESS_TOKEN` is read by `create()` too (design D6 — audit C-08, prose only)

- [x] 4.1 `docs/site/docs/concepts.md` ("Dos tokens", `:113-115`): replace "(o exporta `RAYITO_ACCESS_TOKEN`)" with the two sentences of D6 — both calls read it, exporting it shares one secret across the sandboxes of the process, `create()` mints 32 fresh bytes per sandbox by default, use `access_token=` / `accessToken` for a fixed per-sandbox secret
- [x] 4.2 `SECURITY.md` T4: add the same fact plus the MCP note (the MCP server creates sandboxes without passing a token, so it cannot opt out today)
- [x] 4.3 Confirm neither text promises the one-time `logger.warning` (design D1: it belongs to `m8-security-fixes`)

## 5. [iam] `CallerPolicy` is the publisher policy (design D7 — audit C-09, prose only)

- [x] 5.1 `SECURITY.md` `## IAM`, `CallerPolicy` bullet: replace "(la máquina que ejecuta el SDK)" with the publisher wording, the warning against attaching it to an application server, and the pointer to `infra/ci-oidc-role.yaml` as the runtime-only shape
- [x] 5.2 Confirm the bullet still does **not** enumerate image verbs, so dropping `lambda:DeleteMicrovmImage` from `spike/m0/iam.yaml` in `m8-security-fixes` needs no further doc edit

## 6. [oidc] The OIDC trust carries no branch (design D8 — audit H-06)

- [x] 6.1 `infra/README.md:122`: replace the false claim with the corrected paragraph (no branch component in the `sub`; any branch declaring `environment: e2e` presents the accepted `sub`; pin the environment's deployment branches to `main`; the `GitHubRef` + `StringLike` variant named as the IAM-side alternative)
- [x] 6.2 `infra/README.md` "Después de desplegar, en GitHub", item 1: add the deployment-branches-limited-to-`main` step next to the required reviewers
- [x] 6.3 `grep -c "otra rama, otro environment u otro repositorio no pueden asumirlo" infra/README.md` → 0

## 7. [gate] `scripts/tests/test_security_docs.py` pins every corrected sentence (design D9)

- [x] 7.1 Write the module: Spanish docstring, English identifiers, no inline comments in bodies, `REPO_ROOT = Path(__file__).resolve().parents[2]`, readers for `SECURITY.md`, `ARCHITECTURE.md`, `infra/README.md` and the three site pages, and a `threat_row(marker)` helper returning the single `| T2 |` / `| T4 |` / `| T15 |` line
- [x] 7.2 Nine tests, each asserting present **and** absent wording: `test_t2_names_the_in_vm_origin`, `test_audit_scope_is_runtime_hooks`, `test_metadata_is_readable_from_inside_the_vm`, `test_prefix_is_not_a_tenant_boundary`, `test_access_token_env_var_is_shared_by_create`, `test_caller_policy_is_the_publisher_policy`, `test_oidc_trust_has_no_branch_component`, `test_persistence_quickstart_stays_out_of_the_artifact_namespace`, `test_retired_sentences_are_gone`
- [x] 7.3 Prove each test fails without its fix: revert one corrected sentence at a time in a scratch copy of the file (or `monkeypatch` the reader to return the pre-change text) and confirm exactly that test fails, naming the file and the fragment
- [x] 7.4 `cd clients/python && uv run pytest ../../scripts/tests -p no:cacheprovider` green (previous count + 9); `uvx ruff check scripts` clean; confirm `git diff --name-only` (or the edited-file list) contains no `Makefile` and no `.github/workflows/` entry

## 8. [gates] Everything still passes, nothing else moved

- [x] 8.1 `cd clients/python && uv run --group docs mkdocs build -f ../../docs/site/mkdocs.yml --strict` green; `docs/site/mkdocs.yml` unchanged
- [x] 8.2 `cd clients/python && uv run pytest tests/unit` (1066) and `uv run ruff check . && uv run ruff format --check . && uv run mypy src tests` unchanged — this change touches no SDK file
- [x] 8.3 Confirm no Rust, TypeScript, CloudFormation or workflow file changed (`cargo`, `pnpm` and `cfn-lint` gates are therefore not re-run for this change; state that in the notes)
- [x] 8.4 `openspec validate m8-security-docs --strict --no-interactive` passes
- [x] 8.5 Re-read every edited sentence against `docs/SECURITY_AUDIT.md` §4–§5 (C-01, C-02, C-03, C-04, C-07, C-08, C-09, H-06) and confirm each one matches the recommendation of its row, with every M8 item named as pending

## Notes

- Out of scope by triage, named here so nobody re-opens them inside this change:
  the code half of C-08 (one-time warning), the code half of C-09
  (`lambda:DeleteMicrovmImage`), and all of H-01, H-02, C-05, C-13, H-03, H-04,
  H-05 and C-11 — all `m8-security-fixes`; the M8 rows (C-10, C-12, peer-uid hook
  auth, `audit()` on `/ready` and `/validate`, the `runHookPayload` persistence
  binding, the MCP opt-out, the runtime/publisher policy split) and the accepted
  row C-06.
