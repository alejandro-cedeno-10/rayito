# Design — m8-security-fixes

Every decision below is closed: the exact file, the exact new code or
wording, why that shape and not another, and which test proves it. Nothing
here needs an AWS call: the whole change is verifiable with the local gates
(`cargo fmt/clippy/test`, `uv run pytest/ruff/mypy`, `pnpm lint/typecheck/test/build`,
`uvx cfn-lint==1.56.3`, `actionlint`, `mkdocs build --strict`, `pytest scripts/tests`).

Source of truth for every row: `docs/SECURITY_AUDIT.md` §4 (H-01…H-06), §5
(C-01…C-13) and the triage table of §8. When this document says "the audit
asks for X", X is the *Corrección* paragraph of that finding, not a
reinterpretation of it.

---

## D1 — H-01 (1): the persistence prefix default stops overlapping the artifact prefix

**File**: `spike/m0/iam.yaml:28-33`.

**Today**: `PersistencePrefix` has `Default: rayito`, and the artifact
namespace is `arn:aws:s3:::${ArtifactBucket}/rayito/*` (`:64` for `BuildRole`,
`:187` for `CallerPolicy`, `rayito/images/...` written by
`clients/python/src/rayito/cli/_publish.py:74`). With one bucket — which is
exactly what `infra/README.md:159-160` tells the reader to do — the execution
role's `${PersistenceBucket}/${PersistencePrefix}/*` covers
`rayito/images/rayd-<hash>.zip`.

**New**:

```yaml
  PersistencePrefix:
    Type: String
    Default: rayito-home
    AllowedPattern: "^[A-Za-z0-9_.-][A-Za-z0-9_./-]*[A-Za-z0-9_.-]$|^[A-Za-z0-9_.-]$"
    Description: >-
      Key prefix under PersistenceBucket the role may read and write (no leading
      or trailing slash); S3Prefix(prefix=) in the SDK must match it. Never
      `rayito` or a prefix of it: that namespace holds the image artifacts
      (`rayito/images/*`) and the execution role must not reach them.
```

**Why `rayito-home` and not something shorter**: the objects under it are
`<prefix>/<name>/home.tar.gz` and `<prefix>/<name>/manifest.json`
(`crates/rayd-core/src/persistence/keys.rs`), so the name says what lives
there. It is also *not* a path prefix of `rayito/`: IAM's `*` crosses `/`,
so only a value whose first path segment differs from `rayito` is disjoint
by construction. `rayito-home/*` and `rayito/*` share no key.

**Why not "move the artifacts instead"**: the artifact prefix is baked into
`_publish.py`, the `BuildRole`, `CallerPolicy`, the deployed stack and the
three published images. Changing the parameter default is the one-line half
of the fix; changing the artifact layout is not, and the audit explicitly
offers the default change as the alternative ("cambiar el `Default` de
`PersistencePrefix` para que no sea `rayito`").

**Blast radius**: the deployed stack passes `PersistencePrefix=rayito-e2e`
explicitly (`infra/README.md:186-191`), so no deployed resource moves. No
SDK default mentions the value; `docs/site/docs/persistence.md` only ever
writes `<prefix>`.

**Proof**: the `cfn-lint` gate plus the delta scenario of
`filesystem-persistence` ("the default deployment cannot reach the
artifacts") and a `grep` in task 1.4 confirming `rayito-home` is the only
default left in the template and the README.

---

## D2 — H-01 (2): an explicit Deny on the execution role over the artifact prefix

**File**: `spike/m0/iam.yaml`, inside the conditional `persistence` policy
of `ExecutionRole` (after the `MissingKeyIs404` statement, `:118-125`).

**New statement**:

```yaml
                - Sid: NeverTheImageArtifacts
                  Effect: Deny
                  Action: s3:*
                  Resource:
                    - !Sub arn:aws:s3:::${ArtifactBucket}/rayito/*
```

**Why a `Deny` at all when D1 already separates the namespaces**: D1 fixes
the *default*; an operator who types `PersistencePrefix=rayito` (or reuses
an old stack parameter) puts it straight back. An explicit `Deny` cannot be
widened by any later `Allow` in any policy attached to the role, so the
artifact namespace is out of reach whatever the parameters say. That is the
"por construcción" the audit asks for.

**Why `Action: s3:*` and not the three object actions**: the role's
`Allow` is three actions today and may gain a fourth tomorrow; a `Deny` that
enumerates actions has to be kept in sync with the `Allow` or it silently
stops covering. `s3:*` in a `Deny` has no over-reach failure mode.

**Why inside the conditional policy and not a fourth top-level policy**:
the statement is only meaningful when the role has S3 permissions at all,
and that whole policy is already `!If [HasPersistenceBucket, …]`. Keeping it
there means a stack deployed without `PersistenceBucket` renders exactly the
same document it renders today.

**Why `${ArtifactBucket}/rayito/*` and not `.../rayito/images/*`**: the
execution role has no business anywhere under the artifact prefix, and
`rayito/` is the prefix `CallerPolicy` and `BuildRole` already name. The
audit's narrower suggestion (`rayito/images/*`) is contained in this one.

**Known consequence, documented in the README**: if somebody deploys with
`ArtifactBucket == PersistenceBucket` *and* `PersistencePrefix=rayito`, the
`Deny` wins and persistence fails closed with `AccessDenied`. That is the
intended outcome and D3 says so in prose.

**cfn-lint risk**: `cfn-lint` 1.56.3 has no rule against a wildcard action
in a `Deny` (the wildcard rules `W11`/`E3510` target `Allow` with
`Resource: "*"`). If a future version disagrees, the fallback recorded here
is to enumerate `s3:PutObject`, `s3:GetObject`, `s3:AbortMultipartUpload`
and `s3:ListBucket` and to note the sync obligation in the template
description — never to drop the statement. Task 1.5 runs the linter.

**Proof**: `uvx cfn-lint==1.56.3` clean, plus the delta scenario that reads
the rendered policy.

---

## D3 — H-01 (3): the README recipe stops reusing one bucket

**File**: `infra/README.md:154-170` (the "Persistencia en S3" block).

**Today** (`:159-160`):

```bash
  --parameter-overrides ArtifactBucket=<bucket> LogGroupPrefix=/rayito \
      PersistenceBucket=<bucket> PersistencePrefix=rayito
```

**New** (Spanish, like the rest of the file):

```bash
  --parameter-overrides ArtifactBucket=<bucket-de-artefactos> LogGroupPrefix=/rayito \
      PersistenceBucket=<bucket-de-persistencia> PersistencePrefix=rayito-home
```

plus this paragraph right below the block:

> **Dos espacios de nombres, nunca uno.** Los artefactos de imagen viven bajo
> `rayito/` del `ArtifactBucket` (`rayito/images/*`, los zips que
> `create/update-microvm-image` descarga con el `BuildRole`). El execution
> role lo lee el código del sandbox por IMDS en la imagen por defecto (T1),
> así que si `PersistenceBucket` y `PersistencePrefix` solapan ese espacio,
> ese código sobrescribe el zip desde el que se construye la siguiente
> imagen. Usa buckets distintos o, como mínimo, un prefijo cuyo primer
> segmento no sea `rayito` (el `*` de IAM atraviesa `/`). La plantilla añade
> además un `Deny` explícito sobre `arn:aws:s3:::<ArtifactBucket>/rayito/*`:
> si alguien despliega los dos parámetros sobre el mismo espacio, la
> persistencia falla cerrada con `AccessDenied` en vez de alcanzar los
> artefactos.

**Why prose and not only the command**: the audit's point is that the recipe
is what other people copy, and a copied command without the reason gets
"simplified" back to one bucket by the next reader.

**Also updated in the same block**: the sentence at `:168-169` that quotes
the old `AllowedPattern` (`^[A-Za-z0-9!_.*'()/-]+$`) must quote the new one
of D4, or the README immediately contradicts the template.

**Not touched here**: `infra/README.md:122` (the OIDC branch claim, H-06)
belongs to `m8-security-docs`. The two changes edit different sections of
the same file; whoever lands second re-reads the file first.

**Proof**: task 1.6 greps the README for `PersistencePrefix=rayito$` and
for the old pattern; `mkdocs build --strict` does not cover `infra/`, so the
check is the grep plus review.

---

## D4 — C-13: the AllowedPattern of PersistencePrefix loses `*` and friends

**File**: `spike/m0/iam.yaml:31`.

**Today**:

```
^[A-Za-z0-9!_.*'()-][A-Za-z0-9!_.*'()/-]*[A-Za-z0-9!_.*'()-]$|^[A-Za-z0-9!_.*'()-]$
```

**New** (already shown in D1):

```
^[A-Za-z0-9_.-][A-Za-z0-9_./-]*[A-Za-z0-9_.-]$|^[A-Za-z0-9_.-]$
```

**What it drops**: `*`, `!`, `'`, `(` and `)`. **What it keeps**: letters,
digits, `_`, `.`, `-` and `/` in the middle — enough for `rayito-home`,
`rayito-e2e` (the deployed value) and any dated or per-tenant prefix.

**Why**: `PersistencePrefix=*` renders
`Resource: arn:aws:s3:::${PersistenceBucket}/*/*` and `s3:prefix: */*`, so a
typo turns the boundary the parameter exists to draw into "most of the
bucket". The other four characters are S3-legal but have no business in a
deployment parameter and each one is a quoting hazard in the shell recipes
of the README.

**Explicitly NOT touched** (the audit is emphatic, and the triage row
repeats it): `clients/python/src/rayito/_models.py` and
`crates/rayd-core/src/persistence/keys.rs`. `*` is a legal S3 key
character; making the SDK stricter than S3 would create a real divergence
(the agent would refuse keys the bucket accepts) to close a hypothetical
one. The template is an input-hygiene surface; the SDK validator is a wire
contract. They are allowed to differ, and D3's README sentence says which
is which.

**Proof**: `uvx cfn-lint==1.56.3` clean and a delta scenario asserting that
`PersistencePrefix=*` is rejected by CloudFormation's parameter validation
while `rayito-e2e` and `rayito-home` are accepted (checked at
`validate-template` time in the task note, no deploy needed).

---

## D5 — C-05: `authorize_identity` becomes a positive check, and the duplicate gate dies

**Files**: `crates/rayd-core/src/process/identity.rs:49-55` (the gate),
`crates/rayd-core/src/process/error.rs`,
`crates/rayd-core/src/filesystem/error.rs`,
`crates/rayd-core/src/filesystem/identity.rs:40-45` (the error mapping),
`crates/rayd-core/src/persistence/error.rs`,
`crates/rayd-core/src/persistence/mod.rs:101-119` (the duplicate gate),
`crates/rayd/src/grpc/process.rs:233`, `crates/rayd/src/grpc/pty.rs:225`,
`crates/rayd/src/grpc/filesystem.rs:369`.

**Today**: two blacklists — `authorize` refuses the literal string `"root"`,
`authorize_identity` refuses `uid == 0`. `user="operator"` (uid 11, gid 0 in
the RHEL `setup` layout) or `user="bin"` (uid 1) passes both, gets
`setgroups/setgid/setuid` with those numbers
(`crates/rayd/src/adapters/process_spawner.rs:274-278`) and lands *outside*
the `uidrange 1000-65535` policy route that blackholes IMDS
(`adapters/imds_block.rs:47`), whose own comment claims to cover "the
sandbox user and anything it could become".

**New**, in `crates/rayd-core/src/process/identity.rs`:

```rust
/// Floor of the image's regular accounts. Below it live the system accounts,
/// which the IMDS blackhole of M6 (`uidrange 1000-65535`) does not cover and
/// which may own files of the root group.
pub const MIN_UNPRIVILEGED_ID: u32 = 1000;
/// The root group: membership grants read access to `/root` and to every
/// root-group file, which is what T11 and T15 assume nobody reaches.
pub const ROOT_GROUP_ID: u32 = 0;
```

```rust
    /// Second gate after the lookup, and the only one that sees numbers:
    /// `authorize` only ever saw a name, so an alias of uid 0, a system
    /// account below the floor and a member of the root group all arrive
    /// here. `RAYITO_ALLOW_ROOT=1` is an image-level opt-in and still
    /// bypasses the whole gate, exactly as before.
    pub fn authorize_identity(self, identity: &ProcessIdentity) -> Result<(), ProcessError> {
        if self.allow_root {
            return Ok(());
        }
        if identity.uid == 0 {
            return Err(ProcessError::RootNotAllowed);
        }
        if is_unprivileged(identity) {
            Ok(())
        } else {
            Err(ProcessError::PrivilegedAccount)
        }
    }
```

```rust
fn is_unprivileged(identity: &ProcessIdentity) -> bool {
    identity.uid >= MIN_UNPRIVILEGED_ID
        && identity.gid >= MIN_UNPRIVILEGED_ID
        && !identity.groups.contains(&ROOT_GROUP_ID)
}
```

**Why keep the `uid == 0` arm instead of folding root into the new error**:
`RootNotAllowed`'s message ("running as root is not allowed by this image")
is what the e2e and the SDK surface today for `user="root"`
(`process-lifecycle` scenario "root refused by default"), and the audit
never asked to change it. Root keeps its own, accurate message; everything
else gets a new, accurate one.

**Why a new error variant instead of reusing `RootNotAllowed`**: the
`Display` strings of the domain errors are the gRPC status messages
(`crates/rayd-core/src/process/error.rs:1-3`). Telling an operator who asked
for `user="operator"` that "running as root is not allowed" is a lie in the
one place people read when debugging. The new variant is:

```rust
    #[error("only unprivileged accounts of this image may run code (uid and gid >= 1000, never in group 0)")]
    PrivilegedAccount,
```

with twins `FilesystemError::PrivilegedAccount` and
`PersistenceError::PrivilegedAccount` (messages in the same style), mapped
to `PERMISSION_DENIED` next to `RootNotAllowed` in the three gRPC mappers
and to `StatusKind::PermissionDenied` in
`PersistenceError::status_kind` (so `stream_code()` is `permission_denied`,
the code the SDK already maps). No message quotes the username: the rule of
`messages_never_quote_input` holds.

**Why `allow_root` bypasses everything except persistence**: it is set by the image
(`RAYITO_ALLOW_ROOT=1`, read once at boot, `main.rs:149`), never by a
request, and an image that opts into root has already accepted the
consequence. Narrowing that bypass would change the behaviour of images that
exist today for no finding in the audit. Persistence is the one exception,
and it predates this change: it refused root whatever the image said, and
`without_root()` is how it keeps doing so through the single gate.

**The duplicate gate**, `crates/rayd-core/src/persistence/mod.rs:116-118`:

```rust
    if identity.uid == 0 {
        return Err(PersistenceError::RootNotAllowed);
    }
```

is deleted. `resolve_home_identity` calls `resolve_identity`, which calls
`authorize_identity`; the `FilesystemError::RootNotAllowed =>
PersistenceError::RootNotAllowed` arm right above it already carries the
refusal, and the new arm `FilesystemError::PrivilegedAccount =>
PersistenceError::PrivilegedAccount` carries the rest. Keeping both would
leave two places to change the next time and is exactly what the audit asks
to remove.

That deletion is only safe because persistence stops inheriting the image
opt-in: `resolve_home_identity` passes `policy.without_root()`, a four-line
helper on `UserPolicy` that returns the same policy with `allow_root`
cleared. The deleted `uid == 0` check ran *after* `allow_root` had already
let root through, so it was the only thing keeping T15's "persistence nunca
archiva `/root`" true in an image with the opt-in; with `without_root()` the
shared gate is strictly stronger than that check on the persistence path —
it refuses root, every uid or gid below 1000 and every member of group 0 —
and the helper, not a second numeric check, is what must survive. Removing
`without_root()` would archive `/root` to S3 on any image with
`RAYITO_ALLOW_ROOT=1`, so the requirement names it.

**Hexagonal boundary**: everything above is in `rayd-core`; the only edits in
`crates/rayd` are three `match` arms in the gRPC mappers. No `tonic`, `axum`
or `tokio` type enters the domain, and no port trait is added.

**Premise the audit marks as unmeasured**: nothing in the repo records the
`/etc/passwd` of `public.ecr.aws/lambda/microvms:al2023-minimal`, so the
gid-0 span (`operator`, `sync`, `shutdown`, `halt`) is inference. The fix
does not depend on it: `bin`(1), `daemon`(2) and `adm`(3) are universal and
are refused by the uid floor alone. Measuring the base image and recording
it in `AWS_API_NOTES.md` needs an image pull, which this local-only change
cannot do; it stays in the M8 row of the triage table together with the two
`uidrange` rules and the e2e, and is named in D12.

**Tests that fail without the fix** (all unit, all local):

- `identity.rs`: `system_accounts_are_refused` — uid 11/gid 0, uid 1/gid 1,
  uid 1000 with `groups = [0]` and uid 1000/gid 0 all give
  `Err(ProcessError::PrivilegedAccount)`; uid 1000/gid 1000/groups `[1000]`
  gives `Ok(())`; uid 0 still gives `RootNotAllowed`; with
  `allow_root: true` every one of them is `Ok(())`.
- `identity.rs`: `without_root_drops_the_image_opt_in` — a permissive policy
  refuses uid 0 through `without_root()`, and `without_root()` is a no-op on
  the default policy.
- `persistence/mod.rs`: `root_is_never_a_persistence_identity` and
  `a_system_account_is_never_a_persistence_identity` run with
  `allow_root: true` as well as the default and still get
  `RootNotAllowed` / `PrivilegedAccount`.
- `process/spec.rs` and `pty/mod.rs`: `plan_spawn`/`plan_pty` over a lookup
  fake that resolves `"operator"` to uid 11/gid 0 return
  `Err(ProcessError::PrivilegedAccount)` (and `PtyError::Process(...)`).
- `filesystem/identity.rs`: the same lookup gives
  `Err(FilesystemError::PrivilegedAccount)`.
- `persistence/mod.rs`: `resolve_home_identity` with that lookup gives
  `Err(PersistenceError::PrivilegedAccount)`, and with a uid-0 lookup still
  gives `RootNotAllowed` — the test that proves deleting the duplicate gate
  changed nothing for root.
- `crates/rayd/src/grpc/{process,pty,filesystem}.rs` and
  `persistence/error.rs`: the existing status-mapping tables gain the new
  variant (`every_variant()` in `persistence/error.rs` is exhaustive, so the
  suite fails until it is listed).

---

## D6 — H-03: the Python pool backend writes through an exclusive descriptor

**File**: `clients/python/src/rayito/_pool_backends.py:93-109`.

**Today**: `os.open(<path>.tmp, O_CREAT | O_WRONLY | O_TRUNC, 0o600)`. The
name is fully predictable, `mode` applies only on creation, there is no
`O_EXCL` and no `O_NOFOLLOW`, and `_read` uses `Path.read_text`, which
follows symlinks.

**New**:

```python
NO_FOLLOW: Final = getattr(os, "O_NOFOLLOW", 0)
READ_FLAGS: Final = os.O_RDONLY | NO_FOLLOW
```

```python
    def _read(self) -> dict[str, SlotRecord]:
        try:
            descriptor = os.open(self._path, READ_FLAGS)
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise not_a_regular_file(self._path) from exc
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            text = handle.read()
        return records_from_document(parse_document(text, self._path))

    def _write(self, records: dict[str, SlotRecord]) -> None:
        document = {
            "schema": POOL_SCHEMA,
            "slots": [record_to_dict(record) for record in records.values()],
        }
        descriptor, temp = tempfile.mkstemp(
            dir=self._path.parent, prefix=f"{self._path.name}.", suffix=TEMP_SUFFIX
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                restrict_to_owner(handle.fileno())
                json.dump(document, handle, indent=2, sort_keys=True)
            os.replace(temp, self._path)
        except BaseException:
            discard(temp)
            raise
```

with three module-level helpers:

```python
def restrict_to_owner(descriptor: int) -> None:
    """`mkstemp` ya crea 0600 en POSIX; el `fchmod` explícito fija el modo
    sobre el descriptor —no sobre un nombre que otro uid podría haber
    cambiado entre medias— y es lo que hace cierta la frase de T14. Windows
    no tiene `fchmod` y tampoco el modelo de modos POSIX."""
    if hasattr(os, "fchmod"):
        os.fchmod(descriptor, FILE_MODE)


def discard(path: str) -> None:
    with suppress(OSError):
        os.unlink(path)


def not_a_regular_file(path: Path) -> InvalidArgumentException:
    return InvalidArgumentException(
        f"{path}: el fichero de estado del pool debe ser un fichero regular, no un enlace"
    )
```

**Why `tempfile.mkstemp` instead of `os.open(<path>.tmp, O_EXCL|O_NOFOLLOW)`**:
the audit offers both and calls `mkstemp` "mejor aún". With `O_EXCL` on a
fixed name, a pre-created `<path>.tmp` is no longer a hijack but it *is* a
permanent denial of service unless the code first `lstat`s it, proves it is
a regular file owned by the current uid and unlinks it — three syscalls of
security-sensitive branching (and `st_uid` does not exist on Windows) to
support a name the attacker can recreate between the `lstat` and the
`unlink`. A random name in the target directory removes the whole class:
there is nothing to pre-create, nothing to symlink and nothing to clean up
from a previous crash that could be confused with ours. `mkstemp` is
`O_CREAT|O_EXCL|O_RDWR` with 0600 by construction.

**Why the temporary stays in `self._path.parent`**: `os.replace` must be an
atomic rename, which requires the same filesystem. `TEMP_SUFFIX` survives as
the `suffix=` argument, so the exported constant keeps meaning and a leftover
is still recognisable as ours.

**Why `O_NOFOLLOW` on the read and an `InvalidArgumentException`**: the
audit's corrección for H-03 names the read (`_read` uses `read_text`, which
follows symlinks). After the write fix an attacker can still place a symlink
at `path` while no file exists and have the pool read *and trust*
`SlotRecord`s from wherever it points. Refusing to follow it, with the same
exception type the backend already raises for a foreign schema, keeps the
error surface unchanged for callers. On Windows `O_NOFOLLOW` does not exist,
`NO_FOLLOW` is `0` and the read behaves as today — the threat model of H-03
is a POSIX multi-uid host.

**Why not a lock file or `fcntl.flock`**: the backend is documented as
single-process (`sandbox-pool` spec, module docstring); concurrency is out
of scope for this row.

**Docstring updates in the same file**: the class docstring still says
"`<path>.tmp` (creado con `O_CREAT | O_WRONLY | O_TRUNC` y modo 0600)". It
becomes "un temporal de nombre aleatorio en el mismo directorio, creado en
exclusiva y con el modo fijado sobre el descriptor, promovido con
`os.replace`".

**Tests that fail without the fix** (`clients/python/tests/unit/test_pool_backends.py`,
POSIX-only where they touch modes or symlinks):

- `test_json_write_ignores_a_pre_created_temp`: create `<path>.tmp` with mode
  0o666 and known content, `save()`, then assert the state file is 0600, its
  content is the pool document, and `<path>.tmp` still holds the original
  bytes. Today the pool document lands in the attacker's 0666 inode and is
  renamed over the state file.
- `test_json_write_never_follows_a_symlink`: `<path>.tmp` is a symlink to
  `victim.txt`; after `save()`, `victim.txt` is untouched.
- `test_json_read_refuses_a_symlinked_state_file`: `pool.json` is a symlink
  to a valid pool file; `load()` raises `InvalidArgumentException`.
- `test_json_write_leaves_no_temporary`: after `save()` and `delete()`, the
  directory holds exactly `pool.json`.
- The existing `test_json_file_mode_is_0600` and the round-trip tests keep
  passing unchanged.

---

## D7 — H-04: the TypeScript twin, landed in the same change

**File**: `clients/typescript/src/pool/backend.ts:82-103`.

**Today**: `writeFile(temp, …, { encoding: "utf8", mode: FILE_MODE })` uses
flag `w` (`O_WRONLY|O_CREAT|O_TRUNC`), so Node applies `mode` only when it
creates the file and follows symlinks; `readFile(this.path, "utf8")` follows
them too.

**New**:

```ts
import { randomBytes } from "node:crypto";
import { constants } from "node:fs";
import { open, rename, unlink } from "node:fs/promises";

export const FILE_MODE = 0o600;
export const TEMP_SUFFIX = ".tmp";

const NO_FOLLOW = constants.O_NOFOLLOW ?? 0;
const READ_FLAGS = constants.O_RDONLY | NO_FOLLOW;

/** Nombre impredecible en el mismo directorio: no hay nada que precrear ni
 * que enlazar, y `rename` sigue siendo atómico por estar en el mismo
 * sistema de ficheros. */
