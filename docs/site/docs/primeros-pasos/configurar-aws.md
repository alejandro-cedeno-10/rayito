# Configurar AWS

Antes del primer sandbox, tu cuenta necesita tres cosas: permisos (una pila
de IAM), un bucket para los artefactos de la imagen y la imagen
`rayito-base` publicada. Se hace **una vez por cuenta y región**, en unos
15 minutos (casi todo es esperar a que AWS construya la imagen).

!!! info "Todo queda en tu cuenta"
    Rayito no tiene servidor ni API key. Lo que creas aquí son recursos tuyos:
    una pila de CloudFormation con roles y una política, un bucket de S3 y
    una imagen de Lambda MicroVMs. Borrar la pila y la imagen lo deja todo
    como estaba.

## 1. Credenciales y región

Elige una de las diez regiones con Lambda MicroVMs (`us-east-1`,
`us-east-2`, `us-west-2`, `eu-west-1`, `eu-central-1`, `eu-north-1`,
`ap-northeast-1`, `ap-south-1`, `ap-southeast-1`, `ap-southeast-2`) y
exporta tu perfil:

```bash
export AWS_PROFILE=<tu-perfil> AWS_REGION=us-east-1
aws sts get-caller-identity          # comprueba que las credenciales funcionan
```

Para estos pasos de preparación hacen falta permisos de administración
(crear roles de IAM y un bucket). Para usar el SDK después basta la política
mínima que crea el paso 3.

## 2. Un bucket para los artefactos

El zip de la imagen se sube a un bucket tuyo, bajo el prefijo `rayito/`.
Créalo en la **misma región** que los sandboxes:

```bash
aws s3 mb s3://amzn-s3-demo-bucket --region us-east-1
export RAYITO_BUCKET=amzn-s3-demo-bucket
```

Sustituye `amzn-s3-demo-bucket` por un nombre único tuyo. `RAYITO_BUCKET`
evita repetir `--bucket` en la CLI.

## 3. La pila de IAM

`infra/iam.yaml` crea el rol con el que AWS construye la imagen, el rol de
ejecución opcional del sandbox y tres políticas mínimas para quien usa el
SDK: `SandboxLauncherPolicy` (`rayito-m0-launcher-<región>`, sólo lanzar y
manejar sandboxes), `ImagePublisherPolicy` (`rayito-m0-publisher-<región>`,
sólo publicar imágenes) y `CallerPolicy` (`rayito-m0-caller-<región>`, las
dos juntas). Descarga la plantilla del repositorio y despliégala:

```bash
curl -fsSLO https://raw.githubusercontent.com/alejandro-cedeno-10/rayito/main/infra/iam.yaml
aws cloudformation deploy --stack-name rayito-m0-iam \
  --template-file iam.yaml --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides ArtifactBucket=amzn-s3-demo-bucket LogGroupPrefix=/rayito
```

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

Con IAM Identity Center (SSO) no se asigna a un usuario: añade la política
al *permission set* de tu acceso desde la consola de Identity Center.

Parámetros opcionales de la plantilla: `TransferBucket` y `TransferPrefix`
para [ficheros grandes y URLs de S3](../files.md), y `PersistenceBucket` y
`PersistencePrefix` para la [persistencia](../persistence.md). Qué permiso
necesita cada función: [IAM](../operacion/iam.md).

## 4. Publicar la imagen `rayito-base`

Cada sandbox arranca desde una imagen de Lambda MicroVMs publicada en tu
cuenta. La forma más corta es publicar el `rayito-image.zip` firmado de la
última release, sin compilar nada:

=== "Desde la release (recomendado)"

    ```bash
    RAYD_VERSION=0.8.0      # la misma versión que tu SDK: python -c "import rayito; print(rayito.__version__)"
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
    make image-publish BUCKET=amzn-s3-demo-bucket
    ```

    Compila `rayd` para `aarch64-unknown-linux-musl`: necesitas Linux o WSL2
    (en macOS, una VM Linux) con Rust y `cargo-zigbuild`, como explica
    [`CONTRIBUTING.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/CONTRIBUTING.md).

`rayito image publish` sube el zip, pide a AWS que construya la imagen y
espera a que la versión sea lanzable (unos 10 minutos). La última línea es
`RAYITO_TEMPLATE=<arn>`. Cada versión publicada cuesta ≈ $0,04 por semana de
almacenamiento ([Costes](../cost.md)).

??? example "Las otras dos variantes: `-caps` y `-poly`"
    - `rayito-base-caps` es el mismo zip publicado con
      `additionalOsCapabilities ALL`; hace falta para la
      [política de red saliente](../network.md):

        ```bash
        rayito image publish --artifact rayito-image.zip --base-image-version 1 \
          --image-name rayito-base-caps --os-capabilities ALL
        ```

    - `rayito-base-poly` añade los kernels bash, JavaScript y TypeScript
      ([Lenguajes y kernels](../kernels.md)). Se construye desde el código
      fuente: `make image-publish-poly BUCKET=amzn-s3-demo-bucket`.

    Qué imagen necesita cada función: [Imágenes](../images.md).

## 5. Apunta el SDK a la imagen

```bash
export RAYITO_TEMPLATE=rayito-base       # nombre o ARN de la imagen
```

Con `RAYITO_TEMPLATE` no hace falta pasar `template=` en cada `create()`. Un
nombre se resuelve al ARN de tu cuenta y región.

## 6. Diagnóstico

`rayito doctor` hace diez comprobaciones de la cuenta (credenciales,
cuotas, IAM, bucket, imagen, versión del agente) y dice qué falta:

```bash
rayito doctor --template rayito-base --launch     # --launch prueba un sandbox efímero (≈ $0,002)
```

<!-- noqa: example: salida de consola, no es código -->
```text
OK   credentials     cuenta 123456789012, us-east-1, assumed-role
OK   managed-images  al2023-1 disponible, versión más nueva …
OK   quotas          … cuotas de MicroVM, ninguna por debajo del default
WARN iam-simulation  sin permiso explícito para lambda:PassNetworkConnector; …
OK   bucket          s3://amzn-s3-demo-bucket accesible en us-east-1
OK   image-gate      rayito-base 1.0 lanzable
OK   sandboxes       ningún MicroVM RUNNING de rayito-base
OK   token           token acuñado por create() para microvm-<id>
OK   agent           rayd 0.5.0 agent_ready, kernel_ready=True, …
OK   compatibility   …

rayito doctor: 9 OK, 1 WARN, 0 FAIL, 0 SKIP
```

El `WARN` de `iam-simulation` sobre `lambda:PassNetworkConnector` es
esperado: el simulador de IAM lo marca incluso con permisos de
administrador y no bloquea nada. Qué hacer con cada `FAIL`:
[Solución de problemas](../operacion/solucion-de-problemas.md#rayito-doctor).

## Siguiente paso

[Primer sandbox](../quickstart.md): comandos, ficheros y código en Python y
TypeScript.

??? info "Fuentes y mediciones"
    - Plantilla IAM: [`infra/iam.yaml`](https://github.com/alejandro-cedeno-10/rayito/blob/main/infra/iam.yaml)
      y [`infra/README.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/infra/README.md).
    - Assets de la release y su verificación: [Verificar una release](../verify.md).
    - Regiones: `SUPPORTED_REGIONS` en
      [`clients/python/src/rayito/_limits.py`](https://github.com/alejandro-cedeno-10/rayito/blob/main/clients/python/src/rayito/_limits.py).
