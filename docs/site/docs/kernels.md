# Kernels

`run_code` ejecuta cada celda en un kernel de Jupyter con estado dentro del
MicroVM. Desde M7 (`m7-poly-kernels`) el kernel se elige por celda con
`language`; el contrato es el mismo para todos (`ExecuteRequest.language`,
`CreateContextRequest.language`) y la imagen decide qué kernels existen.

## Lenguajes e imágenes

| `language` | Kernel | `rayito-base` | `rayito-base-poly` |
|---|---|---|---|
| `python` (por defecto) | `ipykernel` con el stack científico, calentado antes de `/ready` | sí | sí |
| `bash` | [`bash_kernel`](https://pypi.org/project/bash_kernel/) 0.10.0 (`pexpect` sobre `bash`), arranque perezoso | `UNIMPLEMENTED` | sí |
| `javascript` (alias `js`) | kernel Jupyter de [Deno](https://deno.com) 2.9.7 (M9), arranque perezoso | `UNIMPLEMENTED` | sí |
| `typescript` (alias `ts`) | el mismo binario de Deno con su kernelspec TypeScript (M9), arranque perezoso | `UNIMPLEMENTED` | sí |

Los nombres se normalizan en el SDK (`Bash`, `JS`, `TypeScript`, `ts` valen);
en el cable sólo viajan `python`, `bash`, `javascript` y `typescript`.
Cualquier otro (`r`, `java`, `tsx`) es `InvalidArgumentException` /
`InvalidArgumentError` antes de llamar al agente; en el shim `rayito.e2b` es
`UnimplementedError`, como en E2B para lo que no existe. En una imagen que no
trae el kernel el agente responde `UNIMPLEMENTED` nombrando
`rayito-base-poly` (ver abajo).

## Arranque perezoso y latencia de la primera celda

`rayito-base-poly` no calienta ni bash ni Deno antes de `/ready`: el
snapshot sigue conteniendo sólo el kernel Python caliente, así el
`kernel_ready` y el tamaño de memoria de la variante son los de
`rayito-base`. La primera celda de cada lenguaje crea su contexto por defecto
(`default-bash`, `default-javascript`, `default-typescript`; el agente arranca
el kernel bajo un lock por lenguaje, así dos celdas concurrentes arrancan un
solo kernel) y paga ese arranque dentro de su propio `timeout`; el deadline
del stream (`timeout + 15 s`) ya lo cubre. Las cifras de bash medidas contra
AWS real están en `AWS_API_NOTES.md` Q57 (`kernel_ready_s` de la variante y
latencia de la primera y la segunda celda bash). Las de Deno están en Q77,
medidas en `rayito-base-poly` 5.0: la primera celda `typescript` o
`javascript` de un sandbox nuevo tarda **0,5-0,75 s** (incluye arrancar
Deno) y las siguientes **≈ 0,1 s**. Justo después de publicar la imagen, las
primeras VMs llegaron a tardar 10-16 s en esa primera celda (lectura en frío
del binario de Deno); si esa espera importa, lanza una celda trivial
(`run_code("0", language="typescript")`) al crear el sandbox. Cada kernel Deno
ocupa ≈ 200 MiB de RSS. `javascript` y `typescript` son dos kernels (dos
procesos Deno) si usas los dos; cada uno cuenta para el tope de 8 contextos,
así que elige uno.

## Ejemplos

=== "Python"

    ```python
    from rayito import Sandbox

    with Sandbox.create(template="rayito-base-poly") as sbx:
        out = sbx.run_code("echo hi", language="bash")
        print("".join(out.logs.stdout))  # "hi\n"

        ctx = sbx.create_code_context(language="bash", envs={"MODE": "dev"})
        print("".join(sbx.run_code("echo $MODE", context=ctx).logs.stdout))  # "dev\n"

        print([c.id for c in sbx.list_code_contexts()])  # ["default", "default-bash", "ctx-…"]
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    const sbx = await Sandbox.create({ template: "rayito-base-poly" });
    const out = await sbx.runCode("echo hi", { language: "bash" });
    console.log(out.logs.stdout.join("")); // "hi\n"
    const ctx = await sbx.createCodeContext({ language: "bash" });
    console.log((await sbx.runCode("echo $HOME", { context: ctx })).logs.stdout.join(""));
    await sbx.kill();
    ```

Reglas:

- `language` y `context` son excluyentes (`INVALID_ARGUMENT`): un contexto ya
  tiene su lenguaje; `language` sólo selecciona el contexto por defecto de
  ese kernel.
- `envs` por ejecución sólo en contextos Python (`INVALID_ARGUMENT` en el
  resto: las celdas silenciosas que fijan y restauran variables son código
  Python). Los `envs` de `create_code_context` sí valen para cualquier
  lenguaje: son el entorno del proceso del kernel.
- `default-bash` es un contexto normal: cuenta para el tope de 8, se puede
  reiniciar y destruir (`remove_code_context("default-bash")`); la siguiente
  celda `bash` lo vuelve a crear. Sólo `default` (Python) está protegido.
- `list_code_contexts()` informa el lenguaje real de cada contexto.

## JavaScript y TypeScript con Deno

=== "Python"

    ```python
    from rayito import Sandbox

    with Sandbox.create(template="rayito-base-poly") as sbx:
        print(sbx.run_code("const x: number = 40 + 2; x", language="typescript").text)  # "42"
        print(sbx.run_code("x + 1", language="ts").text)                                # "43"
        print(sbx.run_code("let y = [1, 2, 3].map((n) => n * 2); y", language="js").text)

        out = sbx.run_code('Deno.jupyter.html`<b>hola</b>`', language="typescript")
        print(out.results[0].html)                                                     # "<b>hola</b>"

        ctx = sbx.create_code_context(language="typescript", envs={"MODE": "dev"})
        print(sbx.run_code('Deno.env.get("MODE")', context=ctx).text)                   # el valor de MODE

        sbx.run_code('import { camelCase } from "npm:lodash-es@4"; camelCase("hola mundo")',
                     language="typescript")
    ```

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncSandbox


    async def main() -> None:
        async with await AsyncSandbox.create(template="rayito-base-poly") as sbx:
            out = await sbx.run_code("const x: number = 40 + 2; x", language="typescript")
            print(out.text)                                                        # "42"
            ctx = await sbx.create_code_context(language="javascript")
            print((await sbx.run_code("[1, 2, 3].length", context=ctx)).text)      # "3"


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    await using sbx = await Sandbox.create({ template: "rayito-base-poly" });
    console.log((await sbx.runCode("const x: number = 40 + 2; x", { language: "typescript" })).text);
    console.log((await sbx.runCode("x + 1", { language: "ts" })).text);
    const ctx = await sbx.createCodeContext({ language: "javascript" });
    console.log((await sbx.runCode("[1, 2, 3].map((n) => n * 2)", { context: ctx })).text);
    ```

=== "Shim de E2B"

    ```python
    from rayito.e2b import Sandbox

    with Sandbox.create("rayito-base-poly") as sbx:
        print(sbx.run_code("console.log('hola'); 1 + 1", language="javascript").text)   # "2"
        print(sbx.run_code("const n: number = 2; n * 21", language="typescript").text)  # "42"
    ```

- **Por qué Deno.** `ijavascript` no instala en `al2023-minimal` ARM64 sin
  compilador (Q57, abajo); Deno es un único binario enlazado contra glibc que
  trae su propio ZeroMQ y un kernel Jupyter (`deno jupyter`), y sirve los dos
  lenguajes (Q61).
- **El pin.** `image/Dockerfile` descarga `deno-aarch64-unknown-linux-gnu.zip`
  de la release `v2.9.7` con un sha256 fijado y comprobado con `sha256sum -c`
  (`scripts/check_pins.py` lo exige, `SECURITY.md` T10) y lo deja en
  `/opt/rayito/deno/deno`, de root. Subir Deno es cambiar `DENO_VERSION` y
  `DENO_SHA256` (el `.sha256sum` oficial de la release), republicar
  `rayito-base-poly` y repetir su e2e; Dependabot no lo sigue.
- **TCP de loopback.** Deno no sabe abrir endpoints `ipc`, así que sus kernels
  escuchan en `127.0.0.1` con la clave HMAC del connection file (ADR-013 de
  `ARCHITECTURE.md`); Python y bash siguen en `ipc`. Consecuencia aceptada: un
  proceso del propio sandbox puede leer las salidas de las celdas Deno
  (`SECURITY.md` T12).
- **Interrupción.** El `timeout` interrumpe con un mensaje `interrupt_request`
  del protocolo (Deno no reenvía `SIGINT`); si el kernel no queda idle en 5 s,
  el agente reinicia el contexto. Una celda Deno interrumpida no emite un
  `error` propio: el agente sintetiza `ExecutionTimeout` igual que con Python.
- **`NO_COLOR` y ANSI.** Los kernelspecs arrancan Deno con `NO_COLOR=1`,
  `DENO_NO_UPDATE_CHECK=1` y un `DENO_DIR` bajo el `HOME`; además el sidecar
  quita las secuencias ANSI del `text/plain` de los resultados Deno (defensa
  en profundidad), así `.text` es `42` y no `\x1b[33m42\x1b[39m`.
- **Permisos.** Deno corre con todos los permisos (`allow_all`), igual que el
  kernel de Python como uid 1000: `Deno.env`, `Deno.readTextFile`, red.
- **Imports `npm:`, `jsr:` y `https:`** descargan código por el mismo egress
  que un `pip install` (con la misma política de [red saliente](network.md)).
- `envs` por ejecución sólo valen en Python: en Deno usa los `envs` de
  `create_code_context`.

## Lo que sigue siendo sólo Python

- Formateadores `e2b/chart` y `e2b/data`, la configuración de IPython y los
  scripts de arranque: un kernel bash emite `stream` y `error` y el agente
  los mapea a `logs`/`error` con las mismas reglas.
- El calentamiento antes de `/ready` y la rotación del kernel en `/run`.
- El reseed de `random`/`numpy.random` tras `/resume`: los contextos no
  Python se listan como `skipped` (su estado aleatorio es el del propio
  proceso).

## Timeouts y errores en bash

El `timeout` sigue la regla genérica: al vencer el agente interrumpe el
kernel (`SIGINT`, que `bash_kernel` reenvía a su `bash`) y, si no queda idle
en 5 s, reinicia el contexto; en ambos casos la `Execution` trae
`error.name == "ExecutionTimeout"` y la siguiente celda funciona (medido en
Q57: `sleep 30` con `timeout=2` vuelve a los 5,8 s por la rama de reinicio,
porque el `SIGINT` no deja idle a `bash_kernel` en 5 s). Un comando que sale
con código distinto de cero termina con el `error` que emite `bash_kernel`:
nombre vacío y el código de salida como `value` (`false` →
`ExecutionError(name='', value='1')`); la salida sigue llegando por
`logs.stdout`/`logs.stderr`.

## `UNIMPLEMENTED` en `rayito-base`

En una imagen sin el kernel, `run_code("echo hi", language="bash")` falla
antes de crear nada con `UNIMPLEMENTED` y un mensaje que nombra
`rayito-base-poly`; el SDK Python lo entrega como `InvalidArgumentException`
con `grpc_code == UNIMPLEMENTED` y el TypeScript como `InvalidArgumentError`.
El agente sabe qué kernels hay porque el sidecar lo anuncia en `ready`
(`languages`, según los kernelspecs que pudo instalar al arrancar).

## Por qué no `ijavascript`

El diseño de M7 preveía `ijavascript` sobre Node 20 (`dnf install nodejs20`).
La prueba del 2026-09-16 (Q57) lo descartó: `ijavascript@5.2.1` depende de
`jmp@2`, que sólo acepta `zeromq@5`, y `zeromq@5.3.1` no publica binarios
precompilados para `linux-arm64` ni para Node 20 (ABI 115: sus `prebuilds`
llegan hasta `node.abi108`, sólo `darwin-x64`, `linux-x64` y `win32`); npm
cae en `node-gyp rebuild`, que muere con `not found: make` porque
`al2023-minimal` no trae compiladores. M9 sirve `javascript` y `typescript`
con Deno, que no necesita nada de eso.

## R y Java

Siguen fuera (`SPEC.md` §4), con números medidos: R por conda-forge
(`r-base` + `r-irkernel`) ocupa 1,4 GB instalado, y R-core por `dnf` 127 MB
más cairo, pango, harfbuzz, tk y fuentes; Java serían 262 MB de Corretto 21
headless más IJava, sin mantenimiento (una release desde 2023, no compila en
JDK 17/18).

## Publicar la variante poly

Mismo `Dockerfile`, un marcador dentro del zip (`kernel-sidecar/kernels_variant`
con `poly`) que activa una capa condicional: instala los pines de
`kernel-sidecar/requirements-poly.txt` (`bash_kernel` y sus dependencias,
`pip check`), comprueba `import bash_kernel` como `user`, descarga y verifica
Deno 2.9.7 y comprueba `deno --version` como `user`. `rayito-base` no lleva
el marcador y no ejecuta nada de esa capa.

```bash
# Linux / macOS
make image-publish-poly BUCKET=<bucket>          # publica rayito-base-poly

# Windows (sin make; toolchain de CONTRIBUTING.md §3)
cargo zigbuild --release --locked --target aarch64-unknown-linux-musl -p rayd
cp target/aarch64-unknown-linux-musl/release/rayd image/rayd
python scripts/copy_sidecar.py kernel-sidecar image/kernel-sidecar
python scripts/image_zip.py image image/rayito-image-poly.zip --variant poly
uv run --project clients/python python scripts/publish_image.py \
    --artifact image/rayito-image-poly.zip --variant poly \
    --base-image-version 1 --bucket <bucket>
```

`publish_image.py` se niega a publicar un zip cuyo marcador no coincida con
`--variant`. Los tamaños medidos de `snapshotBuild` de `rayito-base-poly` y
de `rayito-base` reconstruida con el mismo `Dockerfile` (dentro de la banda
de ±20 MB de memoria / ±10 MB de código respecto a 17.0) están en Q57.

## Agentes anteriores a M7

`ExecuteRequest.language` es un campo proto3 opcional: un `rayd` anterior a
M7 lo ignora y ejecuta la celda en el kernel Python del contexto por defecto
sin avisar. Usa una imagen publicada desde este cambio (o posterior) antes de
confiar en `language`; la tabla de compatibilidad SDK/agente se publica con
cada release (`docs/RELEASING.md`).

## Agentes anteriores a M9

Un `rayd` anterior a M9 no conoce `typescript`: responde `INVALID_ARGUMENT`
("language must be one of python, bash, javascript") y los SDKs lo entregan
como `InvalidArgumentException` / `InvalidArgumentError` (también a través del
shim). `javascript` contra ese agente sigue siendo `UNIMPLEMENTED` en toda
imagen. Publica `rayito-base-poly` desde un árbol M9 antes de usar JS/TS.
