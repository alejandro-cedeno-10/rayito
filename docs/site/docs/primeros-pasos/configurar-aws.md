---
description: "Prepara tu cuenta de AWS para Rayito una vez con rayito setup: la pila de IAM, la imagen rayito-base y el bucket (lo reutiliza o lo crea), y rayito doctor."
---

# Configurar AWS

Antes del primer sandbox, tu cuenta necesita permisos (una pila de IAM), un
bucket para los artefactos de la imagen y la imagen `rayito-base` publicada.
`rayito setup` lo prepara en una sola orden, idempotente, como `cdk bootstrap`:
**una vez por cuenta y región**. La primera ejecución tarda unos 10–12 minutos,
casi todo esperando a que AWS construya el snapshot de la imagen; las
siguientes, que no tienen nada que crear, terminan en segundos.

| Paso | Una vez por | Tiempo | Coste |
|---|---|---|---|
| 1. Credenciales y región | máquina | 1 min | — |
| 2. `rayito setup` | cuenta y región | 10–12 min la primera vez; segundos después | pila de IAM gratis; ≈ $0,04/semana por versión de imagen; zip en el bucket, céntimos |
| 3. Asignar las políticas de IAM | cuenta y región | 2 min | gratis |
| 4. `RAYITO_TEMPLATE` | entorno de tu app | — | — |
| 5. `rayito doctor --launch` | cuando quieras | 1 min | ≈ $0,002 |

