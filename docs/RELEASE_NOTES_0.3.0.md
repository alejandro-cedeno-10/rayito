# Rayito 0.3.0 — notas de la release (borrador)

**Borrador.** Tercera release: M9 ("paridad con E2B"). Los seis cambios
OpenSpec están **aceptados contra AWS real** (2026-09-24, `MILESTONES.md`,
M9): regresión e2e de Python 62/62 sobre las imágenes M9, corpus de
programas de E2B 18/18 (Python) y 8 de TypeScript, git y CLI verdes, y
TypeScript 26/26. Los números vienen de `AWS_API_NOTES.md` §16 (Q58–Q78) y
lo dicen. Queda el cierre: gates finales, archivo de los cambios y la
release; esta nota deja de ser borrador con ella.

## Versiones

Los tres componentes suben a **0.3.0 en lockstep** (`docs/RELEASING.md`):
SDK Python `rayito` 0.2.0 → 0.3.0, SDK TypeScript `rayito` 0.2.0 → 0.3.0 y
`rayd` 0.2.0 → 0.3.0. La fila `0.3` de la tabla de compatibilidad
(`rayd` mínimo 0.3.0, en `docs/site/docs/limits.md` y
`rayito.cli._compat.COMPATIBILITY`, que un test mantiene iguales) ya está
en el árbol para que `rayito doctor` no dé `FAIL` con el SDK 0.3.0; el salto
de versión en los manifiestos y los tags son un paso aparte del proceso de
release; este borrador no los toca.

Los SDK 0.3 necesitan una imagen publicada con el `rayd` de M9 para
cualquier feature nueva. Todo el contrato gRPC nuevo es aditivo y compatible
con `buf breaking` (FILE): un SDK 0.2 sigue funcionando contra una imagen
M9, y el SDK nativo 0.3 contra una imagen 0.2 funciona igual que 0.2
mientras no use nada de M9 (cada feature nueva falla cerrado con su error;
el shim de E2B exige siempre una imagen M9, y `rayito doctor` marcará esa
imagen por debajo del mínimo en cuanto exista la fila `0.3`).

## Destacados

La tabla completa, fila a fila (113 features de E2B 2.51: 72
implementadas, 17 divergentes con la diferencia escrita, 13 fuera por
`SPEC.md` y 11 imposibles en la plataforma), está en
`docs/site/docs/e2b-parity.md`.

1. **Plazo del servidor** (`m9-server-timeout`, ADR-011, sustituye a
   ADR-007): `create(timeout=, max_lifetime=, on_timeout="kill" | "pause")`.
   `rayd` hace cumplir el plazo aunque tu proceso muera; `set_timeout()` lo
   mueve con exactitud (puede acortarlo) y `connect(timeout=)` lo alarga.
   En modo `kill`, `rayd` sale con código 124, la plataforma lo propaga
   (`Container Stopped with Exit Code: 124`) y la VM pasa a `TERMINATED`
   entre ≈ 15 y 19 s después del plazo sin IAM (Q58, Q63). En modo `pause`
   con un cliente vivo, el sandbox queda suspendido ≈ 2,3 s después del
   plazo y se reanuda en ≈ 1 s (Q64). `max_lifetime` es el tope fijo de la
   plataforma (≤ 8 h, running + suspendido). Página:
   `docs/site/docs/lifecycle.md`.
2. **Transferencias por S3** (`m9-file-transfer`, ADR-010):
   `files.upload_url()`/`download_url()` son URLs de S3 prefirmadas con
   **tus** credenciales; `rayd` no guarda ninguna y sólo llama a URLs cuyo
   host y ruta son los del objeto nombrado (`SECURITY.md` T16). Con
   `transfer=S3Staging(...)`, `files.write`/`files.read` de ficheros
   grandes van por S3 (entre el VM y S3 se midieron 55,7–106,2 MB/s de subida
   y 84,2–99,0 MB/s de bajada, Q59, frente a 0,68 MB/s por el proxy). Además
   `gzip=` (20 MB de texto: de 0,80 a 52,34 MB/s por el proxy, Q74),
   `metadata=` (xattrs `user.rayito.*`), `stream_idle_timeout=` y
   `format="blob"` en TS. Página: `docs/site/docs/files.md`.
