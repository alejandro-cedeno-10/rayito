# Contribuir a Rayito

Gracias por querer contribuir. Este documento explica cómo se trabaja en el
repositorio: qué leer antes, cómo se propone un cambio, cómo se ejecutan los
gates en Linux/WSL2 y en Windows, qué convenciones sigue el código y cómo se
firman los commits. Las decisiones de licencia y gobernanza están en
`LICENSE`, `NOTICE` y `GOVERNANCE.md`; las normas de conducta en
`CODE_OF_CONDUCT.md`.

## 1. Antes de empezar

Leer, en este orden: `CLAUDE.md` → `SPEC.md` → `ARCHITECTURE.md` →
`AWS_API_NOTES.md` → `MILESTONES.md`. Son cortos en proporción a lo que
ahorran.

Cuatro reglas duras, con su razón:

1. **No inventar parámetros de la API de AWS.** Si un nombre de parámetro,
   campo o forma de respuesta no aparece literalmente en `AWS_API_NOTES.md`,
   se para y se pregunta. Deducir por analogía con otras APIs de AWS es la
   principal fuente de trabajo desperdiciado en este repo.
2. **El `.proto` es la fuente de verdad.** Nunca se escriben structs de
   request o response a mano en Rust, Python ni TypeScript: se cambia
   `proto/rayito/v1/` y se regenera (`make proto`; Rust se regenera solo en
   `cargo build`). Así los tres clientes no pueden divergir.
3. **Un hito a la vez.** Se trabaja sólo en el hito activo de
   `MILESTONES.md`; nada de módulos ni abstracciones "para más adelante"
   (no-objetivos en `SPEC.md` §4). El alcance acotado es lo que permite
   aceptar cada hito contra AWS real.
4. **Ningún hito se cierra con mocks.** El test de aceptación corre contra
   AWS real: el valor del proyecto está en el comportamiento real de la
   plataforma, no en el de un doble.

Y una restricción de plataforma: **sólo ARM64**. Todo compila para
`aarch64-unknown-linux-musl`; si una dependencia no cruza, se busca otra, no
se cambia de arquitectura.

## 2. Flujo OpenSpec

Todo cambio no trivial empieza como un cambio OpenSpec en
`openspec/changes/<nombre>/` con `proposal.md` (por qué y qué), `design.md`
(todas las decisiones cerradas), `tasks.md` (checklist ordenada) y las
especificaciones delta en `specs/<capacidad>/spec.md`. Antes de implementar:

```bash
openspec validate <nombre> --strict --no-interactive
```

debe pasar. Quien implementa marca las tareas de `tasks.md` a medida que las
termina. La aceptación (el e2e contra AWS real, o el conjunto de gates cuando
el cambio no toca runtime) se archiva con:

```bash
openspec archive <nombre> --yes
```

Los cambios archivados viven en `openspec/changes/archive/` y las
especificaciones vigentes en `openspec/specs/`. Los cambios de arquitectura se
escriben como ADR en `ARCHITECTURE.md` (ver `GOVERNANCE.md`).

## 3. Toolchain y gates

### Linux / macOS / WSL2

Linux (o WSL2) es el entorno de referencia: coincide con CI (`ubuntu-24.04`).
En macOS se usan las mismas herramientas y los mismos comandos; CI sólo
verifica Linux. Con `make`:

```bash
make lint       # buf lint + fmt + clippy + ruff + gen_limits --check + check_license + mypy + biome
make test       # cargo test + pytest (cliente y scripts) + vitest
make build      # rayd para aarch64-unknown-linux-musl (cargo zigbuild)
make test-e2e   # aceptación contra AWS real (RAYITO_E2E=1 y RAYITO_TEMPLATE)
make docs       # sitio mkdocs en modo estricto
```

Herramientas, cada una con su instalador oficial o el gestor de paquetes del
sistema: Rust con `rustup`, que toma de `rust-toolchain.toml` la versión fijada
(1.98.1), los componentes `clippy`/`rustfmt` y el target
`aarch64-unknown-linux-musl`; `cargo-zigbuild` 0.23.4 y `zig` 0.16.0 para
compilar `rayd` para ese target desde cualquier host; `buf` 1.73.0; `uv`
(Python 3.11+ para el cliente, 3.12 para el sidecar); Node 20 y **`pnpm`**
9.15.4 (el `packageManager` fijado en `clients/typescript`).

Los tests de adaptadores de `rayd` marcados `#[cfg(unix)]` y los tests del
sidecar con un ipykernel real (`pytest -m kernel`) necesitan Linux o WSL2: en
Windows se omiten.

### Windows

