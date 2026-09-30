# Secretos

Rayito puede **guardar** secretos en AWS Secrets Manager de tu cuenta
(`SecretStore`, y el `Secret`/`AsyncSecret` del shim de E2B) y **entregarlos**
a un sandbox como variables de entorno (`secrets=`), con una caché que evita
traer el secreto en cada llamada (`SecretCache`).

!!! note "Estado: implementado, pendiente de aceptación en AWS real"
    Probado con fakes de Secrets Manager en los dos SDK; el e2e contra AWS
    real y las medidas SEC-9/SEC-10 (`AWS_API_NOTES.md` §19) aún no se han
    ejecutado. Hasta entonces, la fila de
    [Funciones opcionales](optional-features.md) no dice "disponible".

!!! info "Apagado por defecto; sólo con `secrets=` / `SecretStore`"
    Las dos funciones consumen dinero de AWS y siguen ADR-014
    ([Funciones opcionales y su coste](optional-features.md)): sin pasar
    `secrets=` / `secret_cache=` (TypeScript: `secrets` / `secretCache`) y sin
    instanciar `SecretStore`, Rayito **no construye ningún cliente de Secrets
    Manager, no importa `@aws-sdk/client-secrets-manager` y no hace ninguna
    llamada** (exactamente el camino de 0.4.0). Ninguna variable de entorno,
    fichero de configuración ni setter global las enciende.

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
    - espera al gateway de credenciales en loopback (fuera de este alcance):
      el valor no sería legible por el código del sandbox.

## Qué activa y qué cuesta

