# API JSON del panel de gestión

La API `/api/v1` que sirve [`rayito dashboard`](../cli.md#rayito-dashboard) y
que consume su página. Es la misma para todas las cáscaras del panel: el
servidor local de hoy y, en la fase siguiente del cambio OpenSpec
`dashboard`, la pila en tu cuenta. Todas devuelven documentos con la misma
forma, que se validan contra el esquema JSON versionado en el repositorio
([`testdata/dashboard/api-v1.schema.json`](https://github.com/alejandro-cedeno-10/rayito/blob/main/testdata/dashboard/api-v1.schema.json)),
y los mismos tests de contrato corren contra cada cáscara.

!!! note "No es una API pública de servicio"
    No hay servidor de Rayito: la API la sirve un proceso tuyo, en
    `127.0.0.1`, mientras el panel corre. Existe para la página y para
    probarla; escribe tus scripts con los servicios del SDK
    ([`Quotas`, `UsageCost` y `Fleet`](../funciones-opcionales/panel-de-gestion.md)).

## Acceso (cáscara local)

Todas las rutas exigen la cookie de sesión que el servidor entrega al canjear
el token de arranque ([Seguridad](../security.md#panel-de-gestion-local)); las
peticiones `POST` exigen además `Content-Type: application/json` y la
cabecera `X-Rayito-CSRF` con el token de `GET /api/v1/session`, una ruta que
sólo tiene la cáscara local. El `GET /api/v1/cost` con `billed=true` también
exige esa cabecera, porque cada consulta se factura. Una petición con
`Sec-Fetch-Site` distinto de `same-origin` o `none` recibe `403`.

## Versionado

La versión va en la ruta (`/api/v1`). Dentro de `v1` sólo se añaden campos y
rutas; quitar o cambiar uno exige `/api/v2`. Las claves van en `camelCase`,
las fechas en ISO 8601 UTC y los importes en USD.

## Lectura

| Ruta | Parámetros | Respuesta |
|---|---|---|
| `GET /api/v1/meta` | | versión de la API y de Rayito, región, `allowActions`, `costExplorer` (`enabled`, `requestPriceUsd` y `cacheTtlSeconds`), `eventsConfigured`, `logsAvailable` y los intervalos y topes que usa la página |
| `GET /api/v1/sandboxes` | `state` (repetible), `image` | `sandboxes[]` con `sandboxId`, `state`, `image`, `imageArn`, `imageVersion`, `startedAt`, `memoryMib` |
| `GET /api/v1/images` | `filter` | `images[]` |
| `GET /api/v1/images/versions` | `image` (obligatorio) | `versions[]`, la más nueva primero |
| `GET /api/v1/quotas` | | `entries[]` (cuotas con su `increaseUrl`), `memory` (asignada, cuota, fracción y `level`), `tps[]`, `warnings[]` y `thresholds` |
| `GET /api/v1/cost` | `billed=true`, `start`, `end`, `region`, `refresh` | `estimated` siempre y `billed` sólo con `billed=true` |
| `GET /api/v1/events` | `sandboxId`, `type` (repetible), `limit`, `order` | `configured` y `events[]` |
| `GET /api/v1/logs` | `sandboxId` (obligatorio), `limit` | las últimas `limit` líneas en orden cronológico, `lines[]` (`timestamp`, `message`, `stream`), `truncated` si hay anteriores y `fallback` si el stream esperado no existía y las líneas vienen de streams hallados por nombre (pueden no ser del sandbox) |

El coste va en **dos** documentos que nunca se mezclan: `estimated` (con
`label: "estimated"`, el cálculo del panel) y `billed` (con
`label: "billed"`, de Cost Explorer, con `partial` si AWS aún estima algún
periodo). `billed=true` sin `--cost-explorer` es `403 cost_explorer_disabled`
y no hace ninguna consulta de pago.

```json
{
  "sandboxes": [
    {
      "sandboxId": "microvm-00000000-0000-0000-0000-000000000001",
      "state": "RUNNING",
      "image": "rayito-base-2gb",
      "imageArn": "arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base-2gb",
      "imageVersion": "1.0",
      "startedAt": "2026-10-13T12:00:00Z",
      "memoryMib": 2048
    }
  ]
}
```

Ninguna respuesta lleva `envs`, `runHookPayload`, tokens ni JWE: no existen
en el modelo del dominio.

## Acciones

Sólo con `--allow-actions`; sin ella, todas son `403 actions_disabled` y no
tocan AWS. Todas exigen `"confirm": true` en el cuerpo y devuelven
`202` con el trabajo encolado (`job`).

| Ruta | Cuerpo | Qué hace |
|---|---|---|
| `POST /api/v1/sandboxes/{id}/{accion}` | `{"confirm": true}` | `accion` es `kill`, `suspend` o `resume` sobre un sandbox |
| `POST /api/v1/actions` | `{"action": "suspend", "sandboxIds": ["..."], "confirm": true}` | la misma acción sobre varios (hasta `bulkMaxSandboxes` de `/meta`) |
| `GET /api/v1/actions` | | los últimos trabajos, el más reciente primero |
| `GET /api/v1/actions/{jobId}` | | un trabajo: `status` (`queued`, `running`, `finished`), `completed`, `total`, `counts` y `items[]` |
| `GET /api/v1/actions/plan` | `action`, `count` | cuánto tardaría: `plan.tps` y `plan.estimatedSeconds` |

Un único trabajador ejecuta los trabajos de uno en uno, al ritmo de la
cuota de TPS de la operación (la aplicada, si es menor que la documentada).
Cada `items[]` lleva `outcome` (`done`, `unchanged` o `failed`) y, si
falló, el **código** de AWS en `code`; nunca el mensaje.

## Errores

Todo error es `{"error": {"code", "message"}}` y, si viene de AWS,
`awsCode`. El mensaje es propio y nunca repite el de AWS. Los `code` son
una lista cerrada:

| `code` | HTTP | Cuándo |
|---|---|---|
| `invalid_argument`, `invalid_json`, `confirmation_required` | 400 | parámetro o cuerpo inválido; falta `confirm` |
| `unauthorized` | 401 | sin sesión o token de arranque ya gastado |
| `forbidden`, `csrf_failed`, `actions_disabled`, `cost_explorer_disabled`, `aws_access_denied` | 403 | `Origin` ajeno, CSRF, acción o coste sin activar, o AWS rechazó las credenciales |
| `not_found`, `method_not_allowed` | 404, 405 | ruta o trabajo desconocido; método no admitido |
| `payload_too_large`, `unsupported_media_type` | 413, 415 | cuerpo mayor que el tope; no es JSON |
| `misdirected_request` | 421 | `Host` ajeno |
| `aws_throttled`, `jobs_busy` | 429 | AWS limitó la tasa; la cola del panel ya tiene el máximo de acciones sin terminar |
| `aws_error`, `internal_error` | 502, 500 | AWS devolvió otro error; fallo inesperado del panel |

## Ver también

- [Panel de gestión](../funciones-opcionales/panel-de-gestion.md)
- [CLI: `rayito dashboard`](../cli.md#rayito-dashboard)
- [Seguridad](../security.md#panel-de-gestion-local)
