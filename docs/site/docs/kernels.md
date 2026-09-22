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
| `javascript` (alias `js`) | reservado: ningún kernel instalado (ver abajo) | `UNIMPLEMENTED` | `UNIMPLEMENTED` |

Los nombres se normalizan en el SDK (`Bash`, `JS` valen); en el cable sólo
viajan `python`, `bash` y `javascript`. Cualquier otro (`r`, `java`,
`typescript`) es `InvalidArgumentException` / `InvalidArgumentError` antes de
llamar al agente; en el shim `rayito.e2b` es `UnimplementedError`, como en
E2B para lo que no existe.

## Arranque perezoso y latencia de la primera celda

`rayito-base-poly` no calienta el kernel bash antes de `/ready`: el snapshot
sigue conteniendo sólo el kernel Python caliente, así el `kernel_ready` y el
tamaño de memoria de la variante son los de `rayito-base`. La primera celda
`bash` de cada sandbox crea el contexto `default-bash` (el agente arranca el
kernel bajo un lock por lenguaje, así dos celdas concurrentes arrancan un
solo kernel) y paga ese arranque dentro de su propio `timeout`; el deadline
del stream (`timeout + 15 s`) ya lo cubre. Las cifras medidas contra AWS
real están en `AWS_API_NOTES.md` Q57 (`kernel_ready_s` de la variante y
latencia de la primera y la segunda celda bash).

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

## JavaScript: reservado, no incluido

El diseño preveía `ijavascript` sobre Node 20 (`dnf install nodejs20`). La
prueba del 2026-09-16 (Q57) lo descartó: `ijavascript@5.2.1` depende de
`jmp@2`, que sólo acepta `zeromq@5`, y `zeromq@5.3.1` no publica binarios
precompilados para `linux-arm64` ni para Node 20 (ABI 115: sus `prebuilds`
llegan hasta `node.abi108`, sólo `darwin-x64`, `linux-x64` y `win32`); npm
cae en `node-gyp rebuild`, que muere con `not found: make` porque
`al2023-minimal` no trae compiladores y no vamos a añadirlos por un kernel
que nadie ha pedido todavía. El nombre `javascript` queda en el catálogo del
sidecar y en la validación de los SDKs para que la respuesta sea
`UNIMPLEMENTED` (nombrando la variante) y no "lenguaje desconocido"; volverá
cuando exista un kernel JS instalable sin compilador.

## Publicar la variante poly

Mismo `Dockerfile`, un marcador dentro del zip (`kernel-sidecar/kernels_variant`
con `poly`) que activa una capa condicional: instala los pines de
`kernel-sidecar/requirements-poly.txt` (`bash_kernel` y sus dependencias,
`pip check`) y comprueba `import bash_kernel` como `user`. `rayito-base` no
lleva el marcador y no ejecuta nada de esa capa.

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
