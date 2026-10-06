# Verificar una release

Cada release publica tres componentes desde sus tags (`python-v<v>`,
`typescript-v<v>`, `rayd-v<v>`) con `.github/workflows/release.yml`. Lo que
puedes comprobar desde una máquina limpia, sin confiar en nada más que en
Sigstore y en los registros:

## Binario `rayd` y `rayito-image.zip` (GitHub Release `rayd-v<v>`)

Los assets de la release son `rayd`, `rayito-image.zip`, sus bundles
`rayd.sigstore.json` y `rayito-image.zip.sigstore.json` (firma keyless de
cosign: el certificado de Sigstore lleva la identidad del workflow y del tag),
`rayd.cdx.json` (SBOM CycloneDX 1.5 del grafo de crates para
`aarch64-unknown-linux-musl`), los avisos de licencia `LICENSE`, `NOTICE` y
`THIRD_PARTY_LICENSES.md`, `SHA256SUMS` y su bundle
`SHA256SUMS.sigstore.json`. Desde la release siguiente a la 0.7.0,
`SHA256SUMS` también va firmado: lista todos los assets salvo él mismo y su
bundle, así que su firma cubre el SBOM y los avisos, que no llevan bundle
propio.

```bash
RAYD_VERSION=0.7.0   # la versión que vas a instalar, sin la "v"

cosign verify-blob --bundle rayito-image.zip.sigstore.json \
  --certificate-identity "https://github.com/alejandro-cedeno-10/rayito/.github/workflows/release.yml@refs/tags/rayd-v${RAYD_VERSION}" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  rayito-image.zip

cosign verify-blob --bundle rayd.sigstore.json \
  --certificate-identity "https://github.com/alejandro-cedeno-10/rayito/.github/workflows/release.yml@refs/tags/rayd-v${RAYD_VERSION}" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  rayd

# Desde la release siguiente a la 0.7.0: la firma de SHA256SUMS
cosign verify-blob --bundle SHA256SUMS.sigstore.json \
  --certificate-identity "https://github.com/alejandro-cedeno-10/rayito/.github/workflows/release.yml@refs/tags/rayd-v${RAYD_VERSION}" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  SHA256SUMS

sha256sum -c SHA256SUMS
```

La verificación falla si cambian el fichero o el bundle, o si la firma la hizo
otro workflow, otro repositorio, otra rama u **otro tag** que no sea
`rayd-v${RAYD_VERSION}`. La identidad va completa y no como expresión
regular a propósito: con una regexp que acaba en `rayd-v`, un bundle firmado
por cualquier release de `rayd` verificaba cualquier otra, así que un asset
antiguo (una versión con fallos conocidos) o uno firmado bajo otro tag
pasaba por el que ibas a instalar. `cosign` ≥ 2.x (la release usa
`sigstore/cosign-installer`, cosign 3.x).

### Qué prueba la firma

Una firma válida prueba que `release.yml` de este repositorio firmó ese
fichero en un run lanzado desde el tag `rayd-v${RAYD_VERSION}`. Desde la
0.7.0 prueba además que el run pasó por la
aprobación del mantenedor: el job que firma corre en el environment
`release`, con revisor obligatorio, así que GitHub no le da el token OIDC
(ni Sigstore emite el certificado) hasta que alguien aprueba el run. La
aprobación llega antes de que exista el token OIDC, y un ensayo (`dry_run`)
no firma nada: sólo construye. El job que firma no hace checkout ni compila,
firma sólo lo que `sha256sum -c` confirma que salió del build, y el que sube
los assets nunca reemplaza uno ya publicado.

Lo que la firma **no** prueba por sí sola es que el código del tag se
revisara en `main`: la identidad la fijan el fichero de workflow y el tag,
no quién creó el tag. Publicar exige que el commit del tag esté en `main`,
pero el `release.yml` que corre es el del commit etiquetado. Para firmar con
esta identidad hace falta, a la vez, poder crear un tag `rayd-v*` y aprobar
el environment `release`. Con un único mantenedor y revisor, eso es su
cuenta: un token filtrado que sólo escribe contenido y PRs (el de
release-please) puede crear el tag, pero no aprobar el environment.

El binario se compila con `cargo auditable`, así que lleva el grafo exacto de
crates en la sección ELF `.dep-v0`; cualquier herramienta que lea ese formato
puede auditarlo contra RustSec sin el código fuente:

```bash
cargo audit bin rayd          # cargo-audit >= 0.17 lee .dep-v0
python3 scripts/check_auditable.py rayd   # del repositorio: raíz, versión y crates clave
```

El SBOM `rayd.cdx.json` es el mismo grafo en CycloneDX 1.5 para las
herramientas que consumen SBOMs (el componente `metadata.component` es `rayd`
con la versión del tag).

### Avisos de licencia de `rayd`

`rayd` es Apache-2.0 y enlaza estáticamente crates de terceros (MIT,
Apache-2.0, BSD, ISC, Unicode-3.0, Zlib y un CC0). Desde la release
siguiente a la 0.7.0, sus avisos viajan con cada copia del binario: como
assets de la release (`LICENSE`, `NOTICE` y `THIRD_PARTY_LICENSES.md`), en
`licenses/` dentro de `rayito-image.zip` y, en la imagen construida desde
ese zip, en `/usr/share/doc/rayd/`. `THIRD_PARTY_LICENSES.md` lo genera
cargo-about desde el `Cargo.lock` del tag, y la release comprueba que lista
todos los crates del `.dep-v0` del binario; puedes repetirlo con el binario
descargado:

```bash
python3 scripts/check_third_party_licenses.py THIRD_PARTY_LICENSES.md --binary rayd   # del repositorio
```

Dentro de un sandbox:

```bash
ls /usr/share/doc/rayd/   # LICENSE  NOTICE  THIRD_PARTY_LICENSES.md
```

## Paquete Python `rayito` (PyPI)

Se publica con Trusted Publishing (OIDC, sin token) y `pypa/gh-action-pypi-publish`
adjunta attestations PEP 740 automáticamente. En la página del fichero en PyPI
aparece la insignia de proveniencia; desde la línea de comandos:

```bash
uvx pypi-attestations verify pypi \
  --repository https://github.com/alejandro-cedeno-10/rayito \
  pypi:rayito-<v>-py3-none-any.whl
```

(`pypi:<fichero>` descarga la distribución y sus attestations desde PyPI y
comprueba que las firmó `release.yml` de ese repositorio.)

## Paquete npm `rayito`

Se publica con npm trusted publishing (OIDC desde `release.yml`, environment
`npm`), que adjunta proveniencia SLSA sin configuración adicional:

```bash
npm view rayito@<v> dist.attestations
npm audit signatures        # en un proyecto que lo instale
```

## Qué imagen corre

La imagen `rayito-base` de tu cuenta se construye con `make image-publish`
desde `image/Dockerfile`, cuyo `FROM` va fijado por digest, y con
`--base-image-version` explícita (`AWS_API_NOTES.md` §4 y Q52).
`get_health().agent_version` devuelve la versión de `rayd` que hay dentro, la
misma del tag `rayd-v<v>` cuyo `rayito-image.zip` verificaste arriba.
