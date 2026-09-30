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
| [Inyección de secretos](#secrets-injection) | planificado (M13a) | `secrets=` | `secrets` | `None` / `undefined` | Entrega el valor de uno o más secretos como variable de entorno de un comando, PTY, ejecución de código o plaza del pool | `secretsmanager:GetSecretValue` (con caché, nunca una vez por llamada) | SM: $0,40/secreto-mes + $0,05/10 000 llamadas ([precios de Secrets Manager](https://aws.amazon.com/secrets-manager/pricing/), consultado 2026-09-30, us-east-1) | `secretsmanager:GetSecretValue` sobre los ARN concretos | No pasar `secrets=` / `secrets` (o pasar `None`/`undefined`) | `clients/python/src/rayito/_secrets.py` / `clients/typescript/src/secrets.ts` (llega en M13a) |
| [Secret CRUD (Secrets Manager)](#secrets-crud) | planificado (M13a) | `SecretStore(...)` | `new SecretStore({...})` | sin instanciar = sin cliente boto3/SDK | Crear, actualizar, listar y borrar secretos nativos de Rayito (y el shim `Secret`/`AsyncSecret` de E2B) | `secretsmanager:CreateSecret/PutSecretValue/GetSecretValue/DescribeSecret/ListSecrets/DeleteSecret` | SM: $0,40/secreto-mes + $0,05/10 000 llamadas ([precios de Secrets Manager](https://aws.amazon.com/secrets-manager/pricing/), consultado 2026-09-30, us-east-1) | CRUD completo de Secrets Manager bajo el prefijo configurado (`rayito/` por defecto) | No instanciar `SecretStore`/`SecretStore` (llamadas explícitas únicamente) | `clients/python/src/rayito/_secrets.py` / `clients/typescript/src/secrets.ts` (llega en M13a) |
| [Índice de metadatos (DynamoDB)](#metadata-index) | planificado (M14) | `index=DynamoDbIndex(...)` | `index: new DynamoDbIndex({...})` | `None` / `undefined` | Copia inmutable de `metadata` por sandbox en DynamoDB para poder filtrar `list()`/`paginate()` sobre estados no `RUNNING` (por ejemplo `SUSPENDED`) sin sondear `Health` | `dynamodb:PutItem/BatchGetItem` por sandbox creado/listado | DynamoDB on-demand: escrituras, lecturas y almacenamiento por GB-mes ([precios de DynamoDB on-demand](https://aws.amazon.com/dynamodb/pricing/on-demand/), consultado 2026-09-30, us-east-1); a menos de $0,10/mes en el volumen medido en `MILESTONES.md` M14 | `dynamodb:PutItem`, `dynamodb:BatchGetItem` sobre la tabla del índice | No pasar `index=` / `index` (o pasar `None`/`undefined`) | `clients/python/src/rayito/_index.py` / `clients/typescript/src/index-store.ts` (llega en M14) |
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
