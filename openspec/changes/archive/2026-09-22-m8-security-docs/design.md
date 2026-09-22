## Context

State on 2026-09-22: release 0.2.0 on disk (SDK Python and TypeScript `rayito`
0.2.0, `rayd` 0.2.0, images `rayito-base` 20.0, `rayito-base-caps` 8.0,
`rayito-base-poly` 4.0), M0–M7 accepted and archived, repository **not yet
published**. The input of this change is the internal audit
`docs/SECURITY_AUDIT.md` (2026-09-22): 38 proposed findings, two independent
refuters each, 0 confirmed blockers, and a triage table (§8) with 11 rows marked
"arreglar ahora = antes de publicar el repositorio".

This change takes the eight documentation-truth rows of those eleven: **C-01,
C-02, C-03, C-04, C-07, C-08 (prose), C-09 (prose), H-06**. The other rows
(H-01, H-02, C-05, C-13, H-03, H-04, H-05, C-11 and the one-time warning of
C-08) are `m8-security-fixes`.

Facts verified in the tree for this design (file:line as of 2026-09-22):

- `SECURITY.md:54` is the **single-line** T2 table row; `:56` is T4; `:67` is
  T15; `:85` is the `CallerPolicy` bullet of `## IAM`; `:157` is the CI row of
  the supply-chain table and is already precise ("trust exacto a
  `environment: e2e`"), so it is not touched.
- `ARCHITECTURE.md:296-306` is the "Origen de los hooks (M6, `SECURITY.md` T2)"
  paragraph; `:299-300` carries "cada hook se audita"; ADR-006 is `:1090-1103`
  and its "Consecuencia" is `:1101-1103`.
- `crates/rayd/src/main.rs:280` binds the hooks listener on `0.0.0.0:9000`;
  `crates/rayd/src/hooks/mod.rs:130-147` mounts the six POST routes with no auth
  layer; `audit()` (`:351`) is called only from `:282` (`/run`), `:306`
  (`/suspend`), `:452` (`/resume`) and `:506` (`/terminate`);
  `crates/rayd/src/code/validate.rs:23` restarts `DEFAULT_CONTEXT_ID`;
  `crates/rayd/src/adapters/imds_block.rs:47` limits the policy route to
  `SANDBOX_UID_RANGE = "1000-65535"` and blackholes only `169.254.169.254/32`.
- `crates/rayd/src/grpc/health.rs:85-99` returns `sandbox_id`, the whole
  `metadata` map, `imds_blocked`, `hook_anomalies`, `clock_offset_ms` and
  `resume_generation`; `crates/rayd-core/src/auth.rs:135-137` lets
  `/rayito.v1.HealthService/Health` through with no metadata (ADR-004).
- `crates/rayd-core/src/persistence/mod.rs:77` (`resolve_location`) receives no
  session and validates syntax only; `manifest.rs:82-98` never checks
  `sandbox_id`.
- `clients/python/src/rayito/_sandbox_base.py:114` and
  `clients/typescript/src/sandbox/launch.ts:64` read `RAYITO_ACCESS_TOKEN` in the
  `create()` path; `clients/python/src/rayito/mcp/_lease.py:115-119` creates
  sandboxes without passing `access_token=`.
- `infra/ci-oidc-role.yaml:72` is the trust condition (`sub` equality only);
  `infra/README.md:122` is the false claim; `infra/README.md:193-200` is the
  "Después de desplegar, en GitHub" list whose item 1 is today only the required
  reviewers.
- The gate `cd clients/python && uv run pytest ../../scripts/tests
  -p no:cacheprovider` already exists in `Makefile` (`test-scripts`, run by
  `test`) and in `.github/workflows/ci.yml:80`; `uvx ruff check scripts`
  (`Makefile` `lint`, `ci.yml:67`) already lints `scripts/tests`.
  `clients/python/tests/unit/cli/test_compat.py:14` is the in-tree precedent for
  a test that reads a docs file through `Path(__file__).resolve().parents[N]`.

## Goals / Non-Goals

**Goals**

1. Every sentence the audit flagged as contradicting the code says what the code
   does, in the file the audit names.
2. Where the honest sentence describes an open risk, the text names the pending
   fix (M8) instead of implying a control that does not exist.
3. A cheap mechanical gate makes a silent revert of any corrected sentence fail
   CI.
4. Zero coupling with `m8-security-fixes`: no shared file, so the two changes can
   land and be archived in either order.

**Non-Goals**

- No production code, no CloudFormation, no workflow, no `Makefile`, no `.proto`,
  no image. (`m8-security-fixes` owns those.)
- No new documentation page, no navigation change, no restructuring of
  `SECURITY.md` or of the docs site.
- No attempt to implement any of the M8 items the corrected sentences mention
  (peer-uid hook auth, `audit()` on `/ready` and `/validate`, binding the
  `S3Location`, the MCP opt-out, the runtime/publisher policy split).
- No change to `docs/SECURITY_AUDIT.md`: the audit is a dated record, not a
  living document.

## Decisions

### D1. Scope split with `m8-security-fixes`: disjoint files, not disjoint topics

Both changes come from the same triage table and several rows have a code half
and a prose half (C-08, C-09). The split is by **file**, which is the only split
that lets either change land first:

| File | This change | `m8-security-fixes` |
|---|---|---|
| `SECURITY.md`, `ARCHITECTURE.md`, `docs/site/docs/{security,persistence,concepts}.md`, `scripts/tests/test_security_docs.py` | yes | no |
| `spike/m0/iam.yaml`, `.github/workflows/*`, `Makefile`, `crates/**`, `clients/**`, `docs/site/docs/mcp.md` | no | yes |
| `infra/README.md` | the OIDC section (`:116-129`) and the post-deploy steps (`:193-200`) | the persistence recipe (`:159-160`, H-01) |

`infra/README.md` is the one file both changes edit, in sections that do not
touch: H-01 rewrites the "same bucket for artifacts and persistence" recipe, this
change rewrites the OIDC trust paragraph and adds one line to the post-deploy
checklist. No requirement is shared (H-01 lands on `filesystem-persistence`, this
one on `e2e-workflow`), so the only cost of landing them in either order is a
textual merge inside one file.

No capability is modified by both changes: this change takes `hook-defense`,
`sandbox-metadata`, `sdk-persistence`, `e2e-workflow` and the new
`security-docs`; `m8-security-fixes` takes `filesystem-persistence` (H-01, C-13),
CI and release capabilities (H-02, C-11), `guest-isolation` / process capabilities
(C-05), the pool capabilities (H-03, H-04) and `mcp-server` (H-05).

Consequences accepted here: (a) the C-08 prose **does not** promise the one-time
`logger.warning` that `m8-security-fixes` adds — a doc that describes a warning
that is not in the tree yet would be a new false sentence, exactly the class of
defect this change exists to remove; (b) the C-09 prose does not enumerate image
verbs, so removing `lambda:DeleteMicrovmImage` from the template needs no second
doc edit; (c) `docs/site/docs/mcp.md:176` (H-05) is not touched here.

### D2. `SECURITY.md` T2 (`:54`): in-VM origin, two more forgeable hooks, runtime-only audit

T2 is one table row, so every edit stays on that single line. Three surgical
edits, in the order they appear:

1. Threat column, right after "(M0 Q24 lo confirmó: el proxy vive dentro de la
   VM)", insert: **"—y, sin ningún token, desde dentro de la propia VM: `rayd`
   escucha en `0.0.0.0:9000` en el mismo namespace de red que los procesos del
   sandbox (no hay netns, seccomp ni cgroups propios, y la ruta de política de M6
   sólo agujerea `169.254.169.254/32`), así que cualquier proceso uid 1000
   alcanza las seis rutas por loopback—"**, and extend the list of forged effects
   with **"; un `/terminate` falso se lleva la VM (`rayd` es el `CMD` de la
   imagen); un `/validate` falso reinicia el contexto `default` del kernel"**.
