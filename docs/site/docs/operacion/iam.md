# IAM

El SDK corre con **tus** credenciales de AWS (las de quien llama: tu usuario,
el rol de tu servicio, el de tu CI). Esta página dice qué permiso necesita
cada función y qué plantilla de [`infra/`](https://github.com/alejandro-cedeno-10/rayito/tree/main/infra)
lo concede.

## Las plantillas

| Plantilla | Crea | Coste | Cuándo |
|---|---|---|---|
| `infra/iam.yaml` | el rol de build de la imagen, el execution role opcional y tres políticas del llamante: `rayito-m0-launcher-<región>` (`SandboxLauncherPolicy`, sólo lanzar sandboxes), `rayito-m0-publisher-<región>` (`ImagePublisherPolicy`, sólo publicar imágenes) y `rayito-m0-caller-<región>` (`CallerPolicy`, las dos juntas) | $0 | siempre ([Configurar AWS](../primeros-pasos/configurar-aws.md)) |
| `infra/secrets-access.yaml` | las políticas `RayitoSecretsReader` y `RayitoSecretsAdmin` | $0 | sólo si usas [secretos](../secrets.md) |
| `infra/metadata-index.yaml` | la tabla DynamoDB y las políticas `RayitoIndexWriter` y `RayitoIndexReader` | $0 en reposo | sólo si usas el [índice de metadatos](../funciones-opcionales/indice-de-metadatos.md) |
| `infra/egress-connector.yaml` | un conector de red VPC con un security group deny-all | el de tu VPC | sólo si quieres controlar la salida a internet fuera del sandbox ([Red saliente](../network.md#la-alternativa-de-plataforma)) |

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
| Crear, conectar, pausar, reanudar, listar y matar sandboxes | `lambda:RunMicrovm`, `GetMicrovm`, `ListMicrovms`, `SuspendMicrovm`, `ResumeMicrovm`, `TerminateMicrovm`, `CreateMicrovmAuthToken`, `lambda:PassNetworkConnector` (conectores gestionados) | `SandboxLauncherPolicy` (o `CallerPolicy`) de `infra/iam.yaml` |
| Publicar y podar imágenes (`rayito image`) | `lambda:CreateMicrovmImage`, `UpdateMicrovmImage`, `GetMicrovmImage*`, `ListMicrovmImage*`, `DeleteMicrovmImageVersion`; `iam:PassRole` del rol de build; `s3:PutObject`/`GetObject` sobre `<bucket>/rayito/*` y `s3:ListBucket` | `ImagePublisherPolicy` (o `CallerPolicy`) |
| `rayito doctor` | lo anterior más `servicequotas:ListServiceQuotas` y, opcionalmente, `iam:SimulatePrincipalPolicy` e `iam:GetRole` | `CallerPolicy` (salvo la simulación, que se salta si falta). Con sólo `SandboxLauncherPolicy`, la simulación avisa (`WARN`) de las acciones de publicación que faltan: es lo esperado |
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
