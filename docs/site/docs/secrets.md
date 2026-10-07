# Secretos

Rayito puede **guardar** secretos en AWS Secrets Manager de tu cuenta
(`SecretStore`, y el `Secret`/`AsyncSecret` del shim de E2B) y **entregarlos**
a un sandbox como variables de entorno (`secrets=`), con una caché que evita
traer el secreto en cada llamada (`SecretCache`).

<small>Desde 0.5.0. Aceptado contra AWS Secrets Manager real.</small>

!!! info "Coste y activación"
    - **Por defecto**: apagado. Sin `secrets=` / `secret_cache=` (TypeScript:
      `secrets` / `secretCache`) y sin instanciar `SecretStore`, Rayito no
      construye ningún cliente de Secrets Manager, no importa
      `@aws-sdk/client-secrets-manager` y no hace ninguna llamada. Ninguna
      variable de entorno ni fichero de configuración lo enciende.
    - **Activa**: `SecretStore(...)` guarda, actualiza, lista y borra
      secretos bajo un prefijo (`rayito/`); `secrets=` entrega su valor como
      variable de entorno de un comando, una PTY, una celda o un contexto.
    - **Recursos y llamadas AWS**: un secreto de Secrets Manager por
      `create`; `GetSecretValue` una vez por secreto y TTL de la caché (300 s),
      nunca una vez por comando.
    - **Coste aproximado** (us-east-1, consultado 2026-09-30,
      [precios](https://aws.amazon.com/secrets-manager/pricing/)): $0,40 por
      secreto y mes **hasta que lo borras**, más $0,05 por 10 000 llamadas
      (≈ $0,04/mes por secreto y proceso con el TTL por defecto).
    - **IAM** (credenciales de quien llama al SDK): `RayitoSecretsReader` para
      inyectar y `RayitoSecretsAdmin` para el CRUD, de
      `infra/secrets-access.yaml`.
    - **Cómo apagarla**: quita `secrets=` y deja de instanciar `SecretStore`;
      `destroy()` los secretos que ya no uses ([Cómo apagarlo](#como-apagarlo)).

!!! warning "Fase 1: el código del sandbox PUEDE leer un secreto inyectado"
    `secrets=` entrega el **valor** como variable de entorno del proceso que lo
    pide. Cualquier código que corra en el sandbox puede leerlo: el entorno de
    su propio proceso, `/proc/<pid>/environ` de los procesos del mismo uid
    (1000) y el **snapshot de memoria de la suspensión** (si el sandbox se
    suspende con el proceso vivo, el valor queda en ese snapshot; si AWS lo
    cifra en reposo es la pregunta abierta SEC-5). La primera vez que un
    proceso usa `secrets=`, el SDK lo recuerda con un `RayitoCompatWarning`.

    Con **código no confiable** (el de un agente, el de un usuario):

    - no inyectes credenciales de larga duración (claves de acceso de IAM,
      tokens personales sin caducidad);
    - inyecta tokens de **vida corta y alcance mínimo** (por ejemplo, un
      token de una API con permiso sólo de lectura y que caduque en una hora);
    - úsalo sólo en procesos que controlas (el comando concreto que lo
      necesita, no el shell entero), o
    - usa la [pasarela de secretos](funciones-opcionales/pasarela-de-secretos.md)
      (`gateways=`, desde 0.6.0): el sandbox usa la credencial contra un
      `upstream` fijo sin poder leer su valor.

## Qué activa y qué cuesta

| | `SecretStore` (CRUD) | `secrets=` (inyección) |
|---|---|---|
| Activa | crear, actualizar, describir, listar y borrar secretos bajo un prefijo (`rayito/` por defecto) | entregar el valor como variable de entorno de un comando, una PTY, una celda Python o un contexto de código |
| Recursos AWS | un secreto de Secrets Manager por `create` | ninguno nuevo |
| Llamadas AWS | `CreateSecret`, `PutSecretValue`, `UpdateSecret`, `DescribeSecret`, `ListSecrets`, `DeleteSecret` (una por método; `update` y `destroy` hacen antes un `DescribeSecret`) | `GetSecretValue` una vez por secreto y TTL de la caché (300 s por defecto) |
| Coste (us-east-1, [precios](https://aws.amazon.com/secrets-manager/pricing/), consultado 2026-09-30) | **$0,40 por secreto y mes**, prorrateado, **hasta que lo borras**, + $0,05 por 10 000 llamadas | $0,05 por 10 000 llamadas: con el TTL por defecto, ≤ 12 lecturas/hora por secreto y proceso ≈ **$0,04/mes** |
| IAM (credenciales del **llamante**, no el execution role) | política `RayitoSecretsAdmin` | política `RayitoSecretsReader` |

!!! danger "El log DEBUG de botocore / AWS SDK imprime el valor"
    Rayito **nunca** escribe el valor ni el nombre de un secreto en sus logs
    (loggers `rayito.*` en Python, el `logger` de las opciones en
    TypeScript), en `repr`/`toJSON` ni en errores. Pero si activas el log
    DEBUG del SDK de AWS que Rayito usa por debajo —`logging.DEBUG` en el
    logger raíz o en `botocore`/`urllib3`, `boto3.set_stream_logger()`, o
    un `logger` en el `SecretsManagerClient` del SDK v3—, **ese** log
    incluye los cuerpos de petición y respuesta de Secrets Manager, con el
    `SecretString` en claro (`CreateSecret`, `PutSecretValue`,
    `GetSecretValue`). No lo actives en procesos que manejan secretos, o
    limita el nivel DEBUG al logger `rayito`
    (`logging.getLogger("rayito").setLevel(logging.DEBUG)`).

Contrato exacto de parámetros y errores: `AWS_API_NOTES.md` §19 (en
[GitHub](https://github.com/alejandro-cedeno-10/rayito/blob/main/AWS_API_NOTES.md)). Una CMK de
KMS (`kms_key_id=`) añade el coste de KMS y los permisos `kms:Decrypt` /
`kms:GenerateDataKey`; la clave gestionada por AWS (`aws/secretsmanager`) no.

## IAM: la plantilla opcional

`infra/secrets-access.yaml` crea dos políticas IAM gestionadas y nada más
(coste $0): `RayitoSecretsReader` (`GetSecretValue`, `DescribeSecret` sobre
`arn:aws:secretsmanager:<región>:<cuenta>:secret:rayito/*`) y
`RayitoSecretsAdmin` (además `CreateSecret`, `PutSecretValue`,
`UpdateSecret`, `DeleteSecret` en ese ARN y `ListSecrets` en `*`;
`destroy` usa `DescribeSecret` y `DeleteSecret`, las dos incluidas). Con
`KmsKeyArn`, añade `kms:Decrypt` (y `kms:GenerateDataKey` en la de
administrador) sólo a través de Secrets Manager (`kms:ViaService`).
`RayitoSecretsReader` además **niega** `GetSecretValue` sobre
`rayito/webhooks/*`: ahí viven los secretos de firma de los
[webhooks](funciones-opcionales/eventos-y-webhooks.md), y un sandbox que
recibiera uno por `secrets=` podría falsificar entregas firmadas. El SDK
también se niega a leerlos por `secrets=`/`SecretCache`
(`InvalidArgumentException`) aunque tu política lo permitiera.
`SecretPrefix` debe terminar en `/`: `rayito` (sin barra) concedería
también secretos ajenos como `rayito-prod-db`. Se despliega como el
componente `secrets-access` de [`rayito stack`](funciones-opcionales/pilas-opcionales.md):

```bash
rayito stack deploy secrets-access --param SecretPrefix=rayito/
# con una clave KMS propia (SecretStore(kms_key_id=...)), añade:
#   --param KmsKeyArn=arn:aws:kms:<región>:123456789012:key/<id>
rayito stack status secrets-access    # salidas ReaderPolicyArn y AdminPolicyArn
```

Asigna `ReaderPolicyArn` (o `AdminPolicyArn` si vas a crear y borrar
secretos) a quien ejecuta el SDK, igual que la política de
[Configurar AWS](primeros-pasos/configurar-aws.md#3-la-pila-de-iam):
`aws iam attach-user-policy` o `aws iam attach-role-policy`. `SecretPrefix`
debe coincidir con `SecretStore(prefix=)`. Para borrarla:
`rayito stack destroy secrets-access` (quita las políticas, no los
secretos). Desde el SDK, `OptionalStacks().deploy("secrets-access", ...)`
hace lo mismo. Si prefieres CloudFormation a mano, la plantilla es
`infra/secrets-access.yaml`; más detalle en
[`infra/README.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/infra/README.md#secretos-infrasecrets-accessyaml-m13a).

## Ejemplos

=== "Python"

    ```python
    import os

    from rayito import Sandbox, SecretCache, SecretRef, SecretStore

    store = SecretStore(region="us-east-1")                 # no llama a AWS
    store.create("openai", os.environ["OPENAI_API_KEY"], metadata={"team": "ml"})

    cache = SecretCache(ttl_seconds=300, region="us-east-1")  # opcional
    sbx = Sandbox.create(secrets={"OPENAI_API_KEY": "openai"}, secret_cache=cache)
    sbx.commands.run("python agent.py")        # 0 llamadas: resuelto al crear
    sbx.commands.run("python eval.py")         # 0 llamadas: acierto de caché
    sbx.commands.run("env", secrets={"GH_TOKEN": SecretRef("gh", version_stage="AWSCURRENT")})
    sbx.kill()

    store.update("openai", os.environ["OPENAI_API_KEY_NUEVA"])  # versión 2
    cache.refresh("openai")                    # sin esperar al TTL
    store.destroy("openai")                    # deja de facturar
    ```

=== "TypeScript"

    ```ts
    import { Sandbox, SecretCache, SecretRef, SecretStore } from "rayito";

    const store = new SecretStore({ region: "us-east-1" }); // no llama a AWS
    await store.create("openai", process.env.OPENAI_API_KEY!, { metadata: { team: "ml" } });

    const secretCache = new SecretCache({ ttlSeconds: 300, region: "us-east-1" });
    const sbx = await Sandbox.create({ secrets: { OPENAI_API_KEY: "openai" }, secretCache });
    await sbx.commands.run("python agent.py"); // 0 llamadas: resuelto al crear
    await sbx.commands.run("env", {
      secrets: { GH_TOKEN: new SecretRef("gh", { versionStage: "AWSCURRENT" }) },
    });
    await sbx.kill();

    await store.update("openai", process.env.OPENAI_API_KEY_NUEVA ?? "");
    await secretCache.refresh("openai");
    await store.destroy("openai");
    ```

    `@aws-sdk/client-secrets-manager` es una **peerDependency opcional**:
    instálala (`npm install @aws-sdk/client-secrets-manager`) sólo si usas
    secretos. Sin ella, nada cambia; con `secrets` o `SecretStore` y sin
    ella, la primera llamada lanza `InvalidArgumentError` con el comando de
    instalación.

=== "Shim E2B"

    ```python
    import os

    from rayito.e2b import Secret

    info = Secret.create("openai-key", os.environ["OPENAI_API_KEY"], region="us-east-1")
    print(info.secret_id)                 # el ARN de Secrets Manager
    Secret.fill("openai-key")             # '${e2b.secrets.openai-key}': NO se resuelve
    Secret.destroy("openai-key")
    ```

    El shim pasa los nombres a **minúsculas** (como E2B):
    `Secret.create("OpenAI", …)` guarda `rayito/openai`. `secrets=` y
    `SecretStore` del SDK nativo no normalizan, así que ese secreto se
    inyecta como `secrets={"OPENAI_API_KEY": "openai"}`; con `"OpenAI"` la
    llamada lanza `SecretNotFoundException`.

    Las diferencias con E2B están en
    [Compatibilidad con E2B](e2b-compat.md#secretos-secret-asyncsecret).

## Dónde se aplica `secrets=`

| Llamada | Python | TypeScript | Notas |
|---|---|---|---|
| Crear / conectar | `Sandbox.create(secrets=, secret_cache=)`, `Sandbox.connect(id, secrets=, …)`, `sbx.connect(secrets=, …)` | `Sandbox.create({ secrets, secretCache })`, `Sandbox.connect(id, { secrets })`, `sbx.connect({ secrets })` | el handle guarda sólo las referencias; se resuelven **antes** de `run-microvm`: un secreto que falta falla sin lanzar un VM; `connect` sin `secrets` conserva los del handle (un `secret_cache=`/`secretCache` solo cambia la caché, no las referencias); `secrets={}` (TS: `secrets: {}`) los borra todos y conserva la caché |
| Pool | `pool.take(secrets=)`, `Sandbox.create(pool=, secrets=)` | `pool.take({ secrets })`, `Sandbox.create({ pool, secrets })` | se enlazan al sandbox que sale del pool; las plazas calientes nunca los llevan (ni en su lanzamiento ni en su `SlotRecord`) |
| Comandos | `commands.run(..., secrets=)` (también `background=True`) | `commands.run(cmd, { secrets })` | `StartRequest.envs` |
| PTY | `pty.create(secrets=)` | `pty.create({ secrets })` | `PtyStart.envs` |
| Código | `run_code(..., secrets=)` | `runCode(code, { secrets })` | `ExecuteRequest.envs`, sólo en contextos **Python** y sólo durante esa celda; con otro `language` es `InvalidArgumentException`, y los del handle no se añaden a celdas de otros lenguajes. Con `context=` dado como **id** (texto), los del handle sólo van si el contexto es `default` o uno que ese handle creó o listó como Python: un id desconocido (creado desde otro handle) va sin ellos; pasa el `CodeContext` o `secrets=` en la llamada |
| Contextos | `create_code_context(secrets=)` | `createCodeContext({ secrets })` | `CreateContextRequest.envs`: el entorno del kernel, de cualquier lenguaje, mientras viva |
| `reincarnate()` | relanza con las referencias (y la caché) que tenga el handle **en ese momento**, incluidas las de `pool.take(secrets=)` o `connect(secrets=)` | igual | sólo referencias, nunca valores |

Reglas:

- **Handle ∪ llamada**: cada llamada inyecta los secretos del handle más los
  suyos; si repiten una variable, gana la llamada.
- **Todo o nada**: una variable que está a la vez en `envs` y en `secrets` es
  `InvalidArgumentException` (TS: `InvalidArgumentError`), nunca se pisa en
  silencio. El error nombra la variable, nunca el secreto.
- Una cadena simple es `SecretRef(nombre)`; `SecretRef(nombre,
  version_id=…)` o `version_stage=…` fija la versión; un ARN completo se usa
  tal cual.
- **Nunca** viajan en el `runHookPayload` (`create(envs=)` no cambia),
  `metadata`, etiquetas, variables de entorno de la imagen, registros del
  pool, logs, `repr`/`JSON.stringify` ni mensajes de error.
- La entrega reutiliza los `envs` de `ProcessService`, `PtyService` y
  `CodeService`, que ya viajan cifrados hasta el proxy de AWS y autenticados
  con el access token; `rayd` construye el entorno del hijo desde cero y no
  registra `envs`. No hay RPC nueva: funciona con imágenes 0.4.0.

## La caché: nunca en cada llamada

`SecretCache(ttl_seconds=300)` (TS: `new SecretCache({ ttlSeconds: 300 })`):

- **Clave**: (región, credenciales —la sesión boto3 en Python, el proveedor
  de credenciales en TypeScript—, `SecretId` resuelto, `VersionId` o
  `VersionStage`, `AWSCURRENT` por defecto). Dos cuentas o dos regiones
  nunca comparten un valor.
- **TTL** de 1 a 86 400 s (300 por defecto). `0` se rechaza: sería traer el
  secreto en cada llamada.
- **Un acierto hace 0 llamadas a AWS.** Una sola petición en vuelo por clave:
  diez hilos, tareas o promesas a la vez hacen **una** `GetSecretValue`.
- **Refresco** sólo al vencer el TTL o con `refresh(nombre)` (lee ya) /
  `invalidate(nombre)` (descarta; sin nombre, todo). Tras un
  `SecretStore.update`, los lectores ven el valor nuevo al vencer su TTL o
  con `refresh()`.
- **Un secreto que no existe no se guarda**: el siguiente intento vuelve a
  preguntar.
- Sin `secret_cache=`, `secrets=` usa una caché **compartida del proceso**
  por (región, sesión), con TTL 300, creada la primera vez que se usa
  `secrets=`. Como mucho hay 32 (`MAX_SHARED_CACHES`): un proceso que crea
  una sesión o un proveedor de credenciales por tenant o por petición
  descarta la usada hace más tiempo (sólo cuesta releer después).
- **Los valores vencidos se descartan**, no se quedan en memoria: cada uso
  de una caché (y cada vez que se pide una compartida, para todas las
  compartidas) borra antes los valores cuyo TTL ya pasó; `sweep()` lo hace
  a mano. No hay temporizador: una caché que nadie vuelve a usar conserva
  sus valores hasta que el proceso la suelta.
- Los valores viven **sólo en la memoria del proceso** del SDK; `repr`,
  `str`, `toJSON` e `inspect` muestran `***`.

## Versiones y metadatos

- La versión entera de E2B se codifica en el `ClientRequestToken` de Secrets
  Manager (`rayito-secret-version-{n:020d}`, 42 caracteres): `create` es la 1
  y cada `update` escribe `n + 1`, donde `n` es la mayor versión de Rayito
  que aún lista `DescribeSecret` (no sólo la `AWSCURRENT`: tras una
  rotación externa la actual no es de Rayito). Dos escritores con valores
  distintos que calculan el mismo `n + 1` chocan: el segundo recibe
  `SecretException` y debe reintentar. Si varias rotaciones externas
  seguidas dejan las versiones de Rayito sin etiqueta, `DescribeSecret` ya
  no las lista y `update` puede repetir un número usado: crea un secreto
  nuevo en ese caso.
- `update` más de una vez cada 600 s para el mismo secreto avisa una vez
  (Secrets Manager recomienda no escribir más de una vez cada 10 minutos y
  conserva como mucho 100 versiones más las de las últimas 24 h).
- `metadata` va en `Description` como `rayito:v1:` + JSON compacto, ≤ 2048
  caracteres (validado antes de llamar a AWS).
- `destroy` es `DescribeSecret` y, si el secreto existe,
  `DeleteSecret(ForceDeleteWithoutRecovery=True)`: no hay ventana de
  recuperación. Devuelve `True` si lo borró y `False` si no existía (o ya
  estaba programado para borrarse, u otro proceso lo borró a la vez), sin
  llamar a `DeleteSecret`, como E2B. El `DescribeSecret` previo es
  necesario porque AWS acepta el borrado forzado de un nombre que no existe
  sin `ResourceNotFoundException`.
- **Recrear un nombre recién borrado**: el borrado es asíncrono y AWS tarda
  en liberar el nombre (en la aceptación de 0.5.0, entre 19 y 28 s).
  Mientras tanto `CreateSecret` responde `InvalidRequestException`
  mencionando el borrado y `create` reintenta con el mismo
  `ClientRequestToken`, con backoff exponencial (0,5 s doblando hasta 8 s,
  ±25 % de jitter) durante como mucho **60 s**
  (`CREATE_RETRY_BUDGET_SECONDS` en Python, `CREATE_RETRY_BUDGET_MS` en
  TypeScript). Pasado ese plazo lanza `SecretException`
  (`SecretError`) con `aws_code="InvalidRequestException"`.
- **`list()` es eventualmente consistente**: `ListSecrets` puede tardar
  ~3–5 s en mostrar un secreto recién creado o el cambio de uno recién
  actualizado (y en dejar de mostrar uno recién borrado). `get_info`,
  `exists` y `destroy` (`DescribeSecret`) no tienen ese retraso. Un test
  que crea y lista debe **sondear** `list()` con un plazo, no comprobarlo
  una sola vez.

## Errores

| Python | TypeScript | Cuándo |
|---|---|---|
| `SecretNotFoundException` (también `NotFoundException`) | `SecretNotFoundError` | el secreto no existe o está programado para borrarse |
| `SecretException` (un `SandboxException`) | `SecretError` (un `SandboxError`) | permiso IAM que falta (nombra la acción), ya existe, choque de versión, límite de versiones, secreto binario |
| `RateLimitException` | `RateLimitError` | `ThrottlingException` tras los reintentos del SDK de AWS |
| `InvalidArgumentException` | `InvalidArgumentError` | nombre, valor (≤ 64 KiB), `metadata`, TTL o variable inválidos; conflicto `envs`/`secrets`; un secreto de firma de webhook (`rayito/webhooks/...`) en `secrets=` o `SecretCache` |

Ningún error contiene el valor ni el nombre del secreto; el mensaje de AWS
tampoco se propaga (puede nombrar el secreto): sólo su código. Tampoco los
logs de Rayito; el log DEBUG del SDK de AWS sí los contiene (ver el aviso de
[Qué activa y qué cuesta](#que-activa-y-que-cuesta)).

## Cómo apagarlo

1. Quita `secrets=` / `secret_cache=` (TS: `secrets` / `secretCache`) de
   tus llamadas y deja de instanciar `SecretStore`: Rayito vuelve al camino
   de 0.4.0.
2. **Borra los secretos** que ya no uses (`SecretStore().destroy(nombre)`,
   `Secret.destroy(nombre)` o la consola): Secrets Manager los factura
   ($0,40/mes cada uno) **hasta que se borran**, uses o no Rayito.
3. Si desplegaste la pila de políticas, bórrala:
   `rayito stack destroy secrets-access`.

Modelo de amenazas: T18 de
[`SECURITY.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/SECURITY.md)
y [Seguridad](security.md#custodia-de-secretos-del-usuario-t18).
