# Contribuir a Rayito

Gracias por querer contribuir. Esta guía explica cómo preparar tu entorno y
cómo trabajamos juntos, para que cualquier persona pueda empezar sin
fricción. Si algo falla en los primeros pasos, mira antes el
[`README.md`](README.md) y [`SUPPORT.md`](SUPPORT.md).

Las decisiones de licencia y gobernanza están en `LICENSE`, `NOTICE` y
[`GOVERNANCE.md`](GOVERNANCE.md); las normas de conducta, en
[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).

## Contenido

1. [Requisitos previos](#1-requisitos-previos)
2. [Puesta en marcha](#2-puesta-en-marcha)
3. [Seguimiento de trabajo](#3-seguimiento-de-trabajo)
4. [Ramas](#4-ramas)
5. [Commits](#5-commits)
6. [Pull requests](#6-pull-requests)
7. [Estilo y calidad](#7-estilo-y-calidad)
8. [Revisión de código](#8-revisión-de-código)
9. [Comunicación](#9-comunicación)
10. [Ayuda](#10-ayuda)

## 1. Requisitos previos

Según lo que vayas a tocar no necesitas todo. Para los SDK y la
documentación basta con Python y Node; para `rayd` hace falta Linux.

| Herramienta | Versión | Para qué |
|---|---|---|
| Python | `>=3.11` (`requires-python` de `clients/python`); 3.12 para el sidecar | SDK de Python, scripts, sidecar |
| [`uv`](https://docs.astral.sh/uv/) | la última | entorno y dependencias de Python (nunca `pip` a mano) |
| Node.js | `>=20` (`engines` de `clients/typescript`; CI usa 22) | SDK de TypeScript |
| [`pnpm`](https://pnpm.io/) | 9.15.4 (`packageManager` de `clients/typescript`) | dependencias de JavaScript (nunca `npm` ni `yarn`) |
| Rust | el de `rust-toolchain.toml` (1.98.1, con `clippy`, `rustfmt` y el target `aarch64-unknown-linux-musl`), con `rustup` | **solo si tocas `rayd`** (`crates/`) |
| `cargo-zigbuild` 0.23.4, `zig` 0.16.0, `buf` 1.73.0 | las indicadas | compilar `rayd` para ARM64 (en Linux o en la VM Linux, sección 1.2); regenerar el `.proto` |
| Docker con Compose v2 | Docker Desktop, Colima, Lima u OrbStack | pruebas punta a punta en local (sección 2.4) |
| `make` | cualquiera | atajos del `Makefile` (en Windows, ver 1.3) |
| Cuenta de AWS | opcional | solo para el e2e real, que cierra un hito o una release (sección 6.4) |

Solo se soporta **ARM64** como destino: todo compila para
`aarch64-unknown-linux-musl`; si una dependencia no cruza, se busca otra, no
se cambia de arquitectura.

No hay `.env` con secretos ni valores compartidos que pedir a nadie. El
código nunca lee credenciales de un fichero del repositorio: el e2e real usa
las credenciales de tu propia sesión de AWS y variables `RAYITO_E2E_*` que
tú exportas en la terminal (sección 6.4). Los tests unitarios y el entorno
local no necesitan ninguna.

### 1.1 Linux / WSL2

Linux (o WSL2) es el entorno de referencia: coincide con CI (`ubuntu-24.04`
y `ubuntu-24.04-arm`). Con `make`:

```bash
make lint       # todos los lint de la raíz y de los dos SDK
make test       # cargo test + pytest (cliente y scripts) + vitest
make build      # rayd para aarch64-unknown-linux-musl (cargo zigbuild)
make test-e2e   # aceptación contra AWS real (RAYITO_E2E=1 y RAYITO_TEMPLATE)
make docs       # sitio mkdocs en modo estricto
```

Los tests de adaptadores de `rayd` marcados `#[cfg(unix)]` y los tests del
sidecar con un ipykernel real (`pytest -m kernel`) necesitan Linux o WSL2: en
Windows se omiten.

Dos requisitos de los tests de integración de `rayd` (`crates/rayd/tests/`):

- `cargo test --workspace` corre como un usuario **sin privilegios con uid
  ≥ 1000**.
- `m9_egress` (rutas de política de ADR-012) necesita **root con
  `CAP_NET_ADMIN` en un network namespace nuevo**. Sin eso se salta; CI lo
  ejecuta así, y con `RAYITO_REQUIRE_EGRESS_NETNS=1` saltarse es un fallo:

  ```bash
  binary="$(cargo test -p rayd --test m9_egress --locked --no-run --message-format=json \
    | jq -r 'select(.reason == "compiler-artifact" and .target.name == "m9_egress" and .executable != null) | .executable')"
  sudo env RAYITO_REQUIRE_EGRESS_NETNS=1 unshare --net -- "$binary" --test-threads=1
  ```

### 1.2 macOS

`rayd` no compila en macOS. Los SDKs (Python y TypeScript), el sidecar (salvo
`pytest -m kernel`), `scripts/`, OpenSpec y el sitio de documentación se
trabajan en macOS con los mismos comandos que en Linux; todo lo que compila o
prueba el workspace de Rust (`cargo clippy`, `cargo test`, `make build`,
`make image-zip` y los `make image-publish*`) se ejecuta **dentro de una VM
Linux**, por ejemplo con [Lima](https://lima-vm.io/) y una imagen Ubuntu
24.04, con las mismas herramientas que en Linux. Dos detalles de Lima:

- Por defecto monta tu home de macOS en solo lectura: clona el repositorio
  dentro de la VM, o apunta `CARGO_TARGET_DIR` a un directorio del disco de
  la VM.
- El usuario de la VM hereda tu uid de macOS (501), por debajo de 1000: crea
  un usuario con uid ≥ 1000 (por ejemplo 1500) para `cargo test` y ejecuta
  `m9_egress` con `sudo … unshare --net` como en Linux.

CI solo verifica Linux; el PR dice en qué sistema corrieron los gates. Si no
tocas `rayd`, no necesitas la VM.

### 1.3 Windows

No hay `make`: los pasos del `Makefile` se ejecutan a mano en Git Bash (las
órdenes de «Los gates, uno a uno», en la sección 7, sirven tal cual). Mismas
herramientas que en Linux: `rustup` con el toolchain de `rust-toolchain.toml`,
`cargo-zigbuild` y `zig` para `aarch64-unknown-linux-musl`, `buf`, `uv`,
Node 20+ y `pnpm`. `RUSTUP_HOME` y `CARGO_HOME` pueden apuntar a cualquier
directorio (por ejemplo, fuera del disco del sistema).

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

Los finales de línea los fuerza a LF el `.gitattributes` de la raíz
(`* text=auto eol=lf`), por encima de `core.autocrlf=true`: `LICENSE` y
`NOTICE` deben ser byte a byte el texto de apache.org para que
`check_license.py` y los paquetes (wheel, tarball npm) sean reproducibles.
Un clon posterior a ese fichero ya sale bien; en un clon anterior, ejecutar
`git add --renormalize .` y volver a hacer checkout.

## 2. Puesta en marcha

### 2.1 Clonar y leer

```bash
git clone https://github.com/alejandro-cedeno-10/rayito.git
cd rayito
```

Para cambiar código, lee en este orden `CLAUDE.md` → `SPEC.md` →
`ARCHITECTURE.md` → `AWS_API_NOTES.md` → `MILESTONES.md`. Son cortos en
proporción a lo que ahorran. Estas cuatro reglas duras, con su razón:

1. **No inventar parámetros de la API de AWS.** Si un nombre de parámetro,
   campo o forma de respuesta no aparece literalmente en `AWS_API_NOTES.md`,
   se para y se pregunta. Deducir por analogía con otras APIs de AWS es la
   principal fuente de trabajo desperdiciado en este repo.
2. **El `.proto` es la fuente de verdad.** Nunca se escriben structs de
   request o response a mano en Rust, Python ni TypeScript: se cambia
   `proto/rayito/v1/` y se regenera (`make proto`; Rust se regenera solo en
   `cargo build`). Así los tres clientes no pueden divergir.
3. **Un hito a la vez.** Se trabaja solo en el hito activo de
   `MILESTONES.md`; nada de módulos ni abstracciones "para más adelante"
   (no-objetivos en `SPEC.md` §4). El alcance acotado es lo que permite
   aceptar cada hito contra AWS real.
4. **Ningún hito se cierra con mocks.** El test de aceptación corre contra
   AWS real: el valor del proyecto está en el comportamiento real de la
   plataforma, no en el de un doble.

### 2.2 SDK de Python

```bash
cd clients/python
uv sync
uv run pytest tests/unit          # toda la suite unitaria (unos minutos)
uv run pytest tests/unit/test_limits.py   # un subconjunto rápido, para comprobar el entorno
```

Los tests unitarios usan dobles (`fake_*.py`), nunca AWS.

### 2.3 SDK de TypeScript y documentación

```bash
cd clients/typescript
pnpm install --frozen-lockfile
pnpm test                         # vitest (proyecto unit) y los tests de infra/functions
```

```bash
make docs                         # desde la raíz: mkdocs build --strict
```

### 2.4 Pruebas punta a punta en local (Docker + Floci), sin AWS

Sin cuenta de AWS ni credenciales, el entorno de `dev/local` corre los SDK
contra un `rayd` real (la imagen de producto en un contenedor) y contra
[Floci](https://github.com/floci-io/floci), un emulador de AWS:

```bash
make local-up      # floci, runner y guest; compila rayd dentro de Docker
make local-e2e     # tests `local` de Python y TypeScript
make local-down    # lo para todo y borra los volúmenes
```

Si la VM de Docker tiene menos de 6 GB, compilar `rayd` dentro de Docker
mata a `rustc` con `SIGKILL`. Compila `rayd` fuera (en Linux, o en la VM
Linux si estás en macOS, sección 1.2) y pásalo ya hecho:

```bash
make build
make local-up LOCAL_RAYD_BIN=target/aarch64-unknown-linux-musl/release/rayd
```

Qué cubre, qué no frente a AWS real, los requisitos de host (arm64) y cómo
se filtran los tests (`LOCAL_E2E_ARGS`) están en
[Probar en local](docs/site/docs/guias/probar-en-local.md). Este entorno
no sustituye al e2e contra AWS que cierra un hito o una release (regla 4).

Cuánto AWS hace falta depende de lo que toca el cambio. Si solo cambian docs, tests o
CI, ninguno. Si cambian el SDK o sus herramientas (CLI, servidor MCP, panel
local) sin tocar `rayd`, la imagen, `proto/`, `infra/` ni el plano de control,
bastan el CI, el e2e local de esta sección (el workflow `local-e2e` se dispara
en los PR que cambian `clients/` (salvo los CHANGELOG), `testdata/`,
`limits.json`, `dev/local/`, `crates/`, `proto/`, `image/Dockerfile`,
`Cargo.lock`, `.dockerignore` o el propio workflow) y un chequeo corto en AWS de la
función tocada. Con cambios en `rayd`, la imagen, `proto/`, `infra/`, el plano
de control o una release, la regresión completa. La tabla está en
`.claude/skills/rayito-engineering/reference/release-and-aws.md`.

## 3. Seguimiento de trabajo

- El trabajo se sigue en **GitHub Issues**: bugs y propuestas con las
  plantillas de `.github/ISSUE_TEMPLATE/`. Busca antes entre los abiertos y
  cerrados, y enlaza el issue en el PR (`Closes #123`).
- Un cambio **no trivial empieza como cambio OpenSpec** en
  `openspec/changes/<nombre>/` con `proposal.md` (por qué y qué), `design.md`
  (todas las decisiones cerradas), `tasks.md` (checklist ordenada) y las
  especificaciones delta en `specs/<capacidad>/spec.md`. Antes de implementar:

  ```bash
  npx -y @fission-ai/openspec@1.10.0 validate <nombre> --strict --no-interactive
  ```

  debe pasar, y antes de abrir el PR también el repositorio entero:

  ```bash
  npx -y @fission-ai/openspec@1.10.0 validate --all --strict --no-interactive
  ```

  La CLI va fijada a la 1.10.0 (las 1.11 y posteriores rompen los `Purpose` de
  las specs). CI no la ejecuta: es un gate local. Quien implementa marca las
  tareas de `tasks.md` a medida que las termina. Cuando la aceptación es verde (el
  e2e contra AWS real, o el conjunto de gates si el cambio no toca runtime),
  el cambio se archiva con
  `npx -y @fission-ai/openspec@1.10.0 archive <nombre> --yes`.
- Los cambios archivados viven en `openspec/changes/archive/` (historia: no
  se editan) y las especificaciones vigentes en `openspec/specs/`. Los
  cambios de arquitectura se escriben como ADR en `ARCHITECTURE.md` (ver
  `GOVERNANCE.md`).
- Un arreglo trivial (una errata, un enlace roto) no necesita cambio
  OpenSpec: abre el PR directamente.

## 4. Ramas

El repositorio es **trunk-based**: existe una sola rama de larga vida,
`main`. No hay `develop` ni Git Flow.

| Rama | Para qué |
|---|---|
| `main` | Siempre publicable. Protegida: sin push directo ni force-push |
| `feat/<tema>` | Funcionalidad nueva |
| `fix/<tema>` | Corrección de un bug |
| `docs/<tema>` | Solo documentación |
| `chore/<tema>` | Herramientas, dependencias, configuración |
| `test/<tema>` | Solo tests |

Ejemplos: `feat/dashboard-extras`, `fix/connect-recovers-gateways`,
`docs/contributing-quickstart`, `test/ts-provider-smoke`. Nombra la rama por
el tema, no por un número de ticket ni por el hito.

Las ramas son **cortas**: nacen de `main`, se abren como PR cuanto antes y se
borran al fusionar. Trabaja en un worktree propio (`git worktree add
../rayito-<tema> -b feat/<tema> origin/main`) y nunca directamente en `main`.

Por qué una sola rama: el ruleset de `main` es la única puerta (PR
obligatorio, checks en verde, commits firmados, sin force-push) y
release-please publica desde `main`, a partir de los Conventional Commits que
llegan a ella (`docs/RELEASING.md`). Una rama `develop` duplicaría la puerta
y retrasaría los releases sin aportar nada.

## 5. Commits

Formato [Conventional Commits](https://www.conventionalcommits.org/):

```text
<tipo>(<ámbito opcional>): <descripción corta>
```

Tipos: `feat`, `fix`, `refactor`, `docs`, `chore`, `test`, `ci`. El cuerpo,
opcional, explica el porqué. Un cambio incompatible lleva `!` (`feat!:`) y
entrada en el CHANGELOG. release-please deduce las versiones y el CHANGELOG
de estos mensajes, así que importan. Ejemplos de este repositorio:

```text
docs: mark the dashboard features available in 0.12.0 and add its release notes
fix(changelog): resolve merge markers in TypeScript changelog
test(e2e): widen the egress allowlist refusal budget to the probe's own timeouts
chore(release): regenerate the stack assets in make release-pr
```

### 5.1 DCO y commits firmados

Dos cosas distintas, las dos obligatorias:

- **DCO, no CLA.** Cada commit lleva un trailer `Signed-off-by:` con tu
  nombre y correo, que `git` añade con `-s`. Es el que acepta el Developer
  Certificate of Origin 1.1, reproducido al final de esta sección.
- **Commits firmados.** Además, cada commit va firmado criptográficamente
  (opción `-S`, con una clave SSH o GPG registrada en tu cuenta de GitHub)
  para que aparezca como *Verified*: el ruleset de `main` lo exige. La firma
  prueba quién hizo el commit; el trailer, que aceptas el DCO.

```bash
git commit -s -S -m "feat: ..."
```

Para no repetir `-S`, configura una vez la firma con tu clave SSH (sirve la
misma con la que te autenticas, añadida en GitHub como *Signing key*):

```bash
git config --global gpg.format ssh
git config --global user.signingkey <ruta-a-tu-clave-publica.pub>
git config --global commit.gpgsign true
```

Con GPG se omite `gpg.format` y `user.signingkey` lleva el id de tu clave.
Si te olvidas, rehaz los commits de tu rama con `git rebase --exec 'git
commit --amend --no-edit -s -S' origin/main` y `git push --force-with-lease`
(solo en tu rama, nunca en `main`).

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

## 6. Pull requests

### 6.1 Cómo abrirlo

- Contra **`main`** (no hay otra rama de integración).
- Título en formato Conventional Commits. La descripción sigue la plantilla
  [`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md):
  - enlace al issue (`Closes #123`) y al cambio OpenSpec, con `tasks.md`
    marcado;
  - qué cambia y por qué;
  - `buf breaking` limpio, o el cambio incompatible del `.proto` justificado;
  - gates ejecutados en local y **en qué sistema** (Linux, WSL2, VM Linux en
    macOS, Windows);
  - e2e contra AWS ejecutado (imagen y coste anotados) o «sin cambio de
    runtime»;
  - sin secretos ni contenido de ficheros en logs, tests o fixtures.
- **Tests con cada cambio.** Toda funcionalidad nueva o corrección llega con
  tests que la cubren en el mismo PR: unitarios en el paquete que cambia
  (`cargo test`, `pytest tests/unit`, `vitest`) y, si toca el runtime, la
  aceptación e2e contra AWS real del cambio OpenSpec. Un PR de comportamiento
  sin tests explica por qué en su descripción.
- **CHANGELOG a mano.** Añade tu entrada en `## [Unreleased]` del
  `CHANGELOG.md` del paquete que tocas (`clients/python`, `clients/typescript`,
  `crates/rayd`). Al publicar, `make release-pr` lo convierte en la sección
  de la versión. Si el cambio no toca ningún paquete publicable (docs, CI,
  infraestructura), no lleva entrada de CHANGELOG.
- El texto del PR (título y cuerpo) no lleva datos de ningún entorno
  (sección 7.4): pásalo antes por `python3 scripts/check_hygiene.py -`.

### 6.2 Checks obligatorios

El ruleset de `main` exige commits firmados, solo merge commit y que pasen
estos checks de estado: los jobs de `ci.yml`, los dos de `leaks.yml` y
CodeQL. En conjunto cubren:

- lint y tests de los dos SDK (Python y TypeScript), también en ARM y con
  Python 3.11 y 3.13;
- el kernel sidecar y `rayd` (compilación para `aarch64-unknown-linux-musl`);
- el sitio de documentación (`mkdocs --strict`);
- los análisis de seguridad (`cargo-deny`, auditoría de dependencias,
  CodeQL y `gitleaks` sobre todo el historial);
- higiene: `check_hygiene.py` sobre los ficheros versionados (en `ci.yml`) y
  sobre el título y cuerpo del PR (`leaks.yml`).

El `Signed-off-by` del DCO es obligatorio por política, pero ningún check lo
verifica todavía (`docs/RELEASING.md` §5). El e2e local con Docker y Floci
(workflow `local-e2e`, sin AWS) corre en los PR que tocan lo que prueba, pero
no es un check obligatorio.

CI tiene que estar en verde antes de pedir revisión. Corre antes los gates
locales (sección 7).

### 6.3 Fusión

- Requiere revisión (sección 8) y CI en verde.
- **Solo merge commit.** Nunca squash ni rebase: release-please lee cada
  Conventional Commit que llega a `main`, y las firmas de tus commits deben
  conservarse. En GitHub, «Create a merge commit»; con `gh`, `gh pr merge
  --merge`.
- Nunca se hace push a `main`. Borra la rama tras fusionar.

### 6.4 Plantilla IAM y e2e contra tu cuenta de AWS

Solo hace falta si tu cambio toca el runtime (`rayd`, imágenes, la forma en
que el SDK habla con la plataforma). Entonces el PR lleva su e2e real, que
corres en **tu** cuenta, o el mantenedor lo corre al aceptarlo. Si no toca
runtime, el PR dice «sin cambio de runtime».

Las plantillas de CloudFormation viven en `infra/` (`infra/README.md`). El
IAM de build, ejecución y cliente es `infra/iam.yaml`; la pila y los recursos
que crea conservan los nombres `rayito-m0-iam` y `rayito-m0-*` con los que
nacieron en M0, porque renombrarlos rompería los despliegues existentes. En
tu propia cuenta, con marcadores en lugar de tus valores:

```bash
aws cloudformation deploy --stack-name rayito-m0-iam \
  --template-file infra/iam.yaml --capabilities CAPABILITY_NAMED_IAM \
  --profile <tu-perfil> \
  --parameter-overrides ArtifactBucket=<tu-bucket> LogGroupPrefix=/rayito
make image-publish BUCKET=<tu-bucket>          # y image-publish-caps / -poly
```

La precondición del e2e (la pila `rayito-m0-iam` y la imagen `rayito-base`) también
la deja `rayito setup` en tu cuenta y región (con `--bucket <tu-bucket>` si
quieres usar uno tuyo); la orden de arriba sigue siendo válida si prefieres
desplegar a mano o publicar otra variante.

Si la ejecución usa `rayito setup` y la cuenta no tenía bucket, `setup` crea la
pila `rayito-m0-artifacts` y su bucket, que se **conserva** al borrar la pila.
El e2e que lo prueba (`RAYITO_E2E_SETUP_CREATE_BUCKET=1`, con
`--bucket rayito-e2e-setup-<hex aleatorio>`) se salta si ya existe
`rayito-m0-artifacts` en la región, y su limpieza borra sólo lo que creó esa
ejecución, en este orden: la imagen y la pila `rayito-m0-iam`, la pila
`rayito-m0-artifacts` y, por último, el bucket retenido (vaciado antes, porque
tiene el zip de la imagen). Si lo ejecutas a mano, haz lo mismo y no toques
ningún otro bucket ni pila de la cuenta.

El e2e cuesta dinero (≈ $0,03 por ejecución completa, más ≈ $0,037 de storage
por cada versión de imagen nueva) y nunca corre sin `RAYITO_E2E=1` y
`RAYITO_TEMPLATE` (`clients/python/tests/e2e/conftest.py`). El PR dice si se
ejecutó y qué costó. El resto de variables es opcional y, si falta, el test
que la necesita se salta:

```bash
export AWS_PROFILE=<tu-perfil> AWS_REGION=us-east-1
export RAYITO_E2E=1
export RAYITO_TEMPLATE=rayito-base               # nombre o ARN de la imagen
export RAYITO_TEMPLATE_VERSION=<versión>         # vacío: la última ACTIVE
export RAYITO_EXECUTION_ROLE_ARN=arn:aws:iam::123456789012:role/rayito-m0-execution-us-east-1
export RAYITO_TEMPLATE_CAPS=rayito-base-caps     # IMDS, egress y persistencia
export RAYITO_TEMPLATE_POLY=rayito-base-poly     # kernels bash/JavaScript/TypeScript
export RAYITO_PERSIST_BUCKET=<tu-bucket>         # RAYITO_PERSIST_PREFIX: rayito-e2e
export RAYITO_E2E_TRANSFER_BUCKET=<tu-bucket>    # RAYITO_E2E_TRANSFER_PREFIX: rayito-e2e-transfer
export RAYITO_E2E_VPC_ID=<tu-vpc>               # efs-volumes: una VPC existente que puedes usar
export RAYITO_E2E_SUBNET_IDS=<subred-a>,<subred-b> # de 1 a 3 subredes privadas de esa VPC, una por AZ
make test-e2e
make test-e2e-typescript
```

Los módulos de `clients/python/tests/e2e/` y `clients/typescript/tests/e2e/`
documentan en su cabecera las variables propias de cada uno. Nunca dos
sesiones e2e contra la misma imagen a la vez. El sweeper del `conftest` de
Python termina solo los MicroVMs que creó su sesión (el `control_plane` de
sesión apunta cada id que devuelve `run_microvm`); los demás vivos de la imagen
se informan por número y no se tocan. Lo que nace por caminos que no pasan por
ese `control_plane` (el corpus E2B, la CLI en subprocesos) lo acota el
`maximumDurationInSeconds=900` de todo sandbox de test.

Si publicas imágenes desechables para una ejecución (aceptación, o dos e2e
en paralelo sobre el mismo commit), exporta antes un id propio,
`RAYITO_E2E_RUN_ID=<id>` (letras, dígitos y guiones): `make image-publish*`
pasa `--artifact-run-id <id>` y el zip va a `rayito/images/runs/<id>/` en vez
de a la clave compartida `rayito/images/rayd-<sha>.zip`, y el e2e de la CLI
reutiliza esa misma clave. Al limpiar, borra solo `rayito/images/runs/<id>/`
o, sin id, solo los artefactos cuyo resumen de `rayito --json image publish`
dijo `artifactUploaded: true`: un objeto que la publicación encontró ya
subido puede ser el de otra ejecución.

Los e2e de `s3-mounts` (`test_m15_s3_mounts.py`, `m15-s3-mounts.e2e.test.ts`)
y de `rayd-otlp` (`test_m15_rayd_otlp.py`, `m15-rayd-otlp.e2e.test.ts`)
necesitan además que el rol de `RAYITO_EXECUTION_ROLE_ARN` lleve adjunta la
política gestionada de su componente. `rayito stack deploy s3-mounts` y
`rayito stack deploy otlp-export` solo la crean (salida `PolicyArn`);
adjuntarla es un paso aparte:

```bash
aws iam attach-role-policy --role-name <tu-rol-de-ejecucion> \
  --policy-arn <PolicyArn> --profile <tu-perfil>
```

Sin ella esos casos no se saltan: fallan con un error de permisos dentro del
sandbox (montaje S3) o porque ningún lote llega a exportarse (OTLP).

## 7. Estilo y calidad

### 7.1 Los gates, uno a uno

Antes de cada push, corre los del lenguaje que tocaste. Son exactamente las
cadenas `run:` de `.github/workflows/ci.yml`; si cambian allí, cambian aquí.
Cada uno tiene su equivalente en `make lint` / `make test`.

Python (cliente), desde `clients/python`:

```bash
uv run pytest tests/unit
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
```

TypeScript, desde `clients/typescript`:

```bash
pnpm install --frozen-lockfile
pnpm lint
pnpm typecheck
pnpm build
pnpm test
pnpm pack:check
```

Rust (`rayd`; Linux o VM Linux, ver 1.1 y 1.2):

```bash
buf lint
cargo fmt --all --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
cargo zigbuild --release --locked --target aarch64-unknown-linux-musl -p rayd
make licenses         # cargo-about 0.9.2: genera THIRD_PARTY_LICENSES.md y comprueba que cubre cargo tree
```

`THIRD_PARTY_LICENSES.md` (los avisos de terceros que viajan con `rayd`) no
se versiona: `make licenses` lo genera desde `Cargo.lock`, y
`make image-licenses` (cada `make image-zip*`, el job `build` de CI y la
release) lo regenera siempre antes de empaquetarlo. Un cambio de
`Cargo.lock`, tuyo o de Dependabot, no pide ningún commit más; si toca
`about.toml` o `about.hbs`, o añade un crate con licencia nueva, corre
`make licenses` para ver que la política lo acepta (CI lo hará igual).

Documentación:

```bash
make docs             # mkdocs build --strict
make docs-examples    # compila y comprueba los ejemplos del sitio (sin AWS)
```

Scripts de la raíz:

```bash
python3 scripts/check_pins.py
python3 scripts/check_hygiene.py
uvx ruff==0.16.7 check scripts
python3 scripts/gen_limits.py --check
python3 scripts/gen_agent_detection.py --check
python3 scripts/check_license.py
```

Gates locales que CI no ejecuta:

```bash
npx -y @fission-ai/openspec@1.10.0 validate --all --strict --no-interactive
T="$(mktemp -d)" && uv venv -q --python 3.12 "$T" && \
  uv pip install --python "$T" --require-hashes --no-deps --only-binary :all: \
  -r .github/release/requirements-cfn-lint.txt && "$T/bin/cfn-lint" infra/*.yaml
```

`make infra-lint` añade `aws cloudformation validate-template` sobre las
mismas plantillas (necesita credenciales de AWS; la llamada es gratuita).

#### Más gates

Scripts del repositorio y herramientas de empaquetado:

```bash
cd clients/python && uv run pytest ../../scripts/tests ../../infra/lambdas/events_webhooks/tests ../../infra/lambdas/dashboard/tests ../../infra/lambdas/quota_alerts/tests -p no:cacheprovider
```

```bash
(cd clients/python && uv build)
python3 scripts/check_wheel.py clients/python/dist/*.whl
T="$(mktemp -d)" && uv venv -q --python 3.12 "$T" && \
  uv pip install --python "$T" --require-hashes --no-deps --only-binary :all: \
  -r .github/release/requirements-twine.txt && "$T/bin/twine" check clients/python/dist/*
```

twine, cfn-lint y pip-audit nunca corren con `uvx`: `==` fija la
herramienta pero no su grafo, que se resolvería al vuelo en cada ejecución.
Se instalan desde `.github/release/requirements-*.txt` (cada pin con
`--hash`), y `scripts/check_pins.py` solo admite `uvx` para herramientas sin
dependencias (`ruff`).

El job `python-versions` repite `uv run pytest tests/unit` con el Python
más antiguo y el más nuevo que declaran los classifiers (3.11 y 3.13; `check`
cubre el 3.12 del runner). En local, sin tocar `.venv`:

```bash
cd clients/python && uv run --isolated --python 3.11 pytest tests/unit
cd clients/python && uv run --isolated --python 3.13 pytest tests/unit
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

Las órdenes exactas, con la VM Linux de `rayd`, también están en
`.claude/skills/rayito-engineering/reference/gates.md`.

### 7.2 Arquitectura y convenciones

- **Hexagonal y DDD.** El dominio es puro y no importa `grpc`, `boto3`,
  `@aws-sdk/*` ni código generado del proto; los puertos son `Protocol` /
  `interface` / traits; los adaptadores traducen los errores técnicos a
  excepciones propias; las dependencias llegan por constructor y los tests
  usan fakes. Nada especulativo: un adaptador es un seam hipotético, dos son
  uno real.
- **Sin valores mágicos.** Cada número o cadena con significado es una
  constante con nombre que cita su fuente (`AWS_API_NOTES.md`, doc de AWS o
  una medición). Los límites compartidos se editan en `limits.json` y
  `scripts/gen_limits.py` genera el código de los dos SDK.
- **Paridad Python / TypeScript.** Lo que existe en un SDK existe en el otro,
  con los mismos valores y nombres equivalentes (`*_seconds` ↔ `*Ms`),
  salvo diferencias intencionales declaradas en `scripts/check_sdk_parity.py`.
  Las superficies sync y async de Python son idénticas.
- **Identificadores en inglés; docs en español.** Ficheros, módulos, clases,
  funciones, variables, constantes y nombres de test, en inglés. El español
  va en cadenas de cara al usuario, docstrings, comentarios y documentación
  (`docs/site`).
- **Comentar el porqué, no el qué.** Nada de comentarios en línea dentro del
  cuerpo de una función: si una rama necesita explicación, se extrae un
  helper con nombre descriptivo o se cuenta en el docstring/TSDoc. Sin
  etiquetas de ticket en el código.
- **Rust**: `clippy` pedantic sin warnings; `thiserror` para errores de
  dominio, `anyhow` solo en `main`; nada de `unwrap()`, `expect()` ni
  `panic!()` fuera de tests. El dominio en `rayd-core` no conoce `tonic`,
  `axum` ni `tokio::process`.
- **Python**: type hints completos (`mypy --strict`), `ruff`.
- **TypeScript**: `strict`, `exactOptionalPropertyTypes`,
  `noUncheckedIndexedAccess`, `verbatimModuleSyntax`; Biome para lint y
  formato.
- **Errores**: dentro de streams, `StreamError` con códigos string
  (`not_found`, `permission_denied`, `unimplemented`); en unarios, status
  gRPC estándar.

Los agentes de código (y quien quiera la versión corta) tienen estas
convenciones como skill del proyecto en
`.claude/skills/rayito-engineering/SKILL.md`, con la checklist de paridad y
el flujo de release. Si una regla cambia aquí, cambia también allí.

### 7.3 Coste apagado por defecto

Nada que cree recursos o haga llamadas facturables corre sin una opción
explícita del SDK o la CLI. Sin la opción no hay ninguna llamada a ese
servicio. Lo que cuesta se documenta con el bloque «Coste y activación» y se
monta como componente opcional (`rayito stack deploy|status|destroy`), sin
servidores propios.

### 7.4 Nunca se versiona ni se loguea un dato de entorno

**Nunca se versionan** IDs de cuenta, ARNs con cuenta real, nombres de
bucket, IDs de recursos (VPC, subred, SG, EFS…), portales o perfiles de
AWS/SSO, IDs de MicroVM, URLs prefirmadas, tokens, rutas locales ni el nombre
de tu empresa o sus dominios: `scripts/check_hygiene.py` (gate de CI) los
rechaza. En documentación, tests y fixtures se usan los marcadores de AWS
(`123456789012`, `amzn-s3-demo-bucket`,
`microvm-00000000-0000-0000-0000-000000000001`, `vpc-0123456789abcdef0`),
`example.com` o `<tu-…>`. Lo que no tiene forma fija (empresa, dominios, tus
cuentas) va en tu lista privada `.git/info/hygiene-denylist`, un término por
línea, que Git nunca versiona y el gate aplica sin imprimirla. Lo mismo vale
para el título y el cuerpo de un PR:
`printf '%s' "$TEXTO" | python3 scripts/check_hygiene.py -` (CI lo comprueba
en `leaks.yml`, junto con `gitleaks` sobre la historia). Si algo se filtra, no
reescribas la historia por tu cuenta: avisa al mantenedor.

**Nunca se loguea** contenido de ficheros, código ejecutado, bytes de PTY,
tokens ni cuerpos de hooks (`SECURITY.md`, «Higiene de logging»).

## 8. Revisión de código

Una revisión, de una persona o de un agente, sigue la checklist del
repositorio:
[`reference/review-checklist.md`](.claude/skills/rayito-engineering/reference/review-checklist.md).
Empieza por el barrido seguro del diff (si algo se filtró, se para y se avisa
al mantenedor) y sigue con arquitectura, código limpio, paridad, coste y gates.

Quien revisa:

- Es amable y concreto; sugiere, no ordena.
- Se centra en la corrección, la legibilidad y el mantenimiento, no en
  preferencias de estilo personales.
- Marca como **bloqueante** lo que filtra un dato o rompe el comportamiento,
  como **mayor** lo que incumple una regla de esta guía y como **menor** lo
  que puede ir en un PR posterior.
- Aprueba cuando el PR cumple lo anterior, aunque lo habría resuelto distinto.

Quien envía el PR:

- Responde a cada comentario, aunque sea para darse por enterado.
- No se lo toma como algo personal: el objetivo es un código mejor para todos.
- Mantiene el PR pequeño y con un solo propósito; lo contrario se parte.
- Corrige con commits nuevos firmados en su rama (`--force-with-lease` solo
  ahí, nunca en `main`).

## 9. Comunicación

- Lo que atañe a un cambio concreto va en su issue o en su PR.
- Las preguntas generales y las propuestas de dirección, en GitHub Issues
  (o en Discussions, si el mantenedor las tiene activas), no enterradas en un
  hilo de PR.
- Si llevas más de un día bloqueado, dilo pronto en el issue o en el PR.
- Se aceptan issues y preguntas en español y en inglés.
- La conducta se rige por `CODE_OF_CONDUCT.md` («Reporting an Issue»).

## 10. Ayuda

- Empieza por la [documentación](https://alejandro-cedeno-10.github.io/rayito/)
  y por [`SUPPORT.md`](SUPPORT.md).
- **Bugs y propuestas**: con las plantillas de issues
  (`.github/ISSUE_TEMPLATE/`). Un bug útil trae SDK y versión,
  `agent_version` (`get_health()` / `getHealth()`), nombre y versión de la
  imagen, región y una reproducción mínima; nunca tokens, JWE ni
  `runHookPayload`.
- **Vulnerabilidades**: en privado, siguiendo [`SECURITY.md`](SECURITY.md)
  («Reportar una vulnerabilidad»). Nunca en un issue público.
- **Esta guía**: si algo está poco claro o desactualizado, abre un PR para
  arreglarlo. Este documento evoluciona con el proyecto.