2. Mitigation column: "auditoría `hook_audit` de cada hook tras el primer `/run`"
   becomes **"auditoría `hook_audit` de cada hook de runtime (`/run`,
   `/suspend`, `/resume`, `/terminate`) tras el primer `/run` — `/ready` y
   `/validate` son hooks de build y no se auditan, así que sus contadores nunca
   suben—"**, and the sentence that today reads as if the port were the whole
   control gains **"el puerto acota sólo el origen externo; contra el origen de
   dentro de la VM no hay mitigación hoy (autenticar `/terminate` y `/validate`
   por el uid del par, resolviendo el `SocketAddr` contra `/proc/net/tcp`, queda
   pendiente)"**.
3. Residual list: delete **"y el kernel no se reinicia"** from the `/run` entry
   (the measured fact that a forged `/run` does not rotate the kernel stays in
   the `hook-defense` acceptance scenario, where it belongs) and append two
   entries: **"`/terminate` forjado → `rayd` cancela y la VM se va con él:
   irreversible, el único efecto de hook que el operador no puede deshacer;
   `/validate` forjado → reinicia el contexto `default` una vez por arranque y
   ejecuta la celda de validación saltándose el `stream_gate` (se pierden las
   variables de ese contexto) y sin línea `hook_audit`"**.

