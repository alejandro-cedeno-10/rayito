# Servidor MCP

`rayito.mcp` es un servidor [Model Context Protocol](https://modelcontextprotocol.io)
que da a cualquier host MCP (Claude Code, Claude Desktop, Cursor, VS Code, el
Inspector) un sandbox de Rayito con un comando de arranque y una variable de
entorno. Cada **proceso** del servidor posee **un** sandbox: se crea en la
primera herramienta que lo necesita, AWS lo suspende cuando pasa
`RAYITO_MCP_IDLE_SECONDS` sin tráfico (la siguiente llamada lo reanuda sola)
y se destruye (`terminate-microvm`) cuando el host cierra el servidor. El
coste es el de un MicroVM en ejecución mientras se usa y el de un snapshot
por ciclo de suspensión ([modelo de costes](cost.md)); nada queda vivo más
allá de `RAYITO_MCP_TIMEOUT_SECONDS`.

Para frameworks que hablan con herramientas propias y no con MCP (LangChain,
Vercel AI SDK) hay dos adaptadores de cincuenta líneas en
`docs/examples/langchain_tool.py` y `docs/examples/vercel_ai_tool.ts`.

## Instalación

```bash
pip install "rayito[mcp]"      # o: uv add "rayito[mcp]"
python -m rayito.mcp --help     # equivalente al script rayito-mcp
```

El extra instala el SDK oficial `mcp` 2.x. Hasta que el paquete esté en PyPI,
desde un checkout del repositorio:

```bash
uv run --project <repo>/clients/python --extra mcp rayito-mcp
```

Sin el extra, `import rayito.mcp` falla con un `ModuleNotFoundError` que
indica el comando de instalación; `import rayito` no cambia.

## Variables de entorno

Toda la configuración va por el entorno (los hosts MCP configuran servidores
con un bloque `env`); ninguna bandera de línea de comandos la duplica.

| Variable | Significado | Por defecto / validación |
|---|---|---|
| `RAYITO_TEMPLATE` | Nombre o ARN de la imagen (la misma variable que lee `Sandbox.create`) | Obligatoria para las herramientas que tocan el sandbox; no se valida al arrancar, la primera llamada que la necesita devuelve un error que la nombra |
| `RAYITO_TEMPLATE_VERSION` | Versión de la imagen | Última `ACTIVE` |
| `RAYITO_EXECUTION_ROLE_ARN` | Execution role del MicroVM; con rol, `logging="cloudwatch"` | Sin rol → `logging="disabled"` |
| `AWS_REGION` / `AWS_DEFAULT_REGION` / `AWS_PROFILE` y las variables de credenciales | Las lee boto3; el servidor no pasa región ni sesión propias | Defaults de boto3 |
| `RAYITO_MCP_TIMEOUT_SECONDS` | Vida máxima del sandbox (running + suspended) | `3600`; entero en `60..=28800`, si no el servidor sale con código 2 |
| `RAYITO_MCP_IDLE_SECONDS` | Segundos sin tráfico antes de que AWS suspenda el sandbox; `0` desactiva la auto-suspensión | `300`; `0` o entero `>= 60` y menor que `RAYITO_MCP_TIMEOUT_SECONDS` (con un timeout por debajo de 300 s hay que bajar el idle o ponerlo a `0`), si no el servidor sale con código 2 |
| `RAYITO_MCP_LOG_LEVEL` | Nivel del log (siempre por stderr) | `INFO`; uno de `DEBUG INFO WARNING ERROR CRITICAL` |

## Herramientas

Nombres de herramienta y de argumento en inglés y estables; descripciones en
español. Las salidas de texto se cortan a 100 000 caracteres con el sufijo
`… [truncado: <n> caracteres más]` y `truncated: true`.

| Herramienta | Argumentos | Devuelve |
|---|---|---|
| `run_code` | `code: str` (Python), `timeout: int = 300` (1–3600 s) | Bloques de contenido: un texto JSON (abajo), después una imagen por `png`/`jpeg` y un recurso embebido por `svg` |
| `run_command` | `cmd: str` (`/bin/sh -c` como el usuario del sandbox), `timeout: int = 60`, `cwd: str \| None` | `{stdout, stderr, exit_code, truncated}`; un exit code distinto de cero es dato, no error |
| `read_file` | `path: str` | `{path, content, size, truncated}` (texto UTF-8) |
| `write_file` | `path: str`, `content: str` | `{path, size}`; sobrescribe |
| `list_files` | `path: str = "/home/user"`, `depth: int = 1` (1–5) | `{path, entries: [{name, path, type, size, modified_time}]}` |
| `list_sandboxes` | — | `{sandboxes: [{sandbox_id, state, template, template_version, started_at, current}]}`; nunca crea un sandbox |

El primer bloque de `run_code` es siempre un JSON con esta forma:

```json
{
  "text": "42",
  "stdout": "hola\n",
  "stderr": "",
  "error": null,
  "execution_count": 3,
  "results": [{"index": 0, "mime_types": ["text/plain"]}],
  "truncated": false
}
```

`text` es el `text/plain` del resultado principal (`null` si no hay);
`error` es `{"name", "value", "traceback"}` cuando la celda lanzó una
excepción (o `ExecutionTimeout` cuando el agente la interrumpió) y llega con
`is_error: false`: la celda corrió y el modelo necesita la traza. `results`
lista los mime types de cada resultado aunque no se adjunten (`text/html`,
`e2b/chart`...); sólo PNG, JPEG y SVG viajan como bloques.

Los fallos que el modelo puede corregir llegan como error de herramienta
(`is_error: true`) con mensaje en español: `RAYITO_TEMPLATE` sin definir, un
`create` fallido (`no se pudo crear el sandbox: ...`; la siguiente llamada
reintenta), un timeout de `run_command`, un fichero inexistente, un sandbox
en plena suspensión o reanudación (`está en transición ...; reintenta en unos
segundos`: el sandbox sigue siendo el mismo) y un sandbox que expiró (`ya no
existe ...; la siguiente llamada crea uno nuevo`).

## Claude Code

```bash
claude mcp add rayito \
  -e RAYITO_TEMPLATE=rayito-base -e AWS_REGION=us-east-1 -e AWS_PROFILE=<perfil> \
  -- uvx --from "rayito[mcp]" rayito-mcp
```

Desde un checkout, sustituye el comando por
`uv run --project <repo>/clients/python --extra mcp rayito-mcp`.

## Claude Desktop

`claude_desktop_config.json` (`~/Library/Application Support/Claude/` en
macOS, `%APPDATA%\Claude\` en Windows):

```json
{
  "mcpServers": {
    "rayito": {
      "command": "uvx",
      "args": ["--from", "rayito[mcp]", "rayito-mcp"],
      "env": {
        "RAYITO_TEMPLATE": "rayito-base",
        "AWS_REGION": "us-east-1",
        "AWS_PROFILE": "<perfil>"
      }
    }
  }
}
```

Claude Desktop sólo relee el fichero al arrancar: ciérralo del todo (no sólo
la ventana) y vuelve a abrirlo. El sandbox se destruye cuando Claude Desktop
cierra el servidor.

## Cursor y VS Code

Cursor, `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "rayito": {
      "command": "uvx",
      "args": ["--from", "rayito[mcp]", "rayito-mcp"],
      "env": {"RAYITO_TEMPLATE": "rayito-base", "AWS_REGION": "us-east-1", "AWS_PROFILE": "<perfil>"}
    }
  }
}
```

VS Code, `.vscode/mcp.json`:

```json
{
  "servers": {
    "rayito": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "rayito[mcp]", "rayito-mcp"],
      "env": {"RAYITO_TEMPLATE": "rayito-base", "AWS_REGION": "us-east-1", "AWS_PROFILE": "<perfil>"}
    }
  }
}
```

## Modo HTTP

```bash
python -m rayito.mcp --http                    # http://127.0.0.1:8000/mcp
python -m rayito.mcp --http --host 127.0.0.1 --port 8000
```

Sirve streamable HTTP para los hosts y herramientas que sólo hablan HTTP (el
Inspector en modo URL, las entradas remotas de Cursor o Claude Code):

```bash
claude mcp add --transport http rayito-http http://127.0.0.1:8000/mcp
```

!!! warning "Sin autenticación"
    El modo HTTP no autentica a nadie. Escuchar en loopback **no** impide que
    un navegador llegue al servidor: una página web puede resolver su propio
    dominio a 127.0.0.x y hablar con él (DNS rebinding), así que el servidor
    exige siempre que `Host` y `Origin` sean exactamente el `host:puerto` con
    el que arrancó —conéctate con la misma grafía que pasaste en `--host`
    (`127.0.0.1:8000`, no `localhost:8000`). Todos los clientes que alcancen
    el proceso comparten **el mismo sandbox**. Por eso `--host` tiene que ser
    la dirección concreta por la que llegan los clientes: una comodín
    (`0.0.0.0`, `::`) liga todas las interfaces pero no es lo que nadie
    escribe en `Host`, así que se rechaza con un error de uso (exit 2). Con un
    `--host` que no sea loopback —una IP concreta de tu red— el servidor avisa
    (`sin autenticación: cualquier cliente que alcance <host>:<port> controla
    el sandbox`) y continúa; es para tu propia máquina, no para exponerlo.

## Inspector

```bash
RAYITO_TEMPLATE=rayito-base AWS_REGION=us-east-1 AWS_PROFILE=<perfil> \
  pnpm dlx @modelcontextprotocol/inspector \
  uv run --project <repo>/clients/python --extra mcp rayito-mcp
