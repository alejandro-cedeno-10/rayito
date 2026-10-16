# Local proxy

`rayito sandbox proxy` serves a sandbox port on your machine
(`http://127.0.0.1:<port>`), setting the AWS proxy headers itself and
renewing its token. That way a browser, `curl` or any HTTP client reaches a
server inside the sandbox without knowing anything about AWS.

!!! info "Cost and activation"
    - **Default**: it does nothing until you run the command. It is CLI-only
      (the Python one and the TypeScript one, with the same options and the
      same `Host` and `Origin` checks): no SDK option turns it on.
    - **Turns on**: a local HTTP/1.1 server that forwards to the sandbox's
      `--port`.
    - **Resources and AWS calls**: `lambda:GetMicrovm` once (for the
      endpoint) and `lambda:CreateMicrovmAuthToken` every ≈ 45 min while it
      runs. No new resources.
    - **Approximate cost**: $0: both calls are free. If the sandbox was
      suspended, the first request wakes it up, and that does bill its normal
      compute and the snapshot read.
    - **IAM**: `lambda:GetMicrovm` and `lambda:CreateMicrovmAuthToken` on the
      MicroVM; they are already in `SandboxLauncherPolicy`
      (`rayito-m0-launcher-<region>`) and in the `CallerPolicy`
      (`rayito-m0-caller-<region>`) of `infra/iam.yaml`.
    - **How to turn it off**: Ctrl-C.

## When to use it { #cuando-usarlo }

- Open in your browser the app the agent started inside the sandbox.
- Try a sandbox API with `curl`, Postman or your HTTP client.
- **When not to**: from code, `get_host(port)` gives you the host and the
  headers without an intermediate process ([Ports and host](../guias/puertos-y-host.md)).

## Quick example { #ejemplo-rapido }

```bash
rayito sandbox proxy microvm-<id> --port 8000
# http://127.0.0.1:8000 → microvm-<id>:8000
#   with cookies isolated from other local apps: http://microvm-<id>.localhost:8000
curl http://127.0.0.1:8000/
```

To open it in the browser, use the second URL (`<id>.localhost`): browsers
resolve it to your machine and give it its own cookie jar
(see [Browser and cookies](#navegador-y-cookies)).

With a different local port:

```bash
rayito sandbox proxy microvm-<id> --port 8000 --local-port 9000
```

## Options { #opciones }

| Option | Default | What it does |
|---|---|---|
| `--port N` | — | sandbox port (1–65535, never 9000) |
| `--local-port M` | same as `--port` | port on your machine |
| `--bind IP` | `127.0.0.1` | local interface; outside loopback it requires `--allow-remote` |
| `--allow-remote` | off | allows listening on a non-loopback interface |
| `--allowed-host H` | — | `Host` accepted in addition to the loopback ones (repeatable); `H` without a port is valid as is and with the local port, `H:P` only as is. Required with `--bind 0.0.0.0`/`::` |
| `--allow-origin O` | — | `http(s)://host[:port]` origin accepted in `Origin` (repeatable) |
| `--max-connections N` | `8` | simultaneous forwarded connections; the next one gets `503` |

!!! warning "`--allow-remote` shares your access"
    Whoever reaches that port uses the sandbox with the same access as you.
    Listen on loopback only unless you know who can reach your machine.

## Which requests it forwards { #que-peticiones-reenvia }

The proxy adds your token to everything it forwards, and the sandbox's
server only sees `Host: <endpoint>`: it cannot tell whether you made the
request or a web page you have open did. That is why, before forwarding
anything:

- **`Host`** must be `127.0.0.1`, `localhost` or `[::1]` with the local
  port, the `--bind` address (if it is not `0.0.0.0`/`::`),
  `<id>.localhost` (with a loopback `--bind`) or an `--allowed-host`. If it
  is missing or different, it answers `421`. This stops *DNS rebinding*: a
  web page whose domain resolves to `127.0.0.1` arrives with its own name in
  `Host`.
- **`Origin`**, if present, must be `http://` plus one of those same hosts,
  or an `--allow-origin`. Otherwise (including `Origin: null`) it answers
  `403`. This stops cross-site POSTs and `WebSocket`s opened from another
  web page.
- At most `--max-connections` simultaneous connections (8, the minimum any
  1-vCPU MicroVM can sustain); the next one gets `503`, so nobody exhausts
  the connections the SDK also uses.

Behind a reverse proxy or with a name from your network:

```bash
rayito sandbox proxy microvm-<id> --port 8000 --bind 0.0.0.0 --allow-remote \
  --allowed-host devbox.example.com --allow-origin https://devbox.example.com
```

## Browser and cookies { #navegador-y-cookies }

Cookies are not separated by port and "same site" ignores the port: a page
served at `http://127.0.0.1:8000` receives the `127.0.0.1`/`localhost`
cookies of your other local apps (Jupyter, Grafana, a development
server…) and its JavaScript shares a "site" with them. The proxy forwards
`Cookie` as is, because dropping it would break the session of the
sandbox's own app.

- Open the sandbox content at `http://<id>.localhost:<port>`, which has its
  own cookie jar, or in a dedicated browser profile.
- Do not open it in the same profile where you have sessions of other local
  apps if the sandbox runs untrusted code.

## Details { #detalles }

- It removes any client `x-aws-proxy-*` header, sets `Host` to the sandbox
  endpoint and adds the current token and the port.
- It forces `Connection: close`, except on an upgrade request (WebSocket).
  WebSocket passthrough is implemented but not measured against AWS.
- One message per connection: it forwards the first request with its exact
  body (`Content-Length` or `chunked`) and nothing the client sends later
  over that connection. An upgrade the sandbox does not accept with `101` is
  answered with `Connection: close` and closed. A client that reuses the
  connection (keep-alive) opens another one, as with any `Connection: close`.
- It answers `502` if there is no current token or it cannot connect to the
  sandbox.
- It never logs the token, the headers, the bodies or the paths of what
  goes through it.
- It does not need the sandbox access token: only the proxy token, which is
  minted with your AWS credentials.

## Errors and troubleshooting { #errores-y-solucion-de-problemas }

| Symptom | Cause | What to do |
|---|---|---|
| exit 2 on startup | `--port 9000`, an out-of-range port, `--bind` without `--allow-remote` or `--bind 0.0.0.0` without `--allowed-host` | fix the options |
| `421` | the request's `Host` is neither loopback nor an `--allowed-host` | open the URL the proxy announces or add `--allowed-host` |
| `403` | the request carries an `Origin` from another web page | if it is yours (e.g. a reverse proxy with HTTPS), add `--allow-origin` |
| `400` | a header that is not strict HTTP/1.1, or a body with ambiguous framing (`Transfer-Encoding` together with `Content-Length`, a `Transfer-Encoding` that does not end in `chunked`, several different `Content-Length`) | send a single `Content-Length` or `Transfer-Encoding: chunked` |
| `503` | all `--max-connections` are busy | close tabs or raise `--max-connections` (the sandbox sustains 8 per vCPU) |
| `502` on every request | nothing is listening on that sandbox port | start the server with `commands.run(..., background=True)` |
| `AccessDenied` | the IAM permissions are missing | attach `SandboxLauncherPolicy` (or the `CallerPolicy`) from `infra/iam.yaml` |

## See also { #ver-tambien }

- [Ports and host](../guias/puertos-y-host.md)
- [CLI](../cli.md#proxy)
- [Security](../security.md)
