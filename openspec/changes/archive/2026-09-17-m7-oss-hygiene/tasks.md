## 0. [pre-flight] Facts

- [x] 0.1 From the implementer's box, fetch `https://www.apache.org/licenses/LICENSE-2.0.txt` and confirm sha256 `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30` (202 lines); fetch `https://www.contributor-covenant.org/version/3/0/code_of_conduct/code_of_conduct.md` and confirm sha256 `ed3f8089262ea04de59d089a267bfa0822b93c96ac5a65d0647fd767a25c8422` (93 lines). If either differs, stop and record the new checksum in `design.md` "Context" before continuing
- [x] 0.2 Confirm the five MIT declarations are still exactly as listed in `design.md` D1 (`grep -n MIT Cargo.toml clients/python/pyproject.toml kernel-sidecar/pyproject.toml clients/typescript/package.json clients/typescript/LICENSE`) and that no other manifest declares a licence

## 1. [licence] Apache-2.0 in the five places, LICENSE, NOTICE, gate (design D1–D3)

- [x] 1.1 Root `LICENSE`: the verbatim apache.org text (unmodified, LF line endings); copy byte-identical to `clients/python/LICENSE`, `crates/rayd/LICENSE`, and **replace** `clients/typescript/LICENSE` (MIT text removed)
- [x] 1.2 Root `NOTICE` with exactly the D2 content (Rayito, Copyright 2026 Rayito contributors, `e2b_charts` 1.0.0 MIT attribution with the vendored path and the `VENDORED.md` pointer); copy byte-identical to `clients/python/NOTICE` and `clients/typescript/NOTICE`; `VENDORED.md` and the vendored MIT `LICENSE` untouched
- [x] 1.3 `Cargo.toml` `[workspace.package]`: `license = "Apache-2.0"`, `repository = "https://github.com/alejandro-cedeno-10/rayito"`, `homepage = "https://github.com/alejandro-cedeno-10/rayito"`; `publish = false` unchanged; `cargo metadata --no-deps --format-version 1` shows `"license":"Apache-2.0"` for the three crates
- [x] 1.4 `clients/python/pyproject.toml`: `license = "Apache-2.0"`, `license-files = ["LICENSE", "NOTICE"]`, delete the `"License :: OSI Approved :: MIT License",` classifier; every other classifier, url, dependency and tool section unchanged; `uv lock --check` still clean
- [x] 1.5 `kernel-sidecar/pyproject.toml`: `license = "Apache-2.0"`; nothing else changes (`Private :: Do Not Upload` stays)
- [x] 1.6 `clients/typescript/package.json`: `"license": "Apache-2.0"`, `"repository": {"type": "git", "url": "git+https://github.com/alejandro-cedeno-10/rayito.git", "directory": "clients/typescript"}`, `"files": ["dist", "README.md", "LICENSE", "NOTICE"]`; `pnpm install --frozen-lockfile` still succeeds (no dependency change)
- [x] 1.7 `scripts/check_license.py` (stdlib only: `tomllib`, `json`, `hashlib`, `pathlib`; `ruff check scripts` clean; Spanish messages, English identifiers; no inline comments in bodies): implement every check of D3, print `OK` or one `KO` line per problem, exit 1 on any problem, 2 on usage error; run from the repo root with no arguments (optional `--root` for tests)
- [x] 1.8 `scripts/tests/test_check_license.py` (runs under `cd clients/python && uv run pytest ../../scripts/tests`): a fixture builds a minimal repo tree in `tmp_path`; tests for the clean tree (exit 0), a `Cargo.toml` still saying MIT, a surviving `License ::` classifier, a missing `license-files`, `files` without `NOTICE` in `package.json`, a `LICENSE` copy that differs by one byte, a `NOTICE` copy that differs, and a root `LICENSE` with the wrong checksum; each failing case names the offending path in the output
- [x] 1.9 Wire `python scripts/check_license.py` into `Makefile` `lint` (right after `gen_limits.py --check`) and into the CI `check` job at the same position; run it on the real tree: `OK`
- [x] 1.10 Root `.gitattributes` forcing LF (`* text=auto eol=lf`, explicit `LICENSE`/`NOTICE` `text eol=lf`, `binary` for `png`/`jpg`/`ico`/`zip`/`gz`/`whl`) so a `core.autocrlf=true` checkout keeps `LICENSE` byte-identical; `check_license.py` appends "tiene finales CRLF … ver CONTRIBUTING.md §3" to the checksum message when the root `LICENSE` contains `\r\n`; `CONTRIBUTING.md` §3 "Windows" explains the rule and `git add --renormalize .`; tests for the CRLF message and for the `.gitattributes` rule in `test_check_license.py`; verified with a real `git init` + `core.autocrlf=true` clone of the tree in a scratch directory (gate `OK` with the attributes, `KO … CRLF` without)

