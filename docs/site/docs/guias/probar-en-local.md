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

## Proveedores de modelo sin claves

Los tests de proveedores (`test_local_agent_providers.py` y
`agent-providers.local.test.ts`) prueban los nueve presets de
[proveedores del agente](agente-proveedores.md) (OpenAI, Gemini, Azure
OpenAI, OpenRouter, Groq, Mistral, DeepSeek, xAI y LiteLLM) con OpenCode y
con deepagents, sin claves reales ni Internet. `make local-providers-up`
añade a la variante de agentes un upstream HTTPS falso
(`dev/local/providers/fake_upstream.py`): en la red interna `providers`
responde a los nombres de los nueve proveedores, con una CA de un solo uso
en la que sólo confía `rayd` (`SSL_CERT_FILE` del guest), y contesta lo
mínimo válido de Chat Completions, Responses y Gemini. Por preset y runtime,
con el egress cerrado, los tests comprueban que:

- el agente completa una vuelta por la pasarela;
- al upstream llega el valor del secreto en la cabecera del preset
  (`authorization`, `x-goog-api-key` o `api-key`), y el marcador que ven
  los runtimes no sale nunca, ni en cabeceras ni en el cuerpo;
- sólo llegan rutas de la allowlist, y una ruta fuera de ella recibe 403
  de `rayd` sin llegar al upstream.

CI los corre en `local-e2e` después de `make local-e2e`.

```bash
make local-up
make local-providers-up
make local-providers-e2e
make local-down
```

### Prueba de humo contra las APIs reales

`test_local_agent_providers_smoke.py` hace una vuelta corta de cada runtime
contra la API real de cada proveedor cuya clave exportes. Va sobre
`make local-agent-up`, sin el upstream falso.

!!! info "Coste y activación"
    - **Por defecto**: apagada. Cada preset se salta si no exportas
      `RAYITO_SMOKE_<PRESET>_SECRET` (`OPENAI`, `GEMINI`, `AZURE_OPENAI`,
      `OPENROUTER`, `GROQ`, `MISTRAL`, `DEEPSEEK`, `XAI` o `LITELLM`).
    - **Activa**: `make local-providers-smoke` con, por preset, la clave en
      `RAYITO_SMOKE_<PRESET>_SECRET` (sin `Bearer`) y el modelo en
      `RAYITO_SMOKE_<PRESET>_MODEL`; Azure pide además
      `RAYITO_SMOKE_AZURE_OPENAI_RESOURCE` y LiteLLM
      `RAYITO_SMOKE_LITELLM_UPSTREAM`. Las variables pasan al runner por
      nombre, nunca por valor en la línea de órdenes.
    - **Recursos y llamadas AWS**: ninguno. La clave vive en el Secrets
      Manager de Floci; `rayd` hace por la pasarela una o dos llamadas al
      proveedor por runtime.
    - **Coste aproximado**: cada ejecución lleva
      `AgentLimits(max_total_tokens=20000)` y un prompt sin herramientas.
      Con un modelo de hasta 0,20 USD por millón de tokens de entrada, los
      dos runtimes juntos quedan por debajo de 0,01 USD por proveedor. Elige
      el modelo más barato de tu cuenta.
    - **IAM**: ninguno en AWS. La clave sólo necesita llamar al modelo;
      ponle un límite de gasto en el proveedor.
    - **Cómo apagarla**: no exportes las variables, o `make local-down`.
    - **Ejemplo**:

        ```bash
        make local-up
        make local-agent-up
        export RAYITO_SMOKE_GROQ_SECRET=<tu-clave> RAYITO_SMOKE_GROQ_MODEL=<modelo-barato>
        make local-providers-smoke
        make local-down
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
| `OptionalStacks` (`rayito stack`) | despliegue, estado, redespliegue y borrado de seis pilas (`metadata-index`, `secrets-access`, `otlp-export`, `s3-mounts`, `sizes-guard`, `templates`), más `events-webhooks` desde `LifecycleEvents` | `custom-domain` y `efs-volumes`; permisos IAM y coste reales |
| Índice de metadatos, `SecretStore`, `secrets=` | sí | |
| Pasarela de secretos (`gateways=`) | la sección de `ConfigureSandbox` y el listener | el reenvío HTTPS al upstream |
| `Template.build` | subida del zip a S3 y `create-microvm-image` | el build real de la imagen y su `/ready` |
| Eventos y webhooks | la pila, los webhooks, `create(events=)` y los tres Lambdas ejecutados en proceso, con una entrega firmada a un receptor local | la suscripción de CloudWatch Logs y la entrega HTTPS con su filtro SSRF |
| Proxy de AWS (JWE, 403, keepalives) | no | sí |
| `sbx.agent` (OpenCode, deepagents) por la pasarela | sí, con `make local-agent-up` y una clave de Bedrock: deny-all de egress real, herramientas, sesión, aborto, límites, eventos | el primer `exec` y la memoria en un MicroVM |
| Presets de proveedor (`openai_gateway`…`litellm_gateway`) | sí, con `make local-providers-up`: la cabecera que llega al upstream, el marcador que no sale, el 403 fuera de la allowlist y una vuelta de cada runtime contra un upstream falso | la respuesta real de cada API (prueba de humo opcional con tu clave) |
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
