# Ports and host

`sbx.get_host(port)` gives you the public address of a sandbox port: start a
server inside and reach it over HTTPS through the AWS proxy.

## When to use it { #cuando-usarlo }

- Preview a web app the agent has just generated.
- Call from your backend an API that runs inside the sandbox.
- To use it from a browser or with `curl` without headers, the CLI's
  [local proxy](../funciones-opcionales/proxy-local.md).

## Quick example { #ejemplo-rapido }

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

    1. A `str` with the sandbox hostname, as in E2B, plus `url`, `port` and
       `headers`.
    2. Every request needs the AWS proxy headers (`X-aws-proxy-auth` and
       `X-aws-proxy-port`).

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
    curl http://127.0.0.1:3000/            # no headers: the local proxy adds them
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

    1. The same `HostAccess` as the native SDK. In the TypeScript shim,
       `getHost(port)` returns only the hostname and the headers come from
       `await sbx.getHostHeaders(port)`.

## Step by step { #paso-a-paso }

1. The server in the sandbox must listen on a free port. Two are taken by
   `rayd`: **8080** (its gRPC, which the SDK talks to) and **9000** (the
   Lambda hooks). `get_host()` only rejects 9000; a `get_host(8080)` reaches
   `rayd`, not your application. It can listen on `127.0.0.1` or on
   `0.0.0.0`: the AWS proxy runs inside the MicroVM.
2. `get_host(port)` mints a proxy token for that port (a JWE that lasts 60
   min) and renews it on its own at 45 min while the handle is alive.
   `host.headers` always returns the current token: read it on every
   request, do not store it.
3. The connection is HTTPS up to the AWS proxy, which speaks plain HTTP (or
   HTTP/2 without TLS) to your server. An HTTPS server inside the sandbox
   does not work behind the proxy.

!!! warning "The headers are a credential"
    Whoever has `host.headers` can reach that port while the token is
    valid. Do not send them to a third-party browser and do not log them.
    To share a preview, put your own authenticated proxy in front.

## Options { #opciones }

| Python | TypeScript | What it does |
|---|---|---|
| `get_host(port)` → `HostAccess` | `getHost(port)` → `HostAccess` | hostname, `url`, `port` and the proxy `headers` |
| `create(allowed_ports=[...])` | `create({ allowedPorts: [...] })` | extra ports on the sandbox's main token (8080 always); `get_host` does not need it: it mints one token per port |

## Errors and troubleshooting { #errores-y-solucion-de-problemas }

| Symptom | Cause | What to do |
|---|---|---|
| `403` from the proxy | the headers are missing or the token expired | use `host.headers` on every request |
| `502` from the proxy | nothing is listening on that port yet, or the sandbox is shutting down | wait for the server to start; check with `commands.run("curl -s localhost:3000")` |
| `InvalidArgumentException` / `InvalidArgumentError` | port outside 1-65535 or 9000 | use another port (and not 8080 either, which belongs to `rayd`) |
| the sandbox was suspended | the first request wakes it (auto-resume) | normal: it takes ≈ 0.7 s more |

## Differences from E2B { #diferencias-con-e2b }

- E2B gives a public URL with no headers; in Rayito every request carries
  the AWS proxy headers. In the TypeScript shim, `getHost(port)` is
  synchronous and returns only the hostname; the headers come from
  `await sbx.getHostHeaders(port)`.
- E2B's `https_ports` (an HTTPS server in the sandbox) does not exist: the
  proxy does not forward TLS to the sandbox.

## See also { #ver-tambien }

- [Local proxy](../funciones-opcionales/proxy-local.md)
- [Custom domain](../funciones-opcionales/dominio-propio.md)
  (**experimental**, optional and with a cost): a URL on your domain in
  front of a sandbox port
- [Commands](comandos.md): start the server in the background
- [Security](../security.md)
