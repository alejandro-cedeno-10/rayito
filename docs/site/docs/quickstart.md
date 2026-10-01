# Primer sandbox

En esta página creas un sandbox, ejecutas un comando, escribes y lees un
fichero, ejecutas código Python con estado y lo destruyes. Después te
reconectas a un sandbox desde otro proceso.

Antes necesitas el SDK instalado ([Instalación](primeros-pasos/instalacion.md))
y la imagen `rayito-base` publicada en tu cuenta
([Configurar AWS](primeros-pasos/configurar-aws.md)):

```bash
export AWS_PROFILE=<tu-perfil> AWS_REGION=us-east-1 RAYITO_TEMPLATE=rayito-base
```

## Crear, usar y destruir

=== "Python"

    ```python
    from rayito import Sandbox

    with Sandbox.create(timeout=900) as sbx:  # (1)!
        print(sbx.commands.run("echo hola").stdout)  # "hola\n"
        sbx.files.write("/home/user/a.txt", "contenido")
        print(sbx.files.read("/home/user/a.txt"))  # "contenido"
        print(sbx.run_code("x = 40; x + 2").text)  # "42" (2)
    ```

    1. `timeout` es la vida máxima del sandbox en segundos (3600 por
       defecto, tope 8 h). `with` llama a `kill()` al salir.
    2. El kernel conserva el estado entre celdas: `x` sigue existiendo en la
       siguiente llamada a `run_code`.

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncSandbox


    async def main() -> None:
        async with await AsyncSandbox.create(timeout=900) as sbx:
            print((await sbx.commands.run("echo hola")).stdout)  # "hola\n"
            await sbx.files.write("/home/user/a.txt", "contenido")
            print(await sbx.files.read("/home/user/a.txt"))  # "contenido"
            print((await sbx.run_code("x = 40; x + 2")).text)  # "42"


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    await using sbx = await Sandbox.create({ timeoutMs: 900_000 }); // (1)!
    console.log((await sbx.commands.run("echo hola")).stdout); // "hola\n"
    await sbx.files.write("/home/user/a.txt", "contenido");
    console.log(await sbx.files.read("/home/user/a.txt")); // "contenido"
    console.log((await sbx.runCode("x = 40; x + 2")).text); // "42"
    ```

    1. En TypeScript los tiempos van en milisegundos (`timeoutMs`).
       `await using` llama a `kill()` al salir del bloque.

Qué pasa por debajo:

1. `create()` lanza un MicroVM desde la imagen (`run-microvm`) y espera a
   que `rayd`, el agente de dentro, esté listo: unos 2 s hasta el agente y
   5–6 s hasta el kernel de Python.
2. Cada llamada viaja por gRPC a través del proxy de AWS, autenticada con
   un token del proxy y con el *access token* del sandbox.
3. Al salir del bloque, `kill()` termina el MicroVM (`terminate-microvm`).

!!! warning "Libera siempre el sandbox"
    Sin `with` / `await using`, llama a `kill()` tú mismo. Un sandbox
    olvidado sigue facturando ≈ $0,126/h (2 GB) hasta su `timeout`. Para
    encontrar huérfanos: `rayito sandbox list` y `rayito sandbox kill`.

## Reconectar desde otro proceso

Un sandbox no depende del proceso que lo creó. Guarda su `sandbox_id` y su
*access token* y conéctate desde otro proceso, otra máquina o tras un
reinicio:

=== "Python"

    ```python
    from rayito import Sandbox

    sbx = Sandbox.create()
    sandbox_id, token = sbx.sandbox_id, sbx.access_token  # (1)!

    again = Sandbox.connect(sandbox_id, access_token=token)
    print(again.commands.run("hostname").stdout)
    again.kill()
    ```

    1. El access token es un secreto: guárdalo como guardarías una
       contraseña. Sin él no se puede hablar con el sandbox.

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncSandbox


    async def main() -> None:
        sbx = await AsyncSandbox.create()
        sandbox_id, token = sbx.sandbox_id, sbx.access_token

        again = await AsyncSandbox.connect(sandbox_id, access_token=token)
        print((await again.commands.run("hostname")).stdout)
        await again.kill()


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    const sbx = await Sandbox.create();
    const { sandboxId, accessToken } = sbx;

    const again = await Sandbox.connect(sandboxId, { accessToken });
    console.log((await again.commands.run("hostname")).stdout);
    await again.kill();
    ```

`connect()` reanuda el sandbox si estaba pausado y no alarga su vida.

## Recap

- `Sandbox.create()` lanza un MicroVM en tu cuenta; `with` / `await using`
  lo destruye al terminar.
- `commands.run` ejecuta procesos, `files` lee y escribe ficheros y
  `run_code` ejecuta código en un kernel con estado.
- `sandbox_id` + `access_token` bastan para reconectar desde cualquier
  proceso con credenciales de AWS.

## Siguiente paso

- [Conceptos](concepts.md): qué corre dónde, plazos, tokens y reconexión.
- [Comandos](guias/comandos.md), [Ejecutar código](guias/ejecutar-codigo.md)
  y [Ficheros](files.md): cada función en detalle.
- [Migrar desde E2B](migrar-desde-e2b/index.md): si vienes de
  `e2b_code_interpreter` o `@e2b/code-interpreter`.
- [Costes](cost.md): lo que cuesta cada operación, medido.
