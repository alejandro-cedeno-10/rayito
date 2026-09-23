# Seguridad

Resumen de `SECURITY.md` (el modelo de amenazas completo, con el hito en el
que entra cada mitigación). Tres principales: el operador del SDK (confianza
total), el código que corre dentro del sandbox (**ninguna**) y AWS (proxy,
hooks, snapshot).

## Lo que protege el SDK por defecto

| Amenaza | Mitigación |
|---|---|
| El código del sandbox lee las credenciales del execution role por IMDS | `create(execution_role_arn=None)` **por defecto**: sin rol no hay credenciales dentro (ni logs de runtime). Con rol, mínimo privilegio |
| Robo del JWE del proxy | Todo RPC salvo `Health` exige además `x-access-token`; TTL 60 min; el SDK nunca loguea cabeceras |
| El sha256 del secreto viaja en `runHookPayload` (CloudTrail) | Sólo el hash sale del cliente; `rayd` compara en tiempo constante y nunca escribe el body de `/run` |
| Escalada a root dentro del guest | Procesos, PTYs y kernels como uid 1000; root sólo con `user="root"` **y** `RAYITO_ALLOW_ROOT=1` en la imagen |
| Agotamiento de recursos desde el sandbox | rlimits, grupos de procesos, timeouts de servidor, canales de salida acotados, máx. 256 procesos/PTYs, máx. 8 kernels |
| `rayd` (root) como *confused deputy* en el filesystem | lista de denegación sobre rutas canónicas, `setfsuid` del usuario en cada operación, `O_NOFOLLOW`, sin `..` |
| Estado clonado del snapshot compartido entre sandboxes | nada único antes de `/ready`; `/run` reinicia el kernel por defecto; `/resume` reseed de `random`/`numpy.random` |
| Exfiltración por red saliente | `egress=` explícito en `create()`; allowlist vía conector VPC (fuera del guest); desde M9, `network=` / `allow_internet_access=False` aplicados dentro del guest en `rayito-base-caps` (rutas por uid + proxy local, [Red saliente](network.md)); en otra imagen fallan cerrados |
| URLs prefirmadas y SSRF de `rayd` (T16, M9) | las firmas el SDK con tus credenciales, `rayd` no guarda ninguna; nunca se loguean; cada URL cubre una clave ligada al sandbox, un método y una caducidad; `rayd` sólo acepta `https` al host regional exacto del bucket y su resolvedor descarta loopback, link-local e IMDS ([Ficheros](files.md)) |
| Proxy de egress de `rayd` como SSRF (T17, M9) | guardia después de resolver (loopback, IMDS, las direcciones propias del guest), el proxy no resuelve nombres denegados, credenciales del proxy del operador sólo por RPC y nunca en logs; **riesgo residual**: bajo deny-all en `rayito-base-caps` los nombres aún se resuelven por los resolvedores de la plataforma dentro del guest (canal de exfiltración por DNS, aunque toda conexión fuera del VM falla); las capas del guest son de mejor esfuerzo y no resisten a root en el guest ni a un exploit del kernel: el conector VPC sigue siendo el control duro de plataforma |

## Qué no poner en `envs` ni en `metadata`

`envs` y `metadata` viajan juntos en `runHookPayload`. AWS puede registrar el
payload en eventos de datos de CloudTrail y cualquier principal con
`lambda:CreateMicrovmAuthToken` sobre la imagen puede leer los metadatos por
`Health`; y el propio código que corre dentro del sandbox también, sin
ninguna credencial: `Health` es el único RPC anónimo y `rayd` lo sirve en
`0.0.0.0:8080`, así que un proceso uid 1000 del sandbox lee su `sandbox_id`
y el mapa `metadata` completo. Son etiquetas y configuración, **no
secretos**: las credenciales van por `sbx.files.write` o por `envs=` de un
comando concreto, nunca en el
payload de creación. Ni `rayd` ni el SDK escriben claves ni valores de
`metadata` en logs (sólo el número de claves).

## El shim de E2B y la red