Why this shape and not another: the audit's C-01 refuters agreed the finding is
**not** a blocker and that the fix due now is documentary; rewriting T2 into a
new structure would lose the measured bounds that M6's acceptance test produced.
Deleting the "kernel no se reinicia" clause instead of qualifying it is what the
triage row asks for, and it is also the honest choice: the clause is true of
`/run` and false of `/validate`, and a reader of a residual-risk list reads it as
a property of forged hooks.

Proven by `test_t2_names_the_in_vm_origin` and `test_audit_scope_is_runtime_hooks`
(the `hook-defense` delta's two new scenarios).

### D3. `ARCHITECTURE.md`: the same two facts where the design is written

Two edits, both minimal:

- `:296-306` ("Origen de los hooks"): "cada hook se audita" → **"cada hook de
  runtime se audita"**, plus, after the ADR-006 clause, **"Ese control acota el
  origen externo. No acota el de dentro de la VM: `rayd` escucha en
  `0.0.0.0:9000` en el netns del sandbox, así que un proceso uid 1000 alcanza los
  hooks por loopback; `/terminate` (irreversible) y `/validate` (reinicia el
  contexto `default`) son los dos con efecto real, y ninguno de los dos pasa por
  `audit()` ni por `hook_anomalies` — `/ready` y `/validate` son hooks de build y
  no se auditan. Autenticar por el uid del par queda pendiente."**
- ADR-006 "Consecuencia" (`:1101-1103`): append the same two sentences in ADR
  voice, so a reader who only reads the ADR is not left with "no acuñar
  `allPorts`" as the whole mitigation.

Why here as well as in T2: `ARCHITECTURE.md` is the file the project's own rule
("las decisiones de arquitectura se cambian por escrito") points implementers at;
leaving ADR-006 claiming a boundary it does not have is how the next milestone
re-derives the wrong threat model.

Proven by `test_t2_names_the_in_vm_origin` (reads `ARCHITECTURE.md` too) and
`test_audit_scope_is_runtime_hooks`.

### D4. `SECURITY.md` T4 and `docs/site/docs/security.md`: name the reader inside the VM

- T4 (`:56`), after "`metadata` además lo devuelve `Health` (sin
  `x-access-token`) a cualquier principal que pueda acuñar un JWE para el
  sandbox", insert **"y, sin credencial alguna, a la propia carga de trabajo del
  sandbox: el listener gRPC está en `0.0.0.0:8080` y `Health` es el único RPC
  anónimo (ADR-004), así que un proceso uid 1000 dentro del MicroVM lee
  `sandbox_id` y el mapa `metadata` completo. Se acepta: partir la respuesta
  rompería la sonda de readiness y el contrato del `.proto` 0.2.0, así que el
  único control es qué se mete en `metadata`"**.
- `docs/site/docs/security.md:23-29` ("Qué no poner en `envs` ni en
  `metadata`"): after "cualquier principal con `lambda:CreateMicrovmAuthToken`
  sobre la imagen puede leer los metadatos por `Health`", insert **"y el propio
  código que corre dentro del sandbox también, sin ninguna credencial: `Health`
  es anónimo y `rayd` escucha en `0.0.0.0:8080`"**.

Why not fix the code: the audit's recommendation for C-04 is explicit — the fix
is documentary, because splitting the `Health` response breaks the readiness
probe (the only anonymous RPC by design, ADR-004) and the 0.2.0 `.proto`
contract. The operator is the one who chooses what `metadata` carries, and that
choice is made while reading exactly these two paragraphs.

Proven by `test_metadata_is_readable_from_inside_the_vm`.

### D5. T15, `persistence.md` and the T15 summary: the prefix is not a tenant boundary

- T15 (`:67`), right after "El rol sólo alcanza `<bucket>/<prefix>/*` (sin
  `DeleteObject`)", insert **"— que es un límite del rol, no una frontera entre
  inquilinos: `rayd` no liga el `S3Location` al sandbox (`resolve_location()`
  sólo valida sintaxis y el `sandbox_id` del manifest es informativo), así que
  quien tenga el access token de un sandbox puede leer o sobrescribir el `HOME`
  de cualquier otro bajo ese mismo prefijo. Aislar inquilinos que no confían
  entre sí exige un execution role y un prefijo por inquilino, no un `name` por
  inquilino; ligar el destino al sandbox en el `runHookPayload` queda
  pendiente—"**.