3. **Kernels JavaScript y TypeScript** (`m9-deno-kernels`, ADR-013):
   `run_code(code, language="javascript" | "typescript")` con Deno 2.9.7
   en `rayito-base-poly`, arranque perezoso (primera celda 0,5–0,75 s y las
   siguientes ≈ 0,1 s en `rayito-base-poly` 5.0, Q77). Página:
   `docs/site/docs/kernels.md`.
4. **Red saliente** (`m9-egress-policy`, ADR-012):
   `network={"allow_out", "deny_out", "egress_proxy"}`,
   `allow_internet_access=False` y `update_network()` con la semántica de
   E2B, aplicados dentro del guest de `rayito-base-caps` (rutas por uid y un
   proxy local para las reglas por nombre de host); en cualquier otra imagen
   el SDK termina el VM y lanza `UnimplementedError`. Página:
   `docs/site/docs/network.md`.
5. **Métricas y listado** (`m9-sandbox-observability`):
   `get_metrics_history()` (una muestra cada 5 s, anillo de 8 h),
   `mem_cache`, CPU y memoria del guest (la vista del guest: 8016 MiB y 4
   CPU con una imagen de 2048 MiB, Q68), y `Sandbox.paginate()` con
   `order`, `started_after`, `states` y un cursor opaco reanudable. Página:
   `docs/site/docs/observability.md`.
6. **Superficie de E2B 2.x** (`m9-e2b-v2-surface`): el shim de Python
   `rayito.e2b` pasa a E2B 2.51 y llega el de TypeScript, `rayito/e2b`
   (misma entrada del paquete npm); `sbx.git` con la API git de E2B
   (`git-core` en la imagen); `rayito sandbox create|connect|exec|metrics`
   en la CLI. Páginas: `docs/site/docs/e2b-compat.md`, `git.md`, `cli.md`.

Qué imagen necesita cada feature (`rayito-base`, `-caps`, `-poly`) y el IAM
que añade M9 (sólo para las transferencias): `docs/site/docs/images.md`.

## Cambios que rompen

- **`rayito.e2b` es ahora el shim de E2B 2.x y exige una imagen M9.**
  `create()` siempre pide a `rayd` un plazo lógico; contra una imagen
  anterior termina el VM y lanza `UnimplementedError("lifecycle")`.
  `timeout` es el plazo lógico (300 s por defecto, como E2B) y el tope es
  `max_lifetime` (por defecto `max(3600, min(timeout + 60, 28800))`).
- **`rayito.e2b`: `sbx.pause()` devuelve `bool`** (antes el id).
- **`rayito.e2b`: `files.watch_dir(path, on_event=cb)` y
  `pty.create(size, on_data=cb)`**: el callback va por nombre; el segundo
  posicional es `user`, como en E2B 2.x.
- **`rayito.e2b`: lo que antes lanzaba ahora funciona**: `set_timeout`,
  `upload_url`/`download_url`, `get_metrics(start=, end=)`,
  `list(next_token=)` y `allow_internet_access=False` ya no son
  `UnimplementedError`, y `mcp=`, `network=` y `lifecycle=` ya no son
  `TypeError` (se mapean o lanzan `UnimplementedError`). Un código que
  capturaba esos errores para degradar cambia de camino.
- **TypeScript: un disco lleno es `DiskFullError`** (antes `RateLimitError`).
- El SDK nativo no rompe: `Sandbox.create()` sin `max_lifetime` ni
  `on_timeout` se comporta byte a byte como en 0.2.0 (ADR-007 para ese
  camino).

La tabla de migración del shim 1.x al 2.x está en
`docs/site/docs/e2b-compat.md` ("Migrar desde el shim 1.x").

## Cómo actualizar

1. **Actualiza los SDKs** a 0.3.0 (`pip install -U rayito`,
   `pnpm add rayito@0.3.0`) y, si usas la CLI, `pip install -U "rayito[cli]"`.
