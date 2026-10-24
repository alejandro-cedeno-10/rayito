# Guía: un CSV de tickets en un sandbox, con la CLI, Python y TypeScript

[English](README.md)

Laboratorio completo de la guía «Un sandbox en tu cuenta de AWS en minutos:
Rayito con la CLI, Python y TypeScript»: el CSV sintético, el contrato que
dice qué resultado es correcto, un script por paso en Python y en TypeScript,
la configuración del agente y los lockfiles. Está fijado a **Rayito 0.15.0**
en todas las piezas: `rayito[cli]==0.15.0` de PyPI, `rayito@0.15.0` de npm,
la CLI que traen y el `rayd` 0.15.0 de las imágenes. Ver «Versiones» al
final. El código está en inglés; los comentarios, los docstrings y los
mensajes que imprimen los scripts, en español. Las columnas del CSV
(`categoria`, `horas`) y las categorías son datos y siguen en español.

Para tener exactamente lo que usa la guía, clona el repositorio y sitúate en
el commit al que apunta su enlace:

```bash
git clone https://github.com/alejandro-cedeno-10/rayito.git
cd rayito && git checkout <commit-del-enlace>
cd examples/csv-tickets-guide
```

Todo lo que crean estos scripts son recursos de tu cuenta de AWS y aparecen
en tu factura. Lee la sección «Costes y limpieza» antes de ejecutar los
pasos 06 a 10.

## Estructura

```
data/tickets.csv            el CSV sintético (7 tickets)
contract/expected.json      el resultado esperado por categoría
contract/verify.py          cálculo determinista, sólo biblioteca estándar
app/summary.py              la app que copia Template.copy("app/", ...)
python/NN_*.py              un script ejecutable por ejemplo (+ _common.py)
typescript/NN_*.ts          lo mismo en TypeScript (+ common.ts)
triage-agent/               forma B: rayito-agent.toml + AGENTS.md
opencode-without-toml/      reproducción mínima de la detección de OpenCode
cli/commands.sh             los comandos de la CLI de la guía, comentados
my_template.py              el template de 06 como variable, para `rayito template build`
pyproject.toml, uv.lock     dependencias de Python (uv) y configuración de ruff y mypy
package.json, pnpm-lock.yaml, pnpm-workspace.yaml, tsconfig.json, biome.json   TypeScript (pnpm)
.python-version             la versión de Python que usa uv
```

Ejecuta todo **desde esta carpeta**: los scripts leen `data/` y
`contract/`, y el template copia `app/` relativo a ella. En la guía, la
carpeta del proyecto de la forma B se llama `./triaje`; aquí es
`triage-agent/`. El agente sigue llamándose `triaje`
(`rayito agent stop triaje`).

## Requisitos

- Python 3.11 o posterior y [uv](https://docs.astral.sh/uv/) (probado con
  Python 3.12.14 y uv 0.12.18). Con pip también vale:
  `pip install "rayito[cli]==0.15.0" "aws-bedrock-token-generator==1.1.0"`.
- Node 20 o posterior y pnpm (probado con Node 22.23.2 y pnpm 12.14.0).
  TypeScript 5.2 o posterior por `await using`; el lockfile fija
  `typescript@7.0.2`, `tsx@4.23.15`, `@types/node@26.6.5` y
  `@biomejs/biome@2.5.14`.
