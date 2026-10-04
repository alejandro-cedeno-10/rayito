# Proxy local

`rayito sandbox proxy` sirve un puerto del sandbox en tu máquina
(`http://127.0.0.1:<puerto>`), poniendo él las cabeceras del proxy de AWS y
renovando su token. Así un navegador, `curl` o cualquier cliente HTTP llega
a un servidor del sandbox sin saber nada de AWS.

!!! info "Coste y activación"
    - **Por defecto**: no hace nada hasta que ejecutas el comando. Es sólo
      CLI: ninguna opción del SDK lo activa.
    - **Activa**: un servidor HTTP/1.1 local que reenvía al puerto `--port`
      del sandbox.
    - **Recursos y llamadas AWS**: `lambda:GetMicrovm` una vez (para el
      endpoint) y `lambda:CreateMicrovmAuthToken` cada ≈ 45 min mientras
      corre. Ningún recurso nuevo.
    - **Coste aproximado**: $0: las dos llamadas son gratuitas. Si el
      sandbox estaba suspendido, la primera petición lo despierta y eso sí
      factura su cómputo normal y la lectura del snapshot.
    - **IAM**: `lambda:GetMicrovm` y `lambda:CreateMicrovmAuthToken` sobre el
      MicroVM; ya están en la política `rayito-m0-caller-<región>` de
      `infra/iam.yaml`.
    - **Cómo apagarlo**: Ctrl-C.

## Cuándo usarlo

- Abrir en tu navegador la app que el agente levantó dentro del sandbox.
- Probar una API del sandbox con `curl`, Postman o tu cliente HTTP.
- **Cuándo no**: desde código, `get_host(port)` da el host y las
  cabeceras sin un proceso intermedio ([Puertos y host](../guias/puertos-y-host.md)).

## Ejemplo rápido

```bash
rayito sandbox proxy microvm-<id> --port 8000
# http://127.0.0.1:8000 → microvm-<id>:8000
#   con cookies aisladas de otras apps locales: http://microvm-<id>.localhost:8000
curl http://127.0.0.1:8000/
```

Para abrirlo en el navegador, usa la segunda URL (`<id>.localhost`): los
navegadores la resuelven a tu máquina y le dan un tarro de cookies propio
(ver [Navegador y cookies](#navegador-y-cookies)).

Con otro puerto local:

```bash
rayito sandbox proxy microvm-<id> --port 8000 --local-port 9000
```

## Opciones

| Opción | Por defecto | Qué hace |
|---|---|---|
| `--port N` | — | puerto del sandbox (1–65535, nunca 9000) |
| `--local-port M` | igual que `--port` | puerto en tu máquina |
| `--bind IP` | `127.0.0.1` | interfaz local; fuera de loopback exige `--allow-remote` |
| `--allow-remote` | apagado | permite escuchar en una interfaz que no es loopback |
| `--allowed-host H` | — | `Host` que se acepta además de los de loopback (repetible); `H` sin puerto vale tal cual y con el puerto local, `H:P` sólo tal cual. Obligatorio con `--bind 0.0.0.0`/`::` |
| `--allow-origin O` | — | origen `http(s)://host[:puerto]` que se acepta en `Origin` (repetible) |
| `--max-connections N` | `8` | conexiones reenviadas a la vez; la siguiente recibe `503` |

!!! warning "`--allow-remote` comparte tu acceso"
    Quien llegue a ese puerto usa el sandbox con tu mismo acceso. Escucha sólo
    en loopback salvo que sepas quién puede alcanzar tu máquina.

## Qué peticiones reenvía

El proxy añade tu token a todo lo que reenvía, y el servidor del sandbox
sólo ve `Host: <endpoint>`: no puede saber si la petición la hiciste tú o
una web que tienes abierta. Por eso, antes de reenviar nada:

- **`Host`** tiene que ser `127.0.0.1`, `localhost` o `[::1]` con el puerto
  local, la dirección de `--bind` (si no es `0.0.0.0`/`::`),
  `<id>.localhost` (con `--bind` de loopback) o un `--allowed-host`. Si falta
  o es otro, responde `421`. Así se corta el *DNS rebinding*: una web cuyo
  dominio resuelve a `127.0.0.1` llega con su propio nombre en `Host`.
- **`Origin`**, si viene, tiene que ser `http://` más uno de esos mismos
  hosts, o un `--allow-origin`. Si no (también `Origin: null`), responde
  `403`. Así se cortan los POST entre sitios y los `WebSocket` abiertos
  desde otra web.
- Como mucho `--max-connections` conexiones a la vez (8, el mínimo que
  aguanta cualquier MicroVM, de 1 vCPU); la siguiente recibe `503`, para
  que nadie agote las conexiones que también usa el SDK.

Detrás de un proxy inverso o con un nombre de tu red:

```bash
rayito sandbox proxy microvm-<id> --port 8000 --bind 0.0.0.0 --allow-remote \
  --allowed-host devbox.example.com --allow-origin https://devbox.example.com
```

## Navegador y cookies

Las cookies no se separan por puerto y "mismo sitio" ignora el puerto: una
página servida en `http://127.0.0.1:8000` recibe las cookies de
`127.0.0.1`/`localhost` de tus otras apps locales (Jupyter, Grafana, un
servidor de desarrollo…) y su JavaScript comparte "sitio" con ellas. El
proxy reenvía `Cookie` tal cual, porque quitarla rompería la sesión de la
propia app del sandbox.

- Abre el contenido del sandbox en `http://<id>.localhost:<puerto>`, que
  tiene su propio tarro de cookies, o en un perfil de navegador dedicado.
- No lo abras en el mismo perfil en el que tienes sesiones de otras apps
  locales si el sandbox ejecuta código no confiable.

## Detalles

- Quita cualquier cabecera `x-aws-proxy-*` del cliente, fija `Host` al
  endpoint del sandbox y añade el token vigente y el puerto.
- Fuerza `Connection: close`, salvo en una petición de upgrade (WebSocket).
  El paso de WebSocket está implementado pero no medido contra AWS.
- Responde `502` si no hay token vigente o no puede conectar con el sandbox.
- Nunca registra el token, las cabeceras, los cuerpos ni las rutas de lo que
  pasa por él.
- No necesita el access token del sandbox: sólo el token del proxy, que se
  acuña con tus credenciales de AWS.

## Errores y solución de problemas

| Síntoma | Causa | Qué hacer |
|---|---|---|
| salida 2 al arrancar | `--port 9000`, un puerto fuera de rango, `--bind` sin `--allow-remote` o `--bind 0.0.0.0` sin `--allowed-host` | corrige las opciones |
| `421` | el `Host` de la petición no es de loopback ni un `--allowed-host` | abre la URL que anuncia el proxy o añade `--allowed-host` |
| `403` | la petición trae un `Origin` de otra web | si es tuyo (p. ej. un proxy inverso con HTTPS), añade `--allow-origin` |
| `503` | todas las `--max-connections` ocupadas | cierra pestañas o sube `--max-connections` (el sandbox aguanta 8 por vCPU) |
| `502` en cada petición | nada escucha en ese puerto del sandbox | arranca el servidor con `commands.run(..., background=True)` |
| `AccessDenied` | faltan los permisos de IAM | asigna la política de `infra/iam.yaml` |

## Ver también

- [Puertos y host](../guias/puertos-y-host.md)
- [CLI](../cli.md#proxy)
- [Seguridad](../security.md)