## 2. [packaging] Wheel and tarball carry the licence (design D4)

- [x] 2.1 `scripts/check_wheel.py`: add the `METADATA` assertions `License-Expression: Apache-2.0`, `License-File: LICENSE`, `License-File: NOTICE`, no line starting with `Classifier: License ::`, and the archive assertions for one entry ending in `.dist-info/licenses/LICENSE` and one ending in `.dist-info/licenses/NOTICE` (suffix match); update the module docstring; `ruff check scripts` clean
- [x] 2.2 `cd clients/python && uv build && python ../../scripts/check_wheel.py dist/*.whl && uvx twine check dist/*` → `OK`, `PASSED`; paste the `License-*` lines of the produced `METADATA` into the "Notes" section at the end of this file
- [x] 2.3 `clients/python/tests/unit/test_packaging.py`: add `test_license_is_apache_2_expression` asserting `project.license == "Apache-2.0"`, `project["license-files"] == ["LICENSE", "NOTICE"]` and that no classifier starts with `License ::`; `uv run pytest tests/unit` green, `ruff`/`mypy` clean
- [x] 2.4 `clients/typescript/scripts/pack-check.mjs`: add `"package/NOTICE"` to `REQUIRED_ENTRIES`; `pnpm pack:check` lists `package/LICENSE` and `package/NOTICE` and exits 0; `pnpm lint` (Biome) clean

## 3. [community] Contributor-facing files (design D5–D9, D11)

- [x] 3.1 `CONTRIBUTING.md` with the six sections of D5 in that order; the gate commands copied verbatim from `.github/workflows/ci.yml` (Rust, Python client, sidecar, TypeScript, docs) plus the Windows block (no `make`, the toolchain exports, zig linker variables, `pnpm` only, `uv`); the DCO 1.1 text quoted verbatim from `https://developercertificate.org/`; `git commit -s` shown once
- [x] 3.2 `CODE_OF_CONDUCT.md`: Contributor Covenant 3.0 verbatim with only the two `[NOTE: …]` placeholders replaced as in D6, the CC BY-SA 4.0 attribution block kept, and a one-line Spanish preface above the title pointing to `CONTRIBUTING.md`
- [x] 3.3 `SECURITY.md`: insert `## Reportar una vulnerabilidad` between the opening blockquote and `## Modelo` with the five items of D7 (private reporting URL and what never to paste; what a useful report contains; supported-versions table; timelines 7 / 90 days; no bounty); diff shows no change from `## Modelo` downward
- [x] 3.4 `GOVERNANCE.md` per D8 (single maintainer named, ADR process, OpenSpec changes, path to maintainership, revisit at three maintainers)
- [x] 3.5 `.github/CODEOWNERS` per D9 (`*` plus `/proto/`, `/crates/`, `/clients/python/`, `/clients/typescript/`, `/AWS_API_NOTES.md`, all `@alejandro-cedeno-10`)
- [x] 3.6 `.github/ISSUE_TEMPLATE/bug_report.yml`, `feature_request.yml` (form syntax, fields of D9, `required: true` on SDK, version, `agent_version`, image and reproduction) and `config.yml` (`blank_issues_enabled: false`, two contact links); validate that every YAML file parses: `uv run --with pyyaml python -c "import sys, yaml; [yaml.safe_load(open(p, encoding='utf-8')) for p in sys.argv[1:]]" .github/ISSUE_TEMPLATE/*.yml .github/dependabot.yml`
- [x] 3.7 `.github/PULL_REQUEST_TEMPLATE.md` with the checklist of D9, every item a `- [x]`
- [x] 3.8 `.github/dependabot.yml` per D9: `version: 2`; entries `cargo` `/`, `uv` `/clients/python`, `uv` `/kernel-sidecar`, `pip` `/kernel-sidecar`, `npm` `/clients/typescript`, `github-actions` `/`, `docker` `/image`; weekly; one group per entry (`patterns: ["*"]`, `update-types: ["minor", "patch"]`); `open-pull-requests-limit: 5`; `commit-message.prefix: "chore(deps)"`; parses as YAML
- [x] 3.9 `docs/RELEASING.md` with the six sections of D11 (what and tags; PyPI incl. the pending-publisher note; npm prerequisites and the manual first publish → then trusted publisher; crates.io later; GitHub one-time settings incl. Private Vulnerability Reporting and the DCO app; per-component release checklist); it states explicitly that this change reserves nothing and that badges stay "not found" until the first publish