function temporaryPath(path: string): string {
  return `${path}.${randomBytes(8).toString("hex")}${TEMP_SUFFIX}`;
}
```

```ts
  async #read(): Promise<Map<string, SlotRecord>> {
    let text: string;
    try {
      const handle = await open(this.path, READ_FLAGS);
      try {
        text = await handle.readFile("utf8");
      } finally {
        await handle.close();
      }
    } catch (error) {
      const code = (error as NodeJS.ErrnoException).code;
      if (code === "ENOENT") {
        return new Map();
      }
      if (code === "ELOOP") {
        throw new InvalidArgumentError(
          `${this.path}: el fichero de estado del pool debe ser un fichero regular, no un enlace`,
          { cause: error },
        );
      }
      throw error;
    }
    return recordsFromDocument(parseDocument(text, this.path));
  }

  async #write(records: Map<string, SlotRecord>): Promise<void> {
    const document = {
      schema: POOL_SCHEMA,
      slots: [...records.values()].map(recordToJson),
    };
    const temp = temporaryPath(this.path);
    try {
      const handle = await open(temp, "wx", FILE_MODE);
      try {
        await handle.chmod(FILE_MODE);
        await handle.writeFile(JSON.stringify(document, null, 2), "utf8");
      } finally {
        await handle.close();
      }
      await rename(temp, this.path);
    } catch (error) {
      await discard(temp);
      throw error;
    }
  }
