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
# http://127.0.0.1:8000 → puerto 8000 del sandbox; Ctrl-C para cortar
curl http://127.0.0.1:8000/
```

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

!!! warning "`--allow-remote` comparte tu acceso"
    Quien llegue a ese puerto usa el sandbox con tu mismo acceso. Escucha sólo
    en loopback salvo que sepas quién puede alcanzar tu máquina.

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
| salida 2 al arrancar | `--port 9000`, un puerto fuera de rango o `--bind` sin `--allow-remote` | corrige las opciones |
| `502` en cada petición | nada escucha en ese puerto del sandbox | arranca el servidor con `commands.run(..., background=True)` |
| `AccessDenied` | faltan los permisos de IAM | asigna la política de `infra/iam.yaml` |

## Ver también

- [Puertos y host](../guias/puertos-y-host.md)
- [CLI](../cli.md#proxy)
- [Seguridad](../security.md)