```

En la pestaña *Tools* aparecen las seis herramientas; `run_command` con
`echo hola` crea el sandbox (la primera llamada tarda lo que tarde el
arranque del MicroVM) y `run_code` con
`import matplotlib.pyplot as plt; plt.plot([1, 2, 3]); plt.show()` devuelve
el JSON y el PNG renderizado. Al cerrar el Inspector el sandbox se destruye.
En modo URL, arranca `python -m rayito.mcp --http` y conecta el Inspector a
`http://127.0.0.1:8000/mcp`.

## Costes y límites

- **Un sandbox por proceso.** Sobre stdio cada conexión del host es un
  proceso, así que "un sandbox por sesión" se cumple exactamente; sobre HTTP
  todos los clientes de un proceso comparten el sandbox.
- **`timeout` por defecto 3600 s.** Si el host mata el proceso sin cerrarlo
  (`SIGKILL`, cierre forzado), el `terminate-microvm` del apagado no corre y
  el MicroVM sigue facturando hasta `RAYITO_MCP_TIMEOUT_SECONDS`. Para
  encontrarlo y matarlo: `list_sandboxes` desde cualquier servidor con la
  misma imagen, `Sandbox.kill(sandbox_id)` desde Python, o
  `aws lambda-microvms terminate-microvm --microvm-identifier <id>`.
- **Salidas truncadas** a 100 000 caracteres (`stdout`, `stderr`, `content`,
  `text`), con `truncated: true`; las imágenes viajan tal como las entregó el
  sidecar (que descarta valores de más de 8 MiB).
- **Suspensión a mitad de celda.** La inactividad cuenta bytes que cruzan el
  endpoint: una celda que no imprime nada durante `RAYITO_MCP_IDLE_SECONDS`
  puede ser suspendida en mitad de la ejecución; el SDK espera la reanudación
  y se reengancha, pero si tus celdas son largas y silenciosas sube el valor
  o desactívalo con `RAYITO_MCP_IDLE_SECONDS=0`.
- **Sin autenticación en HTTP**, sin sandboxes por cliente, sin pools ni
  persistencia: cada proceso empieza vacío y termina destruyendo su sandbox.
