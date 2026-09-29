# Tasks — m10-security-supply-chain

The whole change is local plus two `workflow_dispatch` dry runs on
GitHub-hosted runners: no AWS call, no image publish, no e2e against a real
registry.

## 0. [prepare] Ground truth and toolchain

- [x] 0.1 Re-read `docs/SECURITY_AUDIT.md` §8 rows C-10 and C-12 and confirm
      nothing else marked open in that table is touched
- [x] 0.2 Record tool versions used: `uv 0.12.18`, `openspec 1.10.0`,
      `actionlint 1.7.12`, `pnpm 9.15.4`
- [x] 0.3 Record baseline counts before touching anything:
      `scripts/tests` 191 passed, `kernel-sidecar` sidecar suite 86 passed,
      TypeScript `pnpm test` 877 passed, `python3 scripts/check_pins.py`
      OK 8 files

## 1. [C-10-npmrc] `clients/typescript/.npmrc`

- [x] 1.1 Add `clients/typescript/.npmrc` (`ignore-scripts=true` + a
      one-line comment)
- [x] 1.2 Clean-tree gate: `rm -rf node_modules dist && pnpm install
      --frozen-lockfile && pnpm lint && pnpm typecheck && pnpm build && pnpm
      test && pnpm pack:check` — all green, no "ignored build scripts"
      warning; file kept (design.md D3)

> **Evidencia.** `pnpm install --frozen-lockfile` sobre árbol limpio: sin
> avisos de scripts ignorados. `pnpm lint`: "Checked 141 files... No fixes
> applied." `pnpm typecheck`: limpio. `pnpm build`: dos bundles (CJS/ESM),
> el único warning es el `MIXED_EXPORTS` preexistente de `tsdown`, no
> relacionado. `pnpm test`: **877 passed** (41 ficheros). `pnpm pack:check`:
> el listado de `package/` sin cambios.

## 2. [C-12] Hash-pinned, wheels-only sidecar installs

- [x] 2.1 Regenerar `kernel-sidecar/requirements.txt` con
      `uv pip compile --generate-hashes` (design.md D4); diff de
      `nombre==versión` contra el fichero anterior vacío (mismas versiones)
- [x] 2.2 Regenerar `kernel-sidecar/requirements-poly.txt` igual, con
      `--no-deps` y el propio fichero como entrada; mismas cuatro versiones
- [x] 2.3 Confirmar cobertura de hash más allá de aarch64 (numpy: 607
      `--hash=`) y que `pip install --require-hashes --no-deps
      --only-binary=:all:` resuelve en un venv macOS real (design.md D4)
- [x] 2.4 `image/Dockerfile`: las dos capas `pip install -r` ganan
      `--require-hashes --no-deps --only-binary=:all:`; comentarios
      actualizados
- [x] 2.5 `scripts/check_pins.py`: gate 5 (`unhashed_pip_installs`,
      `unhashed_requirement_pins`), documentado en el docstring del módulo,
      `kernel-sidecar/requirements*.txt` en `DEFAULT_PATHS`
- [x] 2.6 `scripts/tests/test_check_pins.py`: casos nuevos para la gate 5 +
      `test_the_sidecar_requirements_are_hash_pinned_and_version_stable`

> **Evidencia.** Diff de versiones vacío (idéntico antes/después) para ambos
> ficheros. `uv run --with-requirements requirements.txt pytest`: **86
> passed**. `uv run --with-requirements requirements-poly.txt pytest -m
> kernel`: 4 failed / 9 passed / 1 skipped, **idéntico en `git stash` sobre
> el árbol sin tocar** (fallo preexistente, "sólo Linux" según
> `pyproject.toml`; ver design.md D4). `uvx pip-audit==2.10.1 -r
> requirements.txt --require-hashes --disable-pip --strict` y lo mismo para
> `-poly`: **"No known vulnerabilities found"** en ambos (el `--no-deps` de
> la invocación real del job `audit` de CI se explica en design.md D4:
> artefacto de resolución local en macOS, no un hallazgo real; CI corre en
> `ubuntu-24.04`, donde el marcador de plataforma nunca dispara).
> `python3 scripts/check_pins.py`: **OK 10 ficheros**. `python -m pip
> install --dry-run --require-hashes --no-deps --only-binary=:all: -r
> requirements.txt` (venv Python 3.12 real): resuelve y valida los 55
> paquetes sin ningún hash ausente; lo mismo para `-poly` (4 paquetes).

