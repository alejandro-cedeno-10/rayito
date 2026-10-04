# Imágenes e IAM

Todo sandbox arranca desde una imagen de Lambda MicroVMs publicada **en tu
cuenta** desde `image/Dockerfile`. El SDK necesita una imagen con un
`rayd` de su misma serie (`MAJOR.MINOR`, ver
[Compatibilidad](limits.md#compatibilidad-sdk-rayd-imagen)) y, para dos
familias de funciones, una variante concreta. `rayito doctor` comprueba la versión del
agente de la imagen ([CLI](cli.md#rayito-doctor)).

<a id="que-imagen-necesita-cada-feature"></a>

## Qué imagen necesita cada función

Las tres variantes salen del mismo `Dockerfile`: `rayito-base-caps` es el
mismo zip que `rayito-base` publicado con `additionalOsCapabilities ALL`, y
`rayito-base-poly` añade una capa con `bash_kernel` y Deno 2.9.7.

| Función | `rayito-base` | `rayito-base-caps` | `rayito-base-poly` |
|---|---|---|---|
| `commands`, `files`, `pty`, `run_code` en Python, metadatos, pool, `persist=` | sí | sí | sí |
| Plazo del servidor: `max_lifetime`, `on_timeout`, `set_timeout`, `connect(timeout=)` ([Plazo](lifecycle.md)) | sí (0.3.0) | sí (0.3.0) | sí (0.3.0) |
| `upload_url`/`download_url`, ficheros grandes por S3, `gzip`, `metadata` ([Ficheros](files.md)) | sí (0.3.0) | sí (0.3.0) | sí (0.3.0) |
| `get_metrics_history()`, hechos del guest ([Observabilidad](observability.md)) | sí (0.3.0) | sí (0.3.0) | sí (0.3.0) |
| `sbx.git` (`git-core` en la imagen, [Git](git.md)) | sí (0.3.0) | sí (0.3.0) | sí (0.3.0) |
| Shim `rayito.e2b` / `rayito/e2b` ([Compatibilidad](e2b-compat.md)) | sí (0.3.0) | sí (0.3.0) | sí (0.3.0) |
| `run_code(language="bash")` | no: `UNIMPLEMENTED` | no | sí |
| `run_code(language="javascript")` o `"typescript"` ([Kernels](kernels.md)) | no: `UNIMPLEMENTED` | no | sí (0.3.0) |
| Política de egress: `network=`, `allow_internet_access=False`, `update_network()` ([Red saliente](network.md)) | no: el SDK termina el VM y lanza `UnimplementedError` | sí (0.3.0) | no: igual que `rayito-base` |
| IMDS bloqueado para uid 1000–65535 (`get_health().imds_blocked`) | no | sí | no |
| `mounts=` ([Montajes S3](funciones-opcionales/montajes-s3.md)) | no: `UnimplementedError` | sí (0.6.0), con el bucket en `RAYITO_ALLOWED_MOUNT_BUCKETS` | no: igual que `rayito-base` |
| `events=` ([Eventos y webhooks](funciones-opcionales/eventos-y-webhooks.md)), `gateways=` ([Pasarela de secretos](funciones-opcionales/pasarela-de-secretos.md)) | sí (0.6.0) | sí (0.6.0) | sí (0.6.0) |
| `telemetry=` ([Exportación OTLP](funciones-opcionales/exportacion-otlp.md)) | sí (0.6.0) con `OtlpAuth.bearer(...)` | sí (0.6.0), también con `OtlpAuth.execution_role()` | sí (0.6.0) con `OtlpAuth.bearer(...)` |
| `Template.set_start_cmd()` ([Templates](funciones-opcionales/templates.md)) como imagen base | sí (0.6.0) | sí (0.6.0) | sí (0.6.0) |
| Recogida de zombis huérfanos ([Novedades de 0.6.1](novedades/0.6.1.md#rayd-recoge-los-procesos-zombi)) | sí (0.6.1) | sí (0.6.1) | sí (0.6.1) |

`size=` ([Tamaños](funciones-opcionales/tamanos.md)) no es una función de
la imagen sino una imagen más por tamaño: `rayito image publish --sizes`
publica `rayito-base-4gb` (o `rayito-base-caps-4gb`) desde el mismo
artefacto que la variante.

"sí (0.3.0)" significa que hace falta una imagen con `rayd` 0.3.0 o
posterior (`agent_version`); contra una anterior, cada feature falla cerrado con
`UnimplementedError` (`LifecycleUnsupportedException` para el plazo del
servidor, subclase suya desde 0.4.0; el resto ya lo era).
No hay una variante que junte `-caps` y `-poly`: ningún objetivo de `make` la
publica y la combinación no está medida.

## Publicar las tres

Sin compilar nada, `rayito-base` y `rayito-base-caps` se publican desde el
`rayito-image.zip` firmado de cada release
([Configurar AWS](primeros-pasos/configurar-aws.md#4-publicar-la-imagen-rayito-base)).
Desde el código fuente (Linux o WSL2, para compilar `rayd`):

```bash
export AWS_PROFILE=<tu-perfil> AWS_REGION=us-east-1
make image-publish      BUCKET=amzn-s3-demo-bucket    # rayito-base
make image-publish-caps BUCKET=amzn-s3-demo-bucket    # rayito-base-caps (additionalOsCapabilities ALL)
make image-publish-poly BUCKET=amzn-s3-demo-bucket    # rayito-base-poly (bash, JavaScript, TypeScript)
```

O con la CLI (`rayito image publish`, [CLI](cli.md#image-publish)); el
bucket es el de los artefactos de imagen (prefijo `rayito/`), no el de
transferencias. Cada versión nueva cuesta ≈ $0,04/semana de storage
([Costes](cost.md)).

El tamaño (CPU/RAM) es también una propiedad de la imagen, con
`--memory-mib` (`resources[0].minimumMemoryInMiB`, por defecto 2048): para
tener sandboxes de varios tamaños publica una imagen por tamaño con un
nombre que lo diga (`--image-name myimg-4gb --memory-mib 4096`) y elige la
imagen al crear el sandbox. Detalle, la tabla de tamaños (de la
documentación de AWS, no medida salvo donde se dice) y el `$/h` por tamaño
en [Límites](limits.md#tamano-cpuram).

```python
from rayito import Sandbox

base = Sandbox.create("rayito-base")                          # o RAYITO_TEMPLATE
caps = Sandbox.create("rayito-base-caps", allow_internet_access=False)
poly = Sandbox.create("rayito-base-poly")
print(poly.run_code("1 + 1", language="typescript").text)
for sbx in (base, caps, poly):
    sbx.kill()
```

## IAM del llamante

El SDK corre con **tus** credenciales (la cadena de boto3 / AWS SDK v3). La
`CallerPolicy` de `infra/iam.yaml` es la política mínima. La tabla completa,
función por función y con las políticas opcionales de secretos e índice, está
en [IAM](operacion/iam.md). Lo que añade cada función de esta página:

| Función | Permisos nuevos |
|---|---|
| Plazo del servidor (`kill` y `pause`) | ninguno: en `kill`, `rayd` sale y la VM termina sola; en `pause`, el SDK usa `suspend-microvm`, que ya estaba |
| Formas de clase (`Sandbox.set_timeout(id)`, `get_metrics_history(id)`, `update_network(id)`) | ninguno: `get-microvm` y `create-microvm-auth-token`, que ya estaban, más el access token del sandbox |
| Política de egress | ninguno en el llamante; la imagen `rayito-base-caps` se publica con `--os-capabilities ALL` |
| Historial de métricas, listado, git, kernels JS/TS | ninguno |
| Transferencias (`upload_url`, `download_url`, ficheros grandes) | `s3:PutObject`, `s3:GetObject`, `s3:DeleteObject`, `s3:AbortMultipartUpload` sobre `<bucket>/<prefijo>/*` y `s3:ListBucket` acotado al prefijo |

Con la plantilla, dos parámetros las añaden (vacío = sin permisos de
transferencia):

```bash
aws cloudformation deploy --stack-name rayito-m0-iam \
  --template-file infra/iam.yaml --capabilities CAPABILITY_NAMED_IAM \
  --profile <tu-perfil> \
  --parameter-overrides ArtifactBucket=amzn-s3-demo-bucket LogGroupPrefix=/rayito \
      TransferBucket=amzn-s3-demo-bucket TransferPrefix=rayito-transfer
```

Sin la plantilla, el equivalente para añadir a tu política:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "TransferObjects",
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject", "s3:AbortMultipartUpload"],
      "Resource": "arn:aws:s3:::amzn-s3-demo-bucket/rayito-transfer/*"
    },
    {
      "Sid": "TransferMissingKeyIs404",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::amzn-s3-demo-bucket",
      "Condition": { "StringLike": { "s3:prefix": "rayito-transfer/*" } }
    }
  ]
}
```

- `s3:PutObject` cubre también `CreateMultipartUpload`, `UploadPart` y
  `CompleteMultipartUpload` (las exportaciones desde 5 GiB).
- Sin `s3:ListBucket`, una clave que falta da 403 en vez de 404 y la
  importación la trata como pendiente hasta caducar.
- Con SSE-KMS, añade `kms:GenerateDataKey` (subidas) y `kms:Decrypt`
  (descargas) sobre la clave; la plantilla no lo incluye.
- El execution role del sandbox **no** necesita nada: `rayd` nunca firma ni
  guarda credenciales para las transferencias (ver [Seguridad](security.md)).

## El bucket de transferencias

Receta completa en `infra/README.md`, "Transferencias de ficheros". Lo
imprescindible:

- **Misma región** que los sandboxes (o `S3Staging(region=)`), nombre sin
  puntos: el SDK firma con el host virtual regional
  (`amzn-s3-demo-bucket.s3.us-east-1.amazonaws.com`) y `rayd` rechaza
  cualquier otra forma.
- **Prefijo propio** (`rayito-transfer` por defecto): nunca `rayito` (los
  artefactos de imagen) ni el de persistencia.
- **Ciclo de vida de 1 día** sobre el prefijo, con
  `AbortIncompleteMultipartUpload`: los objetos son de un solo uso.
- **Política del bucket** que rechace lo que no sea SigV4
  (`s3:signatureversion`) y lo que no vaya por TLS (`aws:SecureTransport`).
- **CORS** sólo si un navegador sube o baja directamente, con orígenes
  explícitos.
- **Red**: con `INTERNET_EGRESS` no hay nada que hacer; con un conector VPC
  propio (`infra/egress-connector.yaml`) el VM necesita alcanzar S3 (gateway
  endpoint o NAT). La política de egress del guest no afecta a `rayd` (root),
  que es quien mueve los bytes.

Configúralo en el SDK con `transfer=S3Staging("amzn-s3-demo-bucket")` (TS
`transfer: { bucket: "amzn-s3-demo-bucket" }`) o con las variables
`RAYITO_TRANSFER_BUCKET`, `RAYITO_TRANSFER_PREFIX` y `RAYITO_TRANSFER_REGION`
(la única vía en el shim de E2B).
