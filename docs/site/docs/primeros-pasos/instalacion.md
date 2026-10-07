# Instalación

Rayito tiene dos SDK con la misma superficie, Python y TypeScript, y una CLI
en Python. Instala el SDK del lenguaje de tu aplicación y la CLI para
preparar la cuenta.

## El SDK

=== "Python"

    ```bash
    pip install rayito             # o: uv add rayito
    ```

    Python ≥ 3.11. Dependencias de runtime: `grpcio`, `protobuf` y `boto3`.
    El SDK trae un árbol síncrono (`Sandbox`) y uno asíncrono
    (`AsyncSandbox`) con la misma superficie.

=== "TypeScript"

    ```bash
    pnpm add rayito                # o: npm i rayito / yarn add rayito / bun add rayito
    ```

    Node ≥ 20. El paquete trae ESM y CommonJS con sus tipos, y el shim de E2B
    en `rayito/e2b`. Todo es asíncrono. Para `await using` hace falta
    TypeScript ≥ 5.2 con `"lib": ["ES2022", "ESNext.Disposable"]`.

## La CLI

La CLI `rayito` publica la imagen, diagnostica la cuenta y opera sandboxes.
Es un extra del paquete de Python:

```bash
pip install "rayito[cli]"          # o: uv tool install "rayito[cli]"
rayito --help
```

Si usas TypeScript, instálala igualmente (o ejecútala sin instalar con
`uvx --from "rayito[cli]" rayito doctor`).

## Extras opcionales

Nada de esto hace falta para empezar.

| Para | Python | TypeScript |
|---|---|---|
| La CLI `rayito` | `pip install "rayito[cli]"` | (usa la de Python) |
| El servidor MCP ([Servidor MCP](../mcp.md)) | `pip install "rayito[mcp]"` | — |
| Trazas OpenTelemetry ([OpenTelemetry](../funciones-opcionales/opentelemetry.md)) | `pip install "rayito[otel]" opentelemetry-sdk` | `npm i @opentelemetry/api @opentelemetry/sdk-trace-base` |
| Secretos de Secrets Manager ([Secretos](../secrets.md)) | (incluido: usa `boto3`) | `npm i @aws-sdk/client-secrets-manager` |
| Pasarela de secretos y agente en el sandbox ([Pasarela de secretos](../funciones-opcionales/pasarela-de-secretos.md), [Agente en el sandbox](../guias/agente-en-el-sandbox.md)) | (incluido: usa `boto3`) | `npm i @aws-sdk/client-secrets-manager` |
| Índice de metadatos en DynamoDB ([Índice](../funciones-opcionales/indice-de-metadatos.md)) | (incluido: usa `boto3`) | `npm i @aws-sdk/client-dynamodb` |
| Eventos y webhooks ([Eventos y webhooks](../funciones-opcionales/eventos-y-webhooks.md)) | (incluido: usa `boto3`) | `npm i @aws-sdk/client-dynamodb @aws-sdk/client-secrets-manager` |
| Pilas opcionales desde código ([Pilas opcionales](../funciones-opcionales/pilas-opcionales.md)) | (incluido: usa `boto3`) | `npm i @aws-sdk/client-cloudformation` |
| Logs de un build fallido de `Template.build()` ([Templates](../funciones-opcionales/templates.md)) | (incluido: usa `boto3`) | `npm i @aws-sdk/client-cloudwatch-logs` |
| Volúmenes EFS, experimental ([Volúmenes EFS](../funciones-opcionales/volumenes-efs.md)) | (incluido: usa `boto3`) | `npm i @aws-sdk/client-efs @aws-sdk/client-ec2` |
| Dominio propio, experimental ([Dominio propio](../funciones-opcionales/dominio-propio.md)) | `pip install "rayito[custom-domain]"` | `npm i @aws-sdk/client-cloudformation @aws-sdk/client-cloudfront-keyvaluestore @aws-sdk/signature-v4a` |

En TypeScript esos paquetes son *peerDependencies* opcionales: el SDK sólo
los carga cuando activas la función. Si la activas sin instalarlos, la
primera llamada lanza `InvalidArgumentError` con el comando de instalación.

## Credenciales

El SDK usa la cadena de credenciales estándar de AWS: la de `boto3` en Python
y la del AWS SDK v3 en TypeScript. Con un perfil:

```bash
export AWS_PROFILE=<tu-perfil> AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1
```

Con AWS IAM Identity Center (SSO), inicia sesión antes con
`aws sso login --profile <tu-perfil>`; cuando la sesión caduca, el SDK lanza
un error de autenticación (ver [Solución de problemas](../operacion/solucion-de-problemas.md)).

## Comprueba la instalación

=== "Python"

    ```bash
    python -c "import rayito; print(rayito.__version__)"
    ```

=== "TypeScript"

    ```bash
    node --input-type=module -e 'import { VERSION } from "rayito"; console.log(VERSION)'
    ```

## Ejecutar los ejemplos

Los ejemplos de esta documentación son programas completos: guárdalos en un
fichero y ejecútalo.

=== "Python"

    ```bash
    python primer.py
    ```

    No hace falta nada más: el ejemplo con `asyncio.run(...)` también se
    ejecuta así.

=== "TypeScript"

    Los ejemplos usan `await` en el nivel superior y `await using`, así que
    el proyecto tiene que ser un módulo ES con un `tsconfig.json` que los
    entienda. Una vez por proyecto:

    ```bash
    npm init -y
    npm pkg set type=module            # "type": "module" en package.json
    pnpm add rayito                    # o: npm i rayito
    pnpm add -D tsx typescript @types/node
    ```

    Y un `tsconfig.json` mínimo:

    ```json
    {
      "compilerOptions": {
        "target": "ES2022",
        "lib": ["ES2022", "ESNext.Disposable"],
        "module": "nodenext",
        "types": ["node"],
        "strict": true,
        "skipLibCheck": true
      }
    }
    ```

    Guarda el ejemplo como `primer.ts` y ejecútalo con
    [`tsx`](https://tsx.is/), que corre TypeScript sin compilar:

    ```bash
    npx tsx primer.ts
    npx tsc --noEmit                   # opcional: comprobar los tipos
    ```

    Si prefieres compilar, `npx tsc` y luego `node primer.js` funcionan con
    el mismo `tsconfig.json`.

## Siguiente paso

[Configurar AWS](configurar-aws.md): la imagen `rayito-base` y los permisos
en tu cuenta.
