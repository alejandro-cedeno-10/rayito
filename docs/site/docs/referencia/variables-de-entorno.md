# Variables de entorno

Rayito se configura con argumentos explícitos; estas variables son atajos
para no repetirlos. Ninguna enciende una de las
[funciones opcionales](../optional-features.md) (secretos, índice de
metadatos, OpenTelemetry): esas sólo se activan con una opción del SDK.

!!! warning "La excepción: `RAYITO_TRANSFER_BUCKET`"
    Con esta variable exportada, `files.write` / `files.read` de 8 MiB o
    más y las URLs firmadas pasan por S3, que cobra sus peticiones y el
    almacenamiento temporal del prefijo de transferencias
    ([Ficheros y S3](../files.md#coste-de-s3), [Costes](../cost.md)).

## SDK (Python y TypeScript)

| Variable | Qué hace | Equivale a |
|---|---|---|
| `RAYITO_TEMPLATE` | nombre o ARN de la imagen por defecto de `create()` | `template=` / `template` |
| `RAYITO_ACCESS_TOKEN` | access token para `connect()` y las formas de clase (`set_timeout`, `get_metrics_history`, `update_network`) y para la CLI | `access_token=` / `accessToken` |
| `RAYITO_TRANSFER_BUCKET` | bucket de transferencias (URLs y ficheros grandes por S3); la única vía en el shim de E2B | `transfer=S3Staging(bucket)` / `transfer: { bucket }` |
| `RAYITO_TRANSFER_PREFIX` | prefijo dentro del bucket (`rayito-transfer` por defecto) | `S3Staging(prefix=)` |
| `RAYITO_TRANSFER_REGION` | región del bucket si no es la del sandbox | `S3Staging(region=)` |

!!! warning "`RAYITO_ACCESS_TOKEN` lo leen también los `create()`"
    Si la exportas, cada `create()` de ese proceso reutiliza el mismo
    secreto, así que una fuga abre todos sus sandboxes, no uno. Por defecto
    `create()` genera 32 bytes aleatorios por sandbox. Úsala para `connect()`
    o la CLI desde otro proceso, no en el proceso que crea sandboxes.

## CLI

| Variable | Qué hace |
|---|---|
| `RAYITO_BUCKET` | bucket de artefactos de imagen para `rayito image publish` y la comprobación `bucket` de `rayito doctor` (equivale a `--bucket`) |
| `RAYITO_ACCESS_TOKEN` | token de `sandbox connect`, `exec` y `metrics` si no pasas `--token-file` (gana el fichero) |

## Servidor MCP

| Variable | Por defecto | Qué hace |
|---|---|---|
| `RAYITO_TEMPLATE` | — | imagen del sandbox (obligatoria en la primera herramienta que lo usa) |
| `RAYITO_TEMPLATE_VERSION` | la última activa | versión de la imagen |
| `RAYITO_EXECUTION_ROLE_ARN` | — | execution role del sandbox; con rol, logs en CloudWatch |
| `RAYITO_MCP_TIMEOUT_SECONDS` | 3600 | vida máxima del sandbox (60–28 800) |
| `RAYITO_MCP_IDLE_SECONDS` | 300 | segundos sin tráfico antes de suspender; `0` lo desactiva |
| `RAYITO_MCP_LOG_LEVEL` | `INFO` | nivel del log (por stderr) |

Detalle: [Servidor MCP](../mcp.md#variables-de-entorno).

## Imagen

| Variable | Dónde | Qué hace |
|---|---|---|
| `RAYITO_ALLOW_ROOT=1` | en el `Dockerfile` de la imagen, nunca en el SDK | permite `user="root"` en comandos y terminales; por defecto todo corre como uid 1000 |
| `RAYITO_ALLOWED_MOUNT_BUCKETS` | en el `Dockerfile`/`rayito image publish --env`, nunca por sandbox | lista de buckets (coma-separados) que `mounts=` puede montar; vacía o ausente deniega todos (nunca permite todos) |

## AWS

Las lee el SDK de AWS que Rayito usa por debajo (`boto3` en Python, AWS SDK
v3 en TypeScript), no Rayito:

| Variable | Qué hace |
|---|---|
| `AWS_PROFILE` | perfil de `~/.aws/config` (incluidos los de SSO) |
| `AWS_REGION`, `AWS_DEFAULT_REGION` | región de los sandboxes |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN` | credenciales explícitas |

## Tests de extremo a extremo

Sólo para quien desarrolla Rayito: `RAYITO_E2E=1` y `RAYITO_TEMPLATE`
activan las suites contra AWS real
([`CONTRIBUTING.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/CONTRIBUTING.md)).