No hay `make`: los pasos del `Makefile` se ejecutan a mano en Git Bash (las
órdenes de «Los gates, uno a uno» sirven tal cual). Mismas herramientas que en
Linux: `rustup` con el toolchain de `rust-toolchain.toml`, `cargo-zigbuild` y
`zig` para `aarch64-unknown-linux-musl`, `buf`, `uv`, Node 20 y `pnpm`.
`RUSTUP_HOME` y `CARGO_HOME` pueden apuntar a cualquier directorio (por
ejemplo, fuera del disco del sistema).

Los tests nativos se enlazan para el host. El host por defecto de rustup,
`x86_64-pc-windows-msvc`, necesita las Build Tools de Visual Studio. Sin
ellas, instalar también el toolchain `1.98.1-x86_64-pc-windows-gnu` (con el
target `aarch64-unknown-linux-musl`) y usar `zig` como linker, compilador de C
y archivador del host mediante dos envoltorios `.cmd` de una línea:
`zigcc-host.cmd` (`cargo-zigbuild zig cc -- -target x86_64-windows-gnu %*`) y
`zigar-host.cmd` (`cargo-zigbuild zig ar -- %*`). Antes de `cargo` en Git
Bash, con `<dir-zig>` y `<dir-envoltorios>` donde estén en tu máquina (dentro
de `PATH`, en la forma `/<unidad>/…` de Git Bash: el `:` de `<unidad>:/`
separaría la entrada en dos):

```bash
RUSTUP_HOME="${RUSTUP_HOME:-$HOME/.rustup}" CARGO_HOME="${CARGO_HOME:-$HOME/.cargo}"
export PATH="$RUSTUP_HOME/toolchains/1.98.1-x86_64-pc-windows-gnu/bin:$CARGO_HOME/bin:<dir-zig>:$PATH"
export CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER=<dir-envoltorios>/zigcc-host.cmd
export CC_x86_64_pc_windows_gnu=<dir-envoltorios>/zigcc-host.cmd
export AR_x86_64_pc_windows_gnu=<dir-envoltorios>/zigar-host.cmd
export CFLAGS_x86_64_pc_windows_gnu=-Wno-error=date-time
```

(`zig` hace de linker del host `x86_64-pc-windows-gnu` para los tests
nativos; el binario de producto sigue siendo `aarch64-unknown-linux-musl`.)
Sólo `pnpm` para JavaScript (nunca `npm` ni `yarn`) y `uv` para Python.

Los finales de línea los fuerza a LF el `.gitattributes` de la raíz
(`* text=auto eol=lf`), por encima de `core.autocrlf=true`: `LICENSE` y
`NOTICE` deben ser byte a byte el texto de apache.org para que
`check_license.py` y los paquetes (wheel, tarball npm) sean reproducibles.
Un clon posterior a ese fichero ya sale bien; en un clon anterior, ejecutar
`git add --renormalize .` y volver a hacer checkout.

### Los gates, uno a uno

Son exactamente las cadenas `run:` de `.github/workflows/ci.yml`; si cambian
allí, cambian aquí.

Rust:

```bash
buf lint
cargo fmt --all --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
cargo zigbuild --release --target aarch64-unknown-linux-musl -p rayd
```

Scripts de la raíz:

```bash
python3 scripts/check_pins.py
python3 scripts/check_hygiene.py
uvx ruff==0.16.7 check scripts
python3 scripts/gen_limits.py --check
python3 scripts/check_license.py
```

Cliente Python:

```bash
cd clients/python
uv run pytest tests/unit
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
```

```bash
cd clients/python && uv run pytest ../../scripts/tests -p no:cacheprovider
```

```bash
(cd clients/python && uv build)
python3 scripts/check_wheel.py clients/python/dist/*.whl
uvx twine==7.0.0 check clients/python/dist/*
```

Kernel sidecar:

```bash
cd kernel-sidecar
uvx ruff==0.16.7 check .
uvx ruff==0.16.7 format --check .
uv run mypy src
```

```bash
cd kernel-sidecar && uv run --with-requirements requirements.txt pytest
```

```bash
cd kernel-sidecar && uv run --with-requirements requirements.txt --with-requirements requirements-poly.txt pytest -m kernel
```

`requirements-poly.txt` añade el kernel `bash` de la variante
`rayito-base-poly`; sin él, el caso de `bash` se salta. CI exporta
`RAYITO_REQUIRE_BASH_KERNEL=1` para que ese caso falle en vez de saltarse si
`bash_kernel` no llegó al entorno de prueba.

TypeScript (desde `clients/typescript`):

```bash
pnpm install --frozen-lockfile
pnpm lint
pnpm typecheck
pnpm build
pnpm test
pnpm pack:check
```

Sitio de documentación:

```bash
cd clients/python && uv run --group docs mkdocs build -f ../../docs/site/mkdocs.yml --strict --site-dir ../../docs/site/_build
```

## 4. Convenciones

- **Identificadores siempre en inglés**: ficheros, módulos, clases,
  funciones, variables, constantes, campos, nombres de test. El español va
  sólo en cadenas de cara al usuario, docstrings, comentarios y
  documentación.
