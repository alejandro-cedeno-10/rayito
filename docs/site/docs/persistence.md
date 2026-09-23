# Persistencia

Un sandbox vive como mucho 8 h y `kill()` borra todo lo que hay dentro. La
persistencia (M7, `m7-s3-persistence`, ADR-009) guarda el `HOME` del usuario
(`/home/user`) en tu bucket de S3 y lo restaura en el sandbox siguiente: los
**ficheros** sobreviven al muro de las 8 h y a `kill()`; las variables del
kernel, los procesos y las PTY no (ADR-007).

Lo hace `rayd`, el agente de la VM, **como root y con el execution role**
leído por IMDSv2: el código del sandbox (uid 1000) sigue sin poder alcanzar
IMDS en la imagen `rayito-base-caps`, y nada pasa por tu máquina.

## Quickstart

```python
from rayito import S3Prefix, Sandbox

sbx = Sandbox.create(
    "rayito-base-caps",
    execution_role_arn="arn:aws:iam::123456789012:role/rayito-m0-execution-us-east-1",
    persist=S3Prefix("mi-bucket", prefix="rayito-home", name="agente-7"),
)
print(sbx.last_restore)          # None la primera vez; después, lo restaurado
sbx.files.write("/home/user/notas.txt", "hola")
sbx.checkpoint_files()           # -> s3://mi-bucket/rayito-home/agente-7/home.tar.gz
sbx.kill()

sbx2 = Sandbox.create("rayito-base-caps", execution_role_arn=..., persist=sbx.persist)
print(sbx2.files.read("/home/user/notas.txt"))   # "hola"
```

```typescript
import { S3Prefix, Sandbox } from "rayito";

const sbx = await Sandbox.create({
  template: "rayito-base-caps",
  executionRoleArn: "arn:aws:iam::123456789012:role/rayito-m0-execution-us-east-1",
  persist: new S3Prefix({ bucket: "mi-bucket", prefix: "rayito-home", name: "agente-7" }),
});
await sbx.checkpointFiles();
const next = await sbx.reincarnate();   // 8 h frescas con el mismo HOME
```

## `S3Prefix`

`S3Prefix(bucket, prefix="rayito", name=None, region=None)` nombra dónde vive
un `HOME` persistido: `s3://bucket/prefix/name/`.

- `prefix` es la base del operador, la que acota la política IAM del
  execution role: tiene que coincidir con el `PersistencePrefix` del
  despliegue (`spike/m0/iam.yaml`, por defecto `rayito-home`) o cada
  `checkpoint_files()` responde
  `PersistenceException(code="permission_denied")`. **El `prefix="rayito"`
  por defecto del SDK no sirve para un despliegue real**: `rayito/` es el
  espacio de nombres de los artefactos de imagen (`rayito/images/*`) y el
  `*` de una política IAM atraviesa `/`, así que un prefijo cuyo primer
  segmento sea `rayito` mete el `HOME` persistido y los zips desde los que
  se construyen las imágenes bajo el mismo permiso. Pasa siempre un
  `prefix=` cuyo primer segmento no sea `rayito` (la plantilla añade además
  un `Deny` explícito sobre `<ArtifactBucket>/rayito/*`, pero sólo cubre ese
  bucket).
- `name` identifica un home concreto. `Sandbox.create(persist=)` lo fija al
  `sandbox_id` si viene vacío; para que dos vidas compartan el mismo home,
  pásalo tú. `connect(persist=)` y `restore_files(source=)` lo exigen.
- `region` sólo si el bucket no está en la región del sandbox (por defecto
  `AWS_REGION` de la VM).
- Se valida en cliente con las mismas reglas que aplica `rayd`: bucket de 3 a
  63 caracteres `[a-z0-9.-]`, prefijo de hasta 900 bytes sin `/` inicial ni
  final, sin `//`, `.` ni `..`, sólo el conjunto seguro de S3
  (`A-Za-z0-9!_.*'()-/`).

**El prefijo no separa inquilinos.** `Checkpoint` y `Restore` aceptan
cualquier `bucket`/`prefix`/`name` sintácticamente válido: `rayd` no
comprueba que el destino sea el del sandbox que lo pide, y el `sandbox_id`
del manifest es informativo. El único límite es hasta dónde llega el
execution role, es decir todo `<bucket>/<prefix>/*`, así que quien tenga el
access token de un sandbox puede leer o sobrescribir el `HOME` de cualquier
otro bajo ese mismo prefijo. Para inquilinos que no confían entre sí: un
execution role y un prefijo por inquilino, no un `name` por inquilino.