- Una cuenta de AWS preparada una vez con `rayito setup` (credenciales de
  administración sólo para ese paso) en una región con Lambda MicroVMs,
  probado en `us-east-1`. Los pasos exactos están en la sección 1 de
  `cli/commands.sh` y en [Configurar AWS](https://alejandro-cedeno-10.github.io/rayito/primeros-pasos/configurar-aws/);
  para ejecutar los ejemplos basta un rol con `SandboxLauncherPolicy`.
- Para 09 y 10, acceso al modelo de Amazon Bedrock que usan
  (Claude Haiku 4.5 por el perfil de inferencia `us.`).

```bash
uv sync                          # crea .venv con rayito 0.15.0
pnpm install --frozen-lockfile   # instala rayito@0.15.0 y las herramientas
python3 contract/verify.py       # el contrato, sin AWS
uv run mypy python contract app my_template.py   # tipos en modo estricto, sin AWS
pnpm run typecheck && pnpm run lint              # tsc y Biome, sin AWS
```

## Variables de entorno

| Variable | Para qué | Por defecto |
|---|---|---|
| `AWS_PROFILE`, `AWS_REGION` | credenciales y región | las de tu entorno |
| `RAYITO_TEMPLATE` | imagen de los sandboxes; el shim de E2B (05) la lee y falla sin ella | `rayito-base` (05: ninguno, expórtala) |
| `RAYITO_BUCKET` | bucket de artefactos (lo imprime `rayito setup`) | ninguno; lo exigen 06 y la CLI de imágenes |
| `TEMPLATE_NAME` | nombre de la imagen del ejemplo 06 | `tickets-demo` |
| `AGENT_TEMPLATE` | imagen con el agente | `rayito-agent` |
| `MODEL_SECRET` | nombre del secreto (queda como `rayito/<nombre>`) | `bedrock-key` |
| `RAYITO_APP_TAG` | valor de la etiqueta `metadata.app` | `tickets-demo` |
| `POOL_FILL_LIMIT_SECONDS` / `POOL_FILL_LIMIT_MS` | cuánto espera 07 a una plaza lista | 120 s |

Ninguna variable lleva un valor secreto salvo `AWS_BEARER_TOKEN_BEDROCK`, que
sólo lee `08_create_secret` y que no debes escribir en ningún fichero.

## Los ejemplos

| Paso | Python | TypeScript | Qué comprueba |
|---|---|---|---|
| 01 | `uv run python/01_first.py` | `pnpm exec tsx typescript/01_first.ts` | comando, fichero y celda con estado (`hola`, `contenido`, `42`) |
| 02 | `uv run python/02_tickets.py` | `pnpm exec tsx typescript/02_tickets.ts` | resumen por categoría comparado con `contract/expected.json`; sale con 1 si una celda falla, si no cumple el contrato o si no hay PNG |
| 03 | `uv run python/03_intentional_error.py` | `pnpm exec tsx typescript/03_intentional_error.ts` | un `KeyError` a propósito llega como `execution.error` |
| 04 | `uv run python/04_reconnect.py` | `pnpm exec tsx typescript/04_reconnect.ts` | otro handle lee lo que escribió el primero |
| 05 | `uv run python/05_shim_e2b.py` | `pnpm exec tsx typescript/05_shim_e2b.ts` | el shim de E2B devuelve `42` |
| 06 | `uv run python/06_template.py` | `pnpm exec tsx typescript/06_template.ts` | imagen propia: `tabulate` 0.9.0, servidor en el puerto 8000 y `app/summary.py` |
| 07 | `uv run python/07_pool.py` | `pnpm exec tsx typescript/07_pool.ts` | `create()` frente a toma de una plaza preparada, con acierto o fallback |
| 08 | `uv run python/08_create_secret.py [--generate]` | `pnpm exec tsx typescript/08_create_secret.ts` (sin `--generate`) | guarda `Bearer <clave>` en `rayito/$MODEL_SECRET` sin imprimirla |
| 09 | `uv run python/09_agent.py` | `pnpm exec tsx typescript/09_agent.ts` | el agente usa `python3` (según la traza) y sus recuentos cumplen el contrato |
| 10 | `uv run python/10_agent_negative.py` | `pnpm exec tsx typescript/10_agent_negative.ts` | prueba negativa opcional: con todo denegado, el fichero pedido no aparece aunque el modelo diga que lo creó |
| 99 | `uv run python/99_cleanup_secret.py [--force]` | `pnpm exec tsx typescript/99_cleanup_secret.ts [--force]` | borra el secreto (con ventana de recuperación por defecto) |

Los dos lenguajes hacen lo mismo en cada paso, salvo que sólo el 08 de
Python tiene `--generate`.

Resultado esperado de 02 (el contrato):

| categoría | tickets | media de horas |
|---|---|---|
| acceso | 2 | 1,5 |
| facturacion | 3 | 4,33 |
| rendimiento | 2 | 8,0 |
| **total** | **7** | |

El PNG del gráfico no forma parte del contrato: depende de la versión de
matplotlib y de sus fuentes; no lo uses como comprobación.

### Antes del paso 09

El agente necesita tres cosas que cuestan dinero y que los scripts no crean
solos (están en `cli/commands.sh`):

1. la imagen `rayito-base-caps` (`rayito image publish ... --os-capabilities ALL`);
2. la imagen `rayito-agent` (`rayito agent template build`);
3. el secreto con la clave del modelo (`08_create_secret`).

`09_agent` no da por buena la respuesta final del modelo: busca en
`result.tool_calls` una llamada a `bash` completada cuyo comando contenga
`python3` y compara los recuentos de la respuesta con el contrato. Aun así es
un resultado limitado: otro modelo, otra versión del runtime u otra redacción
del prompt pueden cambiarlo, y el script lo dirá saliendo con 1.

La instrucción «No modifiques ficheros» es una petición al modelo. La
configuración de permisos deniega todas las herramientas salvo `bash` con
comandos que coincidan con `python3 *`. Eso no convierte el entorno en solo
lectura: Python todavía puede modificar ficheros accesibles al proceso. Para
impedirlo hacen falta restricciones adicionales sobre el sistema de ficheros
o una herramienta que ejecute únicamente una operación predefinida. Los
permisos tampoco son una frontera de seguridad: la frontera es la MicroVM con
la salida a Internet cerrada.

`10_agent_negative` detecta una respuesta sin respaldo: el modelo puede
afirmar que creó el fichero, y el script lo comprueba en la traza y en el
sistema de ficheros del sandbox. Que no haya llamadas a herramientas no
demuestra por sí solo que una regla de permisos concreta se aplicó.

### La detección de OpenCode sin fichero de Rayito

`opencode-without-toml/` tiene sólo un `opencode.json` (que niega `edit`,
`bash` y `webfetch`) y un `AGENTS.md`. Para ver qué proyecto resuelve la
detección, sin llamar a AWS, cópialo a un directorio temporal y ejecuta
dentro `AWS_REGION=us-east-1 rayito agent detect --write`; mira la sección
`agent` del `rayito-agent.json` resultante. Con 0.15.0 no aparece (ni
instrucciones ni permisos); con 0.17.0 aparecen las dos cosas.

## Costes y limpieza

Precios de lista de AWS en `us-east-1` (USD) recogidos en la documentación de
Rayito 0.15.0 ([Precios](https://alejandro-cedeno-10.github.io/rayito/cost/)),
no una factura observada. Para lo que pagaste de verdad, mira Cost Explorer al
cabo de uno o dos días.

- 01 a 05 y 07: segundos de MicroVM; del orden de céntimos en total.
- 06: una versión de imagen nueva por ejecución, ≈ 0,04 USD por semana y con
  mínimo de una semana aunque la borres antes.
- `rayito-base-caps` y `rayito-agent`: una versión cada una (la del agente,
  ≈ 0,056 USD por semana).
- 08: un secreto, 0,40 USD al mes prorrateado por horas, más 0,05 USD por
  10 000 llamadas.
- 09 y 10: los tokens del modelo, que se facturan en Bedrock aparte.
- `rayito agent deploy ./triage-agent`: ≈ 0,126 USD/h por VM hasta
  `rayito agent stop triaje` (o hasta que vence su plazo de 8 horas), más
  la versión de imagen del agente `triaje`.

Limpieza: `rayito sandbox list`, `rayito agent stop triaje`,
`rayito image prune` y los comandos del final de `cli/commands.sh`. Las
imágenes que crea el laboratorio son `tickets-demo` (06), `rayito-base-caps`,
`rayito-agent` y la del agente `triaje`; `rayito image prune` sólo borra
versiones antiguas, así que para retirar una imagen entera usa
`delete-microvm-image` de la API de Lambda MicroVMs y borra después su grupo
de logs `/rayito/<nombre>`, si existe. `99_cleanup_secret` programa el
borrado con 7 días de recuperación; `--force` borra sin recuperación y sólo
tiene sentido con un secreto creado para esta prueba. Terminar un sandbox no
deshace lo que su código haya hecho fuera de él (llamadas a APIs, escrituras
en otros servicios).

Cuidado con `rayito sandbox kill --all`: termina todos los MicroVMs de la
cuenta y la región, también los de otras personas; si compartes la cuenta,
mata por id.

## Versiones

Todo se probó con 0.15.0. Después han salido versiones nuevas
([releases](https://github.com/alejandro-cedeno-10/rayito/releases)); estos
ejemplos no se han repetido con ellas y sus resultados no se atribuyen a
ninguna versión distinta de la 0.15.0. La única diferencia conocida que
afecta a este laboratorio es la detección de OpenCode sin fichero de Rayito
(`opencode-without-toml/`), corregida en la 0.17.0. La documentación exacta
de la 0.15.0 está en el
[código etiquetado](https://github.com/alejandro-cedeno-10/rayito/tree/python-v0.15.0/docs/site/docs).
Las dependencias de esta carpeta están fijadas por sus lockfiles a propósito
y esta carpeta no está en la configuración de Dependabot.