| | `SecretStore` (CRUD) | `secrets=` (inyección) |
|---|---|---|
| Activa | crear, actualizar, describir, listar y borrar secretos bajo un prefijo (`rayito/` por defecto) | entregar el valor como variable de entorno de un comando, una PTY, una celda Python o un contexto de código |
| Recursos AWS | un secreto de Secrets Manager por `create` | ninguno nuevo |
| Llamadas AWS | `CreateSecret`, `PutSecretValue`, `UpdateSecret`, `DescribeSecret`, `ListSecrets`, `DeleteSecret` (una por método) | `GetSecretValue` una vez por secreto y TTL de la caché (300 s por defecto) |
| Coste (us-east-1, [precios](https://aws.amazon.com/secrets-manager/pricing/), consultado 2026-09-30) | **$0,40 por secreto y mes**, prorrateado, **hasta que lo borras**, + $0,05 por 10 000 llamadas | $0,05 por 10 000 llamadas: con el TTL por defecto, ≤ 12 lecturas/hora por secreto y proceso ≈ **$0,04/mes** |
| IAM (credenciales del **llamante**, no el execution role) | política `RayitoSecretsAdmin` | política `RayitoSecretsReader` |

Contrato exacto de parámetros y errores: `AWS_API_NOTES.md` §19. Una CMK de
KMS (`kms_key_id=`) añade el coste de KMS y los permisos `kms:Decrypt` /
`kms:GenerateDataKey`; la clave gestionada por AWS (`aws/secretsmanager`) no.

## IAM: la plantilla opcional

`infra/secrets-access.yaml` crea dos políticas IAM gestionadas y nada más
(coste $0): `RayitoSecretsReader` (`GetSecretValue`, `DescribeSecret` sobre
`arn:aws:secretsmanager:<región>:<cuenta>:secret:rayito/*`) y
`RayitoSecretsAdmin` (además `CreateSecret`, `PutSecretValue`,
`UpdateSecret`, `DeleteSecret` en ese ARN y `ListSecrets` en `*`). Con
`KmsKeyArn`, añade `kms:Decrypt` (y `kms:GenerateDataKey` en la de
administrador) sólo a través de Secrets Manager (`kms:ViaService`).
Despliegue y borrado: [`infra/README.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/infra/README.md#secretos-infrasecrets-accessyaml-m13a).

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

    store.update("openai", "sk-rotada")        # versión 2
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

    await store.update("openai", "sk-rotada");
    await secretCache.refresh("openai");
    await store.destroy("openai");
    ```

    `@aws-sdk/client-secrets-manager` es una **peerDependency opcional**:
    instálala (`npm install @aws-sdk/client-secrets-manager`) sólo si usas
    secretos. Sin ella, nada cambia; con `secrets` o `SecretStore` y sin
    ella, la primera llamada lanza `InvalidArgumentError` con el comando de
    instalación.

=== "Shim de E2B"

    ```python
    from rayito.e2b import Secret

    info = Secret.create("openai-key", "sk-...", region="us-east-1")
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
| Crear / conectar | `Sandbox.create(secrets=, secret_cache=)`, `Sandbox.connect(id, secrets=, …)`, `sbx.connect(secrets=, …)` | `Sandbox.create({ secrets, secretCache })`, `Sandbox.connect(id, { secrets })`, `sbx.connect({ secrets })` | el handle guarda sólo las referencias; se resuelven **antes** de `run-microvm`: un secreto que falta falla sin lanzar un VM; `connect` sin `secrets` conserva los del handle (un `secret_cache=`/`secretCache` solo cambia la caché, no las referencias) |
| Pool | `pool.take(secrets=)`, `Sandbox.create(pool=, secrets=)` | `pool.take({ secrets })`, `Sandbox.create({ pool, secrets })` | se enlazan al sandbox que sale del pool; las plazas calientes nunca los llevan (ni en su lanzamiento ni en su `SlotRecord`) |
| Comandos | `commands.run(..., secrets=)` (también `background=True`) | `commands.run(cmd, { secrets })` | `StartRequest.envs` |
| PTY | `pty.create(secrets=)` | `pty.create({ secrets })` | `PtyStart.envs` |
| Código | `run_code(..., secrets=)` | `runCode(code, { secrets })` | `ExecuteRequest.envs`, sólo en contextos **Python** y sólo durante esa celda; con otro `language` es `InvalidArgumentException`, y los del handle no se añaden a celdas de otros lenguajes |
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
  `secrets=`.
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
- `destroy` es `DeleteSecret(ForceDeleteWithoutRecovery=True)`: no hay
  ventana de recuperación. Recrear el mismo nombre justo después puede
  tardar unos segundos (el borrado es asíncrono); `create` reintenta con
  backoff hasta 30 s si AWS responde `InvalidRequestException` mencionando
  el borrado (supuesto de SEC-9, pendiente de medir en AWS real).

## Errores

| Python | TypeScript | Cuándo |
|---|---|---|
| `SecretNotFoundException` (también `NotFoundException`) | `SecretNotFoundError` | el secreto no existe o está programado para borrarse |
| `SecretException` (un `SandboxException`) | `SecretError` (un `SandboxError`) | permiso IAM que falta (nombra la acción), ya existe, choque de versión, límite de versiones, secreto binario |
| `RateLimitException` | `RateLimitError` | `ThrottlingException` tras los reintentos del SDK de AWS |
| `InvalidArgumentException` | `InvalidArgumentError` | nombre, valor (≤ 64 KiB), `metadata`, TTL o variable inválidos; conflicto `envs`/`secrets` |

Ningún error contiene el valor ni el nombre del secreto; el mensaje de AWS
tampoco se propaga (puede nombrar el secreto): sólo su código.

## Cómo apagarlo

1. Quita `secrets=` / `secret_cache=` (TS: `secrets` / `secretCache`) de
   tus llamadas y deja de instanciar `SecretStore`: Rayito vuelve al camino
   de 0.4.0.
2. **Borra los secretos** que ya no uses (`SecretStore().destroy(nombre)`,
   `Secret.destroy(nombre)` o la consola): Secrets Manager los factura
   ($0,40/mes cada uno) **hasta que se borran**, uses o no Rayito.
3. Si desplegaste `infra/secrets-access.yaml`, borra el stack.

Modelo de amenazas: T18 de
[`SECURITY.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/SECURITY.md)
y [Seguridad](security.md#custodia-de-secretos-del-usuario-t18).
