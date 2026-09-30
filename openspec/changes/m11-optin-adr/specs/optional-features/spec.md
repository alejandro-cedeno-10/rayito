## ADDED Requirements

### Requirement: ADR-014 fixes the cost-bearing opt-in rule
`ARCHITECTURE.md` SHALL contain a section `## ADR-014 — Componentes
opcionales en la cuenta del cliente` stating: (1) Rayito never operates a
hosted service on behalf of its users; (2) optional components run only in
the customer's own AWS account, shipped as independent CloudFormation
templates under `infra/`; (3) three invariants — `create()`/`connect()`/
`kill()` work without them, none of them is the source of truth for sandbox
state (`list-microvms` always wins), no data leaves the customer's account;
(4) the cost rule — any AWS-quota-or-money feature is off by default and
turned on only by an explicit SDK option (never an environment variable, a
config file, or a global setter), and with no options set the SDK creates
zero extra AWS resources and makes zero extra AWS calls compared to 0.4.0;
(5) a reference to the shared opt-in convention; (6) that phase-1 secret
injection exposes the value to sandbox code, documented with the
untrusted-code recommendation; (7) that S3 volumes are approved for a later
milestone and out of scope for M11–M14. The text SHALL contain the
substrings "apagad", "opción explícita" and "sin servidor".

#### Scenario: the ADR is present with its cost-rule wording
- **WHEN** `scripts/tests/test_optional_features_docs.py::test_architecture_documents_adr_014` reads `ARCHITECTURE.md`
- **THEN** it finds `## ADR-014` and the three required substrings

### Requirement: SPEC.md §4 and openspec/project.md reflect ADR-014
`SPEC.md` §4 SHALL cite `ADR-014` and SHALL NOT contain the sentence
"Servicio de plano de control: en M1–M5 el SDK llama a AWS directamente".
`openspec/project.md` hard rule 3 SHALL cite `ADR-014` next to its
control-plane-service clause, without relaxing any other clause of that
rule.

#### Scenario: the retired sentence is gone and ADR-014 is cited
- **WHEN** `scripts/tests/test_optional_features_docs.py::test_spec_section_4_reflects_the_adr_014_amendment` reads SPEC.md §4
- **THEN** the retired sentence is absent and `ADR-014` is present

#### Scenario: hard rule 3 carries the carve-out
- **WHEN** `scripts/tests/test_optional_features_docs.py::test_openspec_project_hard_rule_3_allows_adr_014_components` reads hard rule 3
- **THEN** it cites `ADR-014`

### Requirement: optional-features.md lists every cost-bearing option in one table
`docs/site/docs/optional-features.md` SHALL exist, SHALL be listed in
`docs/site/mkdocs.yml` nav, and SHALL contain a markdown table whose header
row is exactly:
`| Función | Estado | Opción Python | Opción TypeScript | Por defecto | Qué activa | Recursos / llamadas AWS | Coste aproximado | IAM necesario | Cómo apagarla | Dónde |`
with exactly one data row per cost-bearing function (`secrets-injection`,
`secrets-crud`, `metadata-index`, `otel-sdk`), each carrying an HTML anchor
`id="<function-id>"` and a Markdown link to that anchor from its "Función"
cell. The page SHALL also contain a `## Sin coste AWS` section listing
`rayito sandbox proxy` with its own anchor `id="local-proxy"`. Before the
table, the page SHALL restate the ADR-014 cost rule and the "Coste y
activación" docstring-block template (section titles Activa / Recursos y
llamadas AWS / Coste aproximado / IAM / Cómo apagarla / Ejemplo).

#### Scenario: the page exists, is navigable and its header matches
- **WHEN** `scripts/tests/test_optional_features_docs.py::test_optional_features_page_is_in_the_nav` and `::test_optional_features_table_header_matches_the_contract` run
- **THEN** the page exists, `mkdocs.yml` lists it, and the table header string matches exactly

#### Scenario: one row per function, with its anchor
- **WHEN** `scripts/tests/test_optional_features_docs.py::test_optional_features_table_has_one_row_per_function` parses the table
- **THEN** there are exactly 4 rows and each of the 4 function ids has a matching `id="..."` anchor and an inbound `#id` link

#### Scenario: the no-cost section lists the local proxy
- **WHEN** `scripts/tests/test_optional_features_docs.py::test_no_cost_section_lists_the_local_proxy` reads the page
- **THEN** it finds `## Sin coste AWS`, the text `rayito sandbox proxy` and the anchor `id="local-proxy"`

