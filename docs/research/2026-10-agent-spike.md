# Spike de agentes: OpenCode y deepagents dentro de un sandbox

Fase 0 de la investigación de integración de agentes, 2026-10-05. Objetivo:
comprobar que [OpenCode](https://github.com/anomalyco/opencode) y
[deepagents](https://github.com/langchain-ai/deepagents) corren **dentro**
de un sandbox de Rayito construido desde **un solo template**, llamando a un
modelo a través de la [pasarela de
secretos](../site/docs/funciones-opcionales/pasarela-de-secretos.md) (el
agente usa la credencial pero no puede leerla) y con el egress cerrado
(deny-all de `rayito-base-caps`), y tomar los primeros números. No añade
API de producto: eso es la fase 1.

**Resultado: GO para la fase 1.** Los dos agentes funcionan en local
(Docker + Floci) y en AWS real con el mismo template: responden, usan sus
herramientas sobre el sistema de ficheros del sandbox y llaman a Claude en
Amazon Bedrock sólo por la pasarela. Ningún proceso del sandbox ve la clave
(ni en su entorno, ni en el de `rayd`, ni en un fichero) y ninguno sale a
Internet por su cuenta. El coste a vigilar en la fase 1 es el **primer
`exec` de OpenCode en cada VM** (mediana 6-11 s, ver
[Números en AWS real](#números-en-aws-real)) y su memoria (≈ 575 MiB de RSS).

## Qué se instaló

| Pieza | Versión | Cómo | Tamaño |
|---|---|---|---|
| OpenCode (MIT) | 1.18.34 (2026-09-30) | `opencode-linux-arm64.tar.gz` de la release (glibc), sha256 `bbdb3f00c2c51e42e315525233151309724226a8776da8e9145e3b0fa3d5310f` (el `digest` que GitHub publica del asset) | binario único de 185 MB |
| ripgrep (MIT/Unlicense) | 15.2.0 | `ripgrep-15.2.0-aarch64-unknown-linux-gnu.tar.gz`, sha256 `a740b91c82eaf9914cfedd353572f2791cbe0162c84101ee0951058f4dcbc90d` | 6 MB |
| deepagents (MIT) | 0.7.22 (`>=0.7,<0.8`) | venv propio en `/opt/agents/deepagents` (Python 3.12 de la imagen), 63 pines con hash en `dev/local/agent/requirements-deepagents.txt`, `--require-hashes --no-deps --only-binary=:all:` | venv de 409 MB |
| langchain-aws (MIT) | 1.8.0 | en el mismo venv: `ChatBedrockConverse` | (incluido) |

OpenCode necesita `rg` para sus herramientas de búsqueda y, si no lo
encuentra, intenta descargarlo: bajo deny-all eso falla, así que va
preinstalado. Las variables `OPENCODE_DISABLE_AUTOUPDATE`,
`OPENCODE_DISABLE_MODELS_FETCH`, `OPENCODE_DISABLE_LSP_DOWNLOAD`,
`OPENCODE_DISABLE_DEFAULT_PLUGINS` y `OPENCODE_PURE` (todas a `1`) apagan
todo lo que OpenCode bajaría de Internet al arrancar; el catálogo de
modelos de models.dev va embebido en el binario, así que
`amazon-bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0` se resuelve
sin red. El proveedor de Bedrock de OpenCode y su SDK de AI van también
dentro del binario: no instala nada de npm.

deepagents arrastra `langchain-anthropic` y `langchain-google-genai` (con
`numpy`, `cryptography`, `google-genai`), de ahí los 409 MB del venv. La
fase 1 puede recortarlo si deepagents deja de exigirlos.

## Cómo llega el modelo: Bedrock por la pasarela

- **Credencial**: una clave de API de Bedrock **de corta duración**. Es una
  URL de `bedrock:CallWithBearerToken` prefirmada en local con SigV4 a
  partir de las credenciales de quien llama (el mismo algoritmo que el
  paquete `aws-bedrock-token-generator` de AWS;
  `dev/local/agent/mint_bedrock_key.py`): no llama a AWS, no crea ningún
  recurso, vale como mucho 12 h y nunca más que la sesión que la firmó. Las
  claves de larga duración crean credenciales de servicio sobre un usuario
  IAM: el spike no las necesita ni las creó.
- **Secreto**: `Bearer <clave>` en Secrets Manager (`SecretStore`; en local,
  el emulado por Floci).
- **Pasarela**: `SecretGateway(upstream="https://bedrock-runtime.us-east-1.amazonaws.com",
  headers={"authorization": <secreto>}, allow=[("POST", "/model/*")])`.
  `/model/*` cubre `Converse`, `ConverseStream`, `InvokeModel` e
  `InvokeModelWithResponseStream`; el `:` del id del modelo pasa la regla
  de rutas de `rayd`.
- **OpenCode**: `opencode.json` con
  `provider.amazon-bedrock.options.endpoint = sbx.gateways["bedrock"].url`.
  OpenCode lo pasa como `baseURL` al SDK de AI, que **acepta
  `http://127.0.0.1:<puerto>`** (era el punto sin verificar). Con
  `AWS_BEARER_TOKEN_BEDROCK` a un marcador (`placeholder-not-a-secret`),
  OpenCode elige la autenticación bearer y no busca credenciales de AWS; la
  pasarela quita esa cabecera `authorization` y pone la real.
- **deepagents**: `ChatBedrockConverse(model=..., endpoint_url=<url de la
  pasarela>)` con el mismo marcador en `AWS_BEARER_TOKEN_BEDROCK`: botocore
  1.43 firma con bearer y acepta un `endpoint_url` `http://`.

## Qué se comprobó

El driver es `dev/local/agent/spike.py` (local) y el mismo código, llamado
desde un script con el template de AWS, en la nube. Dentro de **un único
sandbox** con la pasarela y `allow_internet_access=False`:

| Comprobación | Local | AWS |
|---|---|---|
| `get_health().egress_enforcement` | `guest_routes` | `guest_routes` |
| OpenCode: `opencode run --format json --auto` lista el directorio y crea `saludo.txt` (herramientas `read` y `write`) | 3/3 | 5/5 |
| deepagents: `create_deep_agent` con `LocalShellBackend` escribe `informe.txt` con el texto pedido (herramientas `write_file` y `ls`) | 1/1 | 3/3 |
| La clave en el entorno del proceso del sandbox | no | no |
| `/proc/1/environ`, `/proc/1/mem` y `/proc/1/fd` de `rayd` (PID 1) legibles como `user` | no | no |
| Ficheros legibles que contienen la clave (`grep -r` en todo `/` salvo `/proc`, `/sys`, `/dev`), antes y después de los agentes | 0 | 0 |
| `curl https://example.com` y `curl https://bedrock-runtime...` directos | fallan (exit 56) | fallan (exit 56) |
| `curl --noproxy '*' https://1.1.1.1` (IP literal, sin DNS ni proxy) | falla (exit 7) | falla (exit 7) |
| `getent hosts example.com` | no resuelve | no resuelve |

Bajo deny-all, `rayd` además exporta `HTTP_PROXY`, `HTTPS_PROXY`,
`ALL_PROXY` y `NO_PROXY` hacia su proxy local, que rechaza el `CONNECT` con
403 (de ahí el exit 56); la ruta directa la cortan las reglas por uid (exit
7). `NO_PROXY` incluye el loopback, y OpenCode (Bun) y botocore lo
respetan: por eso llegan a la pasarela. Un agente que ignore `NO_PROXY`
fallaría contra el proxy; la fase 1 debe probarlo por runtime.

La comprobación de lectura busca la clave sólo para decir sí o no; ningún
informe ni log del spike contiene el valor.

## Números en AWS real

Cuenta de pruebas, us-east-1, 2026-10-05. Template `rayito-acc071spk-agent`
compuesto con el DSL de `Template` sobre una `rayito-base-caps` desechable
(el `rayito-image.zip` firmado de la release `rayd-v0.7.0`), tamaño por
defecto (2048 MB). Modelo: perfil de inferencia de sistema
`us.anthropic.claude-haiku-4-5-20251001-v1:0`. Medianas con el rango entre
paréntesis.

| Métrica | n | Resultado |
|---|---|---|
| Build del template (`Template.build`) | 1 | 277 s |
| Snapshot: `codeInstallSizeInBytes` | 1 | 1 447 485 440 → 2 058 858 496 B (**+611 MB**) |
| Snapshot: `memorySnapshotSizeInBytes` | 1 | 915 988 480 → 911 777 792 B (sin cambio: no se calienta nada) |
| Snapshot: `diskSnapshotSizeInBytes` | 1 | 36 954 112 → 35 971 072 B |
| `Sandbox.create()` (sin pasarela) | 5 | 8,7 s (6,5–13,7) |
| Primer `opencode --version` tras `create()` | 5 | **11,3 s (4,6–43,8)** |
| `create()` → `opencode --version` terminado | 5 | 17,8 s (13,5–57,5) |
| `pool.take()` (pool de 2 caliente) | 5 | 0,68 s (0,67–0,85) |
| Primer `opencode --version` tras `take()` | 5 | **6,2 s (4,5–16,6)** |
| `take()` → `opencode --version` terminado | 5 | 6,9 s (5,2–17,3) |
| Segundo `opencode --version` en el mismo VM | 5 | 0,54 s (0,53–0,67) |
| `true` en el mismo VM | 5 | 0,11 s |
| `create()` con pasarela y deny-all | 1 | 9,6 s |
| Primer token, `ConverseStream` por la pasarela desde el sandbox | 5 | **0,57 s (0,57–0,61)** (local: 0,70 s) |
| `opencode run` (listar + escribir + responder), total | 5 | 5,2 s (4,9–5,3) |
| `opencode run`, primer evento de texto | 5 | 4,2 s (3,6–4,3) |
| RSS máximo de `opencode run` | 5 | **575 MiB** (573–585) |
| deepagents: proceso completo / `agent.invoke` | 3 | 3,8 s / 1,8 s |
| RSS máximo de deepagents | 3 | 135 MiB |

El primer `exec` de OpenCode es lo único lento: la segunda ejecución y un
`true` son inmediatos, así que todo apunta a la primera lectura del binario
de 185 MB desde el disco de código del VM restaurado (`AWS_API_NOTES.md`
Q142). Un segundo template, idéntico pero con un `set_start_cmd` que lee el
binario y corre `opencode --version` antes del snapshot, sube el snapshot
de memoria a 1 237 110 784 B (+325 MB) y deja ese primer `exec` en una
mediana de 7,6 s (1,6–17,8) tras `create()`: mejor, pero con n=5 y esta
dispersión no concluyente (Q143).

El guest de 2048 MB ve 8016 MiB y 4 CPU (`free -m`, `nproc`; §16 ya
documenta que el guest ve más memoria de la configurada), así que los
575 MiB de OpenCode caben con holgura.

## Bloqueos y avisos

- **Ninguno bloquea la fase 1.** La cuenta de pruebas ya tenía acceso a los
  modelos de Anthropic en Bedrock (`ListFoundationModels` y un `Converse`
  firmado con SigV4 respondieron); en otra cuenta, el mantenedor tiene que
  habilitar el acceso al modelo antes, y Rayito no debe hacerlo por él.
- **Primer `exec` de OpenCode** (Q142): la fase 1 tiene que medir un
  OpenCode residente (`opencode serve` arrancado por `set_start_cmd`, con
  `opencode run --attach`) antes de fijar el diseño; si no baja, el
  `sbx.agent()` de un pool debería pagar ese primer `exec` al calentar la
  plaza, no al tomarla.
- **Caducidad de la clave**: la clave de corta duración muere con la sesión
  que la firmó (≤ 12 h). Un agente largo necesita rotarla con
  `sbx.gateways.refresh()` (mismo puerto, valor nuevo) o una credencial
  propia del servicio.
- **Un `upstream` que refleje la petición** entregaría la clave
  ([Lo que la pasarela no puede impedir](../site/docs/funciones-opcionales/pasarela-de-secretos.md#lo-que-la-pasarela-no-puede-impedir)):
  `bedrock-runtime` no lo hace en `/model/*`.
- **Permisos de los agentes**: `opencode run --auto` y `LocalShellBackend`
  ejecutan lo que el modelo pida sin preguntar. No son una frontera de
  seguridad; la frontera es el MicroVM más el deny-all.
- En la cuenta compartida desaparecieron a mitad de la corrida dos objetos
  que el spike había subido a S3 (el zip de la base y el del primer
  template), ninguno de ellos borrado por el spike; se volvió a subir el de
  la base para el segundo build. El ciclo de vida del bucket sólo expira
  versiones no actuales: todo apunta a la limpieza de otra corrida
  concurrente.

## Go/no-go para la fase 1

**GO** para `sbx.agent()` con `runtime="opencode" | "deepagents"` sobre un
único template, con estas condiciones de diseño:

1. Template `rayito-agent` sobre `rayito-base-caps`, con la receta de abajo
   (sin caché de capas: cada cambio de pin es un build de ≈ 5 min).
2. La credencial del modelo sólo por la pasarela, nunca por `envs=`:
   Bedrock con clave bearer en `authorization` y `allow=[("POST",
   "/model/*")]`; la API de Anthropic directa (`x-api-key`) encaja igual.
3. Egress en deny-all por defecto (`allow_internet_access=False`), con la
   pasarela como única salida.
4. Resolver antes del diseño final el primer `exec` (Q142/Q143) y probar
   `NO_PROXY` en cada runtime.
5. Tamaño mínimo 2048 MB para OpenCode.

## Receta del template

Con el DSL de `Template` (Python; la de TypeScript es la misma cadena con
`camelCase`), y `requirements-deepagents.txt` en el `context_dir`:

```python
from pathlib import Path

from rayito import Template

OPENCODE_VERSION = "1.18.34"
OPENCODE_SHA256 = "bbdb3f00c2c51e42e315525233151309724226a8776da8e9145e3b0fa3d5310f"
RIPGREP_VERSION = "15.2.0"
RIPGREP_SHA256 = "a740b91c82eaf9914cfedd353572f2791cbe0162c84101ee0951058f4dcbc90d"
RG_DIR = f"ripgrep-{RIPGREP_VERSION}-aarch64-unknown-linux-gnu"

agent = (
    Template()
    .from_base_image("rayito-base-caps")
    .run_cmd(
        "curl -fsSL --retry 3 -o /tmp/opencode.tar.gz "
        f"https://github.com/anomalyco/opencode/releases/download/v{OPENCODE_VERSION}/opencode-linux-arm64.tar.gz"
        f" && echo '{OPENCODE_SHA256}  /tmp/opencode.tar.gz' | sha256sum -c -"
        " && curl -fsSL --retry 3 -o /tmp/ripgrep.tar.gz "
        f"https://github.com/BurntSushi/ripgrep/releases/download/{RIPGREP_VERSION}/{RG_DIR}.tar.gz"
        f" && echo '{RIPGREP_SHA256}  /tmp/ripgrep.tar.gz' | sha256sum -c -"
        " && mkdir -p /opt/agents/bin"
        " && tar -xzf /tmp/opencode.tar.gz -C /opt/agents/bin opencode"
        f" && tar -xzf /tmp/ripgrep.tar.gz -C /tmp && mv /tmp/{RG_DIR}/rg /opt/agents/bin/rg"
        f" && rm -rf /tmp/opencode.tar.gz /tmp/ripgrep.tar.gz /tmp/{RG_DIR}"
        " && chown -R root:root /opt/agents"
        " && chmod 0755 /opt/agents /opt/agents/bin /opt/agents/bin/opencode /opt/agents/bin/rg"
        " && ln -sf /opt/agents/bin/opencode /usr/local/bin/opencode"
        " && ln -sf /opt/agents/bin/rg /usr/local/bin/rg"
    )
    .copy("requirements-deepagents.txt", "/opt/agents/requirements-deepagents.txt")
    .run_cmd(
        "python3 -m venv /opt/agents/deepagents"
        " && /opt/agents/deepagents/bin/python -m pip install --no-cache-dir"
        " --require-hashes --no-deps --only-binary=:all:"
        " -r /opt/agents/requirements-deepagents.txt"
        " && /opt/agents/deepagents/bin/python -m pip check"
        " && chown -R root:root /opt/agents && chmod -R a+rX,go-w /opt/agents"
    )
    .set_envs({
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "OPENCODE_DISABLE_MODELS_FETCH": "1",
        "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
        "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
        "OPENCODE_PURE": "1",
    })
    .run_cmd(
        "su user -c 'opencode --version' && su user -c 'rg --version'"
        " && su user -c \"/opt/agents/deepagents/bin/python -c 'import deepagents, langchain_aws'\""
    )
)
Template.build(agent, "rayito-agent", bucket="amzn-s3-demo-bucket",
               context_dir=Path("dev/local/agent"))
```

Todo queda de root y `0755`: `user` (uid 1000) no puede reemplazar los
binarios ni el venv. El build necesita salida a Internet (GitHub y PyPI);
el sandbox que nace del template, no.

Configuración de OpenCode dentro del sandbox (`OPENCODE_CONFIG` apuntando a
este fichero) y el entorno de cada `opencode run`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "model": "amazon-bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0",
  "autoupdate": false,
  "share": "disabled",
  "provider": {
    "amazon-bedrock": {
      "options": {"region": "us-east-1", "endpoint": "http://127.0.0.1:<puerto de la pasarela>"}
    }
  }
}
```

```text
HOME=/home/user
OPENCODE_CONFIG=/home/user/work/opencode.json
AWS_BEARER_TOKEN_BEDROCK=placeholder-not-a-secret
AWS_REGION=us-east-1
(+ las cinco OPENCODE_* del template)
```

## Cómo repetirlo

En local (Docker arm64; sólo el `Converse` de Bedrock sale a AWS, con una
clave de corta duración de tu sesión):

```bash
make local-up LOCAL_RAYD_BIN=<ruta al rayd de la release o de make build>
docker compose -f dev/local/compose.yaml -f dev/local/agent/compose.yaml \
  up -d --build --wait guest
AWS_PROFILE=<tu-perfil> uv run --project clients/python \
  python dev/local/agent/mint_bedrock_key.py \
  | docker compose -f dev/local/compose.yaml -f dev/local/agent/compose.yaml \
      exec -T runner bash -c \
      'cd /src/clients/python && uv run --no-sync python /src/dev/local/agent/spike.py'
make local-down
```

El guest de `dev/local/agent/compose.yaml` es el de `make local-up` más los
dos agentes y `CAP_NET_ADMIN`, para que `rayd` aplique el deny-all de
verdad como en `rayito-base-caps` (las reglas son por uid, así que ni el
runner, uid 993, ni `rayd`, root, quedan afectados). La imagen pesa 1,56 GB
frente a 0,96 GB del guest base (OpenCode y ripgrep 190 MB, el venv de
deepagents 409 MB).

En AWS se usaron imágenes desechables con el prefijo `rayito-acc071spk-`
(la base caps, el template y su variante calentada), un secreto de Secrets
Manager durante la corrida y 18 MicroVMs de pocos minutos. Coste
estimado: ≈ $0,12 de almacenamiento de snapshots (tres versiones, mínimo
una semana) más céntimos de cómputo, Bedrock y Secrets Manager; menos de
$0,30. Inventario antes y después sin diferencias atribuibles al spike.
