# Installation

Rayito has two SDKs with the same surface, Python and TypeScript, and a CLI
written in Python. Install the SDK for your application's language and the
CLI to set up the account.

## The SDK { #el-sdk }

=== "Python"

    ```bash
    pip install rayito             # or: uv add rayito
    ```

    Python ≥ 3.11. Runtime dependencies: `grpcio`, `protobuf` and `boto3`.
    The SDK ships a synchronous tree (`Sandbox`) and an asynchronous one
    (`AsyncSandbox`) with the same surface.

=== "TypeScript"

    ```bash
    pnpm add rayito                # or: npm i rayito / yarn add rayito / bun add rayito
    ```

    Node ≥ 20. The package ships ESM and CommonJS with their types, and the
    E2B shim at `rayito/e2b`. Everything is asynchronous. `await using`
    needs TypeScript ≥ 5.2 with `"lib": ["ES2022", "ESNext.Disposable"]`.

## The CLI { #la-cli }

The `rayito` CLI publishes the image, diagnoses the account and operates
sandboxes. It is an extra of the Python package:

```bash
pip install "rayito[cli]"          # or: uv tool install "rayito[cli]"
rayito --help
```

If you use TypeScript, install it anyway (or run it without installing with
`uvx --from "rayito[cli]" rayito doctor`).

## Optional extras { #extras-opcionales }

None of this is needed to get started.

| For | Python | TypeScript |
|---|---|---|
| The `rayito` CLI | `pip install "rayito[cli]"` | (use the Python one) |
| The MCP server ([MCP server](../mcp.md)) | `pip install "rayito[mcp]"` | — |
| OpenTelemetry traces ([OpenTelemetry](../funciones-opcionales/opentelemetry.md)) | `pip install "rayito[otel]" opentelemetry-sdk` | `npm i @opentelemetry/api @opentelemetry/sdk-trace-base` |
| Secrets Manager secrets ([Secrets](../secrets.md)) | (included: uses `boto3`) | `npm i @aws-sdk/client-secrets-manager` |
| Secret gateway and agent in the sandbox ([Secret gateway](../funciones-opcionales/pasarela-de-secretos.md), [Agent in the sandbox](../guias/agente-en-el-sandbox.md)) | (included: uses `boto3`) | `npm i @aws-sdk/client-secrets-manager` |
| Metadata index in DynamoDB ([Index](../funciones-opcionales/indice-de-metadatos.md)) | (included: uses `boto3`) | `npm i @aws-sdk/client-dynamodb` |
| Events and webhooks ([Events and webhooks](../funciones-opcionales/eventos-y-webhooks.md)) | (included: uses `boto3`) | `npm i @aws-sdk/client-dynamodb @aws-sdk/client-secrets-manager` |
| Optional stacks from code ([Optional stacks](../funciones-opcionales/pilas-opcionales.md)) | (included: uses `boto3`) | `npm i @aws-sdk/client-cloudformation` |
| Logs of a failed `Template.build()` build ([Templates](../funciones-opcionales/templates.md)) | (included: uses `boto3`) | `npm i @aws-sdk/client-cloudwatch-logs` |
| EFS volumes, experimental ([EFS volumes](../funciones-opcionales/volumenes-efs.md)) | (included: uses `boto3`) | `npm i @aws-sdk/client-efs @aws-sdk/client-ec2` |
| Quotas and cost of the management dashboard ([Management dashboard](../funciones-opcionales/panel-de-gestion.md)) | (included: uses `boto3`) | `npm i @aws-sdk/client-service-quotas @aws-sdk/client-cost-explorer` |
| Custom domain, experimental ([Custom domain](../funciones-opcionales/dominio-propio.md)) | `pip install "rayito[custom-domain]"` | `npm i @aws-sdk/client-cloudformation @aws-sdk/client-cloudfront-keyvaluestore @aws-sdk/signature-v4a` |

In TypeScript those packages are optional *peerDependencies*: the SDK only
loads them when you enable the feature. If you enable it without installing
them, the first call throws `InvalidArgumentError` with the install command.

## Credentials { #credenciales }

The SDK uses the standard AWS credential chain: `boto3`'s in Python and the
AWS SDK v3's in TypeScript. With a profile:

```bash
export AWS_PROFILE=<your-profile> AWS_REGION=us-east-1
```

With AWS IAM Identity Center (SSO), sign in first with
`aws sso login --profile <your-profile>`; when the session expires, the SDK
raises an authentication error (see [Troubleshooting](../operacion/solucion-de-problemas.md)).

## Check the installation { #comprueba-la-instalacion }

=== "Python"

    ```bash
    python -c "import rayito; print(rayito.__version__)"
    ```

=== "TypeScript"

    ```bash
    node --input-type=module -e 'import { VERSION } from "rayito"; console.log(VERSION)'
    ```

## Running the examples { #ejecutar-los-ejemplos }

The examples in this documentation are complete programs: save them to a
file and run it.

=== "Python"

    ```bash
    python first.py
    ```

    Nothing else is needed: the example with `asyncio.run(...)` runs this
    way too.

=== "TypeScript"

    The examples use top-level `await` and `await using`, so the project has
    to be an ES module with a `tsconfig.json` that understands them. Once
    per project:

    ```bash
    npm init -y
    npm pkg set type=module            # "type": "module" in package.json
    pnpm add rayito                    # or: npm i rayito
    pnpm add -D tsx typescript @types/node
    ```

    And a minimal `tsconfig.json`:

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

    Save the example as `first.ts` and run it with
    [`tsx`](https://tsx.is/), which runs TypeScript without compiling:

    ```bash
    npx tsx first.ts
    npx tsc --noEmit                   # optional: check the types
    ```

    If you prefer to compile, `npx tsc` and then `node first.js` work with
    the same `tsconfig.json`.

## Next step { #siguiente-paso }

[Set up AWS](configurar-aws.md): the `rayito-base` image and the permissions
in your account.