### Requirement: a row marked "disponible" must cite its docstring marker in its named SDK file
For every row of the `optional-features.md` table whose "Estado" cell
contains "disponible", each option symbol quoted in its "Opción Python" and
"Opción TypeScript" cells (the identifier before `=` or before `(`, stripped
of a leading `new `) SHALL appear, verbatim, inside every file named as a
code span ending in `.py` or `.ts` in that row's "Dónde" cell, and that same
file SHALL contain the literal marker "Coste y activación". A row still
"planificado" is exempt.

#### Scenario: a planned row is exempt
- **WHEN** the gate runs against this change's initial page, where all four
  rows are "planificado (Mxx)"
- **THEN** the per-row symbol/marker check runs zero times and the test
  passes

#### Scenario: a later group's row is checked automatically
- **WHEN** a later change (for example `m13-secrets`) edits the
  `secrets-injection` row to "disponible (0.5.0)" citing `` `secrets=` `` and
  `` `secrets` `` and names `clients/python/src/rayito/_secrets.py` /
  `clients/typescript/src/secrets.ts` in "Dónde"
- **THEN** `test_available_rows_cite_their_docstring_marker_in_the_named_sdk_file`
  fails unless both files contain the literal marker "Coste y activación" and
  the string `secrets=` (Python) / `secrets` (TypeScript) near it — with no
  change to `test_optional_features_docs.py` itself

### Requirement: MILESTONES.md carries acceptance criteria for M11 through M14
`MILESTONES.md` SHALL contain the headings `## M11`, `## M12`, `## M13` (at
least once, for M13a and/or M13b) and `## M14`, each stating "sin opciones =
comportamiento de 0.4.0" as an explicit acceptance criterion and an
approximate AWS budget for its acceptance e2e (M11's being $0, since it ships
no SDK option).

#### Scenario: the four milestone headers exist
- **WHEN** `scripts/tests/test_optional_features_docs.py::test_milestones_has_m11_through_m14_headers` reads `MILESTONES.md`
- **THEN** all four headings are present

### Requirement: `require_module` and `loadOptionalPeer` are the only opt-in helpers
`clients/python/src/rayito/_optional.py` SHALL export exactly one public
function, `require_module(module: str, *, extra: str, feature: str) ->
ModuleType`, which imports `module` via `importlib.import_module` and, on
`ImportError` only, raises `rayito.exceptions.InvalidArgumentException`
naming `pip install rayito[<extra>]` and chaining the original `ImportError`
as its cause; any other exception from the import SHALL propagate unchanged.
The module SHALL NOT import any optional third-party package at module load
time, and importing `rayito` SHALL NOT import any optional package as a side
effect.

`clients/typescript/src/optional.ts` SHALL export exactly one public
function, `loadOptionalPeer<T>(specifier: string, feature: string):
Promise<T>`, which resolves `specifier` via dynamic `import()` and, on a
module-resolution failure (`ERR_MODULE_NOT_FOUND` or `MODULE_NOT_FOUND`),
rejects with `InvalidArgumentError` naming `npm install <specifier>` and
chaining the original error as its `cause`; any other rejection SHALL
propagate unchanged.

Neither helper SHALL be called from any sandbox-lifecycle, command,
filesystem, code-execution or PTY module in this change.

#### Scenario: a missing Python module raises with the extra hint
- **WHEN** `clients/python/tests/unit/test_optional.py::test_missing_module_raises_invalid_argument_with_extra_hint` calls `require_module` with a module name that does not exist
- **THEN** it raises `InvalidArgumentException` whose message contains `pip install rayito[<extra>]`, the feature name and the module name, chaining the original `ImportError`

#### Scenario: importing rayito never imports an optional package
- **WHEN** `clients/python/tests/unit/test_optional.py::test_importing_rayito_never_imports_an_optional_package` imports `rayito` in a fresh subprocess and checks `sys.modules`
- **THEN** none of the known optional packages (`opentelemetry`, `opentelemetry.trace`, `opentelemetry.sdk`) are present

#### Scenario: a missing TypeScript specifier rejects with the npm hint
- **WHEN** `clients/typescript/tests/unit/optional.test.ts` calls `loadOptionalPeer` with a specifier that cannot be resolved
- **THEN** the promise rejects with `InvalidArgumentError` whose message contains the specifier and `npm install <specifier>`, with the original error as `cause`

#### Scenario: an existing specifier resolves
- **WHEN** `clients/typescript/tests/unit/optional.test.ts` calls `loadOptionalPeer("node:util", ...)`
- **THEN** the promise resolves to the `node:util` module
