# Rayito

[![CI](https://github.com/alejandro-cedeno-10/rayito/actions/workflows/ci.yml/badge.svg)](https://github.com/alejandro-cedeno-10/rayito/actions/workflows/ci.yml) [![PyPI](https://img.shields.io/pypi/v/rayito)](https://pypi.org/project/rayito/) [![npm](https://img.shields.io/npm/v/rayito)](https://www.npmjs.com/package/rayito) [![Licencia](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE) [![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/alejandro-cedeno-10/rayito/badge)](https://scorecard.dev/viewer/?uri=github.com/alejandro-cedeno-10/rayito) [![Docs](https://img.shields.io/badge/docs-sitio-amber)](https://alejandro-cedeno-10.github.io/rayito/)
<!-- OpenSSF Best Practices: tras registrar el proyecto (docs/research/openssf-badge-answers.md), añade este badge al final de la línea anterior con el id del proyecto:
[![OpenSSF Best Practices](https://www.bestpractices.dev/projects/<id>/badge)](https://www.bestpractices.dev/projects/<id>)
-->

Sandboxes de ejecución para agentes de IA, aislados por hardware, corriendo en tu
propia cuenta de AWS sobre Lambda MicroVMs.

> Sandboxes que aparecen en un destello. Dentro de tu propia cuenta de AWS.

Documentación completa: **https://alejandro-cedeno-10.github.io/rayito/**

**Empieza aquí** (unos 15 minutos, en tu cuenta):
[Primeros pasos](https://alejandro-cedeno-10.github.io/rayito/primeros-pasos/)
→ [Configurar AWS](https://alejandro-cedeno-10.github.io/rayito/primeros-pasos/configurar-aws/)
→ [Primer sandbox](https://alejandro-cedeno-10.github.io/rayito/quickstart/).
¿Vienes de E2B? [Migrar desde E2B](https://alejandro-cedeno-10.github.io/rayito/migrar-desde-e2b/).
Una guía por función en [Guías](https://alejandro-cedeno-10.github.io/rayito/guias/) y la API de Python, TypeScript
y la CLI en [Referencia](https://alejandro-cedeno-10.github.io/rayito/referencia/).

La ergonomía de E2B sin que el código de tus clientes salga de tu cuenta, y sin
clúster que operar: el SDK habla directamente con la API de Lambda MicroVMs.

## Instalar

```bash
pip install "rayito[cli]"   # SDK Python (Python >= 3.11) y la CLI `rayito`
pnpm add rayito             # SDK TypeScript (Node >= 20); o: npm i rayito / yarn add rayito / bun add rayito
```

La CLI (`rayito image publish`, `rayito doctor`…) es Python aunque uses el
SDK de TypeScript. Extras de Python: `cli`, `mcp` (servidor MCP), `otel`
(trazas OpenTelemetry) y `custom-domain` (dominio propio, experimental); las
funciones opcionales de TypeScript cargan *peerDependencies* opcionales
([Instalación](https://alejandro-cedeno-10.github.io/rayito/primeros-pasos/instalacion/#extras-opcionales)).

## Primer sandbox

Con la imagen `rayito-base` publicada en tu cuenta
([Empezar en tu cuenta de AWS](#empezar-en-tu-cuenta-de-aws)) y
`AWS_PROFILE`, `AWS_REGION` y `RAYITO_TEMPLATE=rayito-base` exportadas:

```python
from rayito import Sandbox

with Sandbox.create() as sbx:  # `with` mata el sandbox al salir
    print(sbx.commands.run("echo hola").stdout)  # "hola\n"
    sbx.files.write("/home/user/data.csv", "a,b\n1,2\n")
    print(sbx.run_code("import pandas as pd; pd.read_csv('/home/user/data.csv').shape").text)  # "(1, 2)"
```

```ts
import { Sandbox } from "rayito";

await using sbx = await Sandbox.create(); // `await using` mata el sandbox al salir
console.log((await sbx.commands.run("echo hola")).stdout); // "hola\n"
console.log((await sbx.runCode("x = 40; x + 2")).text); // "42"
```

Si vienes del SDK de E2B, `rayito.e2b` (Python) y `rayito/e2b` (TypeScript)
son un drop-in a nivel de import de E2B 2.x: lo que Lambda MicroVMs no puede
hacer lanza `UnimplementedError` en vez de aproximarse en silencio
([Diferencias con E2B](https://alejandro-cedeno-10.github.io/rayito/e2b-compat/)). El shim exige una imagen 0.3.0 o
posterior.

```python
from rayito.e2b import Sandbox  # antes: from e2b_code_interpreter import Sandbox

with Sandbox.create(timeout=300, metadata={"run": "42"}) as sbx:
    print(sbx.run_code("1 + 1").text)  # "2"
    sbx.set_timeout(600)  # rayd mueve el plazo, hasta max_lifetime
```

## Un agente de código dentro del sandbox

`sbx.agent` corre un agente de código ([OpenCode](https://github.com/anomalyco/opencode),
o deepagents) **dentro** del sandbox. El agente llama a su modelo (Bedrock,
Anthropic o un endpoint compatible con OpenAI) sólo a través de la
[pasarela de secretos](https://alejandro-cedeno-10.github.io/rayito/funciones-opcionales/pasarela-de-secretos/): usa la
credencial, pero nunca puede leerla. Basta un `Sandbox.create()` normal sobre
la imagen `rayito-agent` (`rayito agent template build`,
[Templates de agente](https://alejandro-cedeno-10.github.io/rayito/funciones-opcionales/templates-de-agente/)):

```python
from rayito import AgentModel, AgentSpec, Sandbox, SecretStore, bedrock_gateway

MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
SecretStore().create("bedrock-key", "Bearer <clave de Bedrock de corta duración>")
spec = AgentSpec(model=AgentModel(provider="bedrock", id=MODEL_ID, gateway="bedrock", region="us-east-1"))

with Sandbox.create(
    template="rayito-agent",
    allow_internet_access=False,
    gateways={"bedrock": bedrock_gateway("bedrock-key", region="us-east-1", models=[MODEL_ID])},
) as sbx:
    result = sbx.agent.run("Lista los ficheros de /home/user y resume qué hay.", spec=spec)
    print(result.text, result.usage.total)
```

El arranque rápido (un pool con `warmup`) es
opcional; cuándo compensa, en
[¿Qué uso?](https://alejandro-cedeno-10.github.io/rayito/guias/agente-en-el-sandbox/#que-uso). Guía completa:
[Agente en el sandbox](https://alejandro-cedeno-10.github.io/rayito/guias/agente-en-el-sandbox/); qué cuesta la VM
frente a los tokens del modelo: [Precios](https://alejandro-cedeno-10.github.io/rayito/cost/#coste-de-un-agente-vm-frente-a-modelo).

## Qué incluye

Todo funciona igual en Python (sync y `asyncio`) y en TypeScript.

- **Ciclo de vida**: `create`, `connect`, `kill`, `list`, metadatos
  ([Ciclo de vida](https://alejandro-cedeno-10.github.io/rayito/guias/ciclo-de-vida/)); `pause()`/`resume()` con
  auto-suspensión por inactividad ([Pausar y reanudar](https://alejandro-cedeno-10.github.io/rayito/guias/pausar-reanudar/));
  un plazo que `rayd` impone aunque tu proceso muera
  ([Plazo del servidor](https://alejandro-cedeno-10.github.io/rayito/lifecycle/)).
- **Ejecutar**: `commands` ([Comandos](https://alejandro-cedeno-10.github.io/rayito/guias/comandos/)), `run_code` con un
  kernel Jupyter con estado ([Ejecutar código](https://alejandro-cedeno-10.github.io/rayito/guias/ejecutar-codigo/)),
  kernels bash, JavaScript y TypeScript ([Lenguajes y kernels](https://alejandro-cedeno-10.github.io/rayito/kernels/))
  y terminales reales ([Terminal (PTY)](https://alejandro-cedeno-10.github.io/rayito/guias/terminal-pty/)).
- **Ficheros y red**: `files`, ficheros grandes y URLs firmadas por S3
  ([Ficheros y S3](https://alejandro-cedeno-10.github.io/rayito/files/)); política de salida a internet de E2B
  ([Red saliente](https://alejandro-cedeno-10.github.io/rayito/network/)); HTTP a un puerto del sandbox
  ([Puertos y host](https://alejandro-cedeno-10.github.io/rayito/guias/puertos-y-host/)).
- **Arranque y duración**: sandboxes en menos de un segundo desde un pool de
  suspendidos ([Pool](https://alejandro-cedeno-10.github.io/rayito/pool/)); el `HOME` en S3 más allá de las 8 h
  ([Persistencia](https://alejandro-cedeno-10.github.io/rayito/persistence/)).
- **Observar**: métricas e historial, listado reanudable
  ([Métricas y listado](https://alejandro-cedeno-10.github.io/rayito/observability/)); la API git de E2B ([Git](https://alejandro-cedeno-10.github.io/rayito/git/)).
- **Agentes**: el agente dentro del sandbox (arriba); el servidor MCP para
  Claude Code, Cursor o VS Code ([Servidor MCP](https://alejandro-cedeno-10.github.io/rayito/mcp/)); adaptadores para
  LangChain y Vercel AI ([LangChain y Vercel AI](https://alejandro-cedeno-10.github.io/rayito/guias/langchain-y-vercel-ai/)).
- **CLI** `rayito`: publicar imágenes, `doctor`, operar sandboxes, pilas
  opcionales ([CLI](https://alejandro-cedeno-10.github.io/rayito/cli/)).
- **Probar en local** con Docker y Floci, sin cuenta de AWS
  ([Probar en local](https://alejandro-cedeno-10.github.io/rayito/guias/probar-en-local/)).
- **Funciones opcionales, apagadas por defecto** (cada una cuesta algo en
  tu cuenta y sólo se activa con una opción explícita): secretos de Secrets
  Manager, pasarela de secretos, montajes S3, tamaños de 512 MiB a 8 GiB,
  eventos y webhooks, exportación OTLP, trazas OpenTelemetry, índice de
  metadatos en DynamoDB, templates declarativos y de agente, proxy local,
  volúmenes EFS (experimental) y dominio propio (experimental). Qué activa y
  qué cuesta cada una: [Funciones opcionales](https://alejandro-cedeno-10.github.io/rayito/optional-features/).

## Cómo funciona

```
tu proceso (Python / TypeScript)                AWS, tu cuenta
┌───────────────────────────────┐   HTTPS/2    ┌──────────────────────────────┐
│ SDK rayito                    │ ───────────► │ proxy de Lambda MicroVMs     │
│  · boto3 / SDK JS: run-microvm│              │   ▼ h2c                      │
│  · gRPC: commands, files,     │              │ MicroVM: rayd → sidecar →    │
│    run_code, pty              │ ◄─────────── │   ipykernel (tu código aquí) │
└───────────────────────────────┘   streams    └──────────────────────────────┘
```

- **El SDK nunca corre dentro del sandbox.** Vive en tu proceso (tu app, tu
  agente, tu CI) y sólo envía RPCs: crea el MicroVM con la API
  `lambda-microvms` de tu cuenta y habla gRPC con `rayd`, el agente Rust que
  va dentro de la imagen. Nada del runtime de tu agente se copia a la VM.
- **Dónde corre tu código.** Lo que envías con `run_code` se ejecuta en un
  kernel de Jupyter dentro del MicroVM como uid 1000 (con estado entre
  celdas); `commands`, `pty` y `sbx.agent` lanzan procesos normales ahí
  mismo, también como uid 1000. `rayd` corre como root y es el único que
  toca los hooks de Lambda. Los kernels bash, JavaScript y TypeScript (Deno)
  viven en la variante de imagen `rayito-base-poly`
  ([Lenguajes y kernels](https://alejandro-cedeno-10.github.io/rayito/kernels/)).
- **Desde qué lenguajes.** Hoy, Python y TypeScript. Cualquier otro lenguaje
  con un cliente gRPC puede hablar con `rayd` generando el cliente desde
  `proto/rayito/v1/` ([Otros lenguajes (gRPC)](https://alejandro-cedeno-10.github.io/rayito/referencia/otros-lenguajes/)).

Detalle de procesos, usuarios, tokens y transportes en
[Conceptos](https://alejandro-cedeno-10.github.io/rayito/concepts/) y en `ARCHITECTURE.md` ("Qué corre dónde").

## Empezar en tu cuenta de AWS

Cuatro pasos, todos en tu propia cuenta (Rayito no tiene servidor ni API
key). La receta completa, con cada orden, está en
[Configurar AWS](https://alejandro-cedeno-10.github.io/rayito/primeros-pasos/configurar-aws/).

1. **IAM**: despliega los roles de build y ejecución y las políticas del
   cliente ([`infra/README.md`](infra/README.md)):

   ```bash
   aws cloudformation deploy --stack-name rayito-m0-iam \
     --template-file infra/iam.yaml --capabilities CAPABILITY_NAMED_IAM \
     --parameter-overrides ArtifactBucket=amzn-s3-demo-bucket LogGroupPrefix=/rayito
   ```

   Asigna a tu usuario la política del output `CallerPolicyArn`
   (`rayito-m0-caller-<región>`) y al rol de tu servicio, sólo la de
   `SandboxLauncherPolicyArn` ([IAM](https://alejandro-cedeno-10.github.io/rayito/operacion/iam/)).

2. **Imagen**: publica `rayito-base` desde el `rayito-image.zip` firmado de
   la release, sin compilar nada. Verifica la firma antes de publicar
   ([Verificar una release](https://alejandro-cedeno-10.github.io/rayito/verify/)):

   ```bash
   RAYD_VERSION=$(python -c "import rayito; print(rayito.__version__)")   # la del SDK instalado
   curl -fsSLO "https://github.com/alejandro-cedeno-10/rayito/releases/download/rayd-v${RAYD_VERSION}/rayito-image.zip"
   curl -fsSLO "https://github.com/alejandro-cedeno-10/rayito/releases/download/rayd-v${RAYD_VERSION}/rayito-image.zip.sigstore.json"
   cosign verify-blob --bundle rayito-image.zip.sigstore.json \
     --certificate-identity "https://github.com/alejandro-cedeno-10/rayito/.github/workflows/release.yml@refs/tags/rayd-v${RAYD_VERSION}" \
     --certificate-oidc-issuer https://token.actions.githubusercontent.com rayito-image.zip
   rayito image publish --artifact rayito-image.zip --base-image-version 1 --bucket amzn-s3-demo-bucket
   ```

   La imagen debe llevar el `rayd` de la misma versión que el SDK
   (`rayito doctor` lo comprueba). Las variantes `-caps` (red saliente y el
   agente) y `-poly` (kernels bash, JavaScript y TypeScript), en
   [Imágenes](https://alejandro-cedeno-10.github.io/rayito/images/). Desde el código fuente: `make image-publish
   BUCKET=amzn-s3-demo-bucket`, que compila `rayd` para
   `aarch64-unknown-linux-musl` en Linux o WSL2 (en macOS, dentro de una VM
   Linux) con `cargo-zigbuild` ([`CONTRIBUTING.md`](CONTRIBUTING.md)).

3. **Diagnóstico**: `rayito doctor --template rayito-base` comprueba
   credenciales, cuotas, IAM, bucket, imagen, versión del agente y
   compatibilidad SDK ↔ `rayd`; `--launch` prueba además un sandbox efímero.

4. **Primer sandbox**: [Primer sandbox](https://alejandro-cedeno-10.github.io/rayito/quickstart/).

## Coste y latencia

Un sandbox factura el cómputo de Lambda MicroVMs mientras corre, más el
almacenamiento de cada versión de imagen y de los sandboxes suspendidos; no
hay cargos de Rayito. Precios de lista, ejemplos, tiempos medidos y el
coste de un agente (VM frente a tokens del modelo), en
[Precios](https://alejandro-cedeno-10.github.io/rayito/cost/). Las mediciones de arranque en frío están en
[`docs/benchmarks/2026-09-cold-start.md`](docs/benchmarks/2026-09-cold-start.md).

## Estado

**Alfa, serie 0.8** ([Licencia, estado y soporte](#licencia-estado-y-soporte)).
Los SDK de Python y TypeScript y `rayd` avanzan en lockstep de
`MAJOR.MINOR`: cada SDK exige una imagen construida con el `rayd` de su
misma serie, y `rayito doctor` lo comprueba (tabla en
[Compatibilidad SDK ↔ rayd ↔ imagen](https://alejandro-cedeno-10.github.io/rayito/limits/#compatibilidad-sdk-rayd-imagen)). Lo que trae cada
versión: [Novedades](https://alejandro-cedeno-10.github.io/rayito/novedades/) y los `CHANGELOG.md` de cada componente.

## Documentos

| Fichero | Contenido |
|---|---|
| `SPEC.md` | Qué construimos, alcance, no-objetivos, decisiones |
| `ARCHITECTURE.md` | Las tres capas y los ADRs |
| `AWS_API_NOTES.md` | Superficie verificada de la API de AWS. **Leer antes de tocar `src/`** |
| `docs/aws-api/` | Apéndice crudo: modelo `service-2.json`, `help` de los 25 comandos, resumen de shapes |
| `MILESTONES.md` | Hitos y criterios de aceptación |
| `infra/` | Plantillas de CloudFormation: IAM de build, ejecución y cliente (`iam.yaml`), conector de egress, rol OIDC del e2e ([`infra/README.md`](infra/README.md)) |
| `CLAUDE.md` | Reglas para trabajar con Claude Code |
| `CONTRIBUTING.md` | Cómo contribuir: flujo OpenSpec, gates en Linux/WSL2, macOS (VM Linux) y Windows, e2e en tu cuenta, convenciones, DCO y commits firmados |
| `SECURITY.md` | Cómo reportar una vulnerabilidad, versiones soportadas y el modelo de amenazas |
| `SUPPORT.md` | Dónde pedir ayuda (documentación, `rayito doctor`, issues) |
| `CODE_OF_CONDUCT.md` | Contributor Covenant 3.0 |
| `GOVERNANCE.md` | Quién decide y cómo (ADRs, cambios OpenSpec, mantenedores) |
| `LICENSE`, `NOTICE`, `CHANGELOG.md` | Apache-2.0, atribuciones de terceros y los changelogs por componente |
| `docs/RELEASING.md` | Pasos manuales de publicación (PyPI, npm, GitHub Release, tags) |
| `proto/rayito/v1/` | El contrato gRPC. Fuente de verdad de toda la API |

## Estructura del repositorio

```
proto/rayito/v1/         contrato gRPC (package rayito.v1), fuente de verdad
crates/rayd/             agente Rust (tonic) que corre dentro del MicroVM
crates/rayd-core/        dominio puro del agente (sin IO; puertos como traits)
crates/rayito-proto/     código Rust generado desde proto/
kernel-sidecar/          sidecar Python con jupyter_client (y Deno en -poly)
clients/python/          SDK Python (paquete `rayito`, shim `rayito.e2b`)
clients/typescript/      SDK TypeScript (paquete `rayito`, entrada `rayito/e2b`)
image/                   Dockerfile ARM64 de las imágenes
infra/                   plantillas de CloudFormation (IAM, egress, CI OIDC)
docs/site/               documentación de usuario (mkdocs)
docs/aws-api/            volcado crudo de la API de Lambda MicroVMs
openspec/                specs y changes archivados de cada hito
```

## Licencia, estado y soporte

- **Estado**: alfa (`Development Status :: 3 - Alpha`). Serie 0.x con
  [SemVer](https://semver.org/lang/es/): una MINOR puede romper
  compatibilidad y lo anuncia en el changelog; un PATCH nunca. Política de
  versionado, obsolescencia y soporte en
  [Versionado y soporte](docs/site/docs/limits.md#versionado-y-soporte).
- **Licencia**: [Apache-2.0](LICENSE). Las atribuciones de terceros (código
  vendorizado o adaptado) están en [`NOTICE`](NOTICE), que viaja con el
  wheel de PyPI y el paquete de npm.
- **Contribuir**: [`CONTRIBUTING.md`](CONTRIBUTING.md) (flujo OpenSpec,
  gates, DCO y commits firmados) y [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).
- **Ayuda y seguridad**: [`SUPPORT.md`](SUPPORT.md); las vulnerabilidades se
  reportan en privado según [`SECURITY.md`](SECURITY.md).
- **Marcas**: E2B es una marca de su titular. Rayito es un proyecto
  independiente, **no afiliado, patrocinado ni respaldado por E2B**; el
  nombre se usa sólo para describir la compatibilidad de API de
  `rayito.e2b` / `rayito/e2b`. AWS, Lambda y las demás marcas citadas
  pertenecen a sus titulares.

## In English

Rayito is an open-source (Apache-2.0) sandbox SDK for AI agents: Python
(`pip install rayito`) and TypeScript (`npm i rayito`) clients that run
hardware-isolated sandboxes on AWS Lambda MicroVMs in your own AWS account,
with no server or API key of ours in between. The in-VM agent `rayd` (Rust)
ships as a signed GitHub Release asset. `rayito.e2b` / `rayito/e2b` offer an
import-level drop-in for the E2B 2.x SDK; Rayito is an independent project,
not affiliated with or endorsed by E2B. The documentation and code comments
are in Spanish, but issues, pull requests and security reports in English
are welcome: see [`CONTRIBUTING.md`](CONTRIBUTING.md),
[`SUPPORT.md`](SUPPORT.md) and [`SECURITY.md`](SECURITY.md).