`rayito.e2b.Sandbox` reproduce los valores por defecto de E2B: endpoint
público (`ingress=["ALL_INGRESS"]`) y salida a internet
(`egress=["INTERNET_EGRESS"]`). Un MicroVM sin conector de egress en
`run-microvm` hereda el de la versión de imagen y sigue saliendo a internet
(medido, `AWS_API_NOTES.md` Q44 y Q60), así que desde M9
`allow_internet_access=False` y `network=` se aplican **dentro del guest**
en `rayito-base-caps` y, en cualquier otra imagen, el SDK termina el VM y
lanza `UnimplementedError`: nunca te devuelve un sandbox con la red abierta
([Red saliente](network.md), `SECURITY.md` T17). Para cerrar la entrada:
`ingress=["NO_INGRESS"]`; fuera del guest, el allowlist vía conector VPC con
`rayito.Sandbox(egress=[...])`.

## Cadena de suministro

`rayd` corre como root dentro de cada MicroVM, así que lo que se construye y
publica está controlado de punta a punta (detalle en `SECURITY.md` T10 y en
la tabla "Cadena de suministro"):

- La imagen base va fijada por digest en `image/Dockerfile`
  (`FROM public.ecr.aws/lambda/microvms:al2023-minimal@sha256:05cb9b38…`) y
  cada publish pasa `--base-image-version` de forma explícita. Cuando AWS
  mueva el tag, refrescar el digest es un PR de Dependabot (`docker`) o
  `docker buildx imagetools inspect public.ecr.aws/lambda/microvms:al2023-minimal`
  (sin Docker: la receta del propio `Dockerfile` con el token de
  `public.ecr.aws` y `Docker-Content-Digest`); un digest nuevo es una versión
  de imagen nueva, publicada y aceptada con el e2e como cualquier otra.
- Dependencias auditadas en CI y cada semana: `cargo deny` (licencias,
  advisories, fuentes), `pip-audit` sobre los pins de la imagen y de los SDK,
  `pnpm audit --prod`.
- `rayd` se compila con `cargo auditable` (grafo de crates embebido) y lleva
  un SBOM CycloneDX; los assets de cada release van firmados keyless con
  cosign, y PyPI y npm publican por OIDC con attestations. Cómo comprobarlo:
  [Verificar una release](verify.md).

## Persistencia en S3 (T15)

Con `Sandbox.create(persist=S3Prefix(...))` el `HOME` del usuario viaja a tu
bucket. Lo hace `rayd` como root con el execution role (que `persist=` exige
explícitamente), no el código del sandbox: en `rayito-base-caps` uid 1000
sigue sin alcanzar IMDS. El rol sólo puede escribir y leer bajo
`<bucket>/<prefix>/*` (`spike/m0/iam.yaml`), nunca borrar; ese prefijo **no
separa inquilinos**: `rayd` no liga el destino al sandbox que lo pide, así
que quien tenga el access token de un sandbox alcanza cualquier `name` bajo
el mismo prefijo. El restore corre con
la identidad del usuario y sólo extrae ficheros regulares, directorios y
symlinks (nada de dispositivos ni hard links), rechaza `..`, rutas absolutas y
padres que salgan del `HOME`, descarta los bits setuid/setgid/sticky y
verifica el sha256 del archivo. Quien pueda escribir bajo tu prefijo puede
plantar ficheros en el `HOME` de la siguiente encarnación: trata el prefijo
como parte del sandbox. Añade al bucket una regla
`AbortIncompleteMultipartUpload` a 1 día. Detalles en
[Persistencia](persistence.md) y en `SECURITY.md` T15.

## Qué nunca se loguea

Contenido de ficheros, código ejecutado, bytes de PTY, tokens, cabeceras del
proxy, `envs`, `metadata`, el body de los hooks; desde M9 tampoco URLs
prefirmadas, buckets, claves o rutas de una transferencia, metadatos de
fichero, entradas de la política de egress, destinos del proxy ni
credenciales de git. Sólo ids, códigos de estado, recuentos y duraciones.