```

```ts
/** El temporal puede no existir (el fallo pudo ser el propio `open`), así que
 * un borrado que falla no vuelve a lanzar: el error que importa es el
 * original. */
async function discard(path: string): Promise<void> {
  try {
    await unlink(path);
  } catch {
    return;
  }
}
```

**Why `wx` plus `chmod` and not only `wx`**: `open(…, "wx", mode)` is
`O_CREAT|O_EXCL|O_WRONLY`, so the file is ours and brand new — but the mode
argument is still subject to the process `umask` (a `umask` of `0` is not
required, and Node does not apply one for us). `handle.chmod(FILE_MODE)` on
the open handle fixes the mode on the object we hold, before the first byte.
On Windows `chmod` is a no-op for group/other bits, exactly like today.

**Why `?? 0` on `constants.O_NOFOLLOW`**: the constant is absent on Windows.
If the repo's `eslint`/`tsc` settings flag the coalescing as unnecessary
(the `@types/node` declaration is non-optional), the recorded fallback is
`const NO_FOLLOW = (constants as Partial<typeof constants>).O_NOFOLLOW ?? 0;`
— never dropping the guard, because a missing constant would make
`READ_FLAGS` `NaN` and break every read on Windows.

**Why the same `catch`/`discard` shape as Python**: both SDKs must leave the
directory with exactly the state file after any failure, so the
`sandbox-pool` scenario ("no temporary remains") reads the same for both.

**Tests that fail without the fix**
(`clients/typescript/tests/unit/pool-backend.test.ts`, symlink and mode
assertions skipped on Windows via `process.platform`): the four tests of D6,
one for one. The "ignores a pre-created temp" test writes
`${path}.tmp` with mode `0o666`, which today is what `writeFile` reuses.

**Why H-03 and H-04 are one task section**: the audit's note under H-04 —
"es **un solo defecto**. No cerrar uno sin el otro" — and both SDKs share the
`rayito.pool/1` file, so a host can have a Python writer and a TypeScript
reader on the same path.

---

## D8 — H-05: the MCP HTTP transport always carries explicit security settings

**Files**: `clients/python/src/rayito/mcp/_cli.py:94-109`,
`docs/site/docs/mcp.md:174-180`.

**Today**: `run()` passes `transport`, `host` and `port` and nothing else.
`mcp` 2.2.0 auto-enables DNS-rebinding protection *only* when the host is
literally `127.0.0.1`, `localhost` or `::1`
(`mcp/server/lowlevel/server.py:741-747`); for any other host —
`127.0.0.2`, `127.1`, an alternative IPv6 loopback spelling — the middleware
is constructed with `enable_dns_rebinding_protection=False` "for backwards
compatibility" and `Host`/`Origin` are never validated. `is_loopback`
accepts all of 127.0.0.0/8, so the CLI does not even warn.

**New**, in `_cli.py`:

```python
from mcp.server.transport_security import TransportSecuritySettings
```

```python
def http_authority(host: str, port: int) -> str:
    """`Host` y `Origin` tal y como los escribe un cliente: un literal IPv6
    va entre corchetes, que es la forma que compara el middleware de `mcp`."""
    try:
        bracketed = ipaddress.ip_address(host).version == 6
    except ValueError:
        bracketed = False
    return f"[{host}]:{port}" if bracketed else f"{host}:{port}"


