# Solución de problemas

Busca el síntoma; cada entrada dice la causa y el arreglo. Lo primero, casi
siempre:

```bash
rayito doctor --template rayito-base           # añade --launch para probar un sandbox real
```

## `rayito doctor` { #rayito-doctor }

Cada comprobación termina en `OK`, `WARN`, `FAIL` o `SKIP`. Un permiso
denegado sale como `<operación> denegado: …`; con `rayito --json doctor`,
`details.operation` nombra la llamada que AWS rechazó y `details.action` el
permiso que hace falta. Las siete primeras son de sólo lectura; las 8 a 10
necesitan un sandbox `RUNNING` de la imagen, o `--launch` para crear uno de
300 s que se mata al final.

| # | Comprobación | `FAIL` (o `WARN`) típico | Arreglo |
|---|---|---|---|
| 1 | `credentials` | sin credenciales, token caducado, sin región; `WARN` si la región no tiene Lambda MicroVMs | `aws sso login --profile <tu-perfil>`; exporta `AWS_PROFILE` y `AWS_REGION` con una de las diez regiones |
| 2 | `managed-images` | `AccessDenied`, endpoint desconocido o región sin el servicio | actualiza `boto3` (`pip install -U boto3`); cambia de región |
| 3 | `quotas` | `WARN`: una cuota de MicroVM por debajo del valor por defecto (cuentas nuevas); `SKIP` si falta el permiso | pide el aumento en Service Quotas |
| 4 | `iam-simulation` | `WARN` por `lambda:PassNetworkConnector`; `SKIP` sin `iam:SimulatePrincipalPolicy` | es un falso positivo del simulador: ignóralo. Otros `implicitDeny`: revisa la [política](iam.md) |
| 5 | `bucket` | 403/404 en el bucket de artefactos, o bucket en otra región; `SKIP` sin `--bucket` ni `RAYITO_BUCKET` | concede `s3:ListBucket`; usa un bucket de la misma región |
| 6 | `image-gate` | la imagen no existe o su versión no es lanzable | `rayito image publish …`; si el build falló, mira su `stateReason` |
| 7 | `sandboxes` | más de 10 sandboxes `RUNNING` de esa imagen (`WARN` de 1 a 10): facturan | `rayito sandbox list`; `rayito sandbox kill --all --template <imagen>` |
| 8 | `token` | `AccessDenied` al acuñar el token del proxy; `SKIP` si no hay ningún sandbox `RUNNING` | concede `lambda:CreateMicrovmAuthToken`; usa `--launch` |
| 9 | `agent` | `rayd responde pero no está agent_ready`, o no responde (`UNAVAILABLE`, 403 del proxy); `WARN` con `kernel_ready=false`, `hook_anomalies` mayor que 0 o `imds_blocked=false` en una imagen `-caps`; `SKIP` sin token | espera unos segundos; republica la imagen; con `hook_anomalies`, revisa quién acuña tokens `allPorts` ([Seguridad](../security.md)) |
| 10 | `compatibility` | el `rayd` de la imagen es más antiguo que el que exige el SDK ([tabla de compatibilidad](../limits.md#compatibilidad-sdk-rayd-imagen)) | publica la imagen de la release de tu SDK ([Configurar AWS](../primeros-pasos/configurar-aws.md#4-publicar-la-imagen-rayito-base)) |
| — | `efs-network` (sólo con `--efs-vpc-id` y `--efs-subnet-ids`) | la VPC o las subredes no sirven para `rayito stack deploy efs-volumes` | corrige lo que nombra; no crea nada ([Volúmenes EFS en tu VPC](../funciones-opcionales/volumenes-efs-vpc.md)) |

## Credenciales y región

**`ExpiredTokenException`, `Token has expired`, `UnauthorizedSSOTokenError`.**
La sesión de SSO caducó. Vuelve a entrar con
`aws sso login --profile <tu-perfil>`. Las URLs de S3 firmadas con
credenciales temporales dejan de valer cuando caducan esas credenciales,
aunque la URL diga más.

**`sin región: pasa --region o exporta AWS_REGION` (CLI, salida 2).** El
SDK y la CLI necesitan una región: `export AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1` (el SDK de Python, vía boto3, sólo lee `AWS_DEFAULT_REGION` o la región del perfil; TypeScript y la CLI leen `AWS_REGION`).

**`AuthenticationException` / `AuthenticationError` al conectar.** El access
token no es el del sandbox (lo has perdido o es de otro). No se puede
recuperar: crea un sandbox nuevo y guarda `sbx.access_token` junto al id.

## La imagen

**`UnimplementedError: … actualiza la imagen` (o "publica una imagen
nueva").** El `rayd` de la imagen es más antiguo que la función que pides
(plazo del servidor, URLs de S3, historial de métricas, kernels de Deno…).
Publica la imagen de la release de tu SDK y comprueba con `rayito doctor`
(comprobación `compatibility`).

**`UnimplementedError` nombrando `rayito-base-poly` o `rayito-base-caps`.**
La función necesita otra variante de imagen: bash, JavaScript y TypeScript
viven en `rayito-base-poly`; la política de red, en `rayito-base-caps`
([Imágenes](../images.md)).

**El build de la imagen falla.** `rayito image publish` imprime los
`stateReason` y las últimas líneas del log de build (grupo
`/rayito/<imagen>` en CloudWatch). Causas habituales: el rol de build no
puede leer el bucket, o el zip es de otra variante (`--variant`).

**Compilar `rayd` en macOS o Windows.** Hace falta Linux o WSL2 (en macOS,
una VM Linux). Para empezar no lo necesitas: publica el `rayito-image.zip`
firmado de la release ([Configurar AWS](../primeros-pasos/configurar-aws.md#4-publicar-la-imagen-rayito-base)).

## Cuotas y ritmo

**`RateLimitException` / `RateLimitError` con `ThrottlingException`.** Las
APIs de Lambda MicroVMs tienen cuotas por cuenta y región: 5 `run-microvm`
por segundo, 2 `suspend`, 5 `resume`, 10 `terminate`, 50 tokens del proxy y
100 `get-microvm`. El SDK ya limita el ritmo por proceso; varios procesos a
la vez pueden superarlas. Espacia los lanzamientos o usa un
[pool](../pool.md).

**`QuotaExceededException` / `QuotaExceededError`.** Has llegado a la cuota
de memoria de la región (por ejemplo 1 024 GB en `us-east-1`, contando los
sandboxes `RUNNING` y `SUSPENDED`). Mata sandboxes o pide un aumento.

**`RateLimitException` dentro del sandbox.** Más de 256 procesos y PTYs
vivos, más de 8 contextos de código o más de 16 transferencias activas. Libera
los que no uses.

## Ficheros y S3

**Una clave que falta da 403 en vez de 404.** A tus credenciales les falta
`s3:ListBucket` sobre el bucket (acotado al prefijo). Sin él, S3 no puede
decir "no existe" y la importación espera hasta caducar. Añádelo
([IAM](iam.md)).

**`UnimplementedError` en `upload_url` / `download_url`.** No hay bucket de
transferencias: pasa `transfer=S3Staging("amzn-s3-demo-bucket")` o exporta
`RAYITO_TRANSFER_BUCKET`.

**El shim de E2B no acepta `transfer=`.** Es a propósito (E2B no lo tiene):
en `rayito.e2b` el bucket sale de `RAYITO_TRANSFER_BUCKET` (y
`RAYITO_TRANSFER_PREFIX`, `RAYITO_TRANSFER_REGION`).

**`FileUploadException` con `wrong_region`.** El bucket está en otra región
que el sandbox: usa uno de la misma región o `S3Staging(region=)`.

## Ejecución

**Una celda o un comando silencioso se queda a medias o tarda de más.** La
auto-suspensión cuenta el tráfico del endpoint: algo que no imprime nada
durante `max_idle_seconds` (300 s por defecto) puede suspenderse mientras
corre. El SDK se reengancha al reanudar, pero sube `max_idle_seconds` o pasa
`idle=None` para tareas largas y silenciosas ([Pausar y
reanudar](../guias/pausar-reanudar.md)). En el servidor MCP:
`RAYITO_MCP_IDLE_SECONDS=0`.

**`SandboxException: output_truncated`.** Un comando produjo salida y nadie
la leyó en 30 s: el agente retiene 64 trozos de 32 KiB y luego corta. Lee el
handle (iterándolo o con `wait()`) o redirige la salida a un fichero.

**`TimeoutException` en un comando.** El `timeout` por defecto es 60 s. Para
procesos largos, `timeout=None` (TypeScript: `timeoutMs: 0`) o
`background=True`.

**`execution.error.name == "ExecutionTimeout"`.** La celda superó su
`timeout` (300 s por defecto): súbelo.

**Procesos `<defunct>` en `ps` tras lanzar un demonio.** Con `rayd` 0.6.0 o
anterior, un proceso que se demoniza (doble `fork`, `nohup … &` dentro de
un subshell, `mount-s3` sin `--foreground`) quedaba como zombi colgado de
`rayd`, que es el PID 1 del sandbox. `rayd` 0.6.1 los recoge en cuanto
terminan sin tocar el código de salida de tus comandos
([Novedades de 0.6.1](../novedades/0.6.1.md#rayd-recoge-los-procesos-zombi));
con una imagen anterior, republícala sobre `rayd` 0.6.1. Los
zombis no consumen CPU ni memoria, sólo una entrada en la tabla de
procesos.

## Red

**`UnimplementedError` al pasar `network=` o `allow_internet_access=False`.**
La política de red sólo se aplica en `rayito-base-caps`; en otra imagen el
SDK termina el sandbox antes de devolverlo, para no darte uno con la red
abierta.

**Un cliente falla con la política activa aunque el host esté permitido.**
Las reglas por nombre de host pasan por un proxy local; los clientes que no
honran `HTTPS_PROXY` (sockets en crudo, `ssh`, `git://`) fallan cerrados
([Red saliente](../network.md)).

**`403` o `502` en `get_host(port)`.** Faltan las cabeceras del proxy
(`host.headers`) o nada escucha en ese puerto ([Puertos y
host](../guias/puertos-y-host.md)).

## TypeScript

**`InvalidArgumentError: <función> necesita el paquete opcional '<paquete>': instala npm install <paquete>`.**
Los clientes de AWS de las funciones opcionales (secretos y pasarela,
índice, pilas opcionales, eventos, volúmenes EFS, dominio propio,
OpenTelemetry...) son *peerDependencies* opcionales del paquete: sólo se
cargan cuando usas esa función. Instala el que nombra el mensaje (por
ejemplo, `npm i @aws-sdk/client-secrets-manager` para `secrets=` o
`gateways=`).

**`await using` no compila.** Necesitas TypeScript ≥ 5.2 con
`"lib": ["ES2022", "ESNext.Disposable"]`. Sin `await using`, usa
`try { … } finally { await sbx.kill(); }`.

## Agente

Los fallos de `sbx.agent.run()`/`.stream()` son `AgentException`
(TypeScript: `AgentError`) con un `reason` cerrado; la tabla completa está
en [Errores](../referencia/errores.md#agentexception-agenterror).

**`reason="runtime_missing"`.** La imagen no trae el runtime (`opencode` no
está en el `PATH`). Crea el sandbox desde una imagen construida con `AgentTemplate`
([Templates de agente](../funciones-opcionales/templates-de-agente.md)).

**`reason="busy"`.** Ya hay un `run`/`stream` en curso en ese sandbox: uno a
la vez por sandbox. Espera a que termine o usa otro sandbox.

**`reason="model_error"`.** El modelo respondió con error. Revisa que el
secreto de la pasarela tenga la credencial completa (para Bedrock,
`Bearer <clave>`, que caduca: rótala con `sbx.gateways.refresh()`) y que el
modelo esté en `models=` del preset
([Agente en el sandbox](../guias/agente-en-el-sandbox.md)).

## Secretos

!!! danger "El log DEBUG de botocore imprime los secretos"
    Rayito nunca escribe el valor ni el nombre de un secreto en sus logs.
    Pero si activas el log DEBUG del SDK de AWS (`logging.DEBUG` en el logger
    raíz o en `botocore`, `boto3.set_stream_logger()`, o un `logger` en el
    cliente del AWS SDK v3), **ese** log incluye los cuerpos de Secrets
    Manager con el `SecretString` en claro. Limita el DEBUG al logger
    `rayito`: `logging.getLogger("rayito").setLevel(logging.DEBUG)`.

**`SecretNotFoundException` con un secreto creado desde el shim.** El shim
pasa los nombres a minúsculas (`Secret.create("OpenAI", …)` guarda
`rayito/openai`); el SDK nativo no. Refiérete a él en minúsculas.

**`SecretException` con `InvalidRequestException` al recrear un nombre.**
AWS tarda ≈ 20–30 s en liberar un nombre borrado. `create` ya reintenta
hasta 60 s; si se agota, espera y vuelve a intentarlo.

## Facturas inesperadas

**Sandboxes que nadie mató.** `rayito sandbox list` los muestra con su
edad; `rayito sandbox kill --all` los termina. Para que no vuelva a pasar,
usa siempre `with` / `await using`, un `timeout` ajustado y el
[plazo del servidor](../lifecycle.md) con `on_timeout="kill"`, que se cumple
aunque tu proceso muera.

**Versiones de imagen acumuladas.** Cada una cuesta ≈ $0,04 por semana
([Precios](../cost.md#cuanto-cuesta-con-ejemplos)):
`rayito image prune --dry-run` y después `rayito image prune --keep 5`.

## Ver también

- [Errores](../referencia/errores.md): cada excepción y qué hacer.
- [FAQ](faq.md)
- [Abrir un issue](https://github.com/alejandro-cedeno-10/rayito/issues)
  con la salida de `rayito --json doctor` (no lleva secretos).