2. **Publica las imágenes desde el tag de 0.3.0**, las que uses:
   `make image-publish BUCKET=amzn-s3-demo-bucket` (`rayito-base`),
   `make image-publish-caps ...` (política de egress) y
   `make image-publish-poly ...` (bash, JavaScript, TypeScript). Con
   `AWS_PROFILE=<tu-perfil>`.
3. **Comprueba la cuenta** con `rayito doctor --template rayito-base`: la
   comprobación `compatibility` exige el `rayd` de 0.3.0 para el SDK 0.3.
4. **Si vas a usar transferencias**, crea el bucket (misma región que los
   sandboxes, sin puntos en el nombre, ciclo de vida de 1 día, política
   SigV4 + TLS) y añade al llamante `s3:PutObject`, `s3:GetObject`,
   `s3:DeleteObject`, `s3:AbortMultipartUpload` sobre
   `<bucket>/rayito-transfer/*` y `s3:ListBucket` acotado al prefijo
   (parámetros `TransferBucket`/`TransferPrefix` de `infra/iam.yaml`;
   receta en `infra/README.md`). Configúralo con
   `transfer=S3Staging("amzn-s3-demo-bucket")` o
   `RAYITO_TRANSFER_BUCKET=amzn-s3-demo-bucket` (la única vía en el shim).
5. **Si usabas el shim de E2B de 0.2.0**, repasa "Cambios que rompen" y
   la tabla de migración de `e2b-compat.md`. Si vienes de E2B directamente,
   cambia el import (`from rayito.e2b import Sandbox`,
   `import { Sandbox } from "rayito/e2b"`).
6. **Guarda el access token** de cada sandbox junto a su id si vas a usar
   las formas de clase (`Sandbox.set_timeout(id)`,
   `Sandbox.get_metrics_history(id)`, `Sandbox.update_network(id)`) o la
   CLI (`--token-file`); en el shim está en `sbx.native.access_token`.

## Limitaciones conocidas

- **DNS bajo deny-all en `rayito-base-caps`.** Los resolvedores de la
  plataforma escuchan dentro del guest, así que con
  `allow_internet_access=False` un nombre **puede resolverse** aunque
  ninguna conexión salga del VM (Q66). Es la adenda de ADR-012, opción C, y
  deja un canal residual de exfiltración por consultas DNS (`SECURITY.md`
  T17). El único control fuera del guest sigue siendo un conector VPC propio
  con security group deny-all, que tampoco filtra el DNS de Amazon.
- **Modo `pause` honesto-parcial con el cliente muerto.** Con un cliente
  vivo, el SDK suspende el sandbox ≈ 1 s después del plazo; sin ninguno,
  lo suspende la política de idle de la plataforma al cumplirse
  `max_idle_seconds` sin tráfico. Entretanto los procesos siguen corriendo.
  La política de idle puede suspender antes del plazo, y un sandbox pausado
  sigue muriendo en el tope de 8 h, que cuenta el tiempo suspendido.
- **Una suspensión real de menos de 2 s** que cruza el plazo no la reconoce
  `rayd`; con `auto_resume` la cubre el SDK que suspendió (arreglo de esta
  release), no otro cliente.
- **Tope de 8 h** (`max_lifetime` ≤ 28 800 s): las 86 400 s de E2B Pro no
  existen; más allá sólo queda `reincarnate()` (ficheros, id nuevo).
- **Hostnames sólo por proxy**: las reglas por nombre de host valen en los
  puertos 80/443 y para clientes que honran `HTTPS_PROXY`; el resto falla
  cerrado. UDP/QUIC no pasan por el proxy; root dentro del guest no se
  filtra.
- **`network.https_ports`** no vacío es `UnimplementedError`: medido, el
  proxy de Lambda MicroVMs no reenvía TLS extremo a extremo a un puerto del
  guest (Q67); `get_host(puerto)` sirve HTTP en claro.
- **Transferencias**: hace falta un bucket tuyo; la subida aterriza de forma
  asíncrona (barrera de 2 s en `rayd` y `ticket.wait()`); tickets de un solo
  uso; la URL vive como mucho lo que tus credenciales (8 h con SSO en Q62).
- **Historial de métricas** con hueco mientras el sandbox está suspendido;
  la CPU y la memoria son la vista del guest.
