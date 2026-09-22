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
`aarch64-unknown-linux-musl`) y `SHA256SUMS`.

```bash
cosign verify-blob --bundle rayito-image.zip.sigstore.json \
  --certificate-identity-regexp '^https://github.com/alejandro-cedeno-10/rayito/\.github/workflows/release\.yml@refs/tags/rayd-v' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  rayito-image.zip

cosign verify-blob --bundle rayd.sigstore.json \
  --certificate-identity-regexp '^https://github.com/alejandro-cedeno-10/rayito/\.github/workflows/release\.yml@refs/tags/rayd-v' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  rayd

sha256sum -c SHA256SUMS
```

La verificación falla si cambian el fichero o el bundle, o si la firma la hizo
otro workflow, otro repositorio u otra rama que no sea un tag `rayd-v*`.
`cosign` ≥ 2.x (la release usa `sigstore/cosign-installer`, cosign 3.x).

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