Bajo el prefijo hay dos objetos: `home.tar.gz` (tar POSIX/GNU comprimido con
gzip nivel 1) y `manifest.json` (sha256 del archivo, tamaños, recuentos,
`sandbox_id`, versión del agente, lista de exclusión). El manifest se escribe
**después** del archivo: si existe, el archivo está completo.

## Qué se archiva y qué no

`checkpoint_files(exclude=(), timeout=600)` empaqueta el `HOME` del usuario
que hace la petición (nunca `/root`, diga lo que diga `RAYITO_ALLOW_ROOT`):

- ficheros regulares (modo `0o7777`, mtime), directorios y symlinks con su
  destino **tal cual** (nunca se resuelven ni se siguen; un directorio
  enlazado no se desciende);
- **lista de exclusión fija**, siempre: los componentes `.cache`,
  `__pycache__`, `.ipynb_checkpoints` y `.rayito-tmp-*` a cualquier
  profundidad, y `.local/share/jupyter/runtime`,
  `.ipython/profile_default/history.sqlite` (y su `-journal`);
- `exclude`: hasta 64 rutas relativas al `HOME`, sin globs, por componentes
  enteros (`data/raw` excluye `data/raw/…` pero no `data/raw2`);
- se saltan y se cuentan en `skipped`: sockets, FIFOs, dispositivos, lo que el
  usuario no puede leer y lo que está en otro sistema de ficheros (un tmpfs
  montado dentro del `HOME`). Los hard links viajan como ficheros
  independientes.

La lectura corre bajo la identidad del usuario, así que un fichero que el
usuario no puede leer no lo lee `rayd` como root. Un fichero que cambia
mientras se archiva sale truncado o rellenado con ceros hasta el tamaño del
`lstat`: **pausa tu trabajo antes de un checkpoint** si necesitas una copia
consistente. Un solo checkpoint o restore a la vez por sandbox
(`PersistenceException(code="failed_precondition")`).

## Qué hace un restore

`restore_files(source=None, timeout=600)` descarga el manifest (`NotFoundException`
si no hay ninguno) y extrae el archivo sobre el `HOME`:

- los ficheros existentes se sobrescriben, los directorios se fusionan y un
  symlink que ocupe la ruta se borra antes (nunca se sigue);
- sólo entran ficheros regulares, directorios y symlinks; hard links,
  dispositivos, FIFOs y sockets se saltan; una entrada con `..`, absoluta o
  cuyo padre salga del `HOME` a través de un symlink aborta el restore
  (`invalid_argument`);
- los bits setuid/setgid/sticky se descartan (`0o777`) y todo lo creado es
  del usuario;
- el sha256 del archivo se comprueba al final (`internal`,
  `archive checksum mismatch`).

Un restore que falla a mitad deja el `HOME` **parcialmente restaurado**; la
recuperación es `kill()` + `create(persist=)`. `create(persist=)` con un `name`
dado restaura automáticamente (`sbx.last_restore`) y, si el restore falla por
algo distinto de "no hay checkpoint", cierra y termina el sandbox como un fallo
de readiness (salvo `keep_on_failure`).

## `reincarnate()`: más allá de `max_lifetime`

`sbx.reincarnate(exclude=(), persist_timeout=600)` hace, en este orden:
`checkpoint_files()` → `Sandbox.create(**mismas opciones de lanzamiento,
persist=sbx.persist)` (que restaura) → `kill()` del sandbox viejo, y devuelve el
nuevo. El nuevo tiene 8 h frescas, otro `sandbox_id`, otro access token
(salvo que el original fuera explícito) y los mismos `metadata`. Si el
`create()` falla, el sandbox viejo sigue vivo y la excepción lleva una nota con
la `uri` del checkpoint ya completo. Sólo sobre un sandbox de
`create(persist=)`: un handle de `connect()` no conoce el lanzamiento.
Desde M9 `set_timeout()` (nativo y en `rayito.e2b`) mueve el plazo lógico
que impone `rayd`, pero nunca más allá de `max_lifetime` (el tope de la
plataforma, ≤ 8 h, fijo tras `create()`, ADR-011): pasado ese tope,
`reincarnate()` sigue siendo el único camino, con los ficheros y sin la
memoria.

## IAM y bucket

`spike/m0/iam.yaml` acepta `PersistenceBucket` y `PersistencePrefix`; con el
bucket definido, el execution role recibe exactamente:

```yaml
- Effect: Allow
  Action: [s3:PutObject, s3:GetObject, s3:AbortMultipartUpload]
  Resource: arn:aws:s3:::<bucket>/<prefix>/*
- Effect: Allow
  Action: s3:ListBucket
  Resource: arn:aws:s3:::<bucket>
  Condition: { StringLike: { s3:prefix: "<prefix>/*" } }
```

Sin `s3:DeleteObject` (el rol nunca borra) y sin `s3:ListMultipartUploadParts`.
`s3:ListBucket` existe sólo para que una clave ausente responda `404
NoSuchKey` en vez de `403`, de lo que depende `NotFoundException`.

Consejos de operación: el bucket en la **misma región** que la imagen (o pasa
`region=`); SSE-S3 por defecto basta (SSE-KMS exige añadir `kms:Decrypt` y
`kms:GenerateDataKey` al rol: no viene en la plantilla); añade una regla de
ciclo de vida `AbortIncompleteMultipartUpload` a 1 día sobre el prefijo (una
VM matada a mitad de checkpoint puede dejar partes huérfanas); con un
conector de egress propio (`infra/egress-connector.yaml`) S3 necesita un
gateway endpoint o un NAT. `rayd` no envía parámetros de cifrado, clase de
almacenamiento ni ACL: aplican los valores por defecto del bucket
(`AWS_API_NOTES.md` §17 lista cada operación y parámetro).

## Errores

| Situación | Python | TypeScript |
|---|---|---|
| `persist=` sin `execution_role_arn`, `exclude` inválido, `S3Prefix` sin `name` | `InvalidArgumentException` (antes de tocar AWS) | `InvalidArgumentError` |
| sin execution role, `AccessDenied`, credenciales caducadas, `user="root"` | `PersistenceException(code="permission_denied")` | `PersistenceError` `permission_denied` |
| no hay checkpoint bajo el prefijo (`restore_files` explícito) | `NotFoundException` | `NotFoundError` |
| otra operación en curso, región desconocida | `PersistenceException(code="failed_precondition")` | `failed_precondition` |
| red, S3 5xx, tar/gzip, checksum, disco lleno | `PersistenceException(code="internal")` | `internal` |
| stream cortado a mitad (`/suspend`, proxy) | `PersistenceException(code="interrupted")`: repite la operación, el SDK no la reanuda | `interrupted` |
| imagen con un `rayd` anterior a M7 | `PersistenceException(code="unimplemented")` con la instrucción de republicar | `unimplemented` |
| deadline (`timeout`, 600 s por defecto) | `TimeoutException` | `TimeoutError` |

## Coste y números medidos

Un checkpoint mueve el `tar.gz` del `HOME` de la VM a S3 (transferencia de
datos del conector `INTERNET_EGRESS` + `PutObject` en partes de 8 MiB) y un
restore lo trae de vuelta; el almacenamiento es el de S3 (≈ $0,023/GB-mes).
El binario de `rayd` crece por el cliente TLS (`rustls` + `aws-lc-rs`) y el
SDK de S3: **4 700 984 → 12 524 384 B** (+7,8 MB, ×2,66); el build limpio
ARM64 pasa de 106 s a 222 s en la máquina de desarrollo.

Medido el 2026-09-17 contra AWS real (`tests/e2e/test_m7_persistence.py`,
`rayito-base-caps` 7.0, bucket en la misma región, 50 MB de datos aleatorios
en 21 entradas; `AWS_API_NOTES.md` Q53/Q54):

| Operación | Agente | Pared (cliente) | Caudal |
|---|---|---|---|
| primer `checkpoint_files()` (código TLS/S3 aún sin paginar) | 1,50 s | 1,67 s | 31,4 MB/s |
| segundo `checkpoint_files()` | 1,40 s | 1,50 s | 35,0 MB/s |
| `restore_files()` dentro de `create(persist=)` | 0,67 s | 7,35 s con el `create` | 78,0 MB/s |
| `reincarnate()` completo | — | 8,85 s | — |
| `restore_files(source=)` sin checkpoint → `NotFoundException` | — | 0,11 s | — |
| `checkpoint_files()` sin execution role → `permission_denied` | — | 1,35 s | — |

El archivo de 50 MB aleatorios pesa 52 479 326 B (gzip nivel 1 no comprime
ruido; un `HOME` de texto y notebooks sí encoge). `imds_blocked` siguió en
`true` en la imagen caps durante todo el ciclo: la persistencia no abre IMDS
al código del sandbox. No se midió a 2 GB.
