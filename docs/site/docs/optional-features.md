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
aparece en el fichero SDK de su columna "Dónde" junto a ese mismo marcador.

## Funciones con coste AWS

| Función | Estado | Opción Python | Opción TypeScript | Por defecto | Qué activa | Recursos / llamadas AWS | Coste aproximado | IAM necesario | Cómo apagarla | Dónde |
|---|---|---|---|---|---|---|---|---|---|---|
| [Inyección de secretos](#secrets-injection) | planificado (M13a) | `secrets=` | `secrets` | `None` / `undefined` | Entrega el valor de uno o más secretos como variable de entorno de un comando, PTY, ejecución de código o plaza del pool | `secretsmanager:GetSecretValue` (con caché, nunca una vez por llamada) | SM: $0,40/secreto-mes + $0,05/10 000 llamadas ([precios de Secrets Manager](https://aws.amazon.com/secrets-manager/pricing/), consultado 2026-09-30, us-east-1) | `secretsmanager:GetSecretValue` sobre los ARN concretos; añade `kms:Decrypt` sobre la clave si el secreto usa una **clave KMS gestionada por el cliente** (con clave gestionada por AWS, `aws/secretsmanager`, no hace falta permiso KMS aparte) | No pasar `secrets=` / `secrets` (o pasar `None`/`undefined`) | `clients/python/src/rayito/_secrets.py` / `clients/typescript/src/secrets.ts` (llega en M13a) |
| [Secret CRUD (Secrets Manager)](#secrets-crud) | planificado (M13a) | `SecretStore(...)` | `new SecretStore({...})` | sin instanciar = sin cliente boto3/SDK | Crear, actualizar, listar y borrar secretos nativos de Rayito (y el shim `Secret`/`AsyncSecret` de E2B) | `secretsmanager:CreateSecret/PutSecretValue/GetSecretValue/DescribeSecret/ListSecrets/DeleteSecret` | SM: $0,40/secreto-mes + $0,05/10 000 llamadas ([precios de Secrets Manager](https://aws.amazon.com/secrets-manager/pricing/), consultado 2026-09-30, us-east-1) | CRUD completo de Secrets Manager bajo el prefijo configurado (`rayito/` por defecto); añade `kms:GenerateDataKey`/`kms:Decrypt` sobre la clave si se pasa `kms_key_id=`/`kmsKeyId` (clave gestionada por el cliente); sin ese parámetro, la clave gestionada por AWS no cobra ni exige permiso KMS aparte | No instanciar `SecretStore` / `new SecretStore(...)` (llamadas explícitas únicamente) | `clients/python/src/rayito/_secrets.py` / `clients/typescript/src/secrets.ts` (llega en M13a) |
| [Índice de metadatos (DynamoDB)](#metadata-index) | planificado (M14) | `index=DynamoDbIndex(...)` | `index: new DynamoDbIndex({...})` | `None` / `undefined` | Copia inmutable de `metadata` por sandbox en DynamoDB para poder filtrar `list()`/`paginate()` sobre estados no `RUNNING` (por ejemplo `SUSPENDED`) sin sondear `Health` | `dynamodb:PutItem/BatchGetItem` por sandbox creado/listado | DynamoDB on-demand ([precios de DynamoDB on-demand](https://aws.amazon.com/dynamodb/pricing/on-demand/), consultado 2026-09-30, us-east-1): $1,25 por millón de `PutItem` (WRU) + $0,25 por millón de `BatchGetItem` (RRU, ítems ≤ 4 KB) + $0,25/GB-mes almacenado. Ejemplo: 10 000 sandboxes/mes con un `list()` diario cada uno ≈ 10 000 `PutItem` + ~300 000 `BatchGetItem` ≈ $0,09/mes en llamadas, más el almacenamiento (ítems de metadatos son del orden de KB, no de GB) | `dynamodb:PutItem`, `dynamodb:BatchGetItem` sobre la tabla del índice | No pasar `index=` / `index` (o pasar `None`/`undefined`) | `clients/python/src/rayito/_index.py` / `clients/typescript/src/index-store.ts` (llega en M14) |
| [Trazas OpenTelemetry del SDK](#otel-sdk) | planificado (M13b) | `tracer_provider=` | `tracerProvider` | `None` / `undefined` | Instrumenta `create/connect/kill/pause/resume` (y las demás llamadas del SDK) con spans OTel sobre el `TracerProvider` que ya tengas configurado | Ninguno propio: Rayito no crea ni llama ningún servicio AWS por esto — el coste depende de **tu** exportador (CloudWatch, un collector propio, …) | $0 desde Rayito: el exportador OTel lo paga y lo configura quien lo activa, no este SDK ([precios de CloudWatch](https://aws.amazon.com/cloudwatch/pricing/) sólo si exportas ahí, consultado 2026-09-30) | Ninguno propio de Rayito; el que exija tu exportador OTel | No pasar `tracer_provider=` / `tracerProvider` (o pasar `None`/`undefined`) | `clients/python/src/rayito/_otel.py` / `clients/typescript/src/otel.ts` (llega en M13b) |

Las cuatro filas empiezan en "planificado (Mxx)": cada grupo cambia **su**
fila a "disponible (0.5.0)" y completa su sección de ejemplo cuando la
entrega de verdad, aceptada contra AWS real (`MILESTONES.md`).

## Sin coste AWS

| Función | Opción | Qué activa | Recursos AWS | Coste | Dónde |
|---|---|---|---|---|---|
| [`rayito sandbox proxy`](#local-proxy) | CLI `rayito sandbox proxy <id> --port N` | Sirve un puerto del guest en `localhost`, renovando el JWE de `x-aws-proxy-port` antes de que expire | `lambda:CreateMicrovmAuthToken` (ya se usa hoy en `get_host()`) | $0: `CreateMicrovmAuthToken` es gratuito (cuota de 50 TPS, `AWS_API_NOTES.md` §12); si el sandbox estaba suspendido, despertarlo por auto-resume factura su cómputo normal, no el proxy en sí | CLI (llega en M12) |

Esta fila no pasa por el ciclo "planificado → disponible" de la tabla de
arriba: no consume cuota ni dinero adicional (reutiliza una llamada que el
SDK ya hace), así que no necesita una aceptación de coste independiente.

## Ejemplos

<a id="secrets-injection"></a>

### Inyección de secretos

*Llega en M13a.* Cuando esté disponible, un ejemplo Python y otro TypeScript
de `secrets=`/`secrets` con `secret_cache=SecretCache(ttl_seconds=300)` /
`secretCache: new SecretCache({ ttlSeconds: 300 })` sustituirá este párrafo.

**Fase 1: el valor es visible para el código del sandbox** (ADR-014, punto
6). La inyección entrega el secreto como variable de entorno del proceso que
lo pide; no hay, todavía, un gateway que medie su uso sin exponer el valor a
uid 1000 dentro del sandbox. Para código no confiable, usa credenciales de
corta vida y mínimo privilegio, nunca credenciales de larga vida.

<a id="secrets-crud"></a>

### Secret CRUD (Secrets Manager)

*Llega en M13a.* Cuando esté disponible, un ejemplo Python y otro TypeScript
de `SecretStore`/`new SecretStore(...)` (crear, leer, listar, borrar)
sustituirá este párrafo.

<a id="metadata-index"></a>

### Índice de metadatos (DynamoDB)

*Llega en M14.* Cuando esté disponible, un ejemplo Python y otro TypeScript
de `index=DynamoDbIndex(...)` con `list(metadata=, states=[SUSPENDED])`
sustituirá este párrafo.

<a id="otel-sdk"></a>

### Trazas OpenTelemetry del SDK

*Llega en M13b.* Cuando esté disponible, un ejemplo Python y otro TypeScript
de `tracer_provider=`/`tracerProvider` con un exportador en memoria
sustituirá este párrafo.

<a id="local-proxy"></a>

### `rayito sandbox proxy`

```bash
rayito sandbox proxy sbx-abc123 --port 8000
# sirve http://localhost:8000 -> puerto 8000 del guest,
# renovando el JWE antes de que expire; Ctrl+C para cortar.
```

No hace falta ninguna opción del SDK: es sólo CLI, sin coste AWS propio.
