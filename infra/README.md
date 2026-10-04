# infra — plantillas de infraestructura de Rayito

Plantillas CloudFormation que un operador despliega en su propia cuenta: el
IAM mínimo del SDK (`iam.yaml`) y las piezas opcionales de egress y de CI.

| Plantilla | Qué crea | Cuándo |
|---|---|---|
| `iam.yaml` | Build role, execution role (sólo logs; S3 con `PersistenceBucket`) y la managed policy `CallerPolicy` del publicador (S3 de transferencias con `TransferBucket`) | Siempre, antes de publicar la primera imagen. La pila y los recursos conservan los nombres `rayito-m0-iam` / `rayito-m0-*` con los que nacieron en M0: renombrarlos rompería los despliegues existentes |
| `egress-connector.yaml` | `AWS::Lambda::NetworkConnector` de egress por VPC + security group allowlist + rol operador | Cuando un sandbox no debe salir a Internet libremente (SECURITY.md T8) |
| `secrets-access.yaml` | Opcional (M13a, $0): dos managed policies, `RayitoSecretsReader` y `RayitoSecretsAdmin`, sobre `secret:<SecretPrefix>*` (y KMS sólo con `KmsKeyArn`) | Sólo si usas `secrets=` / `SecretStore` / `Secret`: se adjuntan a las credenciales del llamante del SDK ([Secretos](#secretos-infrasecrets-accessyaml-m13a)) |
| `metadata-index.yaml` | Opcional (M14, on-demand, $0 en reposo): tabla DynamoDB `PAY_PER_REQUEST` con TTL + políticas `RayitoIndexWriter` / `RayitoIndexReader` | Sólo si listas por metadatos con `index=DynamoDbIndex(...)` / `--index-table`, también sobre `SUSPENDED` ([Índice de metadatos](#índice-de-metadatos-inframetadata-indexyaml-m14)) |
| `custom-domain.yaml` | Opcional (M15, experimental, $0 en reposo): distribución CloudFront con alias comodín + CloudFront Function de enrutado + KeyValueStore; sin Lambda ni IAM propio | Sólo si usas `CustomDomain` para exponer sandboxes en tu propio dominio ([Dominio propio](#dominio-propio-infracustom-domainyaml-m15)); `Sandbox.create(domain=)` sigue sin cablear, ver `ARCHITECTURE.md` ADR-024 |
| `ci-oidc-role.yaml` | Proveedor OIDC de GitHub (opcional) + rol que asume `.github/workflows/e2e.yml` con sólo las acciones de MicroVM sobre las imágenes de test | Para correr la aceptación e2e desde GitHub Actions sin credenciales de larga duración (SECURITY.md T10, m7-supply-chain) |
| `events-webhooks.yaml` | Opcional (M15, m15-events-webhooks): secreto HMAC del stack, tabla DynamoDB, tres Lambdas (forwarder/deliverer/reconciliador), suscripción de CloudWatch Logs y regla de EventBridge Scheduler | Sólo si usas `events=LifecycleEvents(...)` / `LifecycleEvents` para eventos de ciclo de vida firmados y webhooks ([Eventos y webhooks](#eventos-y-webhooks-infraevents-webhooksyaml-m15)) |

## Egress allowlist (`egress-connector.yaml`)

Por defecto cada MicroVM sale a Internet por el conector gestionado
`INTERNET_EGRESS` (AWS_API_NOTES.md §2). Un conector VPC propio sustituye esa
salida por ENIs en tus subnets, detrás de un security group cuyas reglas de
egress son la allowlist: **sin `AllowedCidrN` no sale nada** (la plantilla
declara una regla placeholder a `127.0.0.1/32`, el mecanismo documentado por
CloudFormation para retirar la regla implícita "allow all" de EC2).

Propiedades del conector tal cual las documenta la referencia de plantillas
(`AWS::Lambda::NetworkConnector` → `Configuration.VpcEgressConfiguration`):
`AssociatedComputeResourceTypes: [MicroVm]`, `NetworkProtocol: IPv4`,
`SecurityGroupIds`, `SubnetIds`; `Name` y `OperatorRole` en el recurso;
`Arn` y `State` como atributos. No se usa ningún campo fuera de esa referencia.

### Desplegar

```bash
aws cloudformation deploy \
  --stack-name rayito-egress \
  --template-file infra/egress-connector.yaml \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
      VpcId=vpc-0123456789abcdef0 \
      SubnetIds=subnet-aaaa,subnet-bbbb \
      AllowedCidr1=10.0.0.0/16 AllowedPort=443

aws cloudformation describe-stacks --stack-name rayito-egress \
  --query "Stacks[0].Outputs[?OutputKey=='ConnectorArn'].OutputValue" --output text
```

Parámetros: `VpcId`, `SubnetIds` (1–16, misma VPC), `ConnectorName`
(`rayito-egress`), `AllowedPort` (443) y hasta cinco `AllowedCidrN` (vacíos =
nada permitido). Cambiar la allowlist es un `deploy` con otros parámetros: el
security group se actualiza sin reemplazar el conector.

### Usar desde el SDK

```python
from rayito import Sandbox

sbx = Sandbox.create(
    "rayito-base",
    egress=["arn:aws:lambda:us-east-1:123456789012:network-connector:rayito-egress"],
)
print(sbx.get_info().egress)   # ('arn:aws:lambda:...:network-connector:rayito-egress',)
print(sbx.get_info().ingress)  # el ALL_INGRESS gestionado que la plataforma añade
```

`egress=` acepta nombres gestionados (`INTERNET_EGRESS`) o ARNs propios, hasta
10; `SandboxInfo.egress`/`.ingress` devuelven lo que `get-microvm` reporta.

### IAM del caller

Quien llama a `run-microvm` necesita `lambda:PassNetworkConnector` sobre el
ARN del conector (además del que ya tiene sobre los gestionados). La salida
`CallerPolicyStatement` de la pila imprime la sentencia exacta; con
`infra/iam.yaml` basta pasar el ARN en el parámetro `NetworkConnectorArns`
(lista, por defecto vacía) al desplegar o actualizar `rayito-m0-iam`.

El rol operador (`OperatorRole`) lo asume `lambda.amazonaws.com` para crear,
describir y borrar las ENIs del conector: `ec2:CreateNetworkInterface`,
`ec2:DescribeNetworkInterfaces`, `ec2:DeleteNetworkInterface`,
`ec2:DescribeSubnets`, `ec2:DescribeSecurityGroups`, `ec2:DescribeVpcs`,
`ec2:AssignPrivateIpAddresses`, `ec2:UnassignPrivateIpAddresses`. Su trust
policy **no** lleva `aws:SourceAccount` (el rol operador de aws-samples tampoco;
AWS_API_NOTES.md §10).

### NAT

La allowlist se aplica en el security group, antes del enrutado: un destino
no listado se descarta aunque la subnet tenga NAT. Para que un destino
**de Internet** listado sea alcanzable, las subnets del conector necesitan
además un NAT gateway (o una ruta egress-only) que este stack no crea: cuesta
dinero por hora y por GB y es decisión del operador. Destinos dentro de la VPC
(bases de datos, caches, APIs internas) funcionan sin NAT.

### Validación y puerta de despliegue en la aceptación

`make infra-lint` ejecuta `aws cloudformation validate-template` (lado
servidor, gratis) y `cfn-lint` (1.56.3 en la aceptación de M6; esa versión ya
conoce `AWS::Lambda::NetworkConnector`, por lo que no hace falta ignorar
E3006; con una versión anterior, añadir `--ignore-checks E3006` sólo para ese
tipo).

La aceptación de M6 despliega la pila (`rayito-egress-e2e`, allowlist vacía)
y ejecuta `test_egress_allowlist` **sólo** si la cuenta tiene, en la región,
una VPC **propia de este proyecto o prestada por su dueño** con al menos una
subnet (la pila crea un security group y las ENIs del conector dentro de esa
VPC, y eso no se hace en la red de otra carga de trabajo sin pedirlo) y la
página de precios de Lambda MicroVMs no lista cargo por conector. La
allowlist vacía **no necesita NAT** (el security group descarta antes del
enrutado), así que la falta de NAT nunca es motivo para saltarse la medida.
Si la puerta no se cumple, el test se salta (falta
`RAYITO_EGRESS_CONNECTOR_ARN`) y la evidencia es esta validación. Resultado
2026-09-16: **no desplegado** — la única VPC de la cuenta
(12 subnets) pertenece a otra carga de trabajo. Cuando haya una VPC propia: `aws cloudformation deploy
--stack-name rayito-egress-e2e --template-file infra/egress-connector.yaml
--capabilities CAPABILITY_IAM --parameter-overrides VpcId=… SubnetIds=…`,
exportar `RAYITO_EGRESS_CONNECTOR_ARN` con el output `ConnectorArn`, correr
`test_egress_allowlist` y borrar la pila. Detalle en AWS_API_NOTES.md §16
Q46.

## e2e desde GitHub Actions (`ci-oidc-role.yaml`)

`.github/workflows/e2e.yml` no guarda ninguna credencial: asume por OIDC el
rol de esta plantilla con `aws-actions/configure-aws-credentials`. El trust
acepta un único `sub` exacto (`repo:<GitHubRepository>:environment:<GitHubEnvironment>`,
por defecto `repo:alejandro-cedeno-10/rayito:environment:e2e`) y `aud` = `sts.amazonaws.com`:
otro environment u otro repositorio no pueden asumirlo. La rama no entra: el
`sub` de un job que declara un environment no lleva componente de rama, así
que cualquier workflow de este repositorio —en cualquier rama— que declare
`environment: e2e` presenta el `sub` aceptado. La rama se cierra fuera de
IAM: al crear el environment `e2e`, fijar sus deployment branches a `main`
(Settings → Environments → Deployment branches and tags → selected
branches). Si algún día hace falta cerrarlo también en IAM, el camino es un
parámetro `GitHubRef` y una segunda condición `StringLike` sobre
`repo:<repo>:ref:refs/heads/main`. La política inline concede sólo
`lambda:RunMicrovm`, `GetMicrovm`,
`SuspendMicrovm`, `ResumeMicrovm`, `TerminateMicrovm` y
`CreateMicrovmAuthToken` sobre `TestImageArns`, `lambda:ListMicrovms` sobre
`*`, `lambda:PassNetworkConnector` sobre los conectores gestionados
(`AWS_API_NOTES.md` §10) y, sólo si se pasa `ExecutionRoleArn`,
`iam:PassRole` sobre ese rol. Nada de imágenes, S3, cuotas ni etiquetas: el
workflow nunca publica una imagen.

### Desplegar

```bash
aws cloudformation deploy   --stack-name rayito-ci-oidc   --template-file infra/ci-oidc-role.yaml   --capabilities CAPABILITY_NAMED_IAM   --parameter-overrides       TestImageArns=arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base       GitHubRepository=alejandro-cedeno-10/rayito GitHubEnvironment=e2e

aws cloudformation describe-stacks --stack-name rayito-ci-oidc   --query "Stacks[0].Outputs[?OutputKey=='RoleArn'].OutputValue" --output text
```

Parámetros: `GitHubRepository` (`alejandro-cedeno-10/rayito`), `GitHubEnvironment`
(`e2e`), `TestImageArns` (lista: `rayito-base` y, si se quiere, la variante
`rayito-base-caps`), `CreateOidcProvider` (`true`; `false` reutiliza el
proveedor `token.actions.githubusercontent.com` que ya exista en la cuenta,
sólo puede haber uno), `ExecutionRoleArn` (vacío: sin `iam:PassRole`; el
workflow no exporta `RAYITO_EXECUTION_ROLE_ARN`) y `RoleName`
(`rayito-e2e-github`). La plantilla se valida con `make infra-lint`
(`validate-template` + `cfn-lint` sobre las plantillas de esta carpeta).

## Persistencia en S3 (`infra/iam.yaml`, M7)

`Sandbox.create(persist=S3Prefix(bucket, prefix, name))` hace que `rayd`, como
root y con el execution role, suba y baje el `HOME` del usuario a
`s3://<bucket>/<prefix>/<name>/` (ADR-009). La plantilla del IAM acepta dos
parámetros nuevos; con `PersistenceBucket` vacío (el valor por defecto) el
execution role no recibe ningún permiso de S3:

```bash
aws cloudformation deploy --stack-name rayito-m0-iam \
  --template-file infra/iam.yaml --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides ArtifactBucket=<bucket-de-artefactos> LogGroupPrefix=/rayito \
      PersistenceBucket=<bucket-de-persistencia> PersistencePrefix=rayito-home
```

**Dos espacios de nombres, nunca uno.** Los artefactos de imagen viven bajo
`rayito/` del `ArtifactBucket` (`rayito/images/*`, los zips que
`create/update-microvm-image` descarga con el `BuildRole`). El execution
role lo lee el código del sandbox por IMDS en la imagen por defecto (T1),
así que si `PersistenceBucket` y `PersistencePrefix` solapan ese espacio,
ese código sobrescribe el zip desde el que se construye la siguiente
imagen. Usa buckets distintos o, como mínimo, un prefijo cuyo primer
segmento no sea `rayito` (el `*` de IAM atraviesa `/`). La plantilla añade
además un `Deny` explícito sobre `arn:aws:s3:::<ArtifactBucket>/rayito/*`:
si alguien despliega los dos parámetros sobre el mismo espacio, la
persistencia falla cerrada con `AccessDenied` en vez de alcanzar los
artefactos.

Con el bucket definido, el execution role gana exactamente `s3:PutObject`,
`s3:GetObject` y `s3:AbortMultipartUpload` sobre `arn:aws:s3:::<bucket>/<prefix>/*`
y `s3:ListBucket` sobre el bucket con la condición `s3:prefix = <prefix>/*`
(para que una clave ausente responda 404 y no 403). Sin `s3:DeleteObject`: el
rol nunca borra; los objetos de test los borra el desarrollador con sus
propias credenciales. `PersistencePrefix` sigue el patrón
`^[A-Za-z0-9_.-][A-Za-z0-9_./-]*[A-Za-z0-9_.-]$|^[A-Za-z0-9_.-]$` (letras,
dígitos, `_`, `.`, `-` y `/` interior; sin `*`, sin comillas y sin `/`
inicial ni final) y debe coincidir con el `prefix` de `S3Prefix`.

Notas de operación:

- El bucket en la **misma región** que la imagen (o `S3Prefix(region=)`).
- **Cifrado**: el SSE-S3 por defecto del bucket basta y `rayd` no envía
  parámetros de cifrado. Con SSE-KMS hay que añadir al execution role
  `kms:Decrypt` y `kms:GenerateDataKey` sobre la clave: no viene en la
  plantilla.
- **Ciclo de vida**: añade una regla `AbortIncompleteMultipartUpload` a 1
  día sobre el prefijo; un MicroVM matado a mitad de checkpoint puede dejar
  partes huérfanas que S3 factura.
- **Red**: con un conector de egress propio (`egress-connector.yaml`) S3
  necesita un gateway endpoint de S3 en la VPC o un NAT; con el conector
  gestionado `INTERNET_EGRESS` no hay que hacer nada.
- La plantilla se valida con `make infra-lint` (`validate-template` +
  `cfn-lint` sobre las tres plantillas). Estado 2026-09-16: stack
  `rayito-m0-iam` actualizado con `PersistenceBucket` =
  el bucket de artefactos de la cuenta y `PersistencePrefix` =
  `rayito-e2e`; `simulate-principal-policy` confirma `allowed` para
  `s3:PutObject`/`GetObject`/`AbortMultipartUpload` sobre
  `rayito-e2e/x/home.tar.gz` e `implicitDeny` sobre `rayito/x`.

### Después de desplegar, en GitHub

1. Environment `e2e` (Settings → Environments) con **required reviewers** y
   **deployment branches limitadas a `main`** (Deployment branches and tags →
   selected branches): ninguna ejecución toca la cuenta sin una aprobación, y
   el trust IAM no cierra la rama por sí solo (el `sub` no lleva componente de
   rama).
2. Variables del repositorio (Settings → Variables → Repository):
   `RAYITO_E2E_ROLE_ARN` (output `RoleArn`), `RAYITO_E2E_TEMPLATE_ARN` (el
   ARN de `rayito-base`, **siempre un ARN**: la política compara ARNs),
   `RAYITO_E2E_REGION` (opcional, `us-east-1` por defecto).
3. AWS Budget de $10/mes (Billing → Budgets) filtrado por servicio `AWS
   Lambda` con alerta por correo al 80 %: el nightly cuesta ≈ $0,03 por
   ejecución (≈ $1/mes), así que un exceso señala sandboxes huérfanos.

Estado 2026-09-16: plantilla validada (`cfn-lint` 1.56.3 y
`validate-template` limpios), **no desplegada**: el despliegue, el
environment, las variables y el presupuesto son los pasos manuales del
Migration Plan de `m7-supply-chain`.


## Transferencias de ficheros (`infra/iam.yaml`, M9)

`files.upload_url`/`download_url` y los ficheros grandes de
`files.write`/`files.read` (`Sandbox.create(transfer=S3Staging(bucket,
prefix))`, ADR-010) usan un bucket tuyo. Las URLs las firma el SDK con las
credenciales **del llamante** (tu proceso); el execution role no recibe
nada y `rayd` no guarda ninguna credencial (`SECURITY.md` T16). Los
parámetros `TransferBucket` y `TransferPrefix` de la plantilla añaden a
`CallerPolicy` exactamente lo necesario; con `TransferBucket` vacío (por
defecto) no hay ningún permiso de transferencia:

```bash
aws cloudformation deploy --stack-name rayito-m0-iam \
  --template-file infra/iam.yaml --capabilities CAPABILITY_NAMED_IAM \
  --profile <tu-perfil> \
  --parameter-overrides ArtifactBucket=<bucket-de-artefactos> LogGroupPrefix=/rayito \
      TransferBucket=amzn-s3-demo-bucket TransferPrefix=rayito-transfer
```

- `s3:PutObject`, `s3:GetObject`, `s3:DeleteObject` y
  `s3:AbortMultipartUpload` sobre `arn:aws:s3:::<TransferBucket>/<TransferPrefix>/*`
  (`CreateMultipartUpload`, `UploadPart` y `CompleteMultipartUpload` los
  autoriza `s3:PutObject`) y `s3:ListBucket` con `s3:prefix =
  <TransferPrefix>/*`, para que una clave que falta dé 404 y no 403.
- `TransferPrefix` sigue el patrón de `PersistencePrefix` y **nunca** puede
  ser `rayito` (los artefactos de imagen) ni igual a `PersistencePrefix`: la
  regla `TransferPrefixIsDisjoint` de la plantilla lo rechaza, porque la
  regla de ciclo de vida de 1 día borraría esos objetos. Debe coincidir con
  el `prefix` de `S3Staging` (`rayito-transfer` por defecto).
- El bucket en la **misma región** que los sandboxes (o `S3Staging(region=)`):
  el SDK firma con el host virtual regional y `rayd` rechaza cualquier otro.
  Nombres de bucket sin puntos.

**Ciclo de vida** sobre el prefijo: los objetos de transferencia son de un
solo uso y los multipart a medias se facturan. S3 redondea la expiración a la
siguiente medianoche UTC, así que un objeto vive entre 24 y 48 h:

```json
{
  "Rules": [
    {
      "ID": "rayito-transfer",
      "Filter": { "Prefix": "rayito-transfer/" },
      "Status": "Enabled",
      "Expiration": { "Days": 1 },
      "AbortIncompleteMultipartUpload": { "DaysAfterInitiation": 1 }
    }
  ]
}
```

```bash
aws s3api put-bucket-lifecycle-configuration --bucket amzn-s3-demo-bucket \
  --lifecycle-configuration file://lifecycle.json --profile <tu-perfil>
```

**Política del bucket**: rechaza las firmas que no sean SigV4 (una URL SigV2
podría vivir más y no la acepta `rayd`) y las peticiones sin TLS:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "OnlySigV4",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "s3:*",
      "Resource": "arn:aws:s3:::amzn-s3-demo-bucket/rayito-transfer/*",
      "Condition": { "StringNotEquals": { "s3:signatureversion": "AWS4-HMAC-SHA256" } }
    },
    {
      "Sid": "OnlyTls",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "s3:*",
      "Resource": [
        "arn:aws:s3:::amzn-s3-demo-bucket",
        "arn:aws:s3:::amzn-s3-demo-bucket/*"
      ],
      "Condition": { "Bool": { "aws:SecureTransport": "false" } }
    }
  ]
}
```

**CORS**, sólo si un navegador sube o baja directamente (`PUT` con el cuerpo
o el formulario `POST` de `upload_url(form=True)`), con orígenes explícitos,
nunca `*`:

```json
{
  "CORSRules": [
    {
      "AllowedOrigins": ["https://app.example.com"],
      "AllowedMethods": ["PUT", "POST", "GET"],
      "AllowedHeaders": ["Content-Type"],
      "ExposeHeaders": ["ETag"],
      "MaxAgeSeconds": 3000
    }
  ]
}
```

**Cifrado**: el SSE-S3 por defecto basta. Con SSE-KMS, las credenciales del
llamante necesitan además `kms:GenerateDataKey` (subidas) y `kms:Decrypt`
(descargas) sobre la clave; la plantilla no lo incluye. `rayd` no envía
parámetros de cifrado.

**Red**: con un conector de egress propio (`egress-connector.yaml`) el VM
necesita alcanzar S3 (gateway endpoint de S3 en la VPC o NAT); con
`INTERNET_EGRESS` no hay que hacer nada. Con la política de egress en el
guest de ADR-012, `rayd` (root) no está sujeto a las rutas por uid.

Estado: parámetros y reglas validados con `cfn-lint` 1.56.3 y
`scripts/tests/test_iam_template.py`; el despliegue y la medida contra AWS
real están *pendientes de aceptación en AWS* (`m9-file-transfer` 8.x).

## Secretos (`infra/secrets-access.yaml`, M13a)

Plantilla **opcional**: sólo hace falta si usas los secretos de Rayito
(`SecretStore`, `SecretCache`, `secrets=` o el `Secret` del shim de E2B,
[docs/site/docs/secrets.md](../docs/site/docs/secrets.md)). Nunca se
despliega sola. Crea **dos políticas IAM gestionadas y nada más** —ningún
secreto, ningún rol—, así que su **coste es $0 (IAM)**; los secretos que
crees con el SDK sí cuestan ($0,40/mes cada uno en Secrets Manager hasta que
los borras). Acciones y parámetros: `AWS_API_NOTES.md` §19.

| Salida | Política | Concede |
|---|---|---|
| `ReaderPolicyArn` | `RayitoSecretsReader` | `secretsmanager:GetSecretValue` y `DescribeSecret` sobre `arn:aws:secretsmanager:<región>:<cuenta>:secret:<SecretPrefix>*`; con `KmsKeyArn`, `kms:Decrypt` sobre esa clave sólo vía Secrets Manager (`kms:ViaService`) |
| `AdminPolicyArn` | `RayitoSecretsAdmin` | lo del lector + `CreateSecret`, `PutSecretValue`, `UpdateSecret`, `DeleteSecret` sobre el mismo ARN y `ListSecrets` sobre `*` (la acción no admite otro recurso); con `KmsKeyArn`, `kms:Decrypt` y `kms:GenerateDataKey` vía Secrets Manager |

Se adjuntan a las credenciales del **llamante** del SDK (la máquina o el
servicio que ejecuta `Sandbox.create(secrets=...)` o administra los
secretos), nunca al execution role del MicroVM: `rayd` no lee secretos.

### Desplegar

```bash
aws cloudformation deploy \
  --stack-name rayito-secrets-access \
  --template-file infra/secrets-access.yaml \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides SecretPrefix=rayito/
# con una CMK propia (SecretStore(kms_key_id=...)):
#   --capabilities CAPABILITY_IAM \
#   --parameter-overrides SecretPrefix=rayito/ KmsKeyArn=arn:aws:kms:<región>:<cuenta>:key/<id>

aws cloudformation describe-stacks --stack-name rayito-secrets-access \
  --query "Stacks[0].Outputs" --output table
```

Parámetros: `SecretPrefix` (`rayito/` por defecto; debe coincidir con
`SecretStore(prefix=)`/`secret_prefix=`; nunca vacío, porque un prefijo vacío
concedería todos los secretos de la cuenta y la región) y `KmsKeyArn`
(vacío por defecto: la clave gestionada por AWS `aws/secretsmanager` no
necesita permisos KMS aparte). `CAPABILITY_IAM` es obligatorio porque la plantilla
crea políticas IAM; no hace falta `CAPABILITY_NAMED_IAM`: las políticas no
llevan nombre fijo.

### Borrar

```bash
aws cloudformation delete-stack --stack-name rayito-secrets-access
```

Borrar el stack quita las políticas, **no** los secretos: bórralos antes con
`SecretStore().destroy(nombre)` (o `Secret.destroy`) si ya no los usas.

Estado: validada con `cfn-lint` 1.56.3 y `scripts/tests/test_secrets_template.py`
(sólo `AWS::IAM::ManagedPolicy`, ninguna acción fuera de la lista, ningún
`Resource: "*"` salvo `ListSecrets`, KMS sólo con clave y `kms:ViaService`);
`make infra-lint` la incluye (`validate-template` + `cfn-lint`).

## Índice de metadatos (`infra/metadata-index.yaml`, M14)

Plantilla **opcional**: sólo hace falta si listas sandboxes por metadatos
con el índice (`index=DynamoDbIndex(...)` en Python, `index: new
DynamoDbIndex({...})` en TypeScript, `rayito sandbox list --index-table`;
[docs/site/docs/observability.md](../docs/site/docs/observability.md#listado-por-metadatos-con-indice-opcional)).
Nunca se despliega sola ni el SDK la crea. Crea **una tabla DynamoDB y dos
políticas IAM**, nada más (ni Lambdas, ni streams, ni EventBridge).
Acciones y parámetros: `AWS_API_NOTES.md` §20.

| Recurso / salida | Qué es |
|---|---|
| `TableName` / `TableArn` | `AWS::DynamoDB::Table` `rayito-sandboxes` (parámetro `TableName`): `PAY_PER_REQUEST`, clave `pk` (`S`), TTL en `expires_at`, cifrado por defecto de DynamoDB; `PointInTimeRecovery` y `DeletionProtection` apagados por defecto |
| `WriterPolicyArn` | `RayitoIndexWriter`: `dynamodb:PutItem` sobre el ARN de la tabla (quien crea sandboxes con `index=` o corre un `SandboxPool` con `PoolConfig(index=)`) |
| `ReaderPolicyArn` | `RayitoIndexReader`: `dynamodb:BatchGetItem` sobre el ARN de la tabla (quien lista con `metadata=` e `index=`) |

Se adjuntan a las credenciales del **llamante** del SDK, nunca al execution
role del MicroVM: `rayd` no toca el índice.

**Coste**: $0 en reposo (on-demand y tabla vacía). Con uso, ~1 WRU por
sandbox creado y 0,5 RRU por sandbox candidato al listar ($0,625 por millón
de WRU y $0,125 por millón de RRU en us-east-1, consultado 2026-09-30) más
$0,25/GB-mes tras los primeros 25 GB; el TTL borra las filas vencidas gratis.
10 000 sandboxes al mes quedan por debajo de $0,10.

### Desplegar

```bash
aws cloudformation deploy \
  --stack-name rayito-metadata-index \
  --template-file infra/metadata-index.yaml \
  --capabilities CAPABILITY_IAM
# otro nombre de tabla: --parameter-overrides TableName=mi-indice

aws cloudformation describe-stacks --stack-name rayito-metadata-index \
  --query "Stacks[0].Outputs" --output table
```

`--capabilities CAPABILITY_IAM` es obligatorio (la plantilla crea dos
políticas IAM; sin él CloudFormation responde
`InsufficientCapabilitiesException`); `CAPABILITY_NAMED_IAM` no hace falta,
porque las políticas no llevan nombre fijo.

### Borrar (apagarlo)

```bash
aws cloudformation delete-stack --stack-name rayito-metadata-index
```

Borra la tabla (con `DeletionProtection=true`, desactívalo antes) y las
políticas; deja de facturar al momento. Deja también de pasar `index=` en el
SDK: un listado con índice contra una tabla borrada falla con
`SandboxIndexException` (nunca devuelve una lista vacía en silencio).

Estado: validada con `cfn-lint` 1.56.3 y
`scripts/tests/test_metadata_index_template.py` (el único recurso de datos es
la tabla, `PAY_PER_REQUEST`, TTL en `expires_at`, cada política con su única
acción sobre el ARN de la tabla, ningún `Resource: "*"`); `make infra-lint`
la incluye (`validate-template` + `cfn-lint`).

## Dominio propio (`infra/custom-domain.yaml`, M15, experimental)

Plantilla **opcional**: sólo hace falta si expones sandboxes en tu propio
dominio con `CustomDomain` (Python sync/async, TypeScript;
[Dominio propio](../docs/site/docs/funciones-opcionales/dominio-propio.md)).
Nunca se despliega sola ni el SDK la crea. Crea **una distribución
CloudFront, una CloudFront Function y un KeyValueStore**, nada más (ni
Lambda, ni rol IAM propio — `CAPABILITY_IAM`/`CAPABILITY_NAMED_IAM` no
hacen falta). Acciones y parámetros del plano de datos del KeyValueStore:
`AWS_API_NOTES.md` §29.

| Recurso / salida | Qué es |
|---|---|
| `DistributionId` / `DistributionDomainName` | `AWS::CloudFront::Distribution`: alias `*.<PublicDomain>`, certificado `CertificateArn` (debe estar en `us-east-1`), un origen "placeholder" que la Function reemplaza en cada petición |
| `RouterFunction` | `AWS::CloudFront::Function` (`cloudfront-js-2.0`, asociada en `viewer-request`): lee la ruta del KeyValueStore por hostname y llama a `cf.updateRequestOrigin` |
| `KvsArn` | `AWS::CloudFront::KeyValueStore`: dos claves por ruta (`j:<etiqueta>` el JWE, `m:<etiqueta>` metadatos), que escriben `CustomDomain.register()`/`unregister()`/`refresh()`, nunca la Function |

**Coste**: $0 en reposo (CloudFront sin tráfico no factura; el KeyValueStore
tampoco). Con uso (us-east-1; cifras de lista de CloudFront Functions/
KeyValueStore desde su lanzamiento, por reconfirmar en la etapa de
aceptación AWS): ~$0,085/GB + $0,0075/10 000 peticiones HTTPS de salida
(CloudFront); ~$0,10 por 1 000 000 de invocaciones de la Function (una por
petición); KeyValueStore ~$0,50 por 1 000 000 de lecturas (las de la
Function) y ~$5 por 1 000 000 de llamadas de gestión (`PutKey`/`DeleteKey`
de `register`/`unregister`/`refresh`) — tres líneas, no una cifra
combinada.

### Desplegar

```bash
aws cloudformation deploy \
  --stack-name rayito-custom-domain \
  --template-file infra/custom-domain.yaml \
  --parameter-overrides PublicDomain=sbx.example.com \
    CertificateArn=arn:aws:acm:us-east-1:<cuenta>:certificate/<id>

aws cloudformation describe-stacks --stack-name rayito-custom-domain \
  --query "Stacks[0].Outputs" --output table
```

Sin `--capabilities`: la plantilla no crea ningún recurso IAM. El
certificado ACM debe estar en `us-east-1` (requisito de CloudFront) y
cubrir `*.<PublicDomain>`; tras desplegar, apunta un `CNAME`/`ALIAS` de
`*.<PublicDomain>` al `DistributionDomainName`.

### Borrar (apagarlo)

```bash
aws cloudformation delete-stack --stack-name rayito-custom-domain
```

Borra la distribución (tarda ~15 min en deshabilitarse primero), la
Function y el KeyValueStore; ninguna ruta sobrevive, son efímeras.

Estado: validada con `cfn-lint` 1.56.3 (el único recurso de datos es el
código de la Function, mantenido en sincronía con
`infra/functions/custom_domain_router.js` por
`scripts/tests/test_custom_domain_function_sync.py`); `make infra-lint` la
incluye (`validate-template` + `cfn-lint`). `Sandbox.create(domain=)` sigue
sin cablear a esta pila (`ARCHITECTURE.md` ADR-024); úsala hoy
instanciando `CustomDomain` directamente.

## Eventos y webhooks (`infra/events-webhooks.yaml`, M15)

Plantilla **opcional**: sólo hace falta si usas `events=LifecycleEvents(...)`
(Python) / `events: new LifecycleEvents(...)` (TypeScript) para recibir
eventos de ciclo de vida firmados y entregarlos a tus webhooks
([Eventos de ciclo de vida y webhooks](../docs/site/docs/funciones-opcionales/eventos-y-webhooks.md)).
Nunca se despliega sola ni el SDK la crea. Acciones y parámetros:
`AWS_API_NOTES.md` §25.

| Recurso / salida | Qué es |
|---|---|
| `EventsTableName` | `AWS::DynamoDB::Table` on-demand, streams (`NEW_IMAGE`), GSI `gsi1` para listar por todos los sandboxes |
| `StackKeySecretArn` | `AWS::SecretsManager::Secret`: la clave HMAC del stack; el SDK la lee para derivar `k_sbx` por sandbox, el forwarder la lee para verificar |
| `OperatorPolicyArn` | `EventsOperatorPolicy`, para las credenciales del **llamante**: `dynamodb:PutItem`/`Query`/`DeleteItem` sobre la tabla y su índice `gsi1` (`register_webhook`/`list_webhooks`/`delete_webhook`/`get_events`), `cloudformation:DescribeStacks` sobre esta pila (resolver sus salidas) y `secretsmanager:GetSecretValue` sólo sobre el secreto del stack |
| `ReconcilerFunctionArn` | El Lambda reconciliador (`rate(<ReconcilerIntervalMinutes> minutes)`, 5 por defecto, mínimo 2) |
| `DelivererFailuresQueueUrl` | Cola SQS: los registros del stream que agotan sus reintentos; vacía en condiciones normales |

Tres funciones Lambda (forwarder, deliverer, reconciliador; Python 3.12,
`infra/lambdas/events_webhooks/`), una suscripción de CloudWatch Logs sobre
`LogGroupName` (parámetro), una cola SQS de fallos y una regla de
EventBridge Scheduler. `scripts/gen_stack_assets.py` inyecta
`docs/aws-api/service-2.json` en el zip bajo
`models/lambda-microvms/<apiVersion>/` y el reconciliador construye su
cliente desde una sesión botocore apuntada ahí (la plantilla también fija
`AWS_DATA_PATH`), para poder llamar a `ListMicrovms` (acción IAM
`lambda:ListMicrovms`) desde un runtime de Lambda que no conoce ese
servicio (decisión 8 de la arquitectura M15).

**Coste**: ~$0,40/mes el secreto; DynamoDB, Lambda y SQS son on-demand/por
uso ($0 en reposo); ~$1,25 por millón de eventos escritos (WRU) más
las lecturas de `get_events`; el reconciliador factura una invocación cada
`ReconcilerIntervalMinutes` (5 por defecto, ~$0,0000002 c/u). us-east-1,
consultado 2026-09-30.

### Desplegar

```bash
aws cloudformation deploy \
  --stack-name rayito-events-webhooks \
  --template-file infra/events-webhooks.yaml \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
      LogGroupName=/rayito/rayito-base \
      ArtifactBucket=mi-bucket \
      ArtifactS3Key=<sha256 del zip, lo resuelve OptionalStacks.deploy()>
```

`OptionalStacks.deploy("events-webhooks", artifact_bucket=...)` / `rayito
events deploy` / `rayito stack deploy events-webhooks --artifact-bucket ...
--param LogGroupName=...` hacen esto por ti: suben el zip, calculan
`ArtifactS3Key`, pasan el mismo bucket como `ArtifactBucket` y despliegan. `CAPABILITY_IAM` es obligatorio (la plantilla crea varios roles
IAM).

### Borrar (apagarlo)

```bash
aws cloudformation delete-stack --stack-name rayito-events-webhooks
```

Borra el secreto (force-delete: cualquier webhook ya registrado deja de
poder verificarse), la tabla, las tres Lambdas, la suscripción, la cola y
el scheduler. Conserva los secretos de cada webhook (`rayito/webhooks/...`)
y el log group de la imagen, que esta pila no creó. Deja de pasar `events=`
en el SDK.

Estado: `make infra-lint` la incluye (`validate-template` + `cfn-lint`
1.56.3).
