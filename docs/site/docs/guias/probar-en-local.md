# Probar en local (Docker + Floci)

<small>En el repositorio desde 0.7.0 ([Novedades](../novedades/0.7.0.md#probar-en-local-con-docker-y-floci)).</small>

Un entorno de Docker para correr los SDK de Python y TypeScript contra un
`rayd` de verdad y contra un AWS emulado, sin cuenta de AWS, sin credenciales
y sin coste. Sirve para el bucle de desarrollo y para revisar un PR antes de
gastar en la aceptación contra AWS real, que sigue siendo la que cierra un
hito o una release.

## Qué levanta

```text
┌──────────── red interna "aws" (sin Internet) ────────────┐
│  floci        emulador de AWS en :4566                    │
│  runner       Python (uv) + Node (pnpm), los tests        │
│   └─ guest    imagen de producto, rayd como PID 1         │
│               (comparte la red del runner: loopback)      │
└───────────────────────────────────────────────────────────┘
```

- **floci**: [Floci](https://github.com/floci-io/floci) 2.1.0 (MIT), fijado
  por digest. Emula S3, DynamoDB, Secrets Manager, CloudFormation, STS,
  CloudWatch Logs y el plano de control de Lambda MicroVMs.
- **guest**: la imagen de `image/Dockerfile` tal cual, con `rayd` y el
  sidecar de kernels. Hace de MicroVM: los tests llaman a sus hooks
  (`/ready`, `/run`, `/suspend`, `/resume`, `/terminate`) como lo haría la
  plataforma.
- **runner**: las toolchains de los dos SDK. Las dependencias se instalan en
  volúmenes, nunca en el `.venv` ni en los `node_modules` de tu máquina.
  Corre como uid 993, fuera del rango del sandbox (1000-65535) como el agente
  de la plataforma: `rayd` rechaza el `/run` y el `/terminate` que llegan
  desde un uid del sandbox. Si levantaste el entorno con una versión anterior
  y no lo bajaste, corre `make local-down` antes de `make local-up` para que
  los volúmenes se creen con el dueño nuevo.

Nada publica puertos en tu máquina, ningún contenedor es privilegiado ni
monta el socket de Docker, y Floci no tiene salida a Internet. El porqué de
cada decisión está en `docs/research/2026-10-local-testing.md`.

## Requisitos

- Docker con Compose v2 (Docker Desktop, Colima, Lima u OrbStack valen).
- Un host **arm64** (Apple Silicon, Graviton o el runner `ubuntu-24.04-arm`
  de GitHub): la base de la imagen de producto solo existe para arm64. En x86
  funciona con emulación QEMU, mucho más despacio.
- Unos 6 GB de disco libres para Docker. De memoria, 3 GB bastan para correr
  el entorno, pero compilar `rayd` dentro de Docker pide unos 6 GB: el crate
  `aws-sdk-s3` (montajes S3) solo ya pasa de 2 GB al compilarse. Con menos,
  compila `rayd` en tu máquina y pásalo con `LOCAL_RAYD_BIN` (abajo).
- `make` y `python3` (para copiar el sidecar al contexto del guest).

## Uso

```bash
make local-up      # construye y arranca floci, runner y guest; instala dependencias
make local-e2e     # corre los tests `local` de Python y de TypeScript
make local-down    # lo para todo y borra los volúmenes
```

`make local-up` compila `rayd` dentro de Docker la primera vez (unos minutos,
sin Rust en tu máquina). Si ya tienes el binario de `make build` (estático,
musl, con `cargo zigbuild`; funciona también desde macOS), pásalo y te
ahorras la compilación; es la vía si tu VM de Docker tiene menos de 6 GB y
rustc muere con `SIGKILL`:

```bash
make build
make local-up LOCAL_RAYD_BIN=target/aarch64-unknown-linux-musl/release/rayd
```

Para correr solo una parte, los argumentos de `LOCAL_E2E_ARGS` van a pytest,
y cada suite se puede lanzar sola dentro del runner:

```bash
make local-e2e LOCAL_E2E_ARGS="-k commands"
docker compose -f dev/local/compose.yaml exec runner \
  bash -c 'cd clients/python && uv run --no-sync pytest tests/local -m local -k secret'
docker compose -f dev/local/compose.yaml exec runner \
  bash -c 'cd clients/typescript && pnpm exec vitest run --project local'
```

## Cómo funciona por dentro

Los tests (`clients/python/tests/local/` y `clients/typescript/tests/local/`)
construyen un `LocalGuestControlPlane`: otro adaptador del puerto
`ControlPlane`, que envuelve el plano real apuntado a Floci. `run-microvm`,
`get-microvm`, `list-microvms` y `terminate-microvm` pasan por el adaptador
de verdad (Floci valida las peticiones contra el modelo del servicio); lo que
Floci no emula lo sustituye el guest:

| Operación | En AWS | En local |
|---|---|---|
| Endpoint del MicroVM | `*.lambda-microvm.<región>.on.aws` por TLS | `127.0.0.1`, h2c por loopback |
| Token del proxy | `create-microvm-auth-token` | un valor fijo (lo valida el proxy, no `rayd`) |
| `/run` | lo llama la plataforma tras restaurar el snapshot | lo llama el plano local tras `/ready` |
| Pausa y reanudación | `suspend-microvm` / `resume-microvm` | los hooks `/suspend` y `/resume` del guest |
| Terminar | `terminate-microvm` | `/terminate`: `rayd` sale y Docker lo rearranca |

El SDK publicado no cambia ni sabe que esto existe: los tests le pasan el
plano y el transporte por los parámetros que ya tiene (`control_plane=` y
`transport=` en Python, `controlPlane` y `transport` en TypeScript). Sin la
variable `RAYITO_LOCAL_GUEST`, que solo define el runner, los tests `local`
se saltan.

## Qué cubre y qué no

| Área | En local | Solo contra AWS real |
|---|---|---|
| Comandos, ficheros, PTY, `run_code`, contextos, `connect`, métricas | sí, contra el `rayd` real | |
| Pausa y reanudación | la máquina de estados de `rayd` | snapshot y restauración de la memoria |
| `OptionalStacks` (`rayito stack`) | despliegue, estado, redespliegue y borrado de las siete pilas | permisos IAM y coste reales |
| Índice de metadatos, `SecretStore`, `secrets=` | sí | |
| Pasarela de secretos (`gateways=`) | la sección de `ConfigureSandbox` y el listener | el reenvío HTTPS al upstream |
| `Template.build` | subida del zip a S3 y `create-microvm-image` | el build real de la imagen y su `/ready` |
| Eventos y webhooks | la pila, los webhooks, `create(events=)` y los tres Lambdas ejecutados en proceso, con una entrega firmada a un receptor local | la suscripción de CloudWatch Logs y la entrega HTTPS con su filtro SSRF |
| Proxy de AWS (JWE, 403, keepalives) | no | sí |
| Bloqueo de IMDS, egress, montajes S3 | no: necesitan privilegios en el guest | sí |
| Persistencia y transferencias prefirmadas | no: `rayd` usa IMDS y exige HTTPS | sí |
| Coste, cuotas, tiempos de arranque | no | sí |

Floci tiene algunas diferencias con AWS que los tests tienen en cuenta (la
versión de un secreto, `ListMicrovmImageVersions` de una imagen que aún no
existe); están en la investigación.

## Seguridad del emulador

Floci es código abierto de terceros y su API no tiene autenticación, así que
el compose lo trata como no confiable: usuario sin privilegios, sin
capabilities, sistema de ficheros de solo lectura, sin socket de Docker, sin
Internet y sin puertos en el host. La versión 2.1.0 corrige los avisos
críticos publicados hasta la fecha. Antes de subir la versión (Dependabot
abre el PR), lee sus notas de release y sus
[avisos de seguridad](https://github.com/floci-io/floci/security/advisories).

## En CI

El workflow `local-e2e` corre `make local-up` y `make local-e2e` en
`ubuntu-24.04-arm` en los PR que tocan el entorno local o `rayd` (`crates/`,
`proto/`, `Cargo.lock`), cada noche y a mano
(`workflow_dispatch`). No usa credenciales ni secretos del repositorio.
