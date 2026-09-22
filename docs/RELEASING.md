# Publicar Rayito

Pasos manuales para publicar cada componente. **Este documento no reserva
nombres ni publica nada**: el cambio `m7-oss-hygiene` deja el repositorio
listo para publicar y describe quién hace cada paso y cómo; la primera
publicación es una decisión del mantenedor (`GOVERNANCE.md`). Hasta que
exista la primera release, las insignias de PyPI y npm del `README.md`
muestran "not found": es el comportamiento esperado, no un fallo.

## 1. Qué se publica y con qué tag

| Componente | Dónde | Tag | Quién lo sube |
|---|---|---|---|
| `clients/python` (paquete `rayito`) | PyPI | `python-v<versión>` | `.github/workflows/release.yml` con Trusted Publishing |
| `clients/typescript` (paquete `rayito`) | npm | `typescript-v<versión>` | la primera vez el mantenedor a mano (§3); después el job `typescript` de `.github/workflows/release.yml` (npm trusted publishing) |
| `crates/rayd` (binario `rayd` + `rayito-image.zip`) | GitHub Release del tag (creada por release-please) | `rayd-v<versión>` | el job `rayd` de `release.yml`: `rayd` (`cargo auditable`), `rayito-image.zip`, `rayd.cdx.json`, los dos bundles cosign y `SHA256SUMS` |
| imagen `rayito-base` | tu cuenta de AWS | ninguno | `make image-publish` (`rayito image publish`, `scripts/publish_image.py` como shim); la versión de imagen es un número de build opaco de AWS, anotado en `MILESTONES.md` |

Regla de paridad: **el tag debe ser igual a la versión del manifiesto**
(`pyproject.toml`, `package.json`, `Cargo.toml` `[workspace.package]`).
`release.yml` lo comprueba para los tres componentes antes de construir
nada, y release-please es quien mueve las versiones, así que en la práctica
nadie edita un manifiesto a mano.

### Flujo automatizado (release-please → tags → `release.yml`)

1. Los commits en `main` siguen Conventional Commits (`feat:`, `fix:`,
   `docs:`, `chore:`…). `.github/workflows/release-please.yml` mantiene
   abierto un único PR de release (`release-please-config.json`, modo
   manifest, plugin `linked-versions`): los tres componentes suben **a la
   misma versión** (un `feat:` desde 0.1.0 → 0.2.0 para Python, TypeScript y
   `rayd`, incluida la subida de TypeScript desde 0.0.5) y el PR actualiza
   `pyproject.toml`, `src/rayito/_version.py`, `package.json`,
   `src/version.ts`, `Cargo.toml` `[workspace.package]`, las tres entradas
   de `Cargo.lock` y los tres `CHANGELOG.md` (secciones Added / Fixed /
   Changed / Documentation).
2. Al fusionar el PR, release-please crea los tags `python-v<v>`,
   `typescript-v<v>` y `rayd-v<v>` y una GitHub Release por tag.
3. Cada tag dispara `release.yml`, que publica sólo su componente (PyPI,
   npm o los assets firmados de la release de `rayd`).

**Token.** Los tags creados con el `GITHUB_TOKEN` por defecto **no disparan**
otros workflows. Dos caminos, los dos soportados:

- Guardar un PAT fine-grained (permisos `contents: write` y `pull-requests:
  write`, sólo este repositorio) como secreto `RELEASE_PLEASE_TOKEN`:
  release-please lo usa y los tags disparan `release.yml` solos.
- Sin el secreto, lanzar `release.yml` a mano por cada tag: Actions →
  release → Run workflow → **Use workflow from: el tag** → `tag` = el mismo
  tag → `dry_run` = false. El job `resolve` se niega a publicar si el run
  no arranca desde el propio tag (la identidad del certificado de cosign
  lleva el ref del run y la receta de verificación exige `refs/tags/rayd-v`).

**Ensayo.** `Run workflow` con `dry_run` = true (por defecto) desde cualquier
rama cuyos manifiestos ya lleven la versión del `tag` (la rama del PR de
release, por ejemplo): construye, comprueba, firma (`rayd`) y sube los
artefactos al run sin publicar nada.

**Verificación** de lo publicado: `docs/site/docs/verify.md` (cosign,
`cargo audit bin`, attestations de PyPI, `npm view … dist.attestations`).

## 2. PyPI (`rayito`, Python)

Una sola vez, antes del primer tag:

1. Registrar el *Trusted Publisher* en PyPI para el proyecto `rayito`:
   propietario `alejandro-cedeno-10`, repositorio `rayito`, workflow `release.yml`,
   environment `pypi`. PyPI admite registrarlo como **pending publisher**
   antes de que el proyecto exista (Your account → Publishing → "Add a new
   pending publisher"), así que **no hace falta ninguna publicación con
   token**: el primer `release.yml` crea el proyecto.
2. Crear el environment `pypi` en GitHub (Settings → Environments); sin
   secretos.

En cada release:

```bash
cd clients/python && uv build
python scripts/check_wheel.py clients/python/dist/*.whl   # desde la raíz
uvx twine==7.0.0 check clients/python/dist/*
git tag python-v<versión> && git push origin python-v<versión>
```

El job `build` de `release.yml` repite las tres comprobaciones y verifica
que el tag coincide con `pyproject.toml`; el job `publish` sube con OIDC. Las
attestations PEP 740 son automáticas con `pypa/gh-action-pypi-publish` ≥
v1.11. `workflow_dispatch` ensaya sólo el `build`.

## 3. npm (`rayito`, TypeScript)

Requisitos verificados (docs.npmjs.com/trusted-publishers): npm CLI ≥
11.5.1 y Node ≥ 22.14.0 en el runner, permiso `id-token: write` en el job,
provenance automática (no hace falta `publishConfig.provenance`). El
*trusted publisher* se configura **desde la página de settings del paquete
ya existente**, así que la primera publicación es manual:

1. El mantenedor, con un token granular de npm (publish sobre `rayito`) y
   2FA:

   ```bash
   cd clients/typescript
   pnpm install --frozen-lockfile && pnpm build && pnpm pack:check && pnpm pack
   npm publish rayito-<versión>.tgz --access public
   ```

   `pnpm` 9.15.4 sigue siendo el gestor para todo; sólo el comando de
   publicación usa `npm` (pnpm 9 no es un publicador OIDC). Revocar el token
   al terminar.
2. En npmjs.com → paquete `rayito` → Settings → Trusted Publisher: GitHub
   Actions, cuenta `alejandro-cedeno-10`, repositorio `rayito`, workflow
   `release.yml`, environment `npm`.
3. Crear el environment `npm` en GitHub (con required reviewers). El job
   `typescript` de `release.yml` (disparado por `typescript-v*`) publica con
   `npm publish` sobre Node 24 (npm ≥ 11.5.1, comprobado en el job); a partir
   de ahí no se vuelve a usar ningún token.

## 4. crates.io

No ahora. `rayd` y `rayd-core` son `publish = false` y lo seguirán siendo.
`rayito-proto` podría publicarse más adelante (informe M7 §2, diferido) y
necesitaría entonces su propio `LICENSE`, `description`, `repository` y
`publish = true` en `crates/rayito-proto/Cargo.toml`. El ensayo es:

```bash
cargo publish --dry-run -p rayito-proto
```

## 5. GitHub, una sola vez

- Activar **Private Vulnerability Reporting** (Settings → Code security →
  "Private vulnerability reporting"): es la vía de `SECURITY.md`.
- Instalar la **app DCO** (https://github.com/apps/dco) y marcar su check
  como obligatorio en la protección de `main`.
- **Protección de `main`**: PR obligatorio, checks `ci` (`lint + test
  (x86_64)`, `typescript client …`, `kernel-sidecar …`, `rayd
  aarch64-unknown-linux-musl`, `docs site …`, `cargo-deny`, `dependency
  audit`, `test on aarch64 (ubuntu-24.04-arm)`) y DCO obligatorios, sin
  force-push.
- Crear los environments `pypi`, `npm` y `e2e`, los tres con required
  reviewers (`e2e` es el del workflow `e2e.yml`; su rol OIDC, variables y
  presupuesto están en `infra/README.md`).
- Secretos del repositorio: **ninguno obligatorio** (PyPI, npm y AWS van
  por OIDC). Opcional: `RELEASE_PLEASE_TOKEN` (PAT fine-grained) para que
  los tags de release-please disparen `release.yml` sin intervención (§1).
- El primer commit del repositorio debe ser `chore: import rayito 0.2.0`
  (no `feat:`), para que el primer PR de release-please refleje sólo el
  trabajo posterior (los manifiestos ya llevan 0.2.0, la versión aceptada en
  M7; 0.1.0 nunca se publicó).
- Opcional: activar Discussions para preguntas que no son issues.

## 6. Checklist de release (los tres componentes a la vez)

1. Revisar el PR de release-please: changelogs generados desde los commits
   (completar a mano lo que falte, en el propio PR) y versiones en
   `pyproject.toml`, `src/rayito/_version.py`, `package.json`,
   `src/version.ts`, `Cargo.toml` `[workspace.package]` y `Cargo.lock`
   idénticas.
2. Gates verdes en CI sobre ese PR (`CONTRIBUTING.md` §3), incluidos
   `python scripts/check_license.py`, `cargo-deny`, la auditoría de
   dependencias y `cargo test --locked` (un `Cargo.lock` que release-please
   no haya actualizado falla aquí, no en la release).
3. e2e verde contra AWS real sobre la imagen que la release requiere
   (`e2e.yml` con `template_version`, o `make test-e2e` local), con el coste
   anotado en el PR (≈ $0,03 por pasada).
4. Fusionar el PR: release-please crea los tres tags y las tres releases.
5. Comprobar que `release.yml` corrió una vez por tag (o lanzarlo a mano
   desde cada tag, §1) y verificar lo publicado con `docs/site/docs/verify.md`;
   la insignia del `README.md` deja de decir "not found".
6. Publicar la imagen desde el árbol etiquetado (`make image-publish`, con
   `BASE_IMAGE_VERSION`; equivale a `rayito image publish`) y anotar la
   versión de imagen en `MILESTONES.md`.
7. Si la release sube el `rayd` mínimo que un SDK exige, añadir la fila a
   `rayito.cli._compat.COMPATIBILITY` **y** a la tabla "Compatibilidad SDK ↔
   rayd ↔ imagen" de `docs/site/docs/limits.md` a la vez
   (`tests/unit/cli/test_compat.py` falla si divergen). La versión de imagen
   no entra en la tabla: es el contador de builds de cada imagen en cada
   cuenta.