## 4. [architecture] Docs the owner asked for (design D12–D14)

- [x] 4.1 `ARCHITECTURE.md`: insert `## Qué corre dónde` after "Vista general" and before "Capa 1 — Imagen base" with the process diagram, the seven-row table and the "Por qué el sidecar es Python" paragraph of D12 (links to ADR-002; no restatement of its consequences beyond one clause); the existing "Vista general" diagram is untouched
- [x] 4.2 `README.md`: insert `## Cómo funciona` after the `rayito.e2b` snippet and before the "Estado" paragraph with the client ↔ VM diagram and the three statements of D13 (the SDK never runs inside the sandbox; where `run_code` / `commands` / `pty` execute and as which uid; languages: Python, TypeScript, any other via the `.proto`), ending with pointers to `ARCHITECTURE.md` "Qué corre dónde" and the docs site
- [x] 4.3 `docs/site/docs/concepts.md`: prepend `## Qué corre dónde` (diagram + table copied from 4.1, three-sentence ADR-002 summary, the line "si difieren, manda `ARCHITECTURE.md`") and `## Desde qué lenguajes se usa Rayito` (the four-row comparison table with E2B, Daytona, Modal, Rayito; the three requirements for "any language"; the Rust-later / Go-no / `rayito-proto`-deferred position from report §2); every existing section kept verbatim below
- [x] 4.4 `cd clients/python && uv run --group docs mkdocs build -f ../../docs/site/mkdocs.yml --strict --site-dir ../../docs/site/_build` green (no new nav entry needed; no links outside `docs/site/docs`)

## 5. [changelogs, badges, milestones] (design D10, D15)

- [x] 5.1 `clients/typescript/CHANGELOG.md` (Keep a Changelog 1.1.0, Spanish; `[Unreleased]` with the Apache-2.0 change; `[0.0.5] - 2026-09-16` summarising the M6 surface, marked unpublished) and `crates/rayd/CHANGELOG.md` (`[Unreleased]` with the licence change; `[0.1.0] - 2026-09-16` listing services and hooks by milestone and the `agent_version` / `rayito-base` ≥ 10.0 compatibility line)
- [x] 5.2 Root `CHANGELOG.md` pointer: the three component changelogs, the tag prefixes `python-v` / `typescript-v` / `rayd-v`, image versions as opaque AWS build numbers in `MILESTONES.md`
- [x] 5.3 `clients/python/CHANGELOG.md`: `[Unreleased]` → `### Changed` "Licencia MIT → Apache-2.0 (PEP 639 `License-Expression`, `NOTICE` en la wheel)"; the "Publicación" subsection keeps its three steps and adds "pasos canónicos en `docs/RELEASING.md`"
- [x] 5.4 `README.md`: the four shields.io badges of D10 on the first line under the title, in the order CI, PyPI, npm, licence, each linked
- [x] 5.5 `MILESTONES.md`: add `## M7 — Preparación open source` (after M6, before "Orden de trabajo dentro de cada hito") with the report §5 table (seven changes + deferred row) and the pointer to `docs/research/2026-09-m7-oss-readiness.md`; mark `m7-oss-hygiene` as in progress with its acceptance evidence being the D16 gate set. If the section already exists, edit only this change's row
- [x] 5.6 `SECURITY.md` "Cadena de suministro" row "Código vendorizado" gains "y listado en `NOTICE`"; `SPEC.md` and `openspec/project.md` need no edit (verify by grep for `MIT`: zero hits outside `_vendor/` and the research report)

