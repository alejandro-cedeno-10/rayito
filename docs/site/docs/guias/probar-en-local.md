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
- Sólo para los tests de agentes (abajo): `uv` y una sesión de AWS en tu
  máquina, porque `make local-bedrock-key` corre con `uv run`.

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

## Agentes contra un modelo real

Los tests de agentes (`test_local_agents.py` y `agents.local.test.ts`)
corren [`sbx.agent`](agente-en-el-sandbox.md) con OpenCode y deepagents
dentro de un sandbox local, con Claude en Amazon Bedrock como modelo y
`bedrock_gateway` / `bedrockGateway` como única salida. Comprueban, en cada
runtime y en los dos SDK, que el agente usa sus herramientas, continúa una
sesión, se aborta sin dejar procesos vivos, respeta `AgentLimits` (pasos,
tokens y timeout), emite el stream de eventos esperado, no puede leer la
credencial, no sale a Internet por su cuenta mientras el modelo sí responde
y que no hay telemetría encendida por defecto. Sin clave se saltan, así que
`make local-e2e` y CI siguen sin credenciales.

!!! info "Coste y activación"
    - **Por defecto**: apagado. Sin `make local-bedrock-key` los tests de
      agentes se saltan y nada sale del entorno local.
    - **Activa**: `make local-agent-up` (cambia el guest por la variante
      con OpenCode, ripgrep, deepagents y su runner de `dev/local/agent/`,
      con `CAP_NET_ADMIN` para que el deny-all de egress se aplique de
      verdad) y `AWS_PROFILE=<tu-perfil> make local-bedrock-key`.
    - **Recursos y llamadas AWS**: ningún recurso. La clave es una URL de
      `bedrock:CallWithBearerToken` prefirmada en tu máquina (vale lo que tu
      sesión, como mucho 12 h); cada suite hace unas treinta llamadas
      `ConverseStream`/`Converse` desde `rayd` por la pasarela. El secreto
      vive en el Secrets Manager de Floci.
    - **Coste aproximado**: céntimos por corrida, de tokens de Bedrock
      (Claude Haiku 4.5, us-east-1, consultado el 2026-10-06:
      <https://aws.amazon.com/bedrock/pricing/>).
    - **IAM**: `bedrock:CallWithBearerToken` e `bedrock:InvokeModel*` sobre
      el perfil de inferencia, y el acceso al modelo ya habilitado en la
      cuenta (Rayito no lo habilita por ti).
    - **Cómo apagarla**: `make local-down` (borra el tmpfs del runner con la
      clave) o no ejecutes `make local-bedrock-key`.
    - **Ejemplo**:

        ```bash
        make local-up
        make local-agent-up
        AWS_PROFILE=<tu-perfil> make local-bedrock-key
        make local-e2e LOCAL_E2E_ARGS="-k agents"
        make local-down
        ```

La clave pasa del host al runner por una tubería y queda en
`/tmp/rayito-local-bedrock-key` (`RAYITO_LOCAL_BEDROCK_KEY_FILE`), un tmpfs
que muere con el contenedor: nunca en un fichero de tu máquina ni en una
variable de entorno. Los tests la buscan dentro del sandbox para decir sí o
no y nunca la imprimen. El modelo y su región se cambian con
`RAYITO_E2E_BEDROCK_MODEL` y `RAYITO_E2E_BEDROCK_REGION` (por defecto,
Claude Haiku 4.5 en us-east-1). Un test marcado como fallo esperado
recuerda que el timeout aún no mata lo que lanzó el shell del agente.

### Otros proveedores sin claves de terceros

`test_local_agent_providers.py` prueba de verdad los caminos al estilo de
OpenAI de los [proveedores del agente](agente-proveedores.md) con lo
que ya tienes en AWS, sin cuentas en OpenAI ni en otros proveedores:

| Ruta | Pasarela y `AgentModel` | Qué comprueba |
|---|---|---|
| `bedrock-chat` | `openai_compatible_gateway` hacia `bedrock-runtime` (`/openai/v1`), gpt-oss-120b | herramientas, sesión, egress cerrado, clave ilegible |
| `mantle-chat` | `openai_compatible_gateway` hacia `bedrock-mantle` (`/v1`) | lo mismo |
| `mantle-responses` | `provider="openai"` (Responses API) hacia `bedrock-mantle` | una vuelta sin herramientas con uso de tokens |
| `litellm` | `litellm_gateway` hacia un proxy de LiteLLM local delante de Bedrock | herramientas, sesión, egress cerrado, clave ilegible |

Las tres primeras usan la misma clave de `make local-bedrock-key`. La de
LiteLLM la crea `make local-litellm-up` (`dev/local/agent/litellm.sh`):
arranca LiteLLM, fijado por digest, en la red del entorno con el nombre
`litellm` y sin puertos en el host; le da un certificado de una CA de
prueba que añade al almacén del sistema del guest (con el que `rayd`
verifica el upstream de la pasarela; como `rayd` lo lee al arrancar,
reinicia el guest, así que no lo lances con un sandbox vivo), y deja la
clave maestra en el tmpfs del runner. LiteLLM habla con Bedrock con las credenciales temporales de tu
sesión, que sólo viajan como variables de entorno del contenedor; cuando
caduquen, repite `make local-litellm-up`.

```bash
make local-agent-up
AWS_PROFILE=<tu-perfil> make local-bedrock-key
AWS_PROFILE=<tu-perfil> make local-litellm-up
make local-e2e LOCAL_E2E_ARGS="-k providers"
make local-litellm-down
```

`bedrock-mantle` se aparta de la API de OpenAI en dos puntos (medido el
2026-10-07): el `response.output_item.done` de una llamada a herramienta
llega con `id: null` y rechaza un mensaje de asistente reenviado sin `id`
ni `status`. Por eso `mantle-responses` sólo prueba una vuelta sin
herramientas; contra la API de OpenAI el camino completo no está probado
de verdad. OpenAI, Gemini, Azure OpenAI, OpenRouter, Groq, Mistral,
DeepSeek y xAI sólo los cubren los tests unitarios contra un upstream falso.

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
| `OptionalStacks` (`rayito stack`) | despliegue, estado, redespliegue y borrado de seis pilas (`metadata-index`, `secrets-access`, `otlp-export`, `s3-mounts`, `sizes-guard`, `templates`), más `events-webhooks` desde `LifecycleEvents` | `custom-domain` y `efs-volumes`; permisos IAM y coste reales |
| Índice de metadatos, `SecretStore`, `secrets=` | sí | |
| Pasarela de secretos (`gateways=`) | la sección de `ConfigureSandbox` y el listener | el reenvío HTTPS al upstream |
| `Template.build` | subida del zip a S3 y `create-microvm-image` | el build real de la imagen y su `/ready` |
| Eventos y webhooks | la pila, los webhooks, `create(events=)` y los tres Lambdas ejecutados en proceso, con una entrega firmada a un receptor local | la suscripción de CloudWatch Logs y la entrega HTTPS con su filtro SSRF |
| Proxy de AWS (JWE, 403, keepalives) | no | sí |
| `sbx.agent` (OpenCode, deepagents) por la pasarela | sí, con `make local-agent-up` y una clave de Bedrock: deny-all de egress real, herramientas, sesión, aborto, límites, eventos | el primer `exec` y la memoria en un MicroVM |
| Bloqueo de IMDS, egress, montajes S3 | no: necesitan privilegios en el guest (salvo el egress con `make local-agent-up`) | sí |
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
