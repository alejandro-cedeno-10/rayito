# IAM

El SDK corre con **tus** credenciales de AWS (las de quien llama: tu usuario,
el rol de tu servicio, el de tu CI). Esta página dice qué permiso necesita
cada función y qué plantilla de [`infra/`](https://github.com/alejandro-cedeno-10/rayito/tree/main/infra)
lo concede.

## Las plantillas

| Plantilla | Crea | Coste | Cuándo |
|---|---|---|---|
| `infra/iam.yaml` | el rol de build de la imagen, el execution role opcional y tres políticas del llamante: `rayito-m0-launcher-<región>` (`SandboxLauncherPolicy`, sólo lanzar sandboxes), `rayito-m0-publisher-<región>` (`ImagePublisherPolicy`, sólo publicar imágenes) y `rayito-m0-caller-<región>` (`CallerPolicy`, las dos juntas) | $0 | siempre ([Configurar AWS](../primeros-pasos/configurar-aws.md)) |
| `infra/artifact-bucket.yaml` | la pila `rayito-m0-artifacts`: un bucket de S3 para los zips de las imágenes (cifrado, sin acceso público, sólo TLS, los zips caducan a los 7 días) y su política; ningún rol ni política de IAM | $0 por existir; los zips, céntimos | sólo si no hay ningún bucket que reutilizar: la crea `rayito setup` ([Configurar AWS](../primeros-pasos/configurar-aws.md#2-prepara-la-cuenta-con-rayito-setup)). Borrar la pila conserva el bucket |
| `infra/secrets-access.yaml` | las políticas `RayitoSecretsReader` y `RayitoSecretsAdmin` | $0 | sólo si usas [secretos](../secrets.md) |
| `infra/metadata-index.yaml` | la tabla DynamoDB y las políticas `RayitoIndexWriter` y `RayitoIndexReader` | $0 en reposo | sólo si usas el [índice de metadatos](../funciones-opcionales/indice-de-metadatos.md) |
| `infra/egress-connector.yaml` | un conector de red VPC con un security group deny-all | el de tu VPC | sólo si quieres controlar la salida a internet fuera del sandbox ([Red saliente](../network.md#la-alternativa-de-plataforma)) |
| `infra/templates.yaml` | la política `RayitoTemplateBuilder` | $0 | sólo si construyes templates (`Template.build`, `AgentTemplate`) ([Templates](../funciones-opcionales/templates.md)) |
| `infra/s3-mounts.yaml` | la política `RayitoS3MountAccess`, para el execution role | $0 | sólo si usas `mounts=` ([Montajes S3](../funciones-opcionales/montajes-s3.md)) |
| `infra/otlp-export.yaml` | la política `RayitoOtlpExport`, para el execution role | $0 | sólo si usas `telemetry=` con `OtlpAuth.execution_role()` ([Exportación OTLP](../funciones-opcionales/exportacion-otlp.md)) |
| `infra/sizes-guard.yaml` | la política `RayitoRunAllowedSizes` (un Deny) | $0 | sólo si quieres limitar los tamaños que se lanzan ([Tamaños](../funciones-opcionales/tamanos.md)) |
| `infra/events-webhooks.yaml` | la pila de eventos y las políticas `EventsLauncherPolicy`, `EventsReaderPolicy`, `EventsWebhookAdminPolicy` y `EventsOperatorPolicy` | ver [Precios](../cost.md) | sólo si usas `events=` ([Eventos y webhooks](../funciones-opcionales/eventos-y-webhooks.md)) |
| `infra/efs-volumes.yaml` (experimental) | EFS en tu VPC, su conector y la política `RayitoEfsVolumeClient` para el execution role | ver [Precios](../cost.md) | sólo si usas volúmenes EFS ([Volúmenes EFS en tu VPC](../funciones-opcionales/volumenes-efs-vpc.md)) |
| `infra/custom-domain.yaml` (experimental) | CloudFront, su Function y un KeyValueStore; ningún rol ni política | ver [Precios](../cost.md) | sólo si usas `CustomDomain` ([Dominio propio](../funciones-opcionales/dominio-propio.md)) |
| `infra/agentcore-runtime.yaml` | la pila `rayito-agentcore-<nombre>` de un agente: el repositorio de ECR, el proyecto de CodeBuild, su grupo de logs y dos roles (el de build y el que asume el runtime) | ver [Precios](../cost.md#coste-de-un-agente-en-agentcore) | sólo si despliegas con `rayito agent deploy --target agentcore`, que la crea sola; no es un componente de `rayito stack deploy` ([Desplegar en AgentCore](../guias/desplegar-en-agentcore.md)). `RayitoAgent` (CDK) usa la misma plantilla como pila anidada |

Las plantillas opcionales se despliegan con `rayito stack deploy <componente>`
(o `aws cloudformation deploy`), que necesita
`cloudformation:CreateStack/UpdateStack/DescribeStacks/DeleteStack` y los
permisos de lo que crea la plantilla
([Pilas opcionales](../funciones-opcionales/pilas-opcionales.md)).

!!! warning "Un servicio en producción sólo lanza sandboxes"
    Vincula `SandboxLauncherPolicy` (output `SandboxLauncherPolicyArn`) al
    rol de un servicio o de un agente que crea sandboxes, nunca
    `CallerPolicy`: con ella, quien comprometa ese servicio (un RCE o un
    SSRF en el backend del agente) podría publicar una versión con puerta
    trasera de `rayito-base` o de cualquier template, que correría en todos
    los sandboxes futuros de la cuenta. Publicar imágenes es cosa del
    mantenedor o del pipeline de release, con `ImagePublisherPolicy`.

## Permisos por función

| Función | Permisos del llamante | Dónde están |
|---|---|---|
| Crear, conectar, pausar, reanudar, listar y matar sandboxes | `lambda:RunMicrovm`, `GetMicrovm`, `ListMicrovms`, `SuspendMicrovm`, `ResumeMicrovm`, `TerminateMicrovm`, `CreateMicrovmAuthToken`; las lecturas de imagen con las que el SDK resuelve nombres y tamaños (`ListMicrovmImages`, `GetMicrovmImage`, `GetMicrovmImageVersion`, `ListMicrovmImageVersions`, `ListTags` y `ListManagedMicrovmImages`/`ListManagedMicrovmImageVersions`); `lambda:PassNetworkConnector` (conectores gestionados) | `SandboxLauncherPolicy` (o `CallerPolicy`) de `infra/iam.yaml` |
| Publicar y podar imágenes (`rayito image`) | `lambda:CreateMicrovmImage`, `UpdateMicrovmImage`, `UpdateMicrovmImageVersion`, `DeleteMicrovmImageVersion`, `GetMicrovmImage*`, `ListMicrovmImage*`, `GetMicrovmImageBuild`, `ListMicrovmImageBuilds`, `TagResource`; `iam:PassRole` del rol de build; `s3:PutObject`/`GetObject` sobre `<bucket>/rayito/*` y `s3:ListBucket` | `ImagePublisherPolicy` (o `CallerPolicy`) |
| Preparar la cuenta (`rayito setup`, desde 0.15.0) | credenciales de administración o de *bootstrap*, una sola vez: `cloudformation:CreateStack`/`UpdateStack`/`DescribeStacks` con `CAPABILITY_NAMED_IAM`, crear roles y políticas de IAM, publicar imágenes y `sts:GetCallerIdentity`; sólo cuando crea el bucket, los permisos de S3 con que CloudFormation lo crea y lo protege (`s3:CreateBucket`, `PutEncryptionConfiguration`, `PutBucketPublicAccessBlock`, `PutBucketOwnershipControls`, `PutLifecycleConfiguration`, `PutBucketTagging`, `GetBucketPolicy`, `PutBucketPolicy`) y `s3:ListBucket` | no viene en las plantillas: es el paso previo a ellas ([Configurar AWS](../primeros-pasos/configurar-aws.md#2-prepara-la-cuenta-con-rayito-setup)). `CallerPolicy` no alcanza |
| `rayito doctor` | lo anterior más `servicequotas:ListServiceQuotas` (en las tres políticas) y, opcionalmente, `iam:SimulatePrincipalPolicy` e `iam:GetRole`; con `--efs-vpc-id`, `ec2:Describe*` | `CallerPolicy` (salvo la simulación, que se salta si falta, y `ec2:Describe*`). Con sólo `SandboxLauncherPolicy`, la simulación avisa (`WARN`) de las acciones de publicación que faltan: es lo esperado |
| Ejecutar código, comandos, ficheros, PTY, git, métricas | ninguno más: viajan por el canal del sandbox con su access token | — |
| Plazo del servidor, `update_network`, formas de clase | ninguno más | — |
| `execution_role_arn=` (credenciales dentro del sandbox) | `iam:PassRole` sobre ese rol | `SandboxLauncherPolicy` o `CallerPolicy` (para el `ExecutionRole` de la plantilla) |
| URLs de S3 y ficheros grandes (`transfer=`) | `s3:PutObject`, `GetObject`, `DeleteObject`, `AbortMultipartUpload` sobre `<bucket>/<prefijo>/*` y `s3:ListBucket` acotado al prefijo | `SandboxLauncherPolicy` o `CallerPolicy` con `TransferBucket` y `TransferPrefix` |
| Persistencia (`persist=`) | ninguno en el llamante; el **execution role** necesita `s3:PutObject`, `GetObject`, `AbortMultipartUpload` y `ListBucket` sobre el prefijo | `ExecutionRole` con `PersistenceBucket` y `PersistencePrefix` |
| Secretos: inyectar (`secrets=`) | `secretsmanager:GetSecretValue` y `DescribeSecret` sobre `secret:rayito/*`, salvo `rayito/webhooks/*` (los secretos de firma de webhooks: denegados, y el SDK los rechaza) | `RayitoSecretsReader` |
| Secretos: CRUD (`SecretStore`) | además `CreateSecret`, `PutSecretValue`, `UpdateSecret`, `DeleteSecret` y `ListSecrets` | `RayitoSecretsAdmin` |
| Secretos con una CMK propia | `kms:Decrypt` (y `kms:GenerateDataKey` para escribir), sólo a través de Secrets Manager | parámetro `KmsKeyArn` de `infra/secrets-access.yaml` |
| Índice de metadatos: crear con `index=` | `dynamodb:PutItem` sobre la tabla | `RayitoIndexWriter` |
| Índice de metadatos: listar con `index=` | `dynamodb:BatchGetItem` sobre la tabla | `RayitoIndexReader` |
| Eventos: lanzar con `events=` | `secretsmanager:GetSecretValue` sobre la clave del stack y `cloudformation:DescribeStacks` sobre la pila | `EventsLauncherPolicy` (pila `events-webhooks`) |
| Eventos: leer (`get_events`) | `dynamodb:Query` sobre las filas de eventos y el índice `gsi1`, y `DescribeStacks` | `EventsReaderPolicy` |
| Eventos: webhooks (`register_webhook`, `list_webhooks`, `delete_webhook`) | `dynamodb:PutItem`/`DeleteItem`/`Query` sólo sobre filas `WEBHOOK`, y `DescribeStacks` | `EventsWebhookAdminPolicy` (de operador: los webhooks son de toda la pila) |
| Templates (`Template.build`, `rayito template build`, `AgentTemplate`, `rayito agent template build`) | `lambda:CreateMicrovmImage`/`UpdateMicrovmImage` (nunca sobre `rayito-base*`), `iam:PassRole` del rol de build, S3 sobre `<bucket>/rayito/templates/*` y lectura de los logs de build | `RayitoTemplateBuilder` (`infra/templates.yaml`) |
| Desplegar en AgentCore (`--target agentcore`, desde 0.15.0) | `cloudformation:CreateStack`/`UpdateStack`/`DescribeStacks`/`DeleteStack` sobre `rayito-agentcore-*` y lo necesario para crear lo que la plantilla contiene (ECR, CodeBuild, logs, dos roles de IAM, con `CAPABILITY_IAM` e `iam:PassRole`); `s3:ListBucket` y `s3:PutObject`/`DeleteObject` sobre `rayito/images/agentcore/*`; `codebuild:StartBuild`/`BatchGetBuilds`; `ecr:DescribeImages`; `bedrock-agentcore:CreateAgentRuntime`/`UpdateAgentRuntime`/`GetAgentRuntime`/`ListAgentRuntimes`/`DeleteAgentRuntime`/`InvokeAgentRuntime`/`StopRuntimeSession`; `logs:DeleteLogGroup` sobre `/aws/bedrock-agentcore/runtimes/*`; `sts:GetCallerIdentity`. No usa `lambda:*` ni la pasarela de secretos ([guía](../guias/desplegar-en-agentcore.md#antes-de-empezar)) | no viene en las plantillas de `infra/iam.yaml`: añádelo a tu política |
| Constructos de CDK (`RayitoAgent`, `RayitoMicrovmImage`, desde 0.15.0) | quien ejecuta `cdk deploy` sólo asume los roles del bootstrap de CDK; despliega CloudFormation con el rol de ejecución del bootstrap. Con el bootstrap por defecto no hace falta nada más | si restringes el bootstrap, los permisos de [Bootstrap restringido](../guias/desplegar-con-cdk.md#bootstrap-restringido) |
| Agente (`sbx.agent.run`/`.stream`) | ninguno más: viaja por el canal del sandbox. La credencial del modelo llega por la pasarela (`gateways=`, presets `bedrock_gateway`…): `secretsmanager:GetSecretValue` sobre ese secreto, en las credenciales del llamante, no en el execution role. Los permisos de Bedrock son los de la clave de API que guardas en el secreto, no los tuyos | `RayitoSecretsReader` ([Agente en el sandbox](../guias/agente-en-el-sandbox.md), [Pasarela de secretos](../funciones-opcionales/pasarela-de-secretos.md)) |
| Montajes S3 (`mounts=`), telemetría con `OtlpAuth.execution_role()`, volúmenes EFS | ninguno en el llamante salvo EFS (`elasticfilesystem:CreateAccessPoint`/`DescribeAccessPoints`/`DeleteAccessPoint` para `VolumeStore`, ver [Volúmenes EFS](../funciones-opcionales/volumenes-efs.md)); el **execution role** necesita la política de la pila | `RayitoS3MountAccess`, `RayitoOtlpExport`, `RayitoEfsVolumeClient` |
| Limitar los tamaños que se pueden lanzar | un Deny de `RunMicrovm` fuera de la lista y de publicar imágenes | `RayitoRunAllowedSizes` (pila `sizes-guard`): sólo para el lanzador, nunca el publicador |
| Receta de la flota (`deploy_agent()` la guarda, `AgentFleet.connect(nombre)` la lee, `stop()` la borra; desde 0.17.0) | `ssm:PutParameter`, `ssm:GetParameter` y `ssm:DeleteParameter` sobre `arn:aws:ssm:<región>:<cuenta>:parameter/rayito/agents/*`. Ningún permiso de KMS: el `SecureString` usa la clave gestionada `aws/ssm`. Sin ellos, el despliegue y la reconexión siguen con un aviso `recipe_access_denied` ([Receta de la flota](../guias/flota-de-agentes.md#la-receta-de-la-flota)) | sentencia `AgentRecipe`: `CallerPolicy` las tres; `SandboxLauncherPolicy` sólo `GetParameter` y `DeleteParameter`, y `ImagePublisherPolicy` sólo `PutParameter` (sentencia `AgentRecipeWrite`), porque escribir la receta equivale a poder desplegar (decide la imagen, la red, `env` y las pasarelas de quien conecta por nombre) |
| Renovar la clave de Bedrock (`renew_gateway_keys`, desde 0.17.0, opcional) | `secretsmanager:DescribeSecret` y `secretsmanager:PutSecretValue` sobre el secreto de la pasarela `bedrock`; para usar la clave, `bedrock:CallWithBearerToken` (condición `bedrock:bearerTokenType` = `SHORT_TERM`) y los permisos de invocación del modelo en el principal que la firma ([Pasarela de secretos](../funciones-opcionales/pasarela-de-secretos.md#renovar-la-clave-de-bedrock)) | `RayitoSecretsAdmin` cubre las dos de Secrets Manager; `bedrock:CallWithBearerToken` no viene en las plantillas: añádelo a tu política |
| Proxy local (`rayito sandbox proxy`) | `lambda:GetMicrovm` y `CreateMicrovmAuthToken` | `SandboxLauncherPolicy` o `CallerPolicy` |
| Logs de CloudWatch (`rayito sandbox logs`) | `logs:DescribeLogStreams` y `logs:GetLogEvents` sobre el grupo `/rayito/<imagen>` | no viene en la plantilla: añádelo a tu política |

!!! note "El execution role del sandbox no necesita nada de esto"
    Las transferencias las firma el SDK con tus credenciales y `rayd` nunca
    guarda credenciales. El execution role sólo hace falta para la
    persistencia y para dar a tu código credenciales dentro del sandbox; por
    defecto no hay ninguno.

## Comprobarlo

```bash
rayito doctor --template rayito-base
```

La comprobación `iam-simulation` simula las acciones del SDK y la CLI. Es
orientativa: el simulador marca `lambda:PassNetworkConnector` como
`implicitDeny` incluso con permisos de administrador, y ese aviso no bloquea
nada.

## Ver también

- [Configurar AWS](../primeros-pasos/configurar-aws.md)
- [Imágenes](../images.md)
- [Seguridad](../security.md)
- [`infra/README.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/infra/README.md)