## 6. [gates] Everything green on the box (design D16)

- [x] 6.1 Rust: `cargo fmt --check`, `cargo clippy --workspace --all-targets -- -D warnings`, `cargo test --workspace`, `cargo zigbuild --release --target aarch64-unknown-linux-musl -p rayd` (no source change expected; confirms the manifest edit is harmless)
- [x] 6.2 Python client: `cd clients/python && uv run pytest tests/unit && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests`; scripts: `uv run pytest ../../scripts/tests -p no:cacheprovider`; `uvx ruff check scripts` from the root
- [x] 6.3 Sidecar: `cd kernel-sidecar && uvx ruff check . && uvx ruff format --check . && uv run mypy src`
- [x] 6.4 TypeScript: `cd clients/typescript && pnpm lint && pnpm typecheck && pnpm test && pnpm build && pnpm pack:check`
- [x] 6.5 `python scripts/check_license.py` → `OK`; `python scripts/gen_limits.py --check` still clean; docs strict build (4.4) green
- [x] 6.6 `grep -rn "MIT" --include=*.toml --include=*.json --include=LICENSE Cargo.toml clients kernel-sidecar/pyproject.toml crates` returns only the vendored `_vendor/e2b_charts/LICENSE`
- [x] 6.7 `openspec validate m7-oss-hygiene --strict --no-interactive` still passes with the ticked tasks; fill the "Notes" section below (checksums re-verified in 0.1, `METADATA` lines from 2.2, `pnpm pack:check` listing, gate durations)

## Notes

Implementado el 2026-09-16 en Windows (Git Bash, toolchain local),
sin git ni AWS: el cambio no tiene superficie de runtime (D16).

- **0.1 checksums re-verificados** en el box: `LICENSE-2.0.txt` sha256
  `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30`
  (202 líneas, LF); `code_of_conduct.md` 3.0 sha256
  `ed3f8089262ea04de59d089a267bfa0822b93c96ac5a65d0647fd767a25c8422`
  (93 líneas). Coinciden con `design.md`. DCO 1.1 de developercertificate.org
  comparado byte a byte con el bloque de `CONTRIBUTING.md`: idéntico.
  `CODE_OF_CONDUCT.md` difiere del original sólo en las dos líneas de los
  placeholders (`diff` desde la línea del título).
- **1.3** `cargo metadata --no-deps`: `rayito-proto`, `rayd-core` y `rayd`
  con `"license": "Apache-2.0"`. `uv lock --check` limpio;
  `pnpm install --frozen-lockfile` sin cambios.
- **2.2 `METADATA` de la wheel** (`uv build`, `uv_build`, `Metadata-Version: 2.4`):
  `License-Expression: Apache-2.0`, `License-File: LICENSE`,
  `License-File: NOTICE`; sin `Classifier: License ::`; entradas
  `rayito-0.1.0.dist-info/licenses/LICENSE` y `.../licenses/NOTICE`.
  `check_wheel.py` → `OK (64 entradas)`; `twine check` → `PASSED` (wheel y
  sdist). Sonda negativa (wheel sin `NOTICE`, con `License: MIT` y
  clasificador): `KO` con cuatro líneas nombrando `License-Expression`,
  `License-File: NOTICE`, el clasificador y la entrada `licenses/NOTICE`.
- **2.4 `pnpm pack:check`** (exit 0): `package/LICENSE`, `package/NOTICE`,
  `package/README.md`, `package/dist/index.cjs`, `package/dist/index.cjs.map`,
  `package/dist/index.d.cts`, `package/dist/index.d.mts`,
  `package/dist/index.mjs`, `package/dist/index.mjs.map`,
  `package/package.json`.
- **3.6** `uv run --with pyyaml ...` sobre `ISSUE_TEMPLATE/*.yml` y
  `dependabot.yml`: parsean; `dependabot.yml` tiene 7 entradas `updates`;
  `bug_report.yml` exige `sdk`, `sdk_version`, `agent_version`, `image`,
  `region`, `reproduction`, `expected_actual`.