- `docs/site/docs/persistence.md`, a new short paragraph at the end of the
  `## S3Prefix` section (before "Bajo el prefijo hay dos objetos"), titled
  **"El prefijo no separa inquilinos."**, with the same three facts in user
  voice: `Checkpoint`/`Restore` accept any syntactically valid
  `bucket`/`prefix`/`name`; the only limit is what the execution role reaches,
  i.e. the whole `<bucket>/<prefix>/*`; one tenant, one role, one prefix.
- `docs/site/docs/security.md:64-78` ("Persistencia en S3 (T15)") gains one
  clause with the same fact. This page is the public summary of T15 and already
  paraphrases the prefix as if it bounded the blast radius; correcting T15 and
  leaving its summary intact would re-create the defect one hop away. That is the
  same row, not a wider scope.

Proven by `test_prefix_is_not_a_tenant_boundary` (T15 and `persistence.md`
asserted; the summary clause asserted in the same test).

### D5b. The `persistence.md` quickstart stops teaching the `rayito/` namespace

The quickstart of the same page (`:21`, `:25`, `:38`) published
`prefix="rayito"` — the SDK default and exactly the namespace H-01 removes from
`spike/m0/iam.yaml`, whose `PersistencePrefix` now defaults to `rayito-home` and
whose `Description` forbids `rayito` or any prefix of it. Leaving it would keep a
published recipe that either fails closed (`permission_denied` on every
`checkpoint_files()` against a default deployment) or, if the operator makes IAM
match the page, re-creates H-01 on a bucket the new explicit `Deny` does not
cover. That is the same defect class this change exists to remove, in the one
file this change owns, so it lands here rather than in `m8-security-fixes`, which
D1 forbids from touching `docs/site/docs/persistence.md`.

Decision: the quickstart passes `prefix="rayito-home"` (Python) and
`prefix: "rayito-home"` (TypeScript) explicitly, the `s3://` comment follows, and
the `prefix` bullet gains two clauses — the value must equal the deployment's
`PersistencePrefix` or every checkpoint answers `permission_denied`, and its
first segment must never be `rayito`, because `rayito/images/*` holds the image
artifacts and the `*` of an IAM resource crosses `/`.

Not decided here: the SDK default itself (`clients/python/src/rayito/_models.py`
`prefix: str = "rayito"` and the `?? "rayito"` of
`clients/typescript/src/sandbox/persistence.ts`). Changing it is a client
behaviour change, is not one of the eleven "arreglar ahora" rows, and belongs to
no file this change owns; the signature line therefore keeps documenting the real
default, with the warning next to it. The root `README.md:214`, which teaches the
same default in one line, belongs to neither change's file list and is recorded as
an open issue instead.

Proven by `test_persistence_quickstart_stays_out_of_the_artifact_namespace`.

### D6. `concepts.md` and T4: `RAYITO_ACCESS_TOKEN` is read by `create()` too

`docs/site/docs/concepts.md:113-115` today ends with "(o exporta
`RAYITO_ACCESS_TOKEN`)". Replace that parenthesis with two sentences:

> **"`RAYITO_ACCESS_TOKEN` existe, pero la leen las dos llamadas: si la exportas,
> cada `create()` de ese proceso reutiliza el mismo secreto y una fuga abre todos
> sus sandboxes, no uno (por defecto `create()` genera 32 bytes frescos por
> sandbox). Úsala para `connect()` desde otro proceso; para fijar el secreto de
> un sandbox concreto, pásalo con `access_token=` / `accessToken`."**

T4 gains one sentence with the same fact plus the MCP note: **"`RAYITO_ACCESS_TOKEN`
la leen también los `create()` de ambos SDKs, no sólo `connect()`: exportarla
convierte el secreto por sandbox en uno de toda la flota del proceso —lo que este
mismo modelo rechaza en T14—, y el servidor MCP crea sus sandboxes sin pasar
token, así que hoy no puede optar por no compartirlo."**

Why not also mention the one-time warning: see D1(a). Why not weaken the
`RAYITO_ACCESS_TOKEN` contract itself: it is the specified behaviour
(`openspec/specs/typescript-sdk/spec.md:98`) and the only way to `connect()` from
another process; the audit's refuter kept the finding at *menor* precisely
because the default is safe.

Proven by `test_access_token_env_var_is_shared_by_create`.

