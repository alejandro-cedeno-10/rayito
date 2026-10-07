# Puertos y host

`sbx.get_host(port)` te da la dirección pública de un puerto del sandbox:
arranca un servidor dentro y llega a él por HTTPS a través del proxy de AWS.

## Cuándo usarlo

- Previsualizar una app web que el agente acaba de generar.
- Llamar desde tu backend a una API que corre dentro del sandbox.
- Para usarlo desde un navegador o con `curl` sin cabeceras, el
  [proxy local](../funciones-opcionales/proxy-local.md) de la CLI.

## Ejemplo rápido

=== "Python"

    ```python
    import urllib.request

    from rayito import Sandbox

    with Sandbox.create() as sbx:
        sbx.commands.run("python3 -m http.server 3000", background=True, timeout=None)
        host = sbx.get_host(3000)  # (1)!
        request = urllib.request.Request(f"https://{host}/", headers=host.headers)  # (2)!
        with urllib.request.urlopen(request) as response:
            print(response.status)  # 200
    ```

    1. Un `str` con el hostname del sandbox, como en E2B, más `url`, `port`
       y `headers`.
    2. Toda petición necesita las cabeceras del proxy de AWS
       (`X-aws-proxy-auth` y `X-aws-proxy-port`).

=== "Python (async)"

    ```python
    import asyncio
    import urllib.request

    from rayito import AsyncSandbox


    async def main() -> None:
        async with await AsyncSandbox.create() as sbx:
            await sbx.commands.run("python3 -m http.server 3000", background=True, timeout=None)
            host = await sbx.get_host(3000)
            request = urllib.request.Request(f"https://{host}/", headers=host.headers)
            with await asyncio.to_thread(urllib.request.urlopen, request) as response:
                print(response.status)  # 200


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    await using sbx = await Sandbox.create();
    await sbx.commands.run("python3 -m http.server 3000", { background: true, timeoutMs: 0 });
    const host = await sbx.getHost(3000);
    const response = await fetch(host.url, { headers: host.headers });
    console.log(response.status); // 200
    ```

=== "CLI"

    ```bash
    rayito sandbox proxy microvm-<id> --port 3000
    curl http://127.0.0.1:3000/            # sin cabeceras: las pone el proxy local
    ```

=== "Shim E2B"

    ```python
    import urllib.request

    from rayito.e2b import Sandbox

    with Sandbox.create() as sbx:
        sbx.commands.run("python3 -m http.server 3000", background=True, timeout=None)
        host = sbx.get_host(3000)  # (1)!
        request = urllib.request.Request(f"https://{host}/", headers=host.headers)
        with urllib.request.urlopen(request) as response:
            print(response.status)  # 200
    ```

    1. El mismo `HostAccess` que el SDK nativo. En el shim de TypeScript,
       `getHost(port)` devuelve sólo el hostname y las cabeceras salen de
       `await sbx.getHostHeaders(port)`.

## Paso a paso

1. El servidor del sandbox debe escuchar en un puerto libre. Dos están
   ocupados por `rayd`: el **8080** (su gRPC, por donde habla el SDK) y el
   **9000** (los hooks de Lambda). `get_host()` sólo rechaza el 9000; un
   `get_host(8080)` llega a `rayd`, no a tu aplicación. Puede escuchar en
   `127.0.0.1` o en `0.0.0.0`: el proxy de AWS corre dentro del MicroVM.
2. `get_host(port)` acuña un token del proxy para ese puerto (un JWE que
   dura 60 min) y lo renueva solo a los 45 min mientras el handle viva.
   `host.headers` siempre devuelve el token vigente: léelo en cada petición,
   no lo guardes.
3. La conexión es HTTPS hasta el proxy de AWS, que habla HTTP en claro (o
   HTTP/2 sin TLS) con tu servidor. Un servidor HTTPS dentro del sandbox no
   funciona detrás del proxy.

!!! warning "Las cabeceras son una credencial"
    Quien tenga `host.headers` puede llegar a ese puerto mientras el token
    sea válido. No las envíes a un navegador de terceros ni las registres en
    logs. Para compartir una previsualización, pon tu propio proxy con
    autenticación delante.

## Opciones

| Python | TypeScript | Qué hace |
|---|---|---|
| `get_host(port)` → `HostAccess` | `getHost(port)` → `HostAccess` | hostname, `url`, `port` y `headers` del proxy |
| `create(allowed_ports=[...])` | `create({ allowedPorts: [...] })` | puertos extra del token principal del sandbox (8080 siempre); `get_host` no lo necesita: acuña un token por puerto |

## Errores y solución de problemas

| Síntoma | Causa | Qué hacer |
|---|---|---|
| `403` del proxy | faltan las cabeceras o el token caducó | usa `host.headers` en cada petición |
| `502` del proxy | nada escucha en ese puerto todavía, o el sandbox está terminando | espera a que el servidor arranque; comprueba con `commands.run("curl -s localhost:3000")` |
| `InvalidArgumentException` / `InvalidArgumentError` | puerto fuera de 1–65535 o el 9000 | usa otro puerto (y tampoco el 8080, que es de `rayd`) |
| el sandbox estaba suspendido | la primera petición lo despierta (auto-resume) | normal: tarda ≈ 0,7 s más |

## Diferencias con E2B

- E2B da una URL pública sin cabeceras; en Rayito toda petición lleva las
  cabeceras del proxy de AWS. En el shim de TypeScript, `getHost(port)` es
  síncrono y devuelve sólo el hostname; las cabeceras salen de
  `await sbx.getHostHeaders(port)`.
- `https_ports` de E2B (un servidor HTTPS en el sandbox) no existe: el
  proxy no reenvía TLS hasta el sandbox.

## Ver también

- [Proxy local](../funciones-opcionales/proxy-local.md)
- [Dominio propio](../funciones-opcionales/dominio-propio.md)
  (**experimental**, opcional y con coste): una URL de tu dominio delante
  de un puerto del sandbox
- [Comandos](comandos.md): arrancar el servidor en segundo plano
- [Seguridad](../security.md)