## 3. [C-10] `release.yml` build/publish split

- [x] 3.1 `python` → `python-build` (sin `environment`, sin `id-token`) +
      `python-publish` (`environment: pypi`, `id-token: write`, sin
      checkout, verifica `sha256sum -c` antes de publicar) — design.md D1
- [x] 3.2 `typescript` → `typescript-build` (`pnpm install
      --frozen-lockfile --ignore-scripts`) + `typescript-publish`
      (`npm publish ... --ignore-scripts`, verifica `sha256sum -c` antes)
- [x] 3.3 `actions/download-artifact` clavado por SHA de 40 hex
      (`3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c # v8.0.1`, resuelto con
      `gh api repos/actions/download-artifact/git/ref/tags/v8.0.1`)
- [x] 3.4 `resolve` y `rayd` byte-idénticos; cabecera del workflow
      actualizada; `docs/RELEASING.md` dice que el Trusted Publisher no
      necesita reconfigurarse (design.md D1)
- [x] 3.5 `scripts/tests/test_release_workflow.py` (PyYAML): las seis
      aserciones de la asignación de tarea
- [x] 3.6 `python3 scripts/check_pins.py` y `actionlint -no-color` limpios
      sobre el workflow nuevo
- [x] 3.7 Ensayo end-to-end: `gh workflow run release.yml --ref
      m10/security-supply-chain -f tag=typescript-v0.3.2 -f dry_run=true` y
      lo mismo para `python-v0.3.2`; los dos jobs `*-build` en verde y con
      su artefacto subido, los dos `*-publish` **skipped**

> **Evidencia.** `python3 scripts/check_pins.py`: OK 10 ficheros (sin
> cambios de conteo de findings). `actionlint -no-color`: sin salida (limpio).
> `pytest scripts/tests/test_release_workflow.py`: **8 passed**. Los
> `permissions.id-token: write` del workflow son exactamente
> `{python-publish, typescript-publish, rayd}` (aserción directa del test).
> Los dos ensayos de `workflow_dispatch` se registran en la nota del PR con
> la URL del run.

## 4. [docs] C-10/C-12 cierran en la documentación

- [x] 4.1 `docs/SECURITY_AUDIT.md` §8: C-10 y C-12 pasan a "Hecho" (✔ §9);
      §9 gana sus dos filas; "Lo que sigue abierto" ya no las nombra (C-06 y
      el resto, intactos)
- [x] 4.2 `SECURITY.md` T10: nombra los installs clavados por hash y el
      split build/publish
- [x] 4.3 `docs/RELEASING.md` §1-§3: describe los jobs `*-build`/`*-publish`,
      que el ensayo salta la publicación y que el Trusted Publisher no se
      reconfigura
- [x] 4.4 `scripts/tests/test_security_docs.py`: nueva aserción sobre la
      tabla de §9 y sobre la retirada de C-10/C-12 de "Lo que sigue abierto"
- [x] 4.5 `CHANGELOG.md` (raíz), `clients/python/CHANGELOG.md`,
      `clients/typescript/CHANGELOG.md`: sección `## [Unreleased]`

> **Evidencia.** `pytest scripts/tests/test_security_docs.py`: ver conteo en
> la nota final del PR.

## 5. [openspec] Validación

- [x] 5.1 `openspec validate m10-security-supply-chain --strict`