¿Prefieres cada paso a mano (bucket, pila, descarga verificada con cosign,
`rayito image publish`)? Sigue siendo posible: [A mano, sin `rayito setup`](#a-mano-sin-rayito-setup).

Después, cada aplicación sólo instala el SDK (`pip install rayito` o
`pnpm add rayito`) y escribe su código; no hay ningún servidor de Rayito que
desplegar.

!!! info "Todo queda en tu cuenta"
    Rayito no tiene servidor ni API key. Lo que se crea aquí son recursos tuyos:
    una pila de CloudFormation con roles y políticas y una imagen de Lambda
    MicroVMs, más un zip en un bucket de S3 (el tuyo, el de `cdk bootstrap` o,
    si no hay ninguno, uno que crea `rayito setup`). Borrar las pilas y la
    imagen lo deja todo como estaba, salvo el bucket creado, que se conserva
    hasta que lo borres tú.

## 1. Credenciales y región { #1-credenciales-y-region }

Elige una de las diez regiones con Lambda MicroVMs (`us-east-1`,
`us-east-2`, `us-west-2`, `eu-west-1`, `eu-central-1`, `eu-north-1`,
`ap-northeast-1`, `ap-south-1`, `ap-southeast-1`, `ap-southeast-2`) y
exporta tu perfil:

```bash
export AWS_PROFILE=<tu-perfil> AWS_REGION=us-east-1
aws sts get-caller-identity          # comprueba que las credenciales funcionan
```

Basta con `AWS_REGION`: los dos SDK y la CLI la leen. Si falta, usan
`AWS_DEFAULT_REGION` y después la región del perfil; `region=` (Python) o
`region` (TypeScript) gana a todas.

Para estos pasos de preparación hacen falta credenciales de administración o
de *bootstrap*, **una sola vez** y como con `cdk bootstrap`: CloudFormation,
IAM (la pila crea roles y políticas), S3 cuando `rayito setup` tiene que crear
el bucket, y publicar imágenes. La política `CallerPolicy` del paso 3 no
alcanza para esto: es la mínima para *usar* el SDK después, no para prepararlo.
No necesitas crear ningún bucket antes: si ya tienes uno, `rayito setup` lo
reutiliza, y si no, lo crea.

Si tu organización exige etiquetas para crear recursos (una SCP que deniega
`s3:CreateBucket` o `cloudformation:CreateStack` sin ellas), pásalas con
`--tag K=V`, repetible: `rayito setup` las pone en las pilas que crea y
CloudFormation las propaga a sus recursos.

## 2. Prepara la cuenta con `rayito setup` { #2-prepara-la-cuenta-con-rayito-setup }

`rayito setup` mira tu cuenta y tu región, enseña el plan y el coste, pide
confirmación y crea sólo lo que falta. Son tres recursos, siempre en este
orden:

- **El bucket de artefactos.** Reutiliza el primero que exista de:
  `--bucket`, la variable `RAYITO_BUCKET`, el bucket de la pila de IAM si ya la
  tienes, o el de `cdk bootstrap` de esa cuenta y región. **Sólo si no hay
  ninguno lo crea**, con la pila `rayito-m0-artifacts`
  (`infra/artifact-bucket.yaml`) y el nombre
  `rayito-artifacts-<cuenta>-<región>`. Un `--bucket` o `RAYITO_BUCKET` que no
  existe también se crea, con ese nombre. El bucket creado está cifrado
  (SSE-S3), es privado (todo el acceso público bloqueado, sin ACL), sólo acepta
  TLS y borra los zips de la imagen a los 7 días; al borrar la pila se
  conserva.
- **La pila de IAM `rayito-m0-iam`**, con la plantilla `infra/iam.yaml`
  (roles y las tres políticas del [paso 3](#3-la-pila-de-iam)). Si ya existe
  y apunta al bucket elegido, no la toca.
- **La imagen `rayito-base`.** Descarga `rayito-image.zip` y `SHA256SUMS` de la
  release `rayd-v<versión de tu SDK>`, comprueba el sha256 y la publica. Si ya
  hay una versión lanzable, no construye otra; si hay un build en curso, lo
  espera.

Empieza con `--dry-run`: sólo lee tu cuenta y no crea ni cambia nada.

=== "Python"

    ```bash
    rayito setup --dry-run
    ```

=== "TypeScript"

    ```bash
    npx rayito setup --dry-run
    ```

En una cuenta nueva sin ningún bucket, el plan es (si ya tienes uno, la primera
línea dice `reutilizar` y el nombre de ese bucket, y el coste del bucket no
aparece):

<!-- noqa: example: salida de consola, no es código -->
```text
rayito setup: cuenta 123456789012, región us-east-1
  bucket rayito-artifacts-<cuenta>-<región>: crear con la pila rayito-m0-artifacts (nombre por defecto de Rayito)
  pila de IAM rayito-m0-iam: crear
  imagen rayito-base: publicar desde rayd-v0.15.0 (≈ 10 min)
Coste aproximado:
  bucket de artefactos: los zips de la imagen caducan a los 7 días (regla de ciclo de vida)
  pila de IAM: gratis
  imagen: ≈ $0,04 por semana por versión publicada (mínimo una semana)
  zip de la imagen en el bucket: céntimos
no se creó ni cambió nada (--dry-run)
```

Cuando el plan te parezca bien, ejecútalo sin `--dry-run`. Pide
confirmación (`¿Preparar la cuenta? [y/N]:`); sin terminal, o con `--json`,
hace falta `--yes`.

=== "Python"

    ```bash
    rayito setup
    ```

=== "TypeScript"

    ```bash
    npx rayito setup
    ```

Imprime el mismo plan y pide confirmación; después, las líneas de progreso de
cada paso, la salida de [`rayito doctor`](#5-diagnostico) y, al final, lo que
necesita tu aplicación. En la cuenta sin bucket de arriba:

<!-- noqa: example: salida de consola, no es código -->
```text
rayito setup: cuenta 123456789012, región us-east-1
  bucket rayito-artifacts-<cuenta>-<región>: crear con la pila rayito-m0-artifacts (nombre por defecto de Rayito)
  pila de IAM rayito-m0-iam: crear
  imagen rayito-base: publicar desde rayd-v0.15.0 (≈ 10 min)
Coste aproximado:
  bucket de artefactos: los zips de la imagen caducan a los 7 días (regla de ciclo de vida)
  pila de IAM: gratis
  imagen: ≈ $0,04 por semana por versión publicada (mínimo una semana)
  zip de la imagen en el bucket: céntimos
¿Preparar la cuenta? [y/N]: y
creando el bucket rayito-artifacts-<cuenta>-<región> con la pila rayito-m0-artifacts
bucket rayito-artifacts-<cuenta>-<región> listo (pila rayito-m0-artifacts)
creando la pila de IAM rayito-m0-iam
pila de IAM rayito-m0-iam lista
descargando SHA256SUMS de rayd-v0.15.0
descargando rayito-image.zip de rayd-v0.15.0
sha256 de rayito-image.zip verificado contra SHA256SUMS: <sha256>
base_image_version=<versión>
uploaded s3://rayito-artifacts-<cuenta>-<región>/rayito/images/rayd-<sha12>.zip (<bytes> bytes)
create-microvm-image accepted: version <versión>
  image=<estado> version=<versión> state=<estado> status=<estado> (<s>s)
  ...                                (una línea por sondeo hasta que la versión es lanzable)
imagen rayito-base <versión> lanzable
...                                  (la salida de rayito doctor)
rayito setup: cuenta preparada en us-east-1
RAYITO_TEMPLATE=arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base
RAYITO_BUCKET=rayito-artifacts-<cuenta>-<región>
```

Las líneas `uploaded`, `create-microvm-image` e `image=` salen del mismo
pipeline que `rayito image publish` y por eso están en inglés. En una cuenta que
ya tiene bucket, el primer paso es `reutilizar` y no hay líneas de bucket; en
una cuenta que ya tiene la imagen, no hay descarga ni build.

Si `rayito doctor` encuentra algún `FAIL`, en lugar de las tres últimas líneas
verás `rayito setup: rayito doctor encontró algún FAIL; la cuenta no está lista`
y la orden sale con código 1. Puedes repetir `rayito setup` las veces que
quieras: en una cuenta ya preparada observa tres recursos reutilizables
(`La cuenta ya está preparada: no hay nada que crear.`), no escribe nada y
termina en segundos. Si
una ejecución se corta, la siguiente retoma lo que quedó en curso (la pila o el
build) sin lanzar un segundo build.

!!! warning "Qué comprueba el hash y qué no"
    `rayito setup` descarga el zip de la release de GitHub y comprueba su
    sha256 contra el `SHA256SUMS` de esa misma release. Eso detecta un fichero
    truncado, corrupto o equivocado, pero **no prueba que sea auténtico**: zip
    y `SHA256SUMS` salen del mismo sitio. La firma se comprueba con `cosign
    verify-blob`; si la quieres, verifica el zip a mano y pásalo con
    `rayito setup --artifact rayito-image.zip` (ver [A mano, sin `rayito
    setup`](#a-mano-sin-rayito-setup) y [Verificar una release](../verify.md)).

!!! info "Coste y activación"
    - **Activa:** ejecutar `rayito setup` (y confirmar). `rayito setup
      --dry-run` sólo lee la cuenta y no crea nada.
    - **Recursos y llamadas AWS:** si no hay ningún bucket, la pila de
      CloudFormation `rayito-m0-artifacts` (`CreateStack` sin capacidades; un
      bucket de S3 y su política); la pila `rayito-m0-iam` (`CreateStack` o
      `UpdateStack` con `CAPABILITY_NAMED_IAM`; roles y tres políticas
      gestionadas); la imagen `rayito-base` (un objeto en el bucket del
      artefacto, `CreateMicrovmImage` o `UpdateMicrovmImage` y su build);
      lecturas de `GetCallerIdentity`, `DescribeStacks`, `HeadBucket`,
      `GetMicrovmImage` y `ListMicrovmImageVersions`; la descarga HTTPS de dos
      ficheros de la release de GitHub. El SDK no llama a `CreateBucket`: el
      bucket lo crea CloudFormation con tus credenciales.
    - **Coste aproximado:** la pila de IAM es gratis; el bucket no cuesta nada
      por existir y sus zips caducan a los 7 días; cada versión publicada de la
      imagen ≈ $0,04 por semana de almacenamiento (mínimo una semana); el zip
      en el bucket, céntimos. Ver [Precios](../cost.md).
    - **IAM:** credenciales de administración o de *bootstrap*, una sola vez:
      `cloudformation:CreateStack` y lecturas de pila, crear roles y políticas
      de IAM (`iam:CreateRole`, `iam:CreatePolicy`), publicar imágenes y, sólo
      cuando crea el bucket, los permisos de S3 que CloudFormation usa para
      crearlo y protegerlo (`s3:CreateBucket`, `s3:PutEncryptionConfiguration`,
      `s3:PutBucketPublicAccessBlock`, `s3:PutBucketOwnershipControls`,
      `s3:PutLifecycleConfiguration`, `s3:PutBucketTagging`,
      `s3:GetBucketPolicy` y `s3:PutBucketPolicy`) y `s3:ListBucket`. Para usar
      el SDK después basta la política mínima del paso 3.
    - **Cómo apagarla:** no ejecutar `rayito setup`. Para deshacer lo creado,
      borra las pilas `rayito-m0-iam` y, si existe, `rayito-m0-artifacts`, y las
      versiones de la imagen que publicaste. El bucket creado se conserva al
      borrar su pila: vacíalo y bórralo a mano si ya no lo quieres.
    - **Ejemplo:** `rayito setup --dry-run`.

## 3. Asigna las políticas de IAM { #3-la-pila-de-iam }

La pila `rayito-m0-iam` (la que crea `rayito setup` o la que desplegaste a
mano) trae el rol con el que AWS construye la imagen, el rol de ejecución
opcional del sandbox y tres políticas mínimas para quien usa el SDK:
`SandboxLauncherPolicy` (`rayito-m0-launcher-<región>`, sólo lanzar y manejar
sandboxes), `ImagePublisherPolicy` (`rayito-m0-publisher-<región>`, sólo
publicar imágenes) y `CallerPolicy` (`rayito-m0-caller-<región>`, las dos
juntas).

Asigna a cada identidad sólo lo que hace:

- **Tu usuario de desarrollo** (publica la imagen y prueba sandboxes): la
  del output `CallerPolicyArn`.
- **El rol de tu servicio o de tu agente** (sólo crea sandboxes): la del
  output `SandboxLauncherPolicyArn`. Nunca `CallerPolicy`: un servicio
  comprometido podría publicar una versión con puerta trasera de la imagen
  que usarán todos tus sandboxes.
- **Tu CI o pipeline de release** (publica imágenes): la del output
  `ImagePublisherPolicyArn` (y la del lanzador si además corre pruebas).

```bash
policy_arn() {
  aws cloudformation describe-stacks --stack-name rayito-m0-iam \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

# tu usuario de IAM:
aws iam attach-user-policy --user-name <tu-usuario> --policy-arn "$(policy_arn CallerPolicyArn)"
# el rol de tu servicio:
aws iam attach-role-policy --role-name <tu-rol> --policy-arn "$(policy_arn SandboxLauncherPolicyArn)"
```

Si ya tienes permisos de administración, puedes saltarte la asignación a tu
usuario; hazla siempre para el rol de tu servicio.

Con IAM Identity Center (SSO) no se asigna a un usuario: añade la política
al *permission set* de tu acceso desde la consola de Identity Center.

Parámetros opcionales de la plantilla: `TransferBucket` y `TransferPrefix`
para [ficheros grandes y URLs de S3](../files.md), y `PersistenceBucket` y
`PersistencePrefix` para la [persistencia](../persistence.md). Qué permiso
necesita cada función: [IAM](../operacion/iam.md).

`rayito setup` sólo fija `ArtifactBucket`; el resto de parámetros se quedan
como estén. Si más adelante despliegas a mano la pila con esos parámetros
opcionales, un `rayito setup` posterior los conserva.

## 4. Apunta el SDK a la imagen { #4-apunta-el-sdk-a-la-imagen }

```bash
export RAYITO_TEMPLATE=rayito-base       # nombre o ARN de la imagen
```

Con `RAYITO_TEMPLATE` no hace falta pasar `template=` en cada `create()`. Un
nombre se resuelve al ARN de tu cuenta y región.

## 5. Diagnóstico { #5-diagnostico }

`rayito doctor` hace diez comprobaciones de la cuenta (credenciales,
imágenes gestionadas, cuotas, IAM, bucket, imagen, sandboxes vivos, token,
agente y compatibilidad SDK ↔ `rayd`) y dice qué falta. Sin `--launch`,
las tres últimas sólo corren si ya hay un sandbox `RUNNING` de esa imagen;
con `--launch` crea uno efímero de 300 s y lo mata al terminar:

```bash
rayito doctor --template rayito-base --launch     # --launch: un sandbox efímero (≈ $0,002)
```

Una salida típica en una cuenta recién preparada (con el SDK 0.11.0):

<!-- noqa: example: salida de consola, no es código -->
```text
OK   credentials     cuenta 123456789012, us-east-1, assumed-role
OK   managed-images  al2023-1 disponible, versión más nueva 1.0
OK   quotas          18 cuotas de MicroVM, ninguna por debajo del default
WARN iam-simulation  sin permiso explícito para lambda:PassNetworkConnector; simulación orientativa: las comprobaciones 2, 5, 6 y 8 son las que cuentan
       policy_source_arn: arn:aws:iam::123456789012:role/<tu-rol>
       explicit_deny: []
       denied_by_organizations: []
       implicit_deny: ['lambda:PassNetworkConnector']
OK   bucket          s3://amzn-s3-demo-bucket accesible en us-east-1
OK   image-gate      rayito-base 1.0 lanzable
OK   sandboxes       ningún MicroVM RUNNING de rayito-base
OK   token           token acuñado por create() para microvm-00000000-0000-0000-0000-000000000001
OK   agent           rayd 0.11.0 agent_ready, kernel_ready=True, imds_blocked=False
OK   compatibility   SDK 0.11.0, agent_version 0.11.0; imagen 1.0 (contador de builds, informativo)

SDK   rayd mínimo  Estado  Nota
0.1   0.1.0                M6: imds_blocked, hook_anomalies y metadata exigen el rayd del tag rayd-v0.1.0
0.2   0.2.0                M7: Checkpoint/Restore (persist=) y language= exigen el rayd del tag rayd-v0.2.0
0.3   0.3.0                M9: max_lifetime/on_timeout, set_timeout, get_metrics_history, network= y los kernels Deno
                           exigen el rayd del tag rayd-v0.3.0
0.4   0.4.0                0.4: UnimplementedError único, SetTimeout validado en el dominio y mensajes del agente en
                           español exigen el rayd del tag rayd-v0.4.0
0.5   0.5.0                0.5: /suspend con sync acotado por sistema de ficheros y las funciones opcionales (secretos,
                           índice, OTel) se validan con el rayd del tag rayd-v0.5.0
0.6   0.6.0                0.6: ConfigureSandbox y las funciones 0.6 (montajes S3, eventos, OTLP, pasarela de secretos,
                           start/ready de templates) exigen el rayd del tag rayd-v0.6.0
0.7   0.7.0                0.7: los volúmenes EFS (create(volumes=)) y el endurecimiento del agente (hooks aislados,
                           persistencia ligada al /run) exigen el rayd del tag rayd-v0.7.0
0.8   0.8.0                0.8: el agente de IA (sbx.agent, AgentTemplate y el warmup de los pools) se valida con el
                           rayd del tag rayd-v0.8.0
0.9   0.9.0                0.9: connect() recupera sbx.gateways desde ConfigureStatus y el arranque rápido del agente
                           sin la opción D se validan con el rayd del tag rayd-v0.9.0
0.10  0.10.0               0.10: el timeout y abort() del agente paran todo el árbol de procesos (kill_tree) y los
                           proveedores del agente se validan con el rayd del tag rayd-v0.10.0
0.11  0.11.0       OK      0.11: el arranque rápido de AgentTemplate (kernel_warmup=False por defecto) y las claves de
                           artefacto por ejecución se validan con el rayd del tag rayd-v0.11.0

rayito doctor: 9 OK, 1 WARN, 0 FAIL, 0 SKIP
sandbox de --launch terminado: microvm-00000000-0000-0000-0000-000000000001
```

Cada línea es `OK`, `WARN`, `FAIL` o `SKIP`; un `WARN` o `FAIL` lista
debajo sus detalles. Después va la tabla de compatibilidad SDK ↔ `rayd`,
con el estado en la fila de tu SDK, y el recuento. La orden sale con código
1 sólo si hay algún `FAIL`. `--json` da lo mismo como un documento JSON.

El `WARN` de `iam-simulation` sobre `lambda:PassNetworkConnector` es
esperado: el simulador de IAM lo marca incluso con permisos de
administrador y no bloquea nada (las comprobaciones que cuentan son las de
imágenes gestionadas, bucket, imagen y token). Qué hacer con cada `FAIL`:
[Solución de problemas](../operacion/solucion-de-problemas.md#rayito-doctor).

## A mano, sin `rayito setup` { #a-mano-sin-rayito-setup }

Los pasos que `rayito setup` encadena, uno a uno. Úsalos si prefieres controlar
cada paso o si quieres
verificar la firma con cosign antes de publicar (después, `rayito setup
--artifact rayito-image.zip` publica ese zip verificado en lugar de descargar
el de la release).

??? example "Bucket, pila de IAM y publicación de la imagen, paso a paso"
    **Un bucket para los artefactos**

    El zip de la imagen se sube a un bucket tuyo, bajo el prefijo `rayito/`.
    Créalo en la **misma región** que los sandboxes:

    ```bash
    export RAYITO_BUCKET=amzn-s3-demo-bucket
    aws s3 mb "s3://$RAYITO_BUCKET" --region "$AWS_REGION"
    ```

    Sustituye `amzn-s3-demo-bucket` por un nombre único tuyo. `RAYITO_BUCKET`
    evita repetir `--bucket` en la CLI.

    **La pila de IAM**

    Descarga la plantilla del repositorio y despliégala (es la misma pila que
    crea `rayito setup`; después sigue en el [paso 3](#3-la-pila-de-iam)):

    ```bash
    curl -fsSLO https://raw.githubusercontent.com/alejandro-cedeno-10/rayito/main/infra/iam.yaml
    aws cloudformation deploy --stack-name rayito-m0-iam \
      --template-file iam.yaml --capabilities CAPABILITY_NAMED_IAM \
      --parameter-overrides ArtifactBucket="$RAYITO_BUCKET" LogGroupPrefix=/rayito
    ```

    **Publicar la imagen `rayito-base`**

    Cada sandbox arranca desde una imagen de Lambda MicroVMs publicada en tu
    cuenta. La forma más corta es publicar el `rayito-image.zip` firmado de la
    última release, sin compilar nada:

    === "Desde la release (recomendado)"

        ```bash
        RAYD_VERSION=0.14.0     # la misma versión que tu SDK: python -c "import rayito; print(rayito.__version__)"  (TypeScript: npm ls rayito)
        BASE=https://github.com/alejandro-cedeno-10/rayito/releases/download/rayd-v${RAYD_VERSION}
        curl -fsSLO "$BASE/rayito-image.zip"
        curl -fsSLO "$BASE/rayito-image.zip.sigstore.json"    # su firma de Sigstore
        curl -fsSLO "$BASE/SHA256SUMS"

        cosign verify-blob --bundle rayito-image.zip.sigstore.json \
          --certificate-identity "https://github.com/alejandro-cedeno-10/rayito/.github/workflows/release.yml@refs/tags/rayd-v${RAYD_VERSION}" \
          --certificate-oidc-issuer https://token.actions.githubusercontent.com \
          rayito-image.zip
        sha256sum -c --ignore-missing SHA256SUMS   # en macOS: shasum -a 256 -c --ignore-missing SHA256SUMS

        rayito image publish --artifact rayito-image.zip --base-image-version 1
        ```

        Comprueba la firma **antes** de publicar: `rayito image publish` no
        verifica nada, y el zip de una release se podría reemplazar con un token
        del repositorio. Si `cosign verify-blob` no dice `Verified OK`, no
        publiques ese fichero. Necesitas [cosign](https://docs.sigstore.dev/cosign/system_config/installation/)
        ≥ 2.x; qué prueba la firma y cómo verificar el binario `rayd` suelto, en
        [Verificar una release](../verify.md).

        Usa el `rayd` de la misma versión que el SDK instalado: si la imagen es
        más vieja, la comprobación `compatibility` de `rayito doctor` lo marca.
        Todas las versiones están en las
        [releases `rayd-v*`](https://github.com/alejandro-cedeno-10/rayito/releases).

        Con la CLI de GitHub (`gh`, autenticada con `gh auth login`) la descarga
        es una línea:
        `gh release download "rayd-v${RAYD_VERSION}" --repo alejandro-cedeno-10/rayito --pattern 'rayito-image.zip*' --pattern SHA256SUMS`.

    === "Desde el código fuente"

        ```bash
        git clone https://github.com/alejandro-cedeno-10/rayito.git && cd rayito
        make image-publish BUCKET="$RAYITO_BUCKET"
        ```

        Compila `rayd` para `aarch64-unknown-linux-musl`: necesitas Linux o WSL2
        (en macOS, una VM Linux) con Rust y `cargo-zigbuild`, como explica
        [`CONTRIBUTING.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/CONTRIBUTING.md).

    `rayito image publish` sube el zip, pide a AWS que construya la imagen y
    espera a que la versión sea lanzable (unos 10 minutos). La última línea es
    `RAYITO_TEMPLATE=<arn>`. Cada versión publicada cuesta ≈ $0,04 por semana de
    almacenamiento ([Precios](../cost.md#componentes-del-precio)).

    `--base-image-version` es la versión de la base gestionada de AWS sobre la que
    se construye. No hace falta buscarla a mano: con `--base-image-version latest`,
    `rayito image publish` usa la más reciente que lista AWS y la imprime
    (`base_image_version=1`); un número explícito (`--base-image-version 1`) fija
    una build reproducible.

    ??? example "Las otras variantes: `-caps`, `-poly` y la imagen del agente"
        - `rayito-base-caps` es el mismo zip publicado con
          `additionalOsCapabilities ALL`; hace falta para la
          [política de red saliente](../network.md) y es la base de la imagen
          del agente:

            ```bash
            rayito image publish --artifact rayito-image.zip --base-image-version 1 \
              --image-name rayito-base-caps --os-capabilities ALL
            ```

        - `rayito-base-poly` añade los kernels bash, JavaScript y TypeScript
          ([Lenguajes y kernels](../kernels.md)). Se construye desde el código
          fuente: `make image-publish-poly BUCKET=amzn-s3-demo-bucket`.

        - `rayito-agent`, para el [agente en el sandbox](../guias/agente-en-el-sandbox.md),
          se construye en tu cuenta sobre `rayito-base-caps` (publícala antes)
          con OpenCode, ripgrep y deepagents
          ([Templates de agente](../funciones-opcionales/templates-de-agente.md)):

            ```bash
            rayito agent template build --bucket amzn-s3-demo-bucket
            ```

        Qué imagen necesita cada función: [Imágenes](../images.md).


## Siguiente paso { #siguiente-paso }

[Primer sandbox](../quickstart.md): comandos, ficheros y código en Python y
TypeScript.

??? info "Fuentes y mediciones"
    - Plantilla IAM: [`infra/iam.yaml`](https://github.com/alejandro-cedeno-10/rayito/blob/main/infra/iam.yaml)
      y [`infra/README.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/infra/README.md).
    - Assets de la release y su verificación: [Verificar una release](../verify.md).
    - Regiones: `SUPPORTED_REGIONS` en
      [`clients/python/src/rayito/_limits.py`](https://github.com/alejandro-cedeno-10/rayito/blob/main/clients/python/src/rayito/_limits.py).
