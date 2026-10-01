# Funciones opcionales y su coste

Rayito nunca cobra por sorpresa. Tres frases resumen ADR-014 (ver
[`ARCHITECTURE.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/ARCHITECTURE.md#adr-014--componentes-opcionales-en-la-cuenta-del-cliente)):
toda función que consuma cuota o dinero de AWS está **apagada por defecto** y
sólo se activa con una **opción explícita** del SDK — nunca con una variable
de entorno, un fichero de configuración ni un setter global; sin ninguna
opción puesta, Rayito crea **cero recursos AWS y hace cero llamadas AWS
extra** frente a 0.4.0; y los componentes que sí hacen falta corren siempre
en tu propia cuenta, **sin servidor** hospedado por Rayito, como plantillas
CloudFormation independientes bajo `infra/` que despliegas tú.

## Convención de opt-in

Cuatro reglas fijas (ADR-014), las mismas para Python y TypeScript, que
siguen `clients/python/src/rayito/_optional.py` y
`clients/typescript/src/optional.ts` y que debe seguir cualquier función
opcional futura:

- **(a) Un kwarg nombrado por función.** Como mucho, un segundo kwarg con el
  objeto de configuración. `None`/`undefined` es "apagada"; ninguna variable
  de entorno, fichero de configuración ni setter global la enciende; activar
  una opción nunca cambia el significado de otro kwarg.
- **(b) Objeto de configuración inmutable y reutilizable.** Su nombre de
  clase dice el servicio de AWS (`DynamoDbIndex`, `SecretCache`,
  `SecretStore`) y crea su cliente boto3 / AWS SDK **de forma perezosa, en su
  primer uso** — nunca en el constructor del objeto de configuración ni en el
  del `Sandbox`.
- **(c) Peers opcionales de TypeScript cargados bajo demanda.** Los clientes
  de AWS SDK v3 que 0.4.0 no usaba (`@aws-sdk/client-secrets-manager`,
  `@aws-sdk/client-dynamodb`) y `@opentelemetry/api` son peerDependencies
  **opcionales**: se cargan con `import()` dinámico sólo dentro de la
  función ya activada (`loadOptionalPeer`), nunca a nivel superior de un
  módulo. En Python, el equivalente es `rayito[otel]` vía `require_module`.
- **(d) Bloque de docstring "Coste y activación".** Toda opción de coste lo
  lleva, con la plantilla de abajo.

## La plantilla de cada opción de coste

Cada función de esta página documenta su coste y activación en el docstring
(Python) / TSDoc (TypeScript) de la opción que la activa, con un bloque
titulado literalmente **"Coste y activación"**:

```text
Coste y activación
-------------------
Activa: <qué hace exactamente esta opción>
Recursos y llamadas AWS: <qué crea o llama, con nombres de API reales>
Coste aproximado: <cifra, región y fecha, con enlace a la página de precios>
IAM: <permisos mínimos necesarios>
Cómo apagarla: <qué valor la deja apagada — normalmente None/undefined>
Ejemplo:
    <fragmento runnable de ≤ 10 líneas>
```

`scripts/tests/test_optional_features_docs.py` comprueba, para cada fila
marcada "disponible" en la tabla de abajo, que el símbolo de opción citado
aparece en el fichero SDK de su columna "Dónde" dentro de un mismo bloque
"Coste y activación" que trae, además, los seis apartados de la plantilla.

## Funciones con coste AWS

| Función | Estado | Opción Python | Opción TypeScript | Por defecto | Qué activa | Recursos / llamadas AWS | Coste aproximado | IAM necesario | Cómo apagarla | Dónde |
|---|---|---|---|---|---|---|---|---|---|---|
| [Inyección de secretos](#secrets-injection) | implementado, pendiente de aceptación en AWS real (M13a) | `secrets=` | `secrets` | `None` / `undefined` | Entrega el valor de uno o más secretos como variable de entorno de un comando, PTY, celda Python, contexto de código o plaza tomada del pool (visible para el código del sandbox, fase 1); la caché se fija con `secret_cache=SecretCache(...)` / `secretCache` | `secretsmanager:GetSecretValue` una vez por secreto y TTL de `SecretCache` (300 s por defecto; un acierto hace 0 llamadas), nunca una vez por llamada; ningún recurso nuevo | SM: $0,05/10 000 llamadas (≈ $0,04/mes por secreto y proceso con el TTL por defecto) + el propio secreto, $0,40/secreto-mes ([precios de Secrets Manager](https://aws.amazon.com/secrets-manager/pricing/), consultado 2026-09-30, us-east-1) | `secretsmanager:GetSecretValue` (y `DescribeSecret`) sobre `…:secret:rayito/*` en las credenciales del **llamante** (política `RayitoSecretsReader` de `infra/secrets-access.yaml`); `kms:Decrypt` sólo con una **clave KMS gestionada por el cliente** | No pasar `secrets=` / `secrets` ni `secret_cache=` / `secretCache` (o pasar `None`/`undefined`) | `clients/python/src/rayito/_secrets.py` / `clients/typescript/src/secrets/inject.ts` |
| [Secret CRUD (Secrets Manager)](#secrets-crud) | implementado, pendiente de aceptación en AWS real (M13a) | `SecretStore(...)` | `new SecretStore({...})` | sin instanciar = sin cliente boto3/SDK | Crear, actualizar, listar y borrar secretos nativos de Rayito bajo un prefijo (y el shim `Secret`/`AsyncSecret` de E2B) | Un secreto de Secrets Manager por `create`; `secretsmanager:CreateSecret/PutSecretValue/UpdateSecret/DescribeSecret/ListSecrets/DeleteSecret` (`AWS_API_NOTES.md` §19) | SM: $0,40/secreto-mes **hasta `destroy`** + $0,05/10 000 llamadas ([precios de Secrets Manager](https://aws.amazon.com/secrets-manager/pricing/), consultado 2026-09-30, us-east-1) | CRUD de Secrets Manager bajo el prefijo configurado (`rayito/` por defecto) y `ListSecrets` en `*` (política `RayitoSecretsAdmin` de `infra/secrets-access.yaml`); añade `kms:GenerateDataKey`/`kms:Decrypt` sobre la clave si se pasa `kms_key_id=`/`kmsKeyId` (clave gestionada por el cliente); sin ese parámetro, la clave gestionada por AWS no cobra ni exige permiso KMS aparte | No instanciar `SecretStore` / `new SecretStore(...)` (llamadas explícitas únicamente) y `destroy()` los secretos creados | `clients/python/src/rayito/_secrets.py` / `clients/typescript/src/secrets/store.ts` |
| [Índice de metadatos (DynamoDB)](#metadata-index) | implementado, pendiente de aceptación en AWS real (M14) | `index=DynamoDbIndex(...)` | `index: new DynamoDbIndex({...})` | `None` / `undefined` | Escribe una fila inmutable por sandbox (`metadata`, imagen, `startedAt`, TTL) en tu tabla DynamoDB al crearlo (`create()`, `PoolConfig`) y la une con `list-microvms` para filtrar `list()`/`paginate()` por metadatos también sobre `SUSPENDED`, sin sondear `Health` ni despertar nada; también en el shim (`Sandbox.list(..., index=)`, `E2B(index=)`) y la CLI (`--index-table`) | `dynamodb:PutItem` una vez por sandbox creado (condicional) y `dynamodb:BatchGetItem` una vez por página de `list-microvms` al listar con `metadata` + `index`; la tabla la despliegas tú (`infra/metadata-index.yaml`, on-demand, TTL en `expires_at`); nunca `DeleteItem` | DynamoDB on-demand ([precios de DynamoDB on-demand](https://aws.amazon.com/dynamodb/pricing/on-demand/), consultado 2026-09-30, us-east-1): $0,625 por millón de WRU (~1 por `create` ≈ $0,000000625) + $0,125 por millón de RRU (`BatchGetItem` se factura por ítem leído: 0,5 RRU por sandbox candidato, lectura eventualmente consistente de ≤ 4 KB) + $0,25/GB-mes tras 25 GB gratis; borrado por TTL gratis. Ejemplo: 10 000 sandboxes/mes ≈ 10 000 WRU ($0,006) y un `list()` diario sobre ellos ≈ 300 000 ítems × 0,5 = 150 000 RRU ($0,019): ≈ $0,03/mes; tabla vacía $0 | `dynamodb:PutItem` (escritor, política `RayitoIndexWriter`) y `dynamodb:BatchGetItem` (lector, `RayitoIndexReader`) sobre el ARN de la tabla, en las credenciales del **llamante** | No pasar `index=` / `index` (o pasar `None`/`undefined`); borrar el stack de `infra/metadata-index.yaml` para dejar de pagar el almacenamiento | `clients/python/src/rayito/_index.py` / `clients/typescript/src/index/dynamodb.ts` |
| [Trazas OpenTelemetry del SDK](#otel-sdk) | planificado (M13b) | `tracer_provider=` | `tracerProvider` | `None` / `undefined` | Instrumenta `create/connect/kill/pause/resume` (y las demás llamadas del SDK) con spans OTel sobre el `TracerProvider` que ya tengas configurado | Ninguno propio: Rayito no crea ni llama ningún servicio AWS por esto — el coste depende de **tu** exportador (CloudWatch, un collector propio, …) | $0 desde Rayito: el exportador OTel lo paga y lo configura quien lo activa, no este SDK ([precios de CloudWatch](https://aws.amazon.com/cloudwatch/pricing/) sólo si exportas ahí, consultado 2026-09-30) | Ninguno propio de Rayito; el que exija tu exportador OTel | No pasar `tracer_provider=` / `tracerProvider` (o pasar `None`/`undefined`) | `clients/python/src/rayito/_otel.py` / `clients/typescript/src/otel.ts` (llega en M13b) |

Las cuatro filas empezaron en "planificado (Mxx)": cada grupo cambia **su**
fila a "disponible (0.5.0)" y completa su sección de ejemplo cuando la
entrega de verdad, aceptada contra AWS real (`MILESTONES.md`). Las dos de
secretos (M13a) están implementadas y probadas con fakes, pero **todavía no
aceptadas contra AWS real**: su e2e (`clients/python/tests/e2e/test_secrets_e2e.py`
y `clients/typescript/tests/e2e/secrets.e2e.test.ts`) no se ha ejecutado y
SEC-9/SEC-10 siguen sin medir (`AWS_API_NOTES.md` §19). Pasarán a
"disponible (0.5.0)" cuando ese e2e pase; es la puerta de archivo de
`m13-secrets`. Lo mismo vale para el índice de metadatos (M14): implementado
y probado con fakes, con su e2e
(`clients/python/tests/e2e/test_metadata_index_e2e.py` y
`clients/typescript/tests/e2e/metadata-index.e2e.test.ts`, IDX-1 en
`AWS_API_NOTES.md` §20) todavía sin ejecutar contra AWS real; es la puerta de
archivo de `m14-metadata-index`.

## Sin coste AWS

| Función | Opción | Qué activa | Recursos AWS | Coste | Dónde |
|---|---|---|---|---|---|
| [`rayito sandbox proxy`](#local-proxy) | CLI `rayito sandbox proxy <id> --port N` | Sirve un puerto del guest en `127.0.0.1` (otra interfaz sólo con `--bind` + `--allow-remote`), renovando el JWE de `x-aws-proxy-port` antes de que expire | `lambda:GetMicrovm` (una vez, para el endpoint) + `lambda:CreateMicrovmAuthToken` (~1 cada 45 min por proxy en marcha); IAM: `lambda:GetMicrovm` y `lambda:CreateMicrovmAuthToken` sobre el MicroVM (ya en la `CallerPolicy` de `infra/iam.yaml`) | $0: ambas llamadas son gratuitas (cuotas de 100 y 50 TPS, `AWS_API_NOTES.md` §11); si el sandbox estaba suspendido, despertarlo por auto-resume factura su cómputo normal más la lectura del snapshot al reanudar, no el proxy en sí | `clients/python/src/rayito/cli/_proxy.py` (CLI) |

Esta fila no pasa por el ciclo "planificado → disponible" de la tabla de
arriba: no consume cuota ni dinero adicional (reutiliza llamadas gratuitas que el
SDK ya hace), así que no necesita una aceptación de coste independiente.

## Ejemplos

<a id="secrets-injection"></a>

### Inyección de secretos

Implementada en `m13-secrets` para 0.5.0, **pendiente de aceptación contra
AWS real** (ver arriba). Guía completa, reglas de la caché y modelo de
amenazas en [Secretos](secrets.md).

=== "Python"

    ```python
    from rayito import Sandbox, SecretCache

    cache = SecretCache(ttl_seconds=300)          # opcional; no llama a AWS
    sbx = Sandbox.create(secrets={"OPENAI_API_KEY": "openai"}, secret_cache=cache)
    sbx.commands.run("python agent.py")           # 1 GetSecretValue por TTL, no por comando
    sbx.commands.run("env", secrets={"GH_TOKEN": "gh"})
    ```

=== "TypeScript"

    ```ts
    // npm install @aws-sdk/client-secrets-manager   (peer opcional)
    import { Sandbox, SecretCache } from "rayito";

    const secretCache = new SecretCache({ ttlSeconds: 300 });
    const sbx = await Sandbox.create({ secrets: { OPENAI_API_KEY: "openai" }, secretCache });
    await sbx.commands.run("python agent.py");
    await sbx.commands.run("env", { secrets: { GH_TOKEN: "gh" } });
    ```

**La caché, nunca una lectura por llamada.** Opción `secret_cache=SecretCache(ttl_seconds=...)`
(TS: `secretCache: new SecretCache({ ttlSeconds })`); sin ella, una caché
compartida del proceso por (región, sesión/credenciales). TTL de 300 s por
defecto, de 1 a 86 400 (`0` se rechaza). Clave: región, credenciales,
secreto y versión (`VersionId`/`VersionStage`). Una sola lectura en vuelo
por clave (diez llamadas a la vez = una `GetSecretValue`); un acierto hace 0
llamadas, y sólo se relee al vencer el TTL o con `refresh()`/`invalidate()`.
Los valores nunca se registran ni van en `metadata`, etiquetas,
`runHookPayload`, el entorno de la imagen ni el del proceso `rayd`/sidecar,
ni en mensajes de error: sólo llegan al entorno del comando, PTY, celda
Python (mientras dura) o contexto de código que los pide explícitamente.
Detalle en [Secretos](secrets.md#la-cache-nunca-en-cada-llamada).

**Fase 1: el valor es visible para el código del sandbox** (ADR-014, punto
6). La inyección entrega el secreto como variable de entorno del proceso que
lo pide; no hay, todavía, un gateway que medie su uso sin exponer el valor a
uid 1000 dentro del sandbox. Para código no confiable, usa credenciales de
corta vida y mínimo privilegio, nunca credenciales de larga vida.

<a id="secrets-crud"></a>

### Secret CRUD (Secrets Manager)

Implementada en `m13-secrets` para 0.5.0, **pendiente de aceptación contra
AWS real** (ver arriba). Cada secreto se factura hasta que lo
borras: apagar la función es dejar de instanciar `SecretStore` **y**
`destroy()` los secretos creados.

=== "Python"

    ```python
    from rayito import SecretStore

    store = SecretStore(region="us-east-1")       # no llama a AWS hasta el primer método
    store.create("openai", "sk-...", metadata={"team": "ml"})
    store.update("openai", "sk-rotada")           # versión 2
    print([info.name for info in store.list(limit=20).items])
    store.destroy("openai")                       # deja de facturar
    ```

=== "TypeScript"

    ```ts
    // npm install @aws-sdk/client-secrets-manager   (peer opcional)
    import { SecretStore } from "rayito";

    const store = new SecretStore({ region: "us-east-1" });
    await store.create("openai", "sk-...", { metadata: { team: "ml" } });
    await store.update("openai", "sk-rotada");
    console.log((await store.list({ limit: 20 })).items.map((info) => info.name));
    await store.destroy("openai");
    ```

El shim de E2B (`from rayito.e2b import Secret`) usa el mismo `SecretStore`;
diferencias en [Compatibilidad con E2B](e2b-compat.md#secretos-secret-asyncsecret).

<a id="metadata-index"></a>

### Índice de metadatos (DynamoDB)

Implementado en `m14-metadata-index` para 0.5.0, **pendiente de aceptación
contra AWS real** (ver arriba). Despliega antes la tabla (una vez, $0 en
reposo):

```bash
aws cloudformation deploy --stack-name rayito-metadata-index \
  --template-file infra/metadata-index.yaml --capabilities CAPABILITY_IAM
```

=== "Python"

    ```python
    from rayito import DynamoDbIndex, Sandbox

    idx = DynamoDbIndex("rayito-sandboxes")       # no llama a AWS todavía
    sbx = Sandbox.create(metadata={"user": "42"}, index=idx)   # 1 PutItem
    sbx.pause()
    for item in Sandbox.list(metadata={"user": "42"}, states=["SUSPENDED"], index=idx):
        print(item.sandbox_id, item.state, item.metadata)      # 1 BatchGetItem por página
    ```

=== "TypeScript"

    ```ts
    import { DynamoDbIndex, Sandbox } from "rayito";

    const index = new DynamoDbIndex({ tableName: "rayito-sandboxes" });
    const sbx = await Sandbox.create({ metadata: { user: "42" }, index });
    await sbx.pause();
    for await (const item of Sandbox.list({ metadata: { user: "42" }, states: ["SUSPENDED"], index })) {
      console.log(item.sandboxId, item.state, item.metadata);
    }
    ```

=== "CLI"

    ```bash
    rayito sandbox list --metadata user=42 --state suspended --index-table rayito-sandboxes
    ```

Qué cambia frente a `list(metadata=)` sin índice: ninguna sonda de `Health`,
ningún token ni `get-microvm`, así que funciona sobre sandboxes en pausa sin
despertarlos. Lo que **no** cambia: el estado sale siempre de
`list-microvms`, y sólo aparecen los sandboxes creados con `index=` (o por un
`PoolConfig(index=)`). `on_write_failure='terminate'` (por defecto; TS
`onWriteFailure: "terminate"`) termina el VM si su fila no se pudo escribir;
`'warn'` sólo avisa. TypeScript necesita el peer opcional
`npm install @aws-sdk/client-dynamodb`. Guía completa en
[Observabilidad](observability.md#listado-por-metadatos-con-indice-opcional).

<a id="otel-sdk"></a>

### Trazas OpenTelemetry del SDK

*Llega en M13b.* Cuando esté disponible, un ejemplo Python y otro TypeScript
de `tracer_provider=`/`tracerProvider` con un exportador en memoria
sustituirá este párrafo.

<a id="local-proxy"></a>

### `rayito sandbox proxy`

```bash
rayito sandbox proxy microvm-<id> --port 8000
# sirve http://127.0.0.1:8000 -> puerto 8000 del guest,
# renovando el JWE antes de que expire; Ctrl+C para cortar.
```

Escucha sólo en loopback (`127.0.0.1`) salvo que pases `--bind <ip>` junto
con `--allow-remote`: quien llegue a ese puerto usa el sandbox con tu acceso.

No hace falta ninguna opción del SDK: es sólo CLI, sin coste AWS propio.