def transport_security(options: RunOptions) -> TransportSecuritySettings:
    """Siempre explícito: `mcp` sólo auto-activa la protección anti-rebinding
    para tres cadenas de host exactas, así que `--host 127.0.0.2` servía sin
    mirar `Host` ni `Origin` y cualquier web podía alcanzar el sandbox por
    DNS rebinding."""
    authority = http_authority(options.host, options.port)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[authority],
        allowed_origins=[f"http://{authority}"],
    )
```

and the tail of `run()`:

```python
    invoke(
        transport=STREAMABLE_HTTP,
        host=options.host,
        port=options.port,
        transport_security=transport_security(options),
    )
```

**Why the settings are built for every host, including the default**: the
audit's "nota estructural" is the point — today even the safe default is
safe only because of a string heuristic inside a third-party SDK that the
pin `mcp>=2.2,<3` does not fix. Building them here makes the guarantee ours.

**Why exactly one authority and not the SDK's three-spelling list**: the
audit's corrección names `allowed_hosts=[f"{options.host}:{options.port}"]`
and `allowed_origins=[f"http://{options.host}:{options.port}"]`. The
consequence is deliberate and must be documented: a server started with
`--host 127.0.0.1` answers `http://127.0.0.1:8000/mcp` and rejects
`http://localhost:8000/mcp` (whose `Host` header is `localhost:8000`). Every
recipe in `docs/site/docs/mcp.md` already uses `127.0.0.1`, and the CLI's
own default is `127.0.0.1`, so nothing in the documented flow breaks.