### D7. `SECURITY.md:85`: `CallerPolicy` is the publisher policy

Replace the bullet's opening parenthesis "(la máquina que ejecuta el SDK)" with
**"(la política del publicador: la máquina que publica imágenes con `rayito image
publish` / `prune` y lanza sandboxes desde el SDK — no la de un servidor de
aplicación, que sólo necesita los verbos de runtime: para eso la forma mínima ya
publicada es `infra/ci-oidc-role.yaml`)"**. The rest of the bullet (the list of
grants) is unchanged and deliberately keeps saying "operaciones de imagen y
MicroVM" without enumerating verbs, so `m8-security-fixes` can drop
`lambda:DeleteMicrovmImage` from `spike/m0/iam.yaml` without touching this file.

Why this and not a second policy in the docs: the runtime-only policy the finding
asks for already exists and is published (`infra/ci-oidc-role.yaml:75-99`, six
MicroVM verbs on named ARNs, no image/S3/quota/tag permissions); the formal
template split is triaged to M8.

Proven by `test_caller_policy_is_the_publisher_policy`.

### D8. `infra/README.md`: the OIDC `sub` carries no branch

- `:122`: "otra rama, otro environment u otro repositorio no pueden asumirlo"
  becomes **"otro environment u otro repositorio no pueden asumirlo. La rama no
  entra: el `sub` de un job que declara un environment no lleva componente de
  rama, así que cualquier workflow de este repositorio —en cualquier rama— que
  declare `environment: e2e` presenta el `sub` aceptado. La rama se cierra fuera
  de IAM: al crear el environment `e2e`, fijar sus deployment branches a `main`
  (Settings → Environments → Deployment branches and tags → selected branches).
  Si algún día hace falta cerrarlo también en IAM, el camino es un parámetro
  `GitHubRef` y una segunda condición `StringLike` sobre
  `repo:<repo>:ref:refs/heads/main`."**
- The post-deploy checklist item 1 (`:195-196`) gains **"y deployment branches
  limitadas a `main`"** next to the required reviewers, because the stack is
  documented as not deployed: the cheapest moment to close the branch is when the
  environment is created.

Why (a) deployment branches and not (b) the `GitHubRef` condition: the audit
offers both and notes the stack is not deployed, so (a) costs nothing and closes
it today; (b) changes a template this change does not own.

Proven by `test_oidc_trust_has_no_branch_component`.

### D9. The gate: `scripts/tests/test_security_docs.py`, eight tests, no wiring

- **Location.** `scripts/tests/`, not `clients/python/tests/unit/`: the subject is
  repository documentation, not the Python SDK, and `scripts/tests` already runs
  in `Makefile` `test-scripts` and `ci.yml:80`, so **no `Makefile` or workflow
  edit is needed** — which is what keeps this change file-disjoint from
  `m8-security-fixes` (D1).
- **Shape.** Module docstring in Spanish, English identifiers, no inline comments
  in function bodies. `REPO_ROOT = Path(__file__).resolve().parents[2]`; small
  readers `security_md()`, `architecture_md()`, `site_doc(name)`,
  `infra_readme()` returning text; a `threat_row(marker)` helper returning the
  single `SECURITY.md` table line that starts with `| T2 |` / `| T4 |` /
  `| T15 |`, so an assertion about T2 cannot accidentally be satisfied by prose
  elsewhere in the file.
- **Assertions.** One test per audit row, each asserting *present* and *absent*:
  `test_t2_names_the_in_vm_origin` (C-01/C-02), `test_audit_scope_is_runtime_hooks`
  (C-03), `test_metadata_is_readable_from_inside_the_vm` (C-04),
  `test_prefix_is_not_a_tenant_boundary` (C-07),
  `test_access_token_env_var_is_shared_by_create` (C-08),
  `test_caller_policy_is_the_publisher_policy` (C-09),
  `test_oidc_trust_has_no_branch_component` (H-06), and
  `test_retired_sentences_are_gone` asserting the four exact retired strings are
  absent from the whole tree of edited files (`el kernel no se reinicia`,
  `cada hook se audita`, `otra rama, otro environment u otro repositorio no
  pueden asumirlo`, `(la máquina que ejecuta el SDK)`).
- **Why string assertions and not a prose review.** The failure mode the audit
  found is a sentence drifting away from the code over four milestones. A gate
  that pins the exact retired strings is the cheapest thing that fails when it
  drifts back; it is the same technique `test_compat.py` already uses to keep
  `limits.md` and `COMPATIBILITY` in step.
- **Tolerance.** Assertions are on short distinctive fragments (e.g.
  `0.0.0.0:9000`, `cada hook de runtime`, `no separa inquilinos`,
  `sin componente de rama`), never on whole paragraphs, so ordinary editing of
  the surrounding Spanish does not break the gate.

### D10. Capability mapping: four modified, one new

- `hook-defense` (C-01, C-02, C-03) and `sandbox-metadata` (C-04) already own the
  exact sentences (`SECURITY.md` T2 and T4 are named inside those requirements),
  so both are `MODIFIED`.
- C-07 goes to `sdk-persistence` ("S3Prefix names one persisted home", which owns
  what `persistence.md` says about the prefix) and **not** to
  `filesystem-persistence` ("Execution role policy scoped to the persistence
  prefix"), even though the latter also mentions the prefix: that requirement
  pins the `spike/m0/iam.yaml` defaults and pattern that `m8-security-fixes`
  modifies for H-01 and C-13. Keeping the two changes off the same requirement
  keeps them independently archivable (D1).
- H-06 goes to `e2e-workflow`, which owns `infra/ci-oidc-role.yaml` and what
  `infra/README.md` documents about it; its scenario "another branch cannot
  assume the role" is itself the false claim and is replaced.
- C-08 and C-09 have **no** owning capability: `concepts.md` belongs to
  `architecture-docs` (whose requirement is about the "Qué corre dónde" section,
  not the tokens table) and the IAM section of `SECURITY.md` is owned by nobody
  (`community-health` owns only the reporting section, and its requirement ends
  with "everything from `## Modelo` onward SHALL be unchanged **by this
  change**", a statement about `m7-oss-hygiene`, not a freeze). Rather than
  stretch an unrelated requirement, they go into a new `security-docs`
  capability together with the gate requirement, which is genuinely new.