- **`order` y los filtros de `list`** se calculan en cliente (O(páginas));
  `metadata=` sondea cada sandbox `RUNNING`.
- **R y Java** siguen fuera (`SPEC.md` §4).

## Diferido al siguiente ciclo

- **Egress, opción A**: bloquear el DNS de uid ≥ 1000 bajo deny-all en
  `rayito-base-caps` con una regla `ip rule` para el puerto 53 antes de la
  regla `local`, con cambio atómico y rollback que nunca dejen el guest sin
  enrutamiento local (adenda de ADR-012). Cuando llegue, la aceptación de
  DNS vuelve a "la resolución falla".
- **Certificados TLS de los tests de `rayd` generados con `rcgen`**: los de
  `crates/rayd/tests/fixtures/tls/` son una clave privada y un certificado
  autofirmado de prueba (CA y host ficticios, sólo para el test unitario del
  cliente HTTPS de transferencias) fijos en el repositorio; los escáneres de
  seguridad los marcan. Se generarán en tiempo de test.
- **La ventana de rotación del kernel en `rayd` tras `/run`**: hoy la cierran
  los SDK exigiendo `sandbox_id` en la readiness (arreglo de esta release,
  `AWS_API_NOTES.md` Q78); el cierre en el propio agente (marcar `Rotating` de forma síncrona
  en `request_rotation`, una ventana residual del orden de 100 µs) queda para
  el siguiente ciclo porque exige republicar las imágenes.
- **`egress_settling` ante un pánico de la tarea de deny-all**: si esa tarea
  entrase en pánico, `Health` seguiría sin dar el agente por listo y el SDK
  terminaría la VM por timeout de readiness (falla cerrado); liberar el
  estado también en ese camino daría un error más rápido.
- **Reconexión tras un reset de stream del proxy de AWS** (`RST_STREAM`,
  "Stream removed" antes del plazo real): tratarlo como corte reconectable
  para que el handle se reenganche con `Connect(pid, from_seq)` en vez de
  fallar; visto una vez en la regresión e2e, no reproducido en 4 intentos.
- **Mensaje amable de `rayito-mcp` sin el extra**: sin `rayito[mcp]`,
  `rayito-mcp` acaba en un `ModuleNotFoundError`; la CLI `rayito` ya dice
  cómo instalar su extra y el servidor MCP tendrá el mismo mensaje.
- **`--remap-path-prefix` en los jobs `rayd` de CI y de release**: `make
  build` quita del binario las rutas del constructor, pero los workflows
  compilan sin ese `rustflags`, así que el `rayd` de la GitHub Release
  lleva rutas del runner; se verifica con un run de Actions.
- **Salida del sidecar dentro de la gracia de `SIGTERM`** en modo `kill`:
  `rayd` manda `SIGKILL` a los 5 s de todas formas y la e2e termina con
  código 124; confirmarlo desde el log de `rayd` en CloudWatch queda para
  el siguiente ciclo.
- Las filas diferidas de `docs/SECURITY_AUDIT.md` §8 siguen diferidas;
  ningún cambio de M9 las empeora.

## Documentación nueva o reescrita

| Página | Qué contiene |
|---|---|
| `docs/site/docs/e2b-compat.md` | migración (un import), tablas de imports, qué se mapea y cómo, los motivos literales de `UnimplementedError` |
| `docs/site/docs/e2b-parity.md` | la tabla de paridad de 113 filas con estado y página |
| `docs/site/docs/images.md` | qué imagen necesita cada feature, IAM del llamante e infraestructura |
| `docs/site/docs/lifecycle.md` | plazo del servidor en Python (sync y async) y TypeScript |
| `docs/site/docs/files.md` | transferencias, ficheros grandes, gzip y metadatos |
| `docs/site/docs/network.md` | política de egress en `rayito-base-caps` |
| `docs/site/docs/observability.md` | historial de métricas y listado reanudable |
| `docs/site/docs/kernels.md`, `git.md`, `cli.md` | JS/TS con Deno, `sbx.git`, `rayito sandbox create/connect/exec/metrics` |
| `ARCHITECTURE.md` ADR-010 a ADR-013, `SECURITY.md` T16/T17 | las decisiones y el modelo de amenazas de M9 |
