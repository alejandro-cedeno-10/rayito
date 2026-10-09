# Publicar Rayito

Pasos para publicar cada componente. `rayito` está publicado en PyPI y npm
desde 0.3.0 y cada release sale de `.github/workflows/release.yml` (PyPI
Trusted Publishing, npm trusted publishing con provenance, assets firmados de
`rayd`); cada versión es una decisión del mantenedor (`GOVERNANCE.md`). Las
secciones de configuración inicial (§2 y §3) se conservan para un fork o
para rehacer la configuración desde cero. La política de versionado,
obsolescencia y soporte que siguen las releases está en
[`docs/site/docs/limits.md`](site/docs/limits.md#versionado-y-soporte)
("Versionado y soporte").

## 1. Qué se publica y con qué tag

| Componente | Dónde | Tag | Quién lo sube |
|---|---|---|---|
| `clients/python` (paquete `rayito`) | PyPI | `python-v<versión>` | `.github/workflows/release.yml` con Trusted Publishing (jobs `python-build` + `python-publish`) |
| `clients/typescript` (paquete `rayito`) | npm | `typescript-v<versión>` | la primera vez el mantenedor a mano (§3); después los jobs `typescript-build` + `typescript-publish` de `.github/workflows/release.yml` (npm trusted publishing) |
| `crates/rayd` (binario `rayd` + `rayito-image.zip`) | GitHub Release del tag (creada por release-please) | `rayd-v<versión>` | los jobs `rayd-build` (compila, sin credenciales), `rayd-sign` (environment `release`, cosign keyless, sin checkout) y `rayd-upload` (environment `release`, sin checkout, sin `--clobber`) de `release.yml`: `rayd` (`cargo auditable`), `rayito-image.zip` (con `licenses/`), `rayd.cdx.json`, `LICENSE`, `NOTICE`, `THIRD_PARTY_LICENSES.md`, los dos bundles cosign, `SHA256SUMS` y su bundle `SHA256SUMS.sigstore.json` |
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
  write`, sólo este repositorio, con caducidad corta) como secreto
  `RELEASE_PLEASE_TOKEN`: release-please lo usa y los tags disparan
  `release.yml` solos. Ojo con su alcance: con `main` sin aprobaciones
  obligatorias, ese token puede abrir y fusionar un PR en cuanto pasen los
  checks, crear tags `rayd-v*` y reemplazar assets de releases ya publicadas
  (las releases inmutables están apagadas), y lo fusionado en `main` llega a
  GitHub Pages sin revisor. Lo único que no puede es aprobar los
  environments `pypi`, `npm` y `release`, así que no publica ni firma nada.
  Rótalo al caducar y en cuanto sospeches de él (Settings → Developer
  settings → Fine-grained tokens), y ver "Ajustes pendientes" abajo.
- Sin el secreto, lanzar `release.yml` a mano por cada tag: Actions →
  release → Run workflow → **Use workflow from: el tag** → `tag` = el mismo
  tag → `dry_run` = false. El job `resolve` se niega a publicar si el run
  no arranca desde el propio tag (la identidad del certificado de cosign
  lleva el ref del run y la receta de verificación exige
  `refs/tags/rayd-v<versión>` exacto).

**El commit del tag tiene que estar en `main`.** Al publicar, `resolve`
comprueba `git merge-base --is-ancestor "$GITHUB_SHA" origin/main` y falla si
no: un tag empujado sobre otra rama (con un PAT filtrado, por ejemplo) no
publica código que no pasó por la revisión y los checks de `main`. Los tags
de release-please siempre apuntan al merge del PR de release, así que el
flujo normal no lo nota.

**Firma y subida de `rayd` con aprobación.** `rayd-sign` y `rayd-upload`
corren en el environment `release` (required reviewer, política de tags
`rayd-v*`): la release espera a que el mantenedor apruebe el despliegue, como
`pypi` y `npm`, y lo aprueba **antes de firmar**, porque sin esa aprobación
GitHub no entrega el token OIDC con el que Sigstore emite el certificado de
`release.yml@refs/tags/rayd-v<versión>` (sec-supply-chain-followups,
SC-A01). Son dos aprobaciones por release de `rayd` (firma y subida). Nunca
reemplaza un asset: si la release ya tiene uno con el mismo nombre, falla
antes de subir nada. Si una subida se cortó a medias, borra a mano los assets
parciales de esa release (`gh release delete-asset <tag> <asset>`) y vuelve a
lanzar el job.

**Ensayo.** `Run workflow` con `dry_run` = true (por defecto) desde cualquier
rama cuyos manifiestos ya lleven la versión del `tag` (la rama del PR de
release, por ejemplo): construye, comprueba y sube los artefactos sin firmar
al run, sin firmar ni publicar nada. Para `python` y `typescript` (M10, C-10)
esto se ve en dos jobs: `python-build`/`typescript-build` corren siempre y
suben su artefacto, y `python-publish`/`typescript-publish` (los únicos con
el token OIDC) se marcan **skipped** por su propio `if:` — ningún job de
publish llega a ejecutarse en un ensayo, así que tampoco descarga ni verifica
nada. `rayd` corre sólo `rayd-build`: `rayd-sign` y `rayd-upload` se
saltan, así que un ensayo nunca obtiene un certificado de Sigstore (antes
firmaba con la identidad del ref del ensayo, y un ensayo lanzado desde un
tag `rayd-v*` firmaba con la identidad exacta que verifican los usuarios).

**Sin cachés.** Ningún job de `release.yml` restaura una caché de Actions: un
run de tag restaura las del ámbito de `main`, que cualquier job de `main`
puede escribir. zig, `cargo-deny` y `cargo-about` llegan por las acciones
locales `.github/actions/zig`, `.github/actions/cargo-deny` y
`.github/actions/cargo-about` (sha256 fijado), las
herramientas de cargo se compilan en cada release (unos minutos más) y twine
sale de `.github/release/requirements-twine.txt` con `--require-hashes`. uv
llega por `.github/actions/setup-uv` (versión y sha256 fijados, la misma
acción en todos los workflows) y `UV_LOCKED=1` impide que re-bloquee un
`uv.lock` desfasado.

**`dist/` exacto.** `python-build` y `python-publish` comprueban que `dist/`
contiene exactamente la wheel y el sdist de la versión del tag, los dos en
`SHA256SUMS`: `sha256sum -c` no ve un fichero que no esté listado, y
`gh-action-pypi-publish` sube todo lo que haya en el directorio
(SC-A04).

**La imagen de `gh-action-pypi-publish`.** La acción va fijada por SHA, pero
ejecuta twine en un contenedor de `ghcr.io/pypa/gh-action-pypi-publish`
direccionado por etiqueta, no por digest, dentro del job que tiene el token
OIDC de PyPI. Es una dependencia de confianza aceptada del Trusted Publishing
(quien controle ese espacio de GHCR podría volver a subir la etiqueta), igual
que confiar en el propio PyPI (SC-A11). Si dejara de aceptarse, la
alternativa es `uv publish --trusted-publishing always` con el uv fijado y
las attestations PEP 740 generadas con `pypi-attestations` desde requisitos
con hash.

**Sin reconfigurar nada al partir build y publish.** El *Trusted Publisher*
de PyPI y de npm liga el token OIDC al fichero de workflow
(`.github/workflows/release.yml`) y al `environment` del job
(`pypi`/`npm`), nunca al nombre del job: mover la publicación de `python`
a `python-publish` (o de `typescript` a `typescript-publish`) no exige tocar
nada en pypi.org ni en npmjs.com.

**Avisos de licencia de `rayd`.** `rayd` enlaza estáticamente unos 220
crates de terceros (MIT, Apache-2.0, BSD, ISC, Unicode-3.0, Zlib y el CC0
de `notify`), y sus licencias piden que el aviso viaje con cada copia del
binario. `THIRD_PARTY_LICENSES.md` (en la raíz, sin versionar: está en
`.gitignore`) lo genera `make licenses` con cargo-about 0.9.2 (`about.toml`, con la misma lista de
licencias que `deny.toml`, y la plantilla `about.hbs`) desde `Cargo.lock`
para `aarch64-unknown-linux-musl`, con `--frozen` (sin red). El job `build`
de CI y `rayd-build` lo generan con `make image-licenses` (falla si sus
crates no son exactamente los que
`cargo tree -p rayd --target aarch64-unknown-linux-musl -e normal` compila)
y `scripts/check_third_party_licenses.py --binary --metadata` (todo crate
listado está en el `.dep-v0` del binario, y ningún crate listado trae un
`NOTICE` que no recoja el `NOTICE` raíz). El `.dep-v0` no sirve como lista
exacta: `cargo auditable` lo saca de `cargo metadata`, que unifica features
con las dev-dependencies del workspace, y nombra crates que el binario no
enlaza (`ring`, por `rcgen`). `LICENSE`, `NOTICE` y
`THIRD_PARTY_LICENSES.md` van en `licenses/` dentro de `rayito-image.zip`
(`make image-licenses`; `image/Dockerfile` los copia a
`/usr/share/doc/rayd/`) y como assets de la release, listados en
`SHA256SUMS`, que `rayd-sign` también firma (`SHA256SUMS.sigstore.json`).
**Cuando Dependabot (o cualquiera) cambie `Cargo.lock`** no hay nada que
regenerar ni versionar: el fichero sale siempre del lock que se compila, en
CI y en la release. Por eso no existe ningún workflow que escriba en las
ramas de Dependabot: un job con `contents: write` sobre PRs es la forma
clásica de "pwn request", haría que Dependabot dejase de rebasar sus PRs y
su commit, hecho con `GITHUB_TOKEN`, no dispararía CI. La política de
licencias la imponen `cargo-deny` (`deny.toml`) y `cargo about --fail` con
la misma lista; el fichero generado de cada PR va en el artefacto
`rayd-aarch64-musl` de CI por si quieres leerlo.
cargo-about llega por `.github/actions/cargo-about` (binario y sha256
fijados); en local, el mismo binario de su release o `cargo install
--locked cargo-about@0.9.2`. Las wheels del sidecar y los RPM no se
redistribuyen en el zip: se instalan en la cuenta del usuario al construir
la imagen y llevan sus propios avisos (`*.dist-info/licenses/`; pyzmq
incluye ahí el MPL-2.0 de libzmq, sin modificar).

**Verificación** de lo publicado: `docs/site/docs/verify.md` (cosign,
`cargo audit bin`, attestations de PyPI, `npm view … dist.attestations`).

### Ajustes pendientes del repositorio

Ajustes que el workflow no puede imponer desde el árbol y que siguen
pendientes (sec-supply-chain-followups); cada uno es una decisión del
mantenedor:

1. **Creación de tags.** El ruleset `release-tags` prohíbe mover y borrar
   tags de release, pero no crearlos. Una regla `creation` para
   `refs/tags/{python,typescript,rayd}-v*`, en un ruleset aparte cuyo único
   bypass sea quien crea los tags (para no abrir `update`/`deletion` a
   nadie), cierra el tag creado fuera de release-please. Con el PAT del
   mantenedor como creador de tags, ese bypass también lo tendría un PAT
   filtrado; el control fino llega con la App del punto 2.
2. **App en vez de PAT.** Sustituir `RELEASE_PLEASE_TOKEN` por
   `actions/create-github-app-token` con una GitHub App limitada a
   `contents` y `pull-requests` de este repositorio. Mientras tanto, mover
   el secreto a un environment `release-please` limitado a `main`.
3. **Releases inmutables.** Encenderlas (release-please en borrador,
   publicar tras `rayd-upload`) para que ningún token pueda reemplazar un
   asset publicado.
4. **Actions.** `allowed_actions=selected` con las acciones actuales y
   `sha_pinning_required`, y `egress-policy: block` con la lista explícita
   de endpoints en los jobs con credenciales (hoy `harden-runner` sólo
   audita). Quitar el bypass de administradores de `pypi`, `npm` y
   `release` es casi simbólico con un único mantenedor, que ya es el
   revisor.

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
make wheel   # uv build + check_wheel.py + twine check (twine desde requisitos con --hash)
git tag python-v<versión> && git push origin python-v<versión>
```

El job `python-build` de `release.yml` repite las tres comprobaciones,
verifica que el tag coincide con `pyproject.toml` y sube `dist/` más un
`SHA256SUMS`; el job `python-publish` (el único con el token OIDC, sin
checkout ni `uv`) descarga ese artefacto, comprueba con `sha256sum -c` que
es el que produjo `python-build` y sube con OIDC. Las attestations PEP 740
son automáticas con `pypa/gh-action-pypi-publish` ≥ v1.11.
`workflow_dispatch` con `dry_run: true` corre siempre `python-build` y deja
`python-publish` en **skipped**.

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
   `typescript-build` de `release.yml` (disparado por `typescript-v*`)
   empaqueta con `pnpm install --frozen-lockfile --ignore-scripts` y `pnpm
   pack`; el job `typescript-publish` (el único con el token OIDC, sin
   checkout ni `pnpm`) descarga ese `.tgz`, comprueba con `sha256sum -c` que
   es el que produjo `typescript-build` y publica con `npm publish
   --ignore-scripts` sobre Node 24 (npm ≥ 11.5.1, comprobado en el mismo
   job); a partir de ahí no se vuelve a usar ningún token.

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
- Crear los environments `pypi`, `npm`, `release` y `e2e`, los cuatro con
  required reviewers. `pypi`, `npm` y `release` limitan sus despliegues a los
  tags `python-v*`, `typescript-v*` y `rayd-v*`; `e2e` (el del workflow
  `e2e.yml`; su rol OIDC, variables y presupuesto están en `infra/README.md`)
  a la rama `main`, porque el `sub` en el que confía el rol no lleva rama.
- Ruleset de tags (`refs/tags/python-v*`, `typescript-v*`, `rayd-v*`) que
  impide moverlos y borrarlos: un tag de release publicado no se reescribe.
- Secretos del repositorio: **ninguno obligatorio** (PyPI, npm y AWS van
  por OIDC). Opcional: `RELEASE_PLEASE_TOKEN` (PAT fine-grained) para que
  los tags de release-please disparen `release.yml` sin intervención (§1).
- El primer commit del repositorio debe ser `chore: import rayito 0.2.0`
  (no `feat:`), para que el primer PR de release-please refleje sólo el
  trabajo posterior (los manifiestos ya llevan 0.2.0, la versión aceptada en
  M7; 0.1.0 nunca se publicó).
- Opcional: activar Discussions para preguntas que no son issues.

## 6. Checklist de release (los tres componentes a la vez)

1. Preparar el PR de release-please con `make release-pr`
   (`scripts/prepare_release_pr.py`; `RELEASE_PR_ARGS=--dry-run` para ver
   el diff sin subir nada). El commit se construye **desde el árbol de
   `origin/main`**, nunca desde el de la rama de release-please: esa rama
   puede haberse cortado antes de los últimos merges y copiar su árbol los
   revierte en silencio (le pasó a 0.5.1). De release-please sólo se toma el
   número de versión de su `.release-please-manifest.json`; los ficheros que
   lo llevan se derivan de `release-please-config.json` (el manifiesto, el
   fichero propio de cada `release-type` —`pyproject.toml`,
   `package.json`— y los `extra-files`: `src/rayito/_version.py`,
   `src/version.ts`, `Cargo.toml` y las entradas de rayd, rayd-core y
   rayito-proto en `Cargo.lock`). Además regenera `clients/python/uv.lock`,
   convierte el `## [Unreleased]` escrito a mano durante el ciclo en
   `## [x.y.z] - fecha` (descartando las notas que genera release-please),
   pone al día los enlaces de comparación del pie de cada CHANGELOG
   (`[Unreleased]: …/compare/<tag x.y.z>...HEAD` y
   `[x.y.z]: …/compare/<tag anterior>...<tag x.y.z>`) y rehace el PR como un
   único commit firmado: el ruleset de `main` exige firmas y los commits que
   crea release-please por la API no lo están.
   - **Guarda**: antes de commitear, `git diff --name-only origin/main` debe
     quedar dentro de manifiesto, ficheros de versión, lockfiles y
     CHANGELOG; también aborta si release-please movió un fichero que la
     configuración no explica, o si un fichero de versión que `main` no
     tocó desde la base de release-please no queda idéntico al de su rama
     (contraste de los actualizadores con los de release-please). En todos
     los casos sale con código 2 sin commitear ni subir nada.
   - **Trailers**: `RELEASE_PR_ARGS='--trailer "Co-Authored-By: …"'`
     (repetible) o la variable `RELEASE_PR_TRAILERS` (uno por línea) los
     añade al mensaje, antes del `Signed-off-by`, sin `--amend` posterior.
   - La rama `release-please--branches--main` no puede estar abierta en otro
     worktree: el script la recrea en el actual.
   Revisar después que las versiones de `pyproject.toml`,
   `src/rayito/_version.py`, `package.json`, `src/version.ts`, `Cargo.toml`
   y `Cargo.lock` son idénticas.
2. Gates verdes en CI sobre ese PR (`CONTRIBUTING.md` §3), incluidos
   `python scripts/check_license.py`, `make image-licenses` (job `build`),
   `cargo-deny`, la auditoría de
   dependencias y `cargo test --locked` (un `Cargo.lock` que release-please
   no haya actualizado falla aquí, no en la release).
3. e2e verde contra AWS real sobre la imagen que la release requiere
   (`e2e.yml` con `template_version`, o `make test-e2e` local), con el coste
   anotado en el PR (≈ $0,03 por pasada). Las imágenes desechables de la
   aceptación se publican con un id de ejecución (`RAYITO_E2E_RUN_ID=<id>`
   antes de `make image-publish*`, o `rayito image publish
   --artifact-run-id <id>`), así su zip va a `rayito/images/runs/<id>/` y
   no se comparte con otra ejecución del mismo commit. La limpieza borra
   sólo ese prefijo, o sólo los artefactos con `artifactUploaded: true` en
   el resumen `--json` de su publicación; nunca la clave compartida
   `rayito/images/rayd-<sha>.zip` que la ejecución encontró ya subida.
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
8. Notas de la release: la nota curada es el bloque del changelog de cada
   componente (la GitHub Release lleva además la lista de commits que
   genera release-please). Una vulnerabilidad conocida públicamente que
   la release corrige va en `### Security` con su identificador (CVE o
   GHSA); un cambio incompatible, en `Changed` o `Removed`, y lo que pasa a
   obsoleto, en `Deprecated`
   ([política](site/docs/limits.md#versionado-y-soporte)). Si la release
   abre una línea `MAJOR.MINOR` nueva, actualizar la tabla "Versiones
   soportadas" de `SECURITY.md` en el mismo PR.