### D11. Language and style of the edits

Spanish prose (these are user-facing documents), house voice, `file:line`-free
sentences except where the existing text already cites code. No new headings in
`SECURITY.md` or `ARCHITECTURE.md`; no new page or nav entry on the docs site.
Table rows in `SECURITY.md` stay single-line. Identifiers in the new test module
are English (`openspec/project.md` conventions). Every pending item is named as
pending ("queda pendiente"), never as a shipped control.

### D12. Acceptance without AWS

The audit picked these rows because they are verifiable locally: the change is
accepted when `cd clients/python && uv run pytest ../../scripts/tests` is green
(eight new tests), `uvx ruff check scripts` is clean, `mkdocs build --strict`
succeeds, and a reviewer confirms each edited sentence against the `file:line`
evidence in `docs/SECURITY_AUDIT.md` §4–§5. No image is published and no e2e is
run; `MILESTONES.md` is not touched because this change closes no milestone (see
Open Questions).

## Risks / Trade-offs

- **The gate pins Spanish strings.** A future rewording that keeps the meaning
  but drops a pinned fragment fails CI. Mitigated by pinning short fragments
  (D9) and by the failure message naming the file and the fragment, so the fix is
  obvious: update the fragment in the test in the same commit as the reword.
- **Honest text makes the project look weaker.** T2 now says the in-VM origin is
  unmitigated, T15 that the prefix is not a boundary, T4 that the workload reads
  `metadata`. That is the point: the audit's own rebuttals show none of these
  crosses a trust boundary today, and a reader who plans a multi-tenant
  deployment needs to know before, not after.
- **Two changes touching the same triage table.** Mitigated by the file-level
  split (D1); if `m8-security-fixes` lands first nothing here changes, and vice
  versa.
- **`SECURITY.md` T2 is already a very long table cell** and grows further.
  Accepted: restructuring the threat table is a separate piece of work and would
  make the diff unreviewable against the audit.

## Migration Plan

None. No runtime behaviour, no API, no data, no image. Readers of the published
0.2.0 docs get corrected text on the next docs build; nothing they run changes.

## Open Questions

1. `MILESTONES.md` has no M8 section yet, and this change deliberately does not
   create one (both `m8-*` changes would edit the same file, and the audit's
   pre-publication track is not a milestone with an AWS acceptance test). Whoever
   opens M8 decides whether the pre-publication rows are recorded there or in a
   short "auditoría interna" note under `SECURITY.md`. Answered for this change:
   out of scope.
2. Whether `docs/site/docs/security.md` should eventually carry a compact
   "riesgos residuales conocidos" list instead of the per-section clauses added
   here. Not decided; not needed for the corrections.