**Why the bracket helper**: `f"{host}:{port}"` with `--host ::1` yields
`::1:8000`, which no browser ever sends; the real header is `[::1]:8000`.
Without the helper the recommendation would lock IPv6 users out instead of
protecting them. This is the mechanics of the recommendation, not a
redesign of it.

**Why `is_loopback` and the existing warning stay**: the warning is about a
*different* risk (no authentication at all on a routable address) and the
audit asks to keep it ("manteniendo el aviso actual para hosts no
loopback"). The rebinding fix does not make binding a routable address safe.

**Why a wildcard `--host` becomes a usage error**: with the fix `--host` is
two things at once, the interface to bind and the authority validated in
`Host`/`Origin`. For every concrete address those coincide; for `0.0.0.0`
and `::` they cannot, because a client that reaches a server bound to every
interface dials one of the real addresses and sends *that* in `Host`, so
`TransportSecurityMiddleware._validate_host` would answer 421 to every
request and the mode would be dead rather than protected. The two ways out
are a second flag (`--public-host`) or refusing the spelling; the flag adds
a knob for a mode the page already calls "para tu propia máquina", so
`parse_args` refuses `0.0.0.0` and `::` through the existing `parser.error`
path (exit 2) and tells the operator to pass the address clients dial. A
routable `--host 192.168.1.5` keeps working with the no-authentication
warning, which is the case the audit asked to preserve.

**Documentation, `docs/site/docs/mcp.md:174-180`**, the admonition becomes:

> !!! warning "Sin autenticación"
>     El modo HTTP no autentica a nadie. Escuchar en loopback **no** impide
>     que un navegador llegue al servidor: una página web puede resolver su
>     propio dominio a 127.0.0.x y hablar con él (DNS rebinding), así que el
>     servidor exige siempre que `Host` y `Origin` sean exactamente el
>     `host:puerto` con el que arrancó — conéctate con la misma grafía que
>     pasaste en `--host` (`127.0.0.1:8000`, no `localhost:8000`). Todos los
>     clientes que alcancen el proceso comparten **el mismo sandbox**. Con un
>     `--host` que no sea loopback el servidor avisa (`sin autenticación:
>     cualquier cliente que alcance <host>:<port> controla el sandbox`) y
>     continúa; es para tu propia máquina, no para exponerlo.

**Tests that fail without the fix**
(`clients/python/tests/unit/test_mcp_main.py`):

- `test_run_http_passes_explicit_transport_security` — parametrised over
  `127.0.0.1`, `127.0.0.2` and `::1`: the recorder receives
  `transport_security` with `enable_dns_rebinding_protection is True`,
  `allowed_hosts == ["127.0.0.2:8000"]` (and `["[::1]:8000"]` for IPv6) and
  the matching `http://…` origin. Today the recorder receives three kwargs
  and the assertion fails on the missing one.
- `test_http_authority_brackets_ipv6` — a pure unit over the helper.
- The existing `--http` default and non-loopback warning tests are updated
  to expect the fourth kwarg and to use `192.168.1.5`, a routable address
  that is not a wildcard; `is_loopback`'s table is unchanged.
- `test_parse_args_rejects_a_wildcard_host` over `0.0.0.0` and `::` —
  `SystemExit(2)` with the explanation on stderr; without the guard
  `parse_args` returns normally. `is_wildcard` gets its own table.

---

## D9 — C-11 and H-02: one gate script for both pins

**New file**: `scripts/check_pins.py`. **Callers**:
`.github/workflows/ci.yml` (the `check` job, replacing the "no unpinned
actions" step) and `Makefile` `lint`.

**Today**: the gate is
`grep -nE 'uses: [^@]+@(v[0-9]|main|master|release/)' .github/workflows/*.yml`
— a denylist of three spellings. `@1.2.3`, `@latest`, `@release-v2` and a
truncated `@ab12cd34` (which git and the runner resolve by prefix) all pass,
while `SECURITY.md:157` and `openspec/specs/ci-hardening/spec.md` claim the
gate forbids tags, major aliases and branches. Nothing at all checks `uvx`.

**New behaviour** — two checks, both fail-closed:

1. **Actions**: every line whose first non-space character is not `#` and
   that contains `uses:` must have a reference matching
   `[^@\s]+@[0-9a-f]{40}( +#.*)?` in full, unless it starts with `./` (a
   local action). The regex is the audit's, verbatim.
2. **uvx**: every `uvx` invocation must name its tool with `==`
   (`uvx twine==7.0.0 …`, `uvx --from pkg==1.2.3 cmd`). Anything else is a
   finding.

Default scope: `.github/workflows/*.yml`, `.github/workflows/*.yaml` and
`Makefile`. Paths can be passed explicitly, which is how the tests drive it.
Output is `path:line: reason` plus the offending line, and exit 1.

**Why a script and not the one-line inverted `grep` the audit suggests**:
the substance of C-11 is "fail unless the reference is a 40-hex SHA", and
that regex is used literally inside the script. The shape changes for three
reasons: (a) a `grep | grep -v | grep -v` pipeline embedded in YAML cannot
be unit-tested, and this repo's rule is that every behaviour change gets a
test that fails without it; (b) H-02 needs a second gate in the same place,
and two fragile pipelines are worse than one file; (c) `scripts/` already
holds exactly this kind of gate with exactly this kind of test
(`check_license.py`, `check_wheel.py`, `check_auditable.py`,
`scripts/tests/`), so `make lint` and CI can share it instead of the
Makefile carrying a copy of the pipeline. The delta spec states the
behaviour, not the pipeline, so the requirement stays true either way.

**Why the `uvx` scope excludes `docs/site/`**: `docs/site/docs/mcp.md`
documents `uvx --from "rayito[mcp]" rayito-mcp`, a command **users** run to
install the published Rayito, where pinning would be wrong. The gate covers
what *this project's automation* executes: the workflows and the Makefile.
`docs/RELEASING.md` and `CONTRIBUTING.md` are maintainer copies of the
Makefile commands, so D10 pins their text for truth, but they are not part
of the gate scope (a doc is not an executor, and a stale doc is caught by
review, not by a linter that would then have to understand user-facing
recipes).

**Why not also feed `actionlint`**: `actionlint` has no pinning check and
`.github/actionlint.yaml` cannot express one; that is stated in C-11 itself.

**Tests that fail without the fix** (`scripts/tests/test_check_pins.py`,
pure functions, no shell):

- `test_tag_alias_branch_and_short_sha_are_unpinned`: `@v7`, `@main`,
  `@master`, `@release/1.0`, `@1.2.3`, `@latest`, `@release-v2`, `@ab12cd34`
  are all findings. The last four are exactly what today's gate misses.
- `test_full_sha_with_and_without_comment_is_pinned`: 40 hex bare and with
  ` # v7.0.1`, plus `uses: ./.github/actions/local` skipped and a commented
  `# uses: foo@v1` skipped.
- `test_uvx_without_a_version_is_a_finding`: `uvx twine check dist/*`,
  `uvx cfn-lint --version`, `uvx ruff check .` are findings;
  `uvx pip-audit==2.10.1 -r req.txt`, `uvx twine==7.0.0 check dist/*` and
  `uvx --from pkg==1.0 tool` are not.
- `test_main_reports_every_finding_and_exits_one`: a temporary tree with one
  bad workflow and one bad Makefile line; the exit code is 1 and both paths
  appear in stdout.
- The repository itself is the fifth test: after D10, `check_pins.py` over
  the real paths exits 0 (CI step and `make lint`).

---

## D10 — H-02: the exact pins

Versions resolved and executed locally on 2026-09-22 (`uvx <pin> --version`):

| Tool | Pin | Why this version |
|---|---|---|
| `twine` | `twine==7.0.0` | current release; `uvx twine==7.0.0 --version` verified locally. Nothing in the repo pinned it before, so there is no earlier value to preserve |
| `ruff` | `ruff==0.16.7` | the exact version `clients/python/uv.lock` and `kernel-sidecar/uv.lock` already resolve for the `ruff>=0.12` dev dependency, so `uvx ruff` and `uv run ruff` can no longer disagree about formatting |
| `cfn-lint` | `cfn-lint==1.56.3` | the version the M6/M7 acceptance recorded and that `infra/README.md:92`, `Makefile:178-180` and `openspec/specs/filesystem-persistence/spec.md` already name |
| `pip-audit` | `pip-audit==2.10.1` | already pinned in `ci.yml:171-181` and `audit.yml:51-61`; untouched |

**Replacements** (the whole list, so the gate goes green):

| File:line | Today | New |
|---|---|---|
| `.github/workflows/ci.yml:67` | `uvx ruff check scripts` | `uvx ruff==0.16.7 check scripts` |
| `.github/workflows/ci.yml:85` | `uvx twine check clients/python/dist/*` | `uvx twine==7.0.0 check clients/python/dist/*` |
| `.github/workflows/ci.yml:131` | `uvx ruff check .` | `uvx ruff==0.16.7 check .` |
| `.github/workflows/ci.yml:132` | `uvx ruff format --check .` | `uvx ruff==0.16.7 format --check .` |
| `.github/workflows/release.yml:139` | `uvx twine check clients/python/dist/*` | `uvx twine==7.0.0 check clients/python/dist/*` |
| `Makefile:102` | `uvx ruff check scripts` | `uvx ruff==0.16.7 check scripts` |
| `Makefile:122` | `uvx ruff format scripts` | `uvx ruff==0.16.7 format scripts` |
| `Makefile:185` | `uvx cfn-lint --version` | `uvx cfn-lint==1.56.3 --version` |
| `Makefile:186` | `uvx cfn-lint -- …` | `uvx cfn-lint==1.56.3 -- …` |
| `Makefile:242` | `uvx twine check …` | `uvx twine==7.0.0 check …` |
| `docs/RELEASING.md:80` | `uvx twine check clients/python/dist/*` | `uvx twine==7.0.0 check clients/python/dist/*` |
| `CONTRIBUTING.md:150` | `uvx twine check clients/python/dist/*` | `uvx twine==7.0.0 check clients/python/dist/*` |

**Why literal versions and not a `Makefile` variable** (`uvx $(RUFF) …`):
the gate reads text, and a variable would hide the pin from it exactly where
the pin matters. Duplication of a version string across two files is the
price of a mechanical gate; the bump is one `grep -rn "ruff==" `.

**What this does not close, and the audit says so**: `uv build` in
`release.yml:135` still resolves `uv_build>=0.7.19,<0.9` from
`clients/python/pyproject.toml:58-60` and executes it in the job that holds
`id-token: write`. The structural fix — build in a job without the token,
publish from a second job that only downloads the `python-dist-*` artifact —
is the M8 row that also closes C-10, and is named in D12. Pinning every
`uvx` is what the triage row asks for now, and it removes the one tool the
audit walks through end to end (`twine` between the wheel check and the
publish step).

---

## D11 — C-08: one warning per process when `create()` falls back to the environment

**File**: `clients/python/src/rayito/_sandbox_base.py:111-117`.

**Today**: `resolve_access_token` returns `os.environ["RAYITO_ACCESS_TOKEN"]`
for every `Sandbox.create()` of the process, with no log line and no
behavioural difference from a fresh secret — which is precisely the shape
T14 rejects ("nunca uno por pool: una fuga abre un VM, no la flota").

**New**:

```python
from logging import getLogger

logger = getLogger("rayito.sandbox")

SHARED_ACCESS_TOKEN_WARNING: Final = (
    "%s está definida: todos los Sandbox.create() de este proceso comparten el "
    "mismo access token, así que una fuga abre la flota y no un solo MicroVM; "
    "pasa access_token= en cada create() para tener uno por sandbox"
)


@functools.cache
def warn_shared_access_token() -> None:
    """Una sola vez por proceso: el aviso describe la configuración, no la
    llamada, y repetirlo por cada `create()` lo convertiría en ruido que el
    operador filtra."""
    logger.warning(SHARED_ACCESS_TOKEN_WARNING, ACCESS_TOKEN_ENV_VAR)


def resolve_access_token(access_token: str | None) -> str:
    """Token explícito, `RAYITO_ACCESS_TOKEN` o uno nuevo; siempre base64url
    canónico, que es lo único que `rayd` acepta en `x-access-token`."""
    if access_token:
        return validate_access_token(access_token)
    from_environment = os.environ.get(ACCESS_TOKEN_ENV_VAR)
    if not from_environment:
        return generate_access_token()
    warn_shared_access_token()
    return validate_access_token(from_environment)
```

**Why `functools.cache` and not a module-level boolean**: it is a one-liner
with no `global` statement, it is typed, and `warn_shared_access_token.cache_clear()`
gives the tests a documented reset instead of poking a private module
attribute. `functools` is already imported in the module.

**Why `require_access_token` (the `connect()` path) does not warn**: reading
the variable there is the documented, intended use — it is how a second
process reaches an existing sandbox. The finding is about `create()`
silently reusing it.

**Why the logger name `rayito.sandbox`**: it is the one
`clients/python/src/rayito/sandbox_sync/main.py:149` already uses for
sandbox-lifecycle warnings (`HOOK_ANOMALIES_WARNING`, `IMDS_OPEN_WARNING`),
so an operator who silences or routes one gets all of them. The module
docstring's "Sin I/O" becomes "Sin I/O de red ni de disco" — a `logging`
call is not a port.

**Why the TypeScript twin is not in this change**:
`clients/typescript/src/sandbox/launch.ts:64` has the same behaviour, but
`resolveAccessToken` is a pure exported function with no logger in reach;
warning there means threading the SDK `Logger` through the exported
`LaunchPlanInput` — a public surface change the audit never asked for, in a
row whose triage line says "un `logger.warning` de una sola vez". It is
recorded in D12 and in the change's open issues so the sibling or M8 picks
it up.

**Tests that fail without the fix**
(`clients/python/tests/unit/test_sandbox_base.py` or the module's existing
launch-plan suite):

- `test_environment_token_warns_once`: with `RAYITO_ACCESS_TOKEN` set and
  `warn_shared_access_token.cache_clear()` in a fixture, two
  `resolve_access_token(None)` calls return the same token and `caplog`
  holds exactly one `WARNING` naming `RAYITO_ACCESS_TOKEN`.
- `test_explicit_and_generated_tokens_never_warn`:
  `resolve_access_token("<base64url>")` with the variable set, and
  `resolve_access_token(None)` with it unset, log nothing.
- `test_connect_path_never_warns`: `require_access_token(None)` with the
  variable set logs nothing.

---

## D12 — What this change deliberately does not do

Recorded here so nothing is lost between the two m8 changes and M8 proper:

1. **`lambda:DeleteMicrovmImage`** (`spike/m0/iam.yaml:148`, C-09) — a
   "arreglar ahora" item that the task split assigned to neither change (the
   sibling owns C-09's *prose* in `SECURITY.md:85`). One line, no code
   depends on it (`_publish.py`/`_prune.py` use the other four verbs). It
   belongs to whichever of the two changes lands second, or to a one-line
   follow-up; it is not silently folded in here.
2. **The TypeScript twin of the C-08 warning** — see D11.
3. **Measuring `/etc/passwd` of the base image** (C-05's unmeasured premise)
   — needs an image pull and an `AWS_API_NOTES.md` entry; the triage row
   already puts the `uidrange` rules and the e2e in M8, and the fix does not
   depend on the measurement.
4. **The M8 residues** listed in the proposal, above all the build/publish
   split that closes H-02's residue and C-10.
5. **`SECURITY.md`, `ARCHITECTURE.md` and `docs/site/docs/{security,persistence,concepts,pool}.md`**
   — every sentence of the documentation-truth rows belongs to
   `m8-security-docs`. The only documentation this change touches is
   `docs/site/docs/mcp.md:176` (named inside H-05's own triage row),
   `infra/README.md`'s persistence block (named inside H-01's), and the two
   maintainer copies of the `twine` command (D10). If both changes are in
   flight at once, `infra/README.md` is the only shared file and the two
   edits are in different sections.

---

## Risks and how each is caught

| Risk | Caught by |
|---|---|
| The `Deny` of D2 breaks a deployment that shares a bucket | Intended and documented in D3; `cfn-lint` and `validate-template` still pass, and the README says the failure mode is `AccessDenied` |
| The identity floor refuses an account a real image uses on purpose | `RAYITO_ALLOW_ROOT=1` still bypasses the gate; the default image has only `user` (uid 1000) and `rayd` as root, asserted by the `process-lifecycle` image requirement |
| `mkstemp` leaves debris if the process is killed mid-write | The name carries `TEMP_SUFFIX` in the same directory; the tests assert the happy path leaves nothing, and a crashed temporary is inert (0600, never read: only `path` is read) |
| The MCP authority list is too strict for a client using another spelling | Documented in the admonition of D8; every recipe in the repo uses the same spelling as the default `--host` |
| A new `uvx` tool appears without a pin | `scripts/check_pins.py` in CI and in `make lint` |
| The pin versions drift from the lockfiles | `ruff` is pinned *to* the lockfile value; a `uv lock` bump that changes it makes `uv run ruff format --check` and `uvx ruff==…` disagree, which the CI format step catches |
