# Rayito

Sandboxes de ejecución para agentes de IA, aislados por hardware, corriendo en
**tu propia cuenta de AWS** sobre Lambda MicroVMs.

> Sandboxes que aparecen en un destello. Dentro de tu propia cuenta de AWS.

La ergonomía del SDK de E2B sin que el código de tus clientes salga de tu
cuenta y sin clúster que operar: el SDK habla directamente con la API de
Lambda MicroVMs y con `rayd`, el agente que corre dentro de cada MicroVM.

```python
from rayito import Sandbox

with Sandbox.create() as sbx:
    sbx.files.write("/home/user/data.csv", "a,b\n1,2\n")
    print(sbx.run_code("import pandas as pd; pd.read_csv('/home/user/data.csv').head()").text)
```

## Qué incluye

| Superficie | Qué hace |
|---|---|
| `Sandbox.create / connect / kill / list / get_info / pause / resume` | ciclo de vida del MicroVM (`run-microvm`, `suspend`, `resume`, `terminate`) |
| `sbx.commands` | procesos en foreground y background, `stdin`, `kill`, `connect` tras un corte |
| `sbx.files` | `read`, `write`, `write_files` en un solo stream, `list`, `watch_dir` |
| `sbx.run_code` | kernel Jupyter con estado, `Execution` con `results`, `logs`, `error` y charts |
| `sbx.pty` | terminales reales con `resize` y reconexión |
| `sbx.get_host(port)` | acceso HTTP a un puerto del sandbox a través del proxy de AWS |
| `Sandbox.create(metadata=)` | etiquetas por sandbox, legibles con `get_info()` y `list(metadata=)` |
| `rayito.e2b` | shim de compatibilidad con el SDK de E2B (cambia sólo el import) |
| `AsyncSandbox` | la misma superficie sobre `asyncio` |

## Estado

| Hito | Contenido | Estado |
|---|---|---|
| M0 | Medición de la plataforma (`AWS_API_NOTES.md`) | aceptado |
| M1 | `rayd` + `Health`, imagen base, `create`/`connect`/`kill` | aceptado |
| M2 | `ProcessService`: `commands` con la postura de seguridad final | aceptado |
| M3 | `FilesystemService`: `files` y `watch_dir` | aceptado |
| M4 | `CodeService`: `run_code` con kernels Jupyter y charts | aceptado |
| M5 | `PtyService`, `pause()`/`resume()` y el contrato de reconexión | aceptado |
| M6 | Endurecimiento, metadatos, `rayito.e2b`, release 0.1.0, cliente TypeScript | aceptado |
| M7 | Preparación open source: Apache-2.0, cadena de suministro, pool de suspendidos, persistencia en S3, servidor MCP, CLI `rayito`, kernel `bash`; release 0.2.0 | aceptado |

Cada hito se acepta contra AWS real, nunca contra un mock: los números de
[costes](cost.md) y [límites](limits.md) son medidos.
