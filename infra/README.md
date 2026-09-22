# infra — plantillas de infraestructura de Rayito

Plantillas CloudFormation que un operador despliega en su propia cuenta. El
IAM mínimo del SDK sigue en `spike/m0/iam.yaml` (build role, execution role,
`CallerPolicy`); esta carpeta añade lo que M6 necesita y que el spike no cubría.

| Plantilla | Qué crea | Cuándo |
|---|---|---|
| `egress-connector.yaml` | `AWS::Lambda::NetworkConnector` de egress por VPC + security group allowlist + rol operador | Cuando un sandbox no debe salir a Internet libremente (SECURITY.md T8) |
| `ci-oidc-role.yaml` | Proveedor OIDC de GitHub (opcional) + rol que asume `.github/workflows/e2e.yml` con sólo las acciones de MicroVM sobre las imágenes de test | Para correr la aceptación e2e desde GitHub Actions sin credenciales de larga duración (SECURITY.md T10, m7-supply-chain) |

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
`spike/m0/iam.yaml` basta pasar el ARN en el parámetro `NetworkConnectorArns`
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
(`validate-template` + `cfn-lint` sobre las dos plantillas de esta carpeta).

## Persistencia en S3 (`spike/m0/iam.yaml`, M7)

`Sandbox.create(persist=S3Prefix(bucket, prefix, name))` hace que `rayd`, como
root y con el execution role, suba y baje el `HOME` del usuario a
`s3://<bucket>/<prefix>/<name>/` (ADR-009). La plantilla del IAM acepta dos
parámetros nuevos; con `PersistenceBucket` vacío (el valor por defecto) el
execution role no recibe ningún permiso de S3:

```bash
aws cloudformation deploy --stack-name rayito-m0-iam \
  --template-file spike/m0/iam.yaml --capabilities CAPABILITY_NAMED_IAM \
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

