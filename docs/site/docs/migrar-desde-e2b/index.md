# Migrar desde E2B

Si tu código usa el SDK de E2B 2.x (`e2b`, `e2b_code_interpreter`, `e2b` de
npm o `@e2b/code-interpreter`), puedes moverlo a tu cuenta de AWS cambiando
**una línea de import**. `rayito.e2b` (Python) y `rayito/e2b` (TypeScript)
implementan la misma API sobre Lambda MicroVMs.

!!! note "Marcas"
    E2B es una marca de su titular. Rayito es un proyecto independiente, no
    afiliado, patrocinado ni respaldado por E2B; el nombre se usa sólo para
    describir con qué SDK es compatible la API de `rayito.e2b` / `rayito/e2b`.

## En cinco pasos

### 1. Prepara tu cuenta de AWS

Sigue [Configurar AWS](../primeros-pasos/configurar-aws.md): pila de IAM,
bucket y la imagen `rayito-base` publicada. Es el equivalente a tener una
cuenta de E2B con su template por defecto.

### 2. Instala Rayito

=== "Python"

    ```bash
    pip uninstall e2b-code-interpreter e2b      # opcional
    pip install rayito
    ```

=== "TypeScript"

    ```bash
    pnpm remove @e2b/code-interpreter e2b       # opcional
    pnpm add rayito
    ```

### 3. Cambia el import

=== "Python"

    ```diff
    - from e2b_code_interpreter import Sandbox
    + from rayito.e2b import Sandbox
    ```

=== "TypeScript"

    ```diff
    - import { Sandbox } from "@e2b/code-interpreter";
    + import { Sandbox } from "rayito/e2b";
    ```

El resto del programa no cambia:

=== "Python"

    ```python
    from rayito.e2b import Sandbox

    with Sandbox.create(timeout=300, metadata={"run": "42"}) as sbx:
        print(sbx.run_code("1 + 1").text)  # "2"
        print(sbx.commands.run("echo hola").stdout)  # "hola\n"
        sbx.files.write("/home/user/a.txt", "a")
        sbx.set_timeout(600)
    ```

=== "Python (async)"

    ```python
    import asyncio

    from rayito.e2b import AsyncSandbox


    async def main() -> None:
        async with await AsyncSandbox.create(timeout=300, metadata={"run": "42"}) as sbx:
            print((await sbx.run_code("1 + 1")).text)  # "2"
            print((await sbx.commands.run("echo hola")).stdout)
            await sbx.set_timeout(600)


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito/e2b";

    await using sbx = await Sandbox.create({ timeoutMs: 300_000, metadata: { run: "42" } });
    console.log((await sbx.runCode("1 + 1")).text); // "2"
    console.log((await sbx.commands.run("echo hola")).stdout);
    await sbx.setTimeout(600_000);
    ```

### 4. Cambia la configuración

| En E2B | En Rayito |
|---|---|
| `E2B_API_KEY` | credenciales de AWS: `AWS_PROFILE` y `AWS_REGION` (o el rol de la máquina). `api_key=` se acepta y se ignora con un aviso |
| el template por defecto (`code-interpreter-v1`) | `RAYITO_TEMPLATE=rayito-base` (o `template=` / `Sandbox.create("rayito-base")`) |
| `template="mi-template"` construido con `Template.build` | el mismo `Template.build` (desde 0.6.0, compuesto sobre `rayito-base` con `from_base_image`; [Templates](../funciones-opcionales/templates.md)) o una imagen tuya publicada con `rayito image publish` |
| `upload_url` / `download_url` | un bucket tuyo: `RAYITO_TRANSFER_BUCKET=amzn-s3-demo-bucket` ([Ficheros y S3](../files.md)) |
| `allow_internet_access=False`, `network=` | la imagen `rayito-base-caps` ([Red saliente](../network.md)) |
| `run_code(language="js" / "ts" / "bash")` | la imagen `rayito-base-poly` ([Lenguajes y kernels](../kernels.md)) |

### 5. Ejecútalo y revisa los avisos

Lo que el shim acepta pero ignora avisa con un `RayitoCompatWarning` (por
ejemplo, `api_key` o `domain`). Lo que Lambda MicroVMs no puede hacer lanza
`UnimplementedError` con el motivo, nunca se aproxima en silencio. Ejecuta
tus tests con los avisos visibles:

```bash
python -W always::UserWarning -m pytest
```

## Lista de comprobación

- [ ] `rayito doctor` termina sin `FAIL` en la cuenta y región de destino.
- [ ] `RAYITO_TEMPLATE` apunta a una imagen de la release actual (el shim
      exige una imagen 0.3.0 o posterior).
- [ ] Ninguna llamada usa algo de [No soportado](../e2b-compat.md#lanza-unimplementederror)
      (`fork`, snapshots, `Volume`, `mcp=`, `iam=`…).
- [ ] Si guardas el `sandbox_id` para reconectar, guarda también el access
      token: `sbx.native.access_token` (TypeScript: `sbx.native.accessToken`).
      E2B no lo necesita; Rayito sí, porque no hay API key.
- [ ] Si usas `upload_url`, existe el bucket de transferencias y tus
      credenciales tienen sus permisos ([IAM](../operacion/iam.md)).
- [ ] Si tus sandboxes viven más de 8 horas (también pausados), usa
      [Persistencia](../persistence.md) con `reincarnate()`.

## Qué cambia

Los valores por defecto del shim son los de E2B (`timeout=300` como plazo
lógico, sin auto-suspensión, salida a internet). Las diferencias que más se
notan:

| Tema | E2B | Rayito |
|---|---|---|
| Dónde corre | la nube de E2B | tu cuenta de AWS, en tu factura |
| Autenticación | `E2B_API_KEY` | credenciales de AWS + un access token por sandbox |
| Vida máxima | 24 h en Pro; pausados sin límite | 8 h desde el arranque, contando el tiempo pausado |
| CPU y memoria | por template | por imagen: `Template.build(memory_mb=)` o `rayito image publish --sizes` y, en el SDK nativo, `size=` ([Tamaños](../funciones-opcionales/tamanos.md)) |
| `get_host(port)` | URL pública | hostname + cabeceras del proxy de AWS en cada petición |
| `upload_url` | `POST` multipart, el fichero aparece al momento | `PUT` a S3, aterriza en ≈ 1 s (o `ticket.wait()`); de un solo uso |
| Red saliente con reglas | fuera del VM | dentro del guest, en `rayito-base-caps` |

Todas las diferencias, por área: [Diferencias](../e2b-compat.md#diferencias-por-area).
Fila a fila (113 funciones): [Paridad](../e2b-parity.md).

## Qué no existe

`fork`, snapshots y `pause(keep_memory=False)`; `Volume` (volúmenes EFS);
`mcp=`; `iam=`; `network.rules` y la resolución de `Secret.fill()`;
kernels R y Java. Todos lanzan `UnimplementedError` con el motivo. Qué usar
en su lugar:
[Qué hacer con lo que no está](../e2b-parity.md#que-hacer-con-lo-que-no-esta).

Desde 0.6.0 sí existen, como funciones opcionales en tu cuenta (apagadas
por defecto): `Template.build`
([Templates](../funciones-opcionales/templates.md)), montajes de buckets
S3 con `mounts=` ([Montajes S3](../funciones-opcionales/montajes-s3.md)),
eventos de ciclo de vida y webhooks con la firma de E2B con `events=`
([Eventos y webhooks](../funciones-opcionales/eventos-y-webhooks.md)) y
exportación de métricas del sandbox por OTLP con `telemetry=`
([Exportación OTLP](../funciones-opcionales/exportacion-otlp.md)).

## Cuándo pasar al SDK nativo

El shim cubre la API de E2B. El SDK nativo (`from rayito import Sandbox`)
añade lo que E2B no tiene: [pool](../pool.md) de sandboxes en < 1 s,
[persistencia](../persistence.md) en S3, la [auto-suspensión](../guias/pausar-reanudar.md)
por defecto y las [funciones opcionales](../optional-features.md). Desde el
shim, `sbx.native` es el sandbox nativo, así que puedes migrar poco a poco.

## Ver también

- [Diferencias](../e2b-compat.md): tabla de imports, valores por defecto,
  lo que se mapea con una nota y lo que lanza `UnimplementedError`.
- [Paridad](../e2b-parity.md): las 113 funciones de E2B, una por fila.
- [Migrar desde el shim 1.x](../e2b-compat.md#migrar-desde-el-shim-1x):
  si usabas Rayito 0.2.
