# rayito (SDK TypeScript)

Cliente TypeScript de Rayito: sandboxes de ejecución para agentes de IA sobre
AWS Lambda MicroVMs, dentro de tu propia cuenta. La misma superficie que el
SDK Python en `camelCase` y milisegundos, generada desde el mismo `.proto` y
validando los mismos límites. No hay servidor de terceros ni API key: el SDK
llama a la API de AWS con tus credenciales.

Documentación: **https://alejandro-cedeno-10.github.io/rayito/** ·
[Primeros pasos](https://alejandro-cedeno-10.github.io/rayito/primeros-pasos/) ·
[Referencia de TypeScript](https://alejandro-cedeno-10.github.io/rayito/referencia/typescript/) ·
[Changelog](https://github.com/alejandro-cedeno-10/rayito/blob/main/clients/typescript/CHANGELOG.md)

## Instalación

```bash
pnpm add rayito        # o: npm i rayito / yarn add rayito / bun add rayito
pip install "rayito[cli]"   # la CLI (publicar la imagen, rayito doctor) es Python
```

Node ≥ 20. El paquete trae ESM y CommonJS con sus tipos, y el shim de E2B en
`rayito/e2b`. Todo es asíncrono. Credenciales: la cadena por defecto del AWS
SDK v3 (`AWS_PROFILE`/`AWS_REGION`, variables de entorno o el rol de la
máquina).

`await using` requiere TypeScript ≥ 5.2 con `"lib": ["ES2022",
"ESNext.Disposable"]`; en Node 20.0–20.3 el paquete instala el polyfill de
`Symbol.asyncDispose`. Sin `await using`, `try { … } finally { await
sbx.kill() }` hace lo mismo. Cómo ejecutar los ejemplos con `tsx`:
[Instalación](https://alejandro-cedeno-10.github.io/rayito/primeros-pasos/instalacion/#ejecutar-los-ejemplos).

Las funciones opcionales cargan *peerDependencies* opcionales sólo cuando
las activas (por ejemplo `@aws-sdk/client-secrets-manager` para
`secrets`); si falta una, la primera llamada lanza `InvalidArgumentError`
con el comando de instalación. Lista completa en
[Extras opcionales](https://alejandro-cedeno-10.github.io/rayito/primeros-pasos/instalacion/#extras-opcionales).

## Antes del primer sandbox

Tu cuenta necesita una pila de IAM, un bucket y la imagen `rayito-base`
publicada con el `rayd` de la misma serie que el SDK. Se hace una vez por
cuenta y región: [Configurar AWS](https://alejandro-cedeno-10.github.io/rayito/primeros-pasos/configurar-aws/).
Después:

```bash
export AWS_PROFILE=<tu-perfil> AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1 RAYITO_TEMPLATE=rayito-base
rayito doctor --template rayito-base     # credenciales, cuotas, IAM, imagen, versión del agente
```

## Primer sandbox

```ts
import { Sandbox } from "rayito";

await using sbx = await Sandbox.create(); // `await using` mata el sandbox al salir
console.log((await sbx.commands.run("echo hola")).stdout); // "hola\n"
await sbx.files.write("/home/user/a.txt", "contenido");
console.log(await sbx.files.read("/home/user/a.txt")); // "contenido"
console.log((await sbx.runCode("x = 40; x + 2")).text); // "42"
```

Recorrido completo (comandos, ficheros, código y reconexión desde otro
proceso): [Primer sandbox](https://alejandro-cedeno-10.github.io/rayito/quickstart/).

## Si vienes de E2B

`rayito/e2b` es un drop-in a nivel de import del SDK JS de E2B 2.x. Lo que
Lambda MicroVMs no puede hacer lanza `UnimplementedError` en vez de
aproximarse en silencio. Exige una imagen 0.3.0 o posterior.

```ts
import { Sandbox } from "rayito/e2b"; // antes: import { Sandbox } from "@e2b/code-interpreter";

await using sbx = await Sandbox.create({ timeoutMs: 300_000, metadata: { run: "42" } });
console.log((await sbx.runCode("1 + 1")).text); // "2"
await sbx.setTimeout(600_000);
```

[Migrar desde E2B](https://alejandro-cedeno-10.github.io/rayito/migrar-desde-e2b/) ·
[Diferencias](https://alejandro-cedeno-10.github.io/rayito/e2b-compat/) ·
[Paridad](https://alejandro-cedeno-10.github.io/rayito/e2b-parity/)

## Un agente de código dentro del sandbox

`sbx.agent` corre un agente de código (OpenCode, o deepagents) **dentro**
del sandbox, sobre la imagen `rayito-agent` (`rayito agent template build`).
El agente llama a su modelo sólo a través de la pasarela de secretos: usa la
credencial, pero nunca puede leerla. La pasarela lee el secreto con el peer
opcional `@aws-sdk/client-secrets-manager`.

```ts
import { AgentModel, AgentSpec, Sandbox, SecretStore, bedrockGateway } from "rayito";

const MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0";
await new SecretStore().create("bedrock-key", "Bearer <clave de Bedrock de corta duración>");
const spec = new AgentSpec({
  model: new AgentModel({ provider: "bedrock", id: MODEL_ID, gateway: "bedrock", region: "us-east-1" }),
});

await using sbx = await Sandbox.create({
  template: "rayito-agent",
  allowInternetAccess: false,
  gateways: { bedrock: bedrockGateway("bedrock-key", { region: "us-east-1", models: [MODEL_ID] }) },
});
const result = await sbx.agent.run("Lista los ficheros de /home/user y resume qué hay.", { spec });
console.log(result.text, result.usage.total);
```

El arranque rápido (`warmup: agentPoolWarmup()` en el pool) es
opcional:
[¿Qué uso?](https://alejandro-cedeno-10.github.io/rayito/guias/agente-en-el-sandbox/#que-uso).
Guía: [Agente en el sandbox](https://alejandro-cedeno-10.github.io/rayito/guias/agente-en-el-sandbox/).

## Qué incluye

| Superficie | Guía |
|---|---|
| `Sandbox.create / connect / kill / list / getInfo`, metadatos | [Ciclo de vida](https://alejandro-cedeno-10.github.io/rayito/guias/ciclo-de-vida/) |
| `pause()` / `resume()`, `idle` | [Pausar y reanudar](https://alejandro-cedeno-10.github.io/rayito/guias/pausar-reanudar/) |
| `timeoutMs`, `maxLifetimeMs`, `onTimeout`, `setTimeout()` | [Plazo del servidor](https://alejandro-cedeno-10.github.io/rayito/lifecycle/) |
| `sbx.commands` | [Comandos](https://alejandro-cedeno-10.github.io/rayito/guias/comandos/) |
| `sbx.runCode`, contextos de código | [Ejecutar código](https://alejandro-cedeno-10.github.io/rayito/guias/ejecutar-codigo/) |
| `runCode(code, { language: "bash" / "javascript" / "typescript" })` | [Lenguajes y kernels](https://alejandro-cedeno-10.github.io/rayito/kernels/) |
| `sbx.pty` | [Terminal (PTY)](https://alejandro-cedeno-10.github.io/rayito/guias/terminal-pty/) |
| `sbx.files`, `transfer`, URLs firmadas | [Ficheros y S3](https://alejandro-cedeno-10.github.io/rayito/files/) |
| `sbx.getHost(port)` | [Puertos y host](https://alejandro-cedeno-10.github.io/rayito/guias/puertos-y-host/) |
| `network`, `allowInternetAccess: false` | [Red saliente](https://alejandro-cedeno-10.github.io/rayito/network/) |
| `SandboxPool` | [Pool](https://alejandro-cedeno-10.github.io/rayito/pool/) |
| `persist`, `reincarnate()` | [Persistencia](https://alejandro-cedeno-10.github.io/rayito/persistence/) |
| `getMetricsHistory()`, `Sandbox.paginate()` | [Métricas y listado](https://alejandro-cedeno-10.github.io/rayito/observability/) |
| `sbx.git` | [Git](https://alejandro-cedeno-10.github.io/rayito/git/) |
| `sbx.agent`, `AgentSpec`, presets de pasarela | [Agente en el sandbox](https://alejandro-cedeno-10.github.io/rayito/guias/agente-en-el-sandbox/) |
| Vercel AI SDK | [LangChain y Vercel AI](https://alejandro-cedeno-10.github.io/rayito/guias/langchain-y-vercel-ai/) |

Cada llamada de ciclo de vida acepta `signal` (`AbortSignal`), y un `logger`
opcional recibe ids, estados y duraciones; nunca salida, ficheros, código,
tokens ni cabeceras. Los errores siguen los nombres del SDK JS de E2B
(`SandboxError`, `CommandExitError`, `TimeoutError`…):
[Errores](https://alejandro-cedeno-10.github.io/rayito/referencia/errores/).

## Funciones opcionales, apagadas por defecto

Cada una crea recursos o hace llamadas facturables en tu cuenta, así que sólo
se activa con una opción explícita; sin ella no se carga su peer ni se hace
ninguna llamada a ese servicio. Qué activa, qué cuesta, qué IAM necesita y
cómo apagarla: [Funciones opcionales](https://alejandro-cedeno-10.github.io/rayito/optional-features/).

| Opción | Función |
|---|---|
| `secrets`, `SecretStore` | [Secretos](https://alejandro-cedeno-10.github.io/rayito/secrets/) de Secrets Manager como variables de entorno |
| `gateways`, `SecretGateway` | [Pasarela de secretos](https://alejandro-cedeno-10.github.io/rayito/funciones-opcionales/pasarela-de-secretos/): usar un secreto sin poder leerlo |
| `mounts`, `S3Mount` | [Montajes S3](https://alejandro-cedeno-10.github.io/rayito/funciones-opcionales/montajes-s3/) |
| `size` | [Tamaños](https://alejandro-cedeno-10.github.io/rayito/funciones-opcionales/tamanos/) de 512 MiB a 8 GiB |
| `events`, `LifecycleEvents` | [Eventos y webhooks](https://alejandro-cedeno-10.github.io/rayito/funciones-opcionales/eventos-y-webhooks/) |
| `telemetry`, `TelemetryExport` | [Exportación OTLP](https://alejandro-cedeno-10.github.io/rayito/funciones-opcionales/exportacion-otlp/) |
| `tracerProvider` | [OpenTelemetry (SDK)](https://alejandro-cedeno-10.github.io/rayito/funciones-opcionales/opentelemetry/) |
| `index: new DynamoDbIndex(...)` | [Índice de metadatos](https://alejandro-cedeno-10.github.io/rayito/funciones-opcionales/indice-de-metadatos/) |
| `Template.build()` | [Templates declarativos](https://alejandro-cedeno-10.github.io/rayito/funciones-opcionales/templates/) |
| `AgentTemplate` | [Templates de agente](https://alejandro-cedeno-10.github.io/rayito/funciones-opcionales/templates-de-agente/) |
| `OptionalStacks` | [Pilas opcionales](https://alejandro-cedeno-10.github.io/rayito/funciones-opcionales/pilas-opcionales/) |
| `volumes`, `VolumeStore` (experimental) | [Volúmenes EFS](https://alejandro-cedeno-10.github.io/rayito/funciones-opcionales/volumenes-efs/) |
| `domain`, `CustomDomain` (experimental) | [Dominio propio](https://alejandro-cedeno-10.github.io/rayito/funciones-opcionales/dominio-propio/) |

## Costes y límites

Un sandbox factura el cómputo de Lambda MicroVMs mientras corre, más el
almacenamiento de cada versión de imagen; Rayito no cobra nada. Precios,
ejemplos y tiempos medidos: [Precios](https://alejandro-cedeno-10.github.io/rayito/cost/).
Vida máxima, tamaños de payload y la tabla de compatibilidad SDK ↔ `rayd`:
[Límites](https://alejandro-cedeno-10.github.io/rayito/limits/).

## Desarrollo

```bash
cd clients/typescript
pnpm install --frozen-lockfile
pnpm lint        # biome check .
pnpm typecheck   # tsc --noEmit
pnpm build       # tsdown: dist/index.{mjs,cjs,d.mts,d.cts}
pnpm test        # vitest, proyecto unit (rayd falso en loopback + plano de control falso)
pnpm pack:check  # empaqueta y lista el tarball (README, LICENSE, dist/*)
RAYITO_E2E=1 RAYITO_TEMPLATE=<arn-o-nombre> pnpm test:e2e   # AWS real
```

El código generado vive en `src/gen/rayito/v1/*_pb.ts` y se regenera desde la
raíz del repo con `make proto` (`buf generate`, plugin remoto
`buf.build/bufbuild/es:v2.15.0`). Si el BSR no es accesible desde tu máquina:
`pnpm add -D @bufbuild/protoc-gen-es@2.15.0` y un `buf.gen.local.yaml` con
`plugins: [{ local: ["pnpm", "--dir", "clients/typescript", "exec",
"protoc-gen-es"], out: clients/typescript/src/gen, opt: [target=ts,
import_extension=js] }]`; `buf generate --template buf.gen.local.yaml`
produce exactamente los mismos ficheros (misma versión del plugin). Los
límites de la API (`src/limits.ts`) se generan con `make limits` desde
`limits.json`; `tests/unit/limits.test.ts` falla si alguien los edita a mano.

Variables del e2e: `RAYITO_E2E=1` y `RAYITO_TEMPLATE` (obligatorias),
`AWS_PROFILE`/`AWS_REGION` (cadena por defecto del SDK) y
`RAYITO_EXECUTION_ROLE_ARN` (activa `logging: "cloudwatch"`). La suite
termina todo lo que crea.

## Licencia y marcas

[Apache-2.0](https://github.com/alejandro-cedeno-10/rayito/blob/main/LICENSE);
las atribuciones de terceros están en el fichero `NOTICE` incluido en el
paquete. E2B es una marca de su titular: Rayito es un proyecto independiente,
no afiliado, patrocinado ni respaldado por E2B, y usa el nombre sólo para
describir la compatibilidad de API del shim. Contribuir:
[`CONTRIBUTING.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/CONTRIBUTING.md);
seguridad: [`SECURITY.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/SECURITY.md).