- **4.3** `concepts.md`: `diff` desde `## Vida del sandbox vs. política de
  idle` contra la copia previa: sin diferencias. **3.3** `SECURITY.md`:
  `diff` desde `## Modelo`: sólo la fila "Código vendorizado" (5.6).
- **Gates (duraciones en el box)**: `cargo fmt --all --check` + `clippy
  --workspace --all-targets -D warnings` 7,6 s; `cargo test --workspace`
  331 tests (62 + 4 `rayd`, 13 `m1_hello`, 252 `rayd-core`) en 3,5 s;
  `cargo zigbuild --release --target aarch64-unknown-linux-musl -p rayd`
  41 s (binario 4 698 744 B; el único warning es el aviso del linker de zig
  "ignoring deprecated linker optimization setting", previo a este cambio);
  `buf lint` limpio; `uv run pytest tests/unit` **617 passed** en 193,6 s;
  `ruff check` / `ruff format --check` / `mypy src tests` (81 ficheros)
  limpios; `scripts/tests` **58 passed** (14 nuevos de
  `test_check_license.py`) en 1,5 s; `uvx ruff check scripts` y `format
  --check` limpios (16 ficheros); sidecar `ruff` / `format` / `mypy src`
  limpios; TypeScript `pnpm lint` (65 ficheros), `typecheck`, `test` (20
  ficheros, **292 passed**), `build` (1,5 s) y `pack:check` limpios, 32,6 s
  en total; `python scripts/check_license.py` → `OK`; `gen_limits.py
  --check` limpio; `mkdocs build --strict` en 0,9 s.
- **6.6** `grep -rn MIT` sobre los manifiestos y `LICENSE` del árbol (sin
  `.venv`/`node_modules`): sólo `_vendor/e2b_charts/LICENSE` y los SBOM
  `crates/*/*.cdx.json` generados por `m7-supply-chain` (licencias de
  dependencias, no declaraciones de Rayito). Menciones restantes en prosa:
  las del `e2b_charts` vendorizado y la `python-release` vigente que este
  cambio reemplaza al archivar.
- **6.7** `openspec validate m7-oss-hygiene --strict --no-interactive`:
  `Change 'm7-oss-hygiene' is valid`.
- **1.10 (revisión: CRLF en Windows)**. Reproducido en el box: una copia del
  árbol pasada por `git init` + `commit` + `clone` con `core.autocrlf=true`
  sale con `LICENSE` `w/crlf` (202 `\r`) y el gate falla con sha256
  `3ddf9be5…`; la misma prueba con el `.gitattributes` de la raíz sale
  `w/lf` (`git ls-files --eol`: `attr/text eol=lf`), 0 `\r`, gate `OK`.
  El gate sigue comparando bytes exactos; sólo añade el aviso "tiene
  finales CRLF" cuando ésa es la causa. `scripts/tests`: **71 passed**
  (3 nuevos). Hallazgo colateral: en el árbol de trabajo actual (sin git)
  hay 256 ficheros de texto con CRLF escritos por herramientas en Windows
  (`crates/**/*.rs`, cliente Python, sidecar, specs archivadas, logs de
  `spike/`); `LICENSE`, `NOTICE` y los cinco manifiestos son LF. `rustfmt`
  y `ruff format` no distinguen finales de línea; Biome sí, y el único
  fichero en su alcance con CRLF, `clients/typescript/src/version.ts`
  (escrito por `m7-supply-chain` 6.2), se normalizó a LF sin cambiar su
  contenido. El primer `git add` con este `.gitattributes` normalizará el
  resto a LF (avisos "CRLF will be replaced by LF", esperados). En Git
  Bash de este box `cat`/`grep` eliminan el `\r` al leer: para auditar
  finales de línea usar Python (`read_bytes().count(b"\r\n")`).
- **Fuera del alcance, para el propietario**: publicar (PyPI/npm), activar
  Private Vulnerability Reporting, instalar la app DCO y crear los
  environments (`docs/RELEASING.md` §5); decidir si `NOTICE` nombra una
  entidad legal en vez de "Rayito contributors" (`design.md`, Open
  Questions). `Cargo.toml` declara `repository`/`homepage` en
  `[workspace.package]` pero los crates no los heredan (no se pidió;
  `publish = false`).
