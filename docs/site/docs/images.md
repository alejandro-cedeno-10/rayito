# Imágenes e IAM

Todo sandbox arranca desde una imagen de Lambda MicroVMs publicada **en tu
cuenta** desde `image/Dockerfile`. Rayito 0.3.0 (M9) necesita una imagen
publicada desde este árbol (con el `rayd` de M9) y, para dos familias de
features, una variante concreta. `rayito doctor` comprueba la versión del
agente de la imagen ([CLI](cli.md#rayito-doctor)).

## Qué imagen necesita cada feature

Las tres variantes salen del mismo `Dockerfile`: `rayito-base-caps` es el
mismo zip que `rayito-base` publicado con `additionalOsCapabilities ALL`, y
`rayito-base-poly` añade una capa con `bash_kernel` y Deno 2.9.7.

| Feature | `rayito-base` | `rayito-base-caps` | `rayito-base-poly` |
|---|---|---|---|
| `commands`, `files`, `pty`, `run_code` en Python, metadatos, pool, `persist=` | sí | sí | sí |
| Plazo del servidor: `max_lifetime`, `on_timeout`, `set_timeout`, `connect(timeout=)` ([Plazo](lifecycle.md)) | sí (M9) | sí (M9) | sí (M9) |
| `upload_url`/`download_url`, ficheros grandes por S3, `gzip`, `metadata` ([Ficheros](files.md)) | sí (M9) | sí (M9) | sí (M9) |
| `get_metrics_history()`, hechos del guest ([Observabilidad](observability.md)) | sí (M9) | sí (M9) | sí (M9) |
| `sbx.git` (`git-core` en la imagen, [Git](git.md)) | sí (M9) | sí (M9) | sí (M9) |
| Shim `rayito.e2b` / `rayito/e2b` ([Compatibilidad](e2b-compat.md)) | sí (M9) | sí (M9) | sí (M9) |
| `run_code(language="bash")` | no: `UNIMPLEMENTED` | no | sí |
| `run_code(language="javascript")` o `"typescript"` ([Kernels](kernels.md)) | no: `UNIMPLEMENTED` | no | sí (M9) |
| Política de egress: `network=`, `allow_internet_access=False`, `update_network()` ([Red saliente](network.md)) | no: el SDK termina el VM y lanza `UnimplementedError` | sí (M9) | no: igual que `rayito-base` |
| IMDS bloqueado para uid 1000–65535 (`get_health().imds_blocked`) | no | sí | no |

"sí (M9)" significa que hace falta una imagen publicada con el `rayd` de M9
(en la release, `agent_version` 0.3.0); contra una anterior, cada feature falla cerrado con
su error (`LifecycleUnsupportedException`, `UnimplementedError("actualiza la
imagen")`, `InvalidArgumentException` con `grpc_code` `UNIMPLEMENTED`...).
No hay una variante que junte `-caps` y `-poly`: ningún objetivo de `make` la
publica y la combinación no está medida.

## Publicar las tres

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
`CallerPolicy` de `infra/iam.yaml` es la política mínima; M9 sólo añade
permisos para las transferencias:

| Feature de M9 | Permisos nuevos |
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
  guarda credenciales para las transferencias (ADR-010, `SECURITY.md` T16).

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