- **Comentar el porqué, no el qué.** Nada de comentarios en línea dentro del
  cuerpo de una función: si una rama necesita explicación, se extrae un
  helper con nombre descriptivo o se cuenta en el JSDoc/docstring de la
  función. Sin etiquetas de ticket en el código.
- **Rust**: `clippy` pedantic sin warnings; `thiserror` para errores de
  dominio, `anyhow` sólo en `main`; nada de `unwrap()`, `expect()` ni
  `panic!()` fuera de tests (los lints del workspace lo deniegan). Fronteras
  hexagonales: el dominio en `rayd-core` no conoce `tonic`, `axum` ni
  `tokio::process`.
- **Python**: type hints completos (`mypy --strict`), `ruff`; las superficies
  sync y async del SDK son idénticas.
- **TypeScript**: `strict`, `exactOptionalPropertyTypes`,
  `noUncheckedIndexedAccess`, `verbatimModuleSyntax`; Biome para lint y
  formato.
- **Errores**: dentro de streams, `StreamError` con códigos string
  (`not_found`, `permission_denied`, `unimplemented`); en unarios, status
  gRPC estándar.
- **Nunca se loguea** contenido de ficheros, código ejecutado, bytes de PTY,
  tokens ni cuerpos de hooks (`SECURITY.md`, "Higiene de logging").

## 5. Commits y PRs

- **Conventional Commits**: `feat:`, `fix:`, `refactor:`, `docs:`, `chore:`,
  `test:`, `ci:`; el cuerpo, opcional, explica el porqué.
- **Ramas**: `feature/`, `bugfix/` o `chore/` + slug descriptivo.
- **DCO, no CLA.** Cada commit lleva un trailer `Signed-off-by:` con tu
  nombre y correo, que `git` añade por ti con la opción `-s` (ver "Firma de
  los commits" más abajo). Firmar significa aceptar el Developer Certificate
  of Origin 1.1, reproducido ahí. Cuando el repositorio sea público, la app
  DCO de GitHub será un check obligatorio en cada PR (paso manual en
  `docs/RELEASING.md`).
- **Sin force-push a `main`.** En tu rama, si hace falta, `--force-with-lease`.
- **PR**: el título en formato Conventional Commits; la descripción sigue la
  plantilla (`.github/PULL_REQUEST_TEMPLATE.md`): cambio OpenSpec enlazado
  con `tasks.md` marcado, `buf breaking` limpio o el cambio justificado,
  gates ejecutados en local (y en qué sistema), e2e contra AWS ejecutado o
  "sin cambio de runtime", sin secretos ni contenido de ficheros en logs,
  tests o fixtures, documentación y `CHANGELOG.md` del paquete actualizados,
  todos los commits firmados.
- **El e2e cuesta dinero** (≈ $0,03 por ejecución completa, más ≈ $0,037 de
  storage por cada versión de imagen nueva): el PR dice si se ejecutó y qué
  costó. Nunca dos sesiones e2e contra la misma imagen a la vez (el sweeper
  del `conftest` termina todos los MicroVMs vivos de la imagen).

### Firma de los commits (DCO)

```bash
git commit -s -m "feat: ..."
```

El trailer `Signed-off-by: Nombre <correo>` certifica lo siguiente
(https://developercertificate.org/, en su idioma original):

```text
Developer Certificate of Origin
Version 1.1

Copyright (C) 2004, 2006 The Linux Foundation and its contributors.

Everyone is permitted to copy and distribute verbatim copies of this
license document, but changing it is not allowed.


Developer's Certificate of Origin 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I
    have the right to submit it under the open source license
    indicated in the file; or

(b) The contribution is based upon previous work that, to the best
    of my knowledge, is covered under an appropriate open source
    license and I have the right under that license to submit that
    work with modifications, whether created in whole or in part
    by me, under the same open source license (unless I am
    permitted to submit under a different license), as indicated
    in the file; or

(c) The contribution was provided directly to me by some other
    person who certified (a), (b) or (c) and I have not modified
    it.

(d) I understand and agree that this project and the contribution
    are public and that a record of the contribution (including all
    personal information I submit with it, including my sign-off) is
    maintained indefinitely and may be redistributed consistent with
    this project or the open source license(s) involved.
```

## 6. Cómo reportar

- **Bugs y propuestas**: con las plantillas de issues
  (`.github/ISSUE_TEMPLATE/`). Un bug útil trae SDK y versión,
  `agent_version` (`get_health()` / `getHealth()`), nombre y versión de la
  imagen, región y una reproducción mínima; nunca tokens, JWE ni
  `runHookPayload`.
- **Vulnerabilidades**: en privado, siguiendo `SECURITY.md` ("Reportar una
  vulnerabilidad"). Nunca en un issue público.
- **Conducta**: `CODE_OF_CONDUCT.md` ("Reporting an Issue").
