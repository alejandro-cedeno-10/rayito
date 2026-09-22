# Tasks — m8-security-fixes

Ordered so every section ends on a gate that can be run on its own. Tick a
box only when its gate is green; paste the evidence (counts, tool versions,
linter output) in the task note.

The whole change is local: no AWS call, no image publish, no e2e.

## 0. [prepare] Ground truth and toolchain

- [x] 0.1 Re-read the nine rows of this change in `docs/SECURITY_AUDIT.md`
      (H-01, H-02, C-05, C-13, H-03, H-04, H-05, C-11, C-08) and the triage
      table of §8; confirm that nothing marked **M8** or **Aceptar** is in
      `design.md`, and that `design.md` D12 still matches the split with
      `m8-security-docs`
- [x] 0.2 Export the Windows toolchain (`RUSTUP_HOME`, `CARGO_HOME`, `PATH`
      with the 1.98.1 toolchain bin, `$CARGO_HOME/bin`, `buf` and `zig`,
      the `zigcc`/`zigar` host wrappers and
      `CFLAGS_x86_64_pc_windows_gnu=-Wno-error=date-time`) and record
      `cargo --version`, `uv --version`, `pnpm --version` and
      `actionlint --version` in the task note. `make` is not installed: every
      Makefile step below is run by hand
- [x] 0.3 Record the baseline counts before touching anything:
      `cargo test --workspace --locked` (382), `cd clients/python && uv run pytest tests/unit`
      (1066), `cd clients/typescript && pnpm test` (386)
- [x] 0.4 Confirm the three pins of design D10 resolve:
      `uvx twine==7.0.0 --version`, `uvx ruff==0.16.7 --version`,
      `uvx cfn-lint==1.56.3 --version`; confirm `ruff` 0.16.7 is still the
      version both `uv.lock` files resolve (`grep -A1 'name = "ruff"'`)


> **Evidencia.** Triaje de `docs/SECURITY_AUDIT.md` §8 releído: las nueve filas
> de este cambio son H-01, H-02, C-05, C-13, H-03, H-04, H-05, C-11 y el aviso
> de C-08; nada marcado **M8** (C-10, C-12, los `uidrange`, el split
> build/publish) ni **Aceptar** (C-06) entra aquí, y D12 sigue describiendo el
> reparto con `m8-security-docs`. Toolchain (Git Bash, nada en C:):
> `cargo 1.98.1 (797e8a9bc 2026-08-05)`, `rustc 1.98.1 (48a229cea 2026-09-01)`,
> `uv 0.7.21`, `pnpm 9.15.4`, `actionlint 1.7.12`, `Python 3.12.10`. `make` no
> está instalado: cada paso del Makefile se corrió a mano. Líneas base:
> `cargo test --workspace --locked` **382**, `uv run pytest tests/unit`
> **1066 passed, 1 skipped**, `pnpm test` **386**. Pines verificados hoy:
> `uvx twine==7.0.0 --version` → `twine version 7.0.0`,
> `uvx ruff==0.16.7 --version` → `ruff 0.16.7`,
> `uvx cfn-lint==1.56.3 --version` → `cfn-lint 1.56.3`; los dos `uv.lock`
> (`clients/python`, `kernel-sidecar`) resuelven `ruff` `version = "0.16.7"`.

## 1. [iam] H-01 and C-13: the template and the recipe

- [x] 1.1 `spike/m0/iam.yaml`: `PersistencePrefix` `Default: rayito-home`
      and the new `AllowedPattern`
      `^[A-Za-z0-9_.-][A-Za-z0-9_./-]*[A-Za-z0-9_.-]$|^[A-Za-z0-9_.-]$`
      (design D1, D4), with the `Description` sentence that says why the
      value must never be `rayito` or a first segment of it
- [x] 1.2 `spike/m0/iam.yaml`: add the `NeverTheImageArtifacts` `Deny`
      statement to the conditional `persistence` policy of `ExecutionRole`,
      exactly as in design D2 (`Action: s3:*`,
      `Resource: arn:aws:s3:::${ArtifactBucket}/rayito/*`)
- [x] 1.3 Verify by rendering, not by reading: `aws cloudformation validate-template`
      is not available offline, so confirm with
      `uvx cfn-lint==1.56.3 spike/m0/iam.yaml infra/egress-connector.yaml infra/ci-oidc-role.yaml`
      (clean) and paste the output; if a wildcard-action rule fires on the
      `Deny`, apply the enumerated fallback of design D2 and say so
- [x] 1.4 Check the parameter pattern by hand against `rayito-home`,
      `rayito-e2e`, `a`, `team/dev` (accepted) and `*`, `rayito/`, `/rayito`,
      `it's` (rejected); record the ten results in the task note
- [x] 1.5 `infra/README.md`, persistence block only: the deploy command with
      two distinct values (`ArtifactBucket=<bucket-de-artefactos>`,
      `PersistenceBucket=<bucket-de-persistencia>`,
      `PersistencePrefix=rayito-home`), the "Dos espacios de nombres, nunca
      uno" paragraph of design D3, and the sentence at `:168-169` updated to
      quote the new pattern
- [x] 1.6 `grep -rn "PersistencePrefix=rayito\b" infra/ docs/` returns
      nothing, and `grep -rn "A-Za-z0-9!_" infra/ spike/` returns nothing
      (the old pattern is gone from both the template and the README).
      `infra/README.md:122` (H-06) is **not** touched: it belongs to
      `m8-security-docs`


> **Evidencia.** `spike/m0/iam.yaml`: `Default: rayito-home`, `AllowedPattern`
> `^[A-Za-z0-9_.-][A-Za-z0-9_./-]*[A-Za-z0-9_.-]$|^[A-Za-z0-9_.-]$`,
> `Description` con la frase de por qué el valor nunca es `rayito`, y el
> statement `NeverTheImageArtifacts` (`Effect: Deny`, `Action: s3:*`,
> `Resource: arn:aws:s3:::${ArtifactBucket}/rayito/*`) dentro de la política
> condicional `persistence`.
> `uvx cfn-lint==1.56.3 spike/m0/iam.yaml infra/egress-connector.yaml infra/ci-oidc-role.yaml`
> → salida vacía, exit 0: ninguna regla de comodín se disparó sobre el `Deny`,
> así que no hizo falta el repliegue enumerado de D2. Patrón comprobado a mano
> (10 casos): `rayito-home`, `rayito-e2e`, `a`, `team/dev` y `rayito`
> aceptados; `*`, `rayito/`, `/rayito`, `it's` y `a*b` rechazados —`rayito` lo
> sigue aceptando el patrón a propósito y lo cierra el `Deny`, que es la
> "construcción" que pide H-01. `infra/README.md`: el `deploy` usa
> `<bucket-de-artefactos>` / `<bucket-de-persistencia>` /
> `PersistencePrefix=rayito-home`, lleva el párrafo "Dos espacios de nombres,
> nunca uno" y la frase del patrón cita el nuevo. `grep -rn
> "PersistencePrefix=rayito$" infra/ docs/site/` no devuelve nada y
> `grep -rn "A-Za-z0-9!_" infra/ spike/` tampoco. `infra/README.md:122` (H-06)
> no se tocó: `m8-security-docs` ya lo había corregido y el fichero se releyó
> justo antes de editar su bloque de persistencia.

## 2. [rayd-core] C-05: positive identity check

- [x] 2.1 `crates/rayd-core/src/process/identity.rs`: add
      `MIN_UNPRIVILEGED_ID`, `ROOT_GROUP_ID` and the private
      `is_unprivileged`, and rewrite `authorize_identity` as in design D5
      (allow-root bypass first, `uid == 0` keeps `RootNotAllowed`, everything
      else goes through the positive check)
- [x] 2.2 `crates/rayd-core/src/process/error.rs`: new variant
      `PrivilegedAccount` with the message of design D5 (no username, no
      request data)
- [x] 2.3 `crates/rayd-core/src/filesystem/error.rs` and
      `filesystem/identity.rs`: twin variant plus the `policy_error` arm
      `ProcessError::PrivilegedAccount => FilesystemError::PrivilegedAccount`
- [x] 2.4 `crates/rayd-core/src/persistence/error.rs`: twin variant,
      `StatusKind::PermissionDenied` in `status_kind`, and the variant added
      to the exhaustive `every_variant()` list of the test module
- [x] 2.5 `crates/rayd-core/src/persistence/mod.rs`: map
      `FilesystemError::PrivilegedAccount => PersistenceError::PrivilegedAccount`
      in `resolve_home_identity` and **delete** the duplicated
      `if identity.uid == 0` gate (design D5)
- [x] 2.6 `crates/rayd/src/grpc/process.rs`, `grpc/pty.rs`,
      `grpc/filesystem.rs`: add the new variant to the existing
      `permission_denied` arm, and to the status-mapping table tests of each
      file
- [x] 2.7 Unit tests that fail without 2.1–2.6: `system_accounts_are_refused`
      in `identity.rs` (uid 11/gid 0, uid 1/gid 1, uid 1000 with `groups=[0]`,
      uid 1000/gid 0 → `PrivilegedAccount`; uid 1000/gid 1000 → `Ok`; uid 0 →
      `RootNotAllowed`; all `Ok` with `allow_root: true`), plus the
      `plan_spawn`, `plan_pty`, `resolve_identity` and `resolve_home_identity`
      cases over a lookup fake resolving `"operator"` to uid 11/gid 0
- [x] 2.8 Gates: `cargo fmt --all --check`,
      `cargo clippy --workspace --all-targets -- -D warnings`,
      `cargo test --workspace --locked` (count > 382, paste it)


> **Evidencia.** `MIN_UNPRIVILEGED_ID = 1000`, `ROOT_GROUP_ID = 0`,
> `is_unprivileged` y el `authorize_identity` positivo en
> `process/identity.rs`; variantes gemelas `PrivilegedAccount` en
> `process/error.rs`, `filesystem/error.rs` y `persistence/error.rs` (esta
> última en el grupo `PermissionDenied` de `status_kind`, en `every_variant()`
> y con su aserción de `stream_code() == "permission_denied"`); el arm
> `ProcessError::PrivilegedAccount => FilesystemError::PrivilegedAccount` en
> `filesystem/identity.rs`; y las tres tablas de estado de
> `crates/rayd/src/grpc/{process,pty,filesystem}.rs` con la variante nueva
> mapeada a `PERMISSION_DENIED`.
>
> **Desviación de D5, declarada.** La puerta de `persistence/mod.rs:116` se
> borró, pero **no** era una duplicada exacta: con `RAYITO_ALLOW_ROOT=1`
> `authorize_identity` devuelve `Ok(())` de inmediato, así que ese `uid == 0`
> era el único que hacía cierta la frase del propio `resolve_home_identity`
> ("minus root, whatever the image's `RAYITO_ALLOW_ROOT` says") y la de T15
> ("nunca archiva `/root`"), y el test existente
> `root_is_never_a_persistence_identity` lo comprueba con `allow_root: true`.
> Borrarla sin más habría hecho que una imagen con el opt-in archivase `/root`
> —una regresión de seguridad dentro del hito que corrige la documentación—.
> En su lugar `resolve_home_identity` resuelve con `policy.without_root()`, un
> helper nuevo de cuatro líneas en el dominio: el módulo de persistencia ya no
> tiene ningún chequeo numérico propio, la refusión sale de la única puerta y
> el comportamiento para root es byte a byte el de antes.
>
> Tests nuevos (6) que fallan sin el arreglo — comprobado revirtiendo
> `authorize_identity` a la lista negra y reponiendo la puerta duplicada:
> `process::identity::tests::system_accounts_are_refused`
> (`left: Ok(()) / right: Err(PrivilegedAccount)`),
> `process::spec::tests::a_system_account_is_refused_although_it_is_not_root`
> (devolvía un `SpawnSpec` con `uid: 11, gid: 0`),
> `pty::tests::a_system_account_is_refused_although_it_is_not_root`,
> `filesystem::identity::tests::a_system_account_is_refused_although_it_is_not_root`
> (`left: Ok(FsIdentity { uid: 11, gid: 0, home: "/root" })`) y
> `persistence::tests::a_system_account_is_never_a_persistence_identity` →
> `test result: FAILED. 0 passed; 5 failed`. El sexto,
> `without_root_drops_the_image_opt_in`, cubre el helper nuevo.
> Gates: `cargo fmt --all --check` limpio,
> `cargo clippy --workspace --all-targets -- -D warnings` limpio (hizo falta
> extraer `every_status_case()` en `grpc/pty.rs`: la tabla llegaba a 101 líneas
> y `clippy::too_many_lines` corta en 100; nada de `#[allow]`),
> `cargo test --workspace --locked` **388** (382 + 6).

## 3. [python-pool] H-03: exclusive temporary and no-follow read

- [x] 3.1 `clients/python/src/rayito/_pool_backends.py`: `NO_FOLLOW`,
      `READ_FLAGS`, `restrict_to_owner`, `discard` and `not_a_regular_file`
      helpers, and `_read`/`_write` rewritten as in design D6
      (`tempfile.mkstemp` in the target directory with
      `prefix=f"{name}."`/`suffix=TEMP_SUFFIX`, `fchmod` on the descriptor,
      `os.replace`, `discard` on any failure)
- [x] 3.2 Same file: update the class docstring so it describes the random
      exclusive temporary instead of
      `<path>.tmp` + `O_CREAT|O_WRONLY|O_TRUNC`
- [x] 3.3 `clients/python/tests/unit/test_pool_backends.py`: the four tests of
      design D6 (`ignores_a_pre_created_temp`, `never_follows_a_symlink`,
      `read_refuses_a_symlinked_state_file`, `leaves_no_temporary`), POSIX
      guards where they touch modes or symlinks; confirm each one fails
      against the pre-change file (run it once with the old `_write` restored
      in a scratch copy and note the failure)
- [x] 3.4 Gates: `cd clients/python && uv run pytest tests/unit` (count >
      1066), `uv run ruff check .`, `uv run ruff format --check .`,
      `uv run mypy src tests`


> **Evidencia.** `_pool_backends.py`: `NO_FOLLOW`, `READ_FLAGS`,
> `restrict_to_owner`, `discard`, `not_a_regular_file`, `_read` por
> `os.open(..., O_RDONLY|O_NOFOLLOW)` y `_write` por `tempfile.mkstemp(dir=...,
> prefix=f"{name}.", suffix=TEMP_SUFFIX)` + `fchmod` sobre el descriptor +
> `os.replace`, con `discard` en cualquier `BaseException`. El docstring de la
> clase ya no habla de `<path>.tmp` ni de `O_CREAT | O_WRONLY | O_TRUNC`.
> Cuatro tests nuevos en `tests/unit/test_pool_backends.py`:
> `test_json_write_ignores_a_pre_created_temp` (multiplataforma: sólo las
> aserciones de modo van tras `os.name == "posix"`, para que el defecto se
> pueda demostrar también en Windows), `test_json_write_never_follows_a_symlink`,
> `test_json_read_refuses_a_symlinked_state_file` (los dos `POSIX_ONLY`) y
> `test_json_write_leaves_no_temporary`. Contra el `_read`/`_write` anterior
> restaurado en el fichero real:
> `FAILED tests/unit/test_pool_backends.py::test_json_write_ignores_a_pre_created_temp`
> — `FileNotFoundError: ...pool.json.tmp`, es decir, el documento del pool se
> había escrito dentro del inodo del atacante y se lo habían renombrado encima.
> Los dos con `symlink` sólo corren en POSIX (en Windows `O_NOFOLLOW` no
> existe): en esta máquina se saltan y los ejecuta el job de CI en ubuntu.
> Gates: `uv run pytest tests/unit` **1075 passed, 3 skipped** (base 1066/1),
> `uv run ruff check .` y `uv run ruff format --check .` limpios,
> `uv run mypy src tests` → `Success: no issues found in 141 source files`.

## 4. [typescript-pool] H-04: the twin, same change

- [x] 4.1 `clients/typescript/src/pool/backend.ts`: `NO_FOLLOW`,
      `READ_FLAGS`, `temporaryPath`, `discard` and the rewritten
      `#read`/`#write` of design D7 (`open(temp, "wx", FILE_MODE)` +
      `handle.chmod`, write through the handle, `rename`, `discard` on
      failure, `ELOOP` → `InvalidArgumentError`)
- [x] 4.2 Same file: update the class and module doc comments to describe the
      random exclusive temporary instead of `<path>.tmp` + `mode`
- [x] 4.3 `clients/typescript/tests/unit/` pool backend suite: the four tests
      of design D7, mirroring 3.3 one for one, skipped on Windows where they
      assert modes or symlinks
- [x] 4.4 Gates: `cd clients/typescript && pnpm lint && pnpm typecheck && pnpm test`
      (count > 386) `&& pnpm build`
- [x] 4.5 Cross-SDK check: a file written by the Python backend loads in the
      TypeScript one and the other way round (the `rayito.pool/1` schema is
      unchanged); note how it was checked


> **Evidencia.** `clients/typescript/src/pool/backend.ts`: `NO_FOLLOW`
> (`(constants as Partial<typeof constants>).O_NOFOLLOW ?? 0`, el repliegue
> que D7 dejaba escrito, porque la declaración de `@types/node` no es
> opcional), `READ_FLAGS`, `temporaryPath` con `randomBytes(8)`, `discard`,
> `#read` por `open(path, READ_FLAGS)` con `ELOOP → InvalidArgumentError` y
> `#write` por `open(temp, "wx", FILE_MODE)` + `handle.chmod(FILE_MODE)` +
> `rename`, con `discard` en el `catch`. Los comentarios de módulo y de clase
> describen el temporal aleatorio. Cuatro tests nuevos en
> `tests/unit/pool.test.ts`, uno a uno los de D6: "a pre-created temporary is
> never reused" (multiplataforma), "a symlinked temporary truncates nothing" y
> "a symlinked state file is refused on read" (`test.skipIf(process.platform
> === "win32")`) y "no temporary is left behind". Contra el `#write` anterior:
> `FAIL |unit| tests/unit/pool.test.ts > JsonFilePoolBackend > a pre-created
> temporary is never reused` → `Tests 1 failed | 387 passed | 2 skipped`.
> Gates: `pnpm lint` (biome) limpio tras `biome check --write`,
> `pnpm typecheck` limpio, `pnpm test` **388 passed | 2 skipped (390)**
> (base 386), `pnpm build` OK.
> Cruce entre SDK: un `pool.json` escrito por `JsonFilePoolBackend` de Python
> lo carga el de TypeScript (`ts read python file: microvm-py/ready`) y el
> fichero que escribe TypeScript lo vuelve a cargar Python
> (`python read ts file: [('microvm-ts', 'ready', 'aaaa')]`); el esquema
> `rayito.pool/1` no cambió.

## 5. [mcp] H-05: explicit transport security

- [x] 5.1 `clients/python/src/rayito/mcp/_cli.py`: import
      `TransportSecuritySettings`, add `http_authority` and
      `transport_security`, and pass `transport_security=` on every
      `--http` invocation (design D8); keep `is_loopback` and the existing
      no-authentication warning untouched
- [x] 5.2 `clients/python/tests/unit/test_mcp_main.py`: the parametrised
      `127.0.0.1` / `127.0.0.2` / `::1` test asserting the fourth kwarg and
      its contents, the `http_authority` bracket test, and the two existing
      `--http` tests updated for the new kwarg
- [x] 5.3 `docs/site/docs/mcp.md`: replace the "Sin autenticación"
      admonition with the wording of design D8 (loopback does not keep a
      browser out; connect with the same spelling as `--host`)
- [x] 5.4 Gates: `cd clients/python && uv run pytest tests/unit`,
      `uv run ruff check .`, `uv run mypy src tests`, and
      `uv run --group docs mkdocs build -f ../../docs/site/mkdocs.yml --strict --site-dir ../../docs/site/_build`


> **Evidencia.** `mcp/_cli.py` importa `TransportSecuritySettings`, añade
> `http_authority` (corchetes para literales IPv6) y `transport_security`, y
> `run()` pasa el cuarto kwarg en **toda** invocación `--http`; `is_loopback` y
> el aviso de "sin autenticación" quedan como estaban.
> `mcp.server.transport_security.TransportSecuritySettings` existe en el
> entorno con los campos `enable_dns_rebinding_protection`, `allowed_hosts` y
> `allowed_origins`. Tests: el parametrizado
> `test_run_http_always_passes_explicit_transport_security` sobre `127.0.0.1`,
> `127.0.0.2` y `::1` (`allowed_hosts == ["[::1]:8000"]` en el tercero),
> `test_http_authority_brackets_ipv6`, y los dos `--http` existentes
> actualizados. Contra el `run()` anterior: 5 fallos, todos
> `KeyError: 'transport_security'`. `docs/site/docs/mcp.md`: la admonición
> "Sin autenticación" ya no presenta loopback como la protección; nombra el DNS
> rebinding, dice que el servidor exige `Host`/`Origin` iguales al `host:puerto`
> de arranque y pide conectarse con la misma grafía que `--host`.
> Gates: `uv run pytest tests/unit/test_mcp_main.py` **20 passed**, ruff y mypy
> limpios, y el build estricto de mkdocs
> (`uv run --group docs mkdocs build -f ../../docs/site/mkdocs.yml --strict
> --site-dir ../../docs/site/_build`) → `Documentation built in 1.77 seconds`.

## 6. [gate] C-11 and the H-02 gate: `scripts/check_pins.py`

- [x] 6.1 `scripts/check_pins.py` per design D9: `unpinned_actions(text)`
      (skip comment lines and `./` local actions; require
      `[^@\s]+@[0-9a-f]{40}( +#.*)?` in full), `unpinned_uvx(text)` (require
      `==`, accept `--from pkg==x`), a `main(argv)` that defaults to
      `.github/workflows/*.yml`, `.github/workflows/*.yaml` and `Makefile`,
      prints `path:line: reason` and exits 1 on any finding
- [x] 6.2 `scripts/tests/test_check_pins.py`: the five test groups of design
      D9, including `@1.2.3`, `@latest`, `@release-v2` and `@ab12cd34` (the
      four spellings today's gate misses)
- [x] 6.3 `.github/workflows/ci.yml`: replace the "no unpinned actions" step
      with `python3 scripts/check_pins.py`, keeping the step name and the
      `actionlint` step as they are
- [x] 6.4 `Makefile` `lint`: run `python scripts/check_pins.py` next to the
      `actionlint` line (no `command -v` guard: it is a repository script)
- [x] 6.5 Gates: `cd clients/python && uv run pytest ../../scripts/tests -p no:cacheprovider`,
      `uvx ruff==0.16.7 check scripts`, `actionlint -no-color`


> **Evidencia.** `scripts/check_pins.py` con las dos puertas en lista blanca:
> `unpinned_actions` exige `[^@\s]+@[0-9a-f]{40}( +#.*)?` completo y salta
> comentarios y acciones `./`; `unpinned_uvx` exige `==` en la herramienta,
> también en la forma `--from paquete==versión`; `main(argv, root)` recorre por
> defecto `.github/workflows/*.yml`, `*.yaml` y `Makefile`, imprime
> `path:line: reason` más la línea y sale 1.
>
> **Ajuste sobre D9, declarado.** El reconocedor de acciones ancla `uses:` como
> clave YAML (`^\s*(?:-\s+)?uses:\s*…`) en vez de buscar la subcadena: el
> propio nombre del paso de CI ("every `uses:` is a commit SHA") y la orden
> `grep` que sustituye llevan `uses:` citado, y un `in` simple los marcaba como
> acciones sin clavar. El test
> `test_full_sha_local_action_and_quoted_uses_are_pinned` cubre las dos líneas.
>
> `scripts/tests/test_check_pins.py`: los cinco grupos de D9, con `@1.2.3`,
> `@latest`, `@release-v2` y `@ab12cd34` entre los hallazgos. Comparación
> directa con la puerta anterior sobre un workflow con esas cuatro grafías: el
> `grep -nE 'uses: [^@]+@(v[0-9]|main|master|release/)'` de `ci.yml` no
> encuentra ninguna (exit 1 → el paso pasaba), la puerta nueva las reporta las
> cuatro y sale 1. `ci.yml` sustituye el paso por
> `run: python3 scripts/check_pins.py` conservando el nombre y dejando intacto
> el paso de `actionlint`; el `lint` del `Makefile` corre
> `python scripts/check_pins.py` al lado de la línea de `actionlint`, sin
> guarda `command -v`.
> Gates: `uv run pytest ../../scripts/tests -p no:cacheprovider` **72 passed**
> (base 66), `uvx ruff==0.16.7 check scripts` → `All checks passed!`,
> `actionlint -no-color .github/workflows/*.yml` exit 0 (este árbol no es un
> repo git, así que `actionlint` sin argumentos aborta con "no project was
> found"; se le pasan los ficheros).

## 7. [pins] H-02: pin every uvx invocation

- [x] 7.1 Apply the twelve replacements of design D10 (`ci.yml` ×4,
      `release.yml` ×1, `Makefile` ×5, `docs/RELEASING.md` ×1,
      `CONTRIBUTING.md` ×1)
- [x] 7.2 `python3 scripts/check_pins.py` exits 0 on the real repository
      (both checks), and `grep -rn "uvx " .github/workflows Makefile` shows a
      version on every line
- [x] 7.3 Confirm the pinned tools still do their job:
      `uvx ruff==0.16.7 check scripts`,
      `uvx cfn-lint==1.56.3 spike/m0/iam.yaml infra/egress-connector.yaml infra/ci-oidc-role.yaml`,
      and `cd clients/python && uv build` + `python3 scripts/check_wheel.py clients/python/dist/*.whl`
      + `uvx twine==7.0.0 check clients/python/dist/*`
- [x] 7.4 `actionlint -no-color` clean after editing both workflows


> **Evidencia.** Las doce sustituciones de D10 más las **tres** copias de
> `CONTRIBUTING.md` que su tabla no listaba (`:128` `uvx ruff check scripts`,
> `:157` `uvx ruff check .`, `:158` `uvx ruff format --check .`): son copias de
> mantenedor de `Makefile:102` y de `ci.yml:131-132`, y el propio D10 pide
> clavarlas "for truth"; dejar tres sin clavar junto a una clavada en el mismo
> fichero contradecía el objetivo de la fila. Quince en total: `ci.yml` ×4,
> `release.yml` ×1, `Makefile` ×5, `docs/RELEASING.md` ×1, `CONTRIBUTING.md`
> ×4. `pip-audit==2.10.1` ya estaba clavado en `ci.yml` y `audit.yml`; no se
> tocó.
> `python scripts/check_pins.py` → `OK 7 fichero(s): toda acción y todo uvx
> clavados`, exit 0. `grep -rn "uvx " .github/workflows Makefile` → 18 líneas,
> todas con `==`. Las herramientas clavadas siguen haciendo su trabajo:
> `uvx ruff==0.16.7 check scripts` → `All checks passed!`;
> `uvx cfn-lint==1.56.3 spike/m0/iam.yaml infra/egress-connector.yaml
> infra/ci-oidc-role.yaml` → exit 0; `cd clients/python && uv build` →
> `rayito-0.2.0-py3-none-any.whl` + `.tar.gz`,
> `python scripts/check_wheel.py clients/python/dist/*.whl` →
> `OK (95 entradas)`, `uvx twine==7.0.0 check clients/python/dist/*` →
> `PASSED` en los dos artefactos. `actionlint -no-color
> .github/workflows/*.yml` exit 0 tras editar los dos workflows.

## 8. [sdk] C-08: one-time shared-token warning

- [x] 8.1 `clients/python/src/rayito/_sandbox_base.py`: `logger`,
      `SHARED_ACCESS_TOKEN_WARNING`, `warn_shared_access_token` (with
      `functools.cache`) and the rewritten `resolve_access_token` of design
      D11; `require_access_token` unchanged; module docstring "Sin I/O" →
      "Sin I/O de red ni de disco"
- [x] 8.2 Unit tests: warn exactly once for two fallbacks, never for an
      explicit token, never for a generated one, never on the `connect()`
      path; the fixture calls `warn_shared_access_token.cache_clear()`
- [x] 8.3 Confirm no token, no prefix of a token and no `metadata` value can
      reach the log line (the warning names only the variable)
- [x] 8.4 Gates: `cd clients/python && uv run pytest tests/unit`,
      `uv run ruff check .`, `uv run ruff format --check .`,
      `uv run mypy src tests`


> **Evidencia.** `_sandbox_base.py`: `logger = getLogger("rayito.sandbox")`
> (el mismo nombre que ya usa `sandbox_sync/main.py`),
> `SHARED_ACCESS_TOKEN_WARNING`, `warn_shared_access_token` con
> `functools.cache` y el `resolve_access_token` de D11;
> `require_access_token` intacto y el docstring del módulo pasa de "Sin I/O" a
> "Sin I/O de red ni de disco". Tres tests nuevos en
> `tests/unit/test_sandbox_base.py` con un fixture que llama a
> `warn_shared_access_token.cache_clear()` antes y después:
> `test_environment_token_warns_once` (dos resoluciones, un solo `WARNING`),
> `test_explicit_and_generated_tokens_never_warn` y
> `test_connect_path_never_warns`. Contra el `resolve_access_token` anterior:
> `FAILED tests/unit/test_sandbox_base.py::test_environment_token_warns_once`
> (`0 = len([])`, ningún registro). El aviso nombra sólo la variable: el test
> comprueba `ACCESS_TOKEN_ENV_VAR in mensaje` **y**
> `ACCESS_TOKEN not in mensaje`, y la línea de log no recibe más argumento que
> el nombre de la variable, así que ni el token, ni un prefijo suyo, ni ningún
> valor de `metadata` puede llegar a ella.
> Gates: `uv run pytest tests/unit` **1075 passed, 3 skipped**, ruff check y
> format limpios, `uv run mypy src tests` sin incidencias.

## 9. [close] Full gate sweep and hand-off

- [x] 9.1 Rust: `cargo fmt --all --check`,
      `cargo clippy --workspace --all-targets -- -D warnings`,
      `cargo test --workspace --locked`
- [x] 9.2 Python: `cd clients/python && uv run pytest tests/unit && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests`
      and `uv run pytest ../../scripts/tests -p no:cacheprovider`
- [x] 9.3 TypeScript: `cd clients/typescript && pnpm lint && pnpm typecheck && pnpm test && pnpm build`
- [x] 9.4 Infra and CI: `uvx cfn-lint==1.56.3 spike/m0/iam.yaml infra/*.yaml`,
      `actionlint -no-color`, `python3 scripts/check_pins.py`
- [x] 9.5 Docs: `cd clients/python && uv run --group docs mkdocs build -f ../../docs/site/mkdocs.yml --strict --site-dir ../../docs/site/_build`
- [x] 9.6 `openspec validate m8-security-fixes --strict --no-interactive`
      passes, and every box above is ticked with its evidence
- [x] 9.7 Write the hand-off note: the final test counts, the `cfn-lint`
      version, the four open items of design D12 (the
      `lambda:DeleteMicrovmImage` line, the TypeScript twin of the C-08
      warning, the `/etc/passwd` measurement, the M8 residues) and whether
      `m8-security-docs` has already edited `infra/README.md`


> **Evidencia del barrido final.**
> - Rust: `cargo fmt --all --check` limpio; `cargo clippy --workspace
>   --all-targets -- -D warnings` limpio; `cargo test --workspace --locked`
>   **388 passed** (base 382).
> - Python: `uv run pytest tests/unit` **1075 passed, 3 skipped**
>   (base 1066/1); `uv run ruff check .` → `All checks passed!`;
>   `uv run ruff format --check .` → `143 files already formatted`;
>   `uv run mypy src tests` → `Success: no issues found in 141 source files`;
>   `uv run pytest ../../scripts/tests -p no:cacheprovider` **72 passed**.
> - TypeScript: `pnpm lint`, `pnpm typecheck` limpios; `pnpm test`
>   **388 passed | 2 skipped (390)** (base 386); `pnpm build` OK.
> - Infra y CI: `uvx cfn-lint==1.56.3 spike/m0/iam.yaml infra/*.yaml` exit 0;
>   `actionlint -no-color .github/workflows/*.yml` exit 0;
>   `python scripts/check_pins.py` → `OK 7 fichero(s)`, exit 0.
> - Docs: `uv run --group docs mkdocs build -f ../../docs/site/mkdocs.yml
>   --strict --site-dir ../../docs/site/_build` → construido sin avisos.
> - `openspec validate m8-security-fixes --strict --no-interactive` →
>   `Change 'm8-security-fixes' is valid`.
>
> **Nota de hand-off.** Cuentas finales: Rust 388, Python 1075 (+3 saltados),
> TypeScript 388 (+2 saltados), `scripts/tests` 72. `cfn-lint` 1.56.3.
> Los cuatro puntos abiertos de D12 siguen abiertos: (1)
> `lambda:DeleteMicrovmImage` en `spike/m0/iam.yaml:155` —la mitad de código de
> C-09, que el reparto no asignó a ninguno de los dos cambios; `_prune.py` usa
> `DeleteMicrovmImageVersion` (`:160`), que sí hace falta, así que es borrar
> exactamente una línea; (2) el gemelo TypeScript del aviso de C-08 en
> `clients/typescript/src/sandbox/launch.ts`, que exige pasar el `Logger` por
> `LaunchPlanInput`; (3) medir el `/etc/passwd` de
> `public.ecr.aws/lambda/microvms:al2023-minimal` y anotarlo en
> `AWS_API_NOTES.md` (necesita un pull de imagen); (4) los residuos de M8,
> sobre todo el split build/publish que cierra C-10 y el residuo de H-02.
> `m8-security-docs` **ya había editado** `infra/README.md`: la frase de
> H-06 sobre las *deployment branches* está en `:118-126`, y el bloque de
> persistencia de este cambio se editó después de releer el fichero, en otra
> sección.

## 10. [review] Two review findings of 2026-09-22

- [x] 10.1 `clients/python/src/rayito/mcp/_cli.py`: `parse_args` refuses a
      wildcard `--host` (`0.0.0.0`, `::`, any unspecified address) through the
      existing `parser.error` path (exit 2), because H-05 makes `--host` the
      authority validated in `Host`/`Origin` and a client that reaches a
      server bound to every interface sends the address it dialled, never the
      wildcard, so `TransportSecurityMiddleware` would answer 421 to every
      request
- [x] 10.2 `clients/python/tests/unit/test_mcp_main.py`: a parametrised test
      over `0.0.0.0` and `::` asserting `SystemExit(2)` and the explanation on
      stderr; the two tests that used `0.0.0.0` as a routable host move to
      `192.168.1.5`
- [x] 10.3 `specs/mcp-server/spec.md` and `design.md` D8: the wildcard is a
      usage error, the streamable-HTTP scenario uses a concrete routable
      address, and `docs/site/docs/mcp.md` says the server refuses a wildcard
      `--host` instead of warning and continuing
- [x] 10.4 `specs/process-lifecycle/spec.md` and `proposal.md`: the archived
      truth SHALL say persistence resolves its identity with
      `UserPolicy::without_root()`, so `resolve_home_identity` refuses root
      and every privileged account even under `RAYITO_ALLOW_ROOT=1`; the
      "strictly stronger" claim holds only with the opt-in off
- [x] 10.5 Gates: `cd clients/python && uv run pytest tests/unit`,
      `uv run ruff check .`, `uv run ruff format --check .`,
      `uv run mypy src tests`, the strict mkdocs build, and
      `openspec validate m8-security-fixes --strict --no-interactive`


> **Evidencia de la revisión.**
> - `mcp/_cli.py`: `is_wildcard` (`ipaddress.ip_address(host).is_unspecified`)
>   y el rechazo en `parse_args`; `--host 0.0.0.0` y `--host ::` salen por
>   `parser.error` (exit 2) diciendo que `--host` es la dirección que los
>   clientes escriben en `Host`. Un `--host 192.168.1.5` sigue funcionando con
>   el aviso de "sin autenticación", que es el caso que la auditoría pedía
>   conservar. Sin la guarda, `test_parse_args_rejects_a_wildcard_host` falla:
>   `parse_args` devuelve `RunOptions(http=True, host='0.0.0.0', port=8000)` en
>   vez de `SystemExit(2)`.
> - `tests/unit/test_mcp_main.py`: +7 casos (2 de rechazo comodín, 5 de la
>   tabla de `is_wildcard`); los dos tests que usaban `0.0.0.0` como dirección
>   enrutable pasan a `192.168.1.5`. `uv run pytest tests/unit/test_mcp_main.py`
>   **27 passed**.
> - `docs/site/docs/mcp.md`: la admonición dice que una comodín se rechaza con
>   exit 2 y que el aviso sin autenticación es para una IP concreta.
> - `specs/process-lifecycle/spec.md` y `proposal.md`: la verdad archivada
>   nombra `UserPolicy::without_root()` y dice que persistencia refuerza el
>   rechazo bajo `RAYITO_ALLOW_ROOT=1`; el escenario del opt-in exceptúa
>   `resolve_home_identity`. `design.md` explica por qué borrar el `uid == 0`
>   duplicado sólo es seguro con `without_root()` y qué pasaría si alguien lo
>   borrase (se archivaría `/root`). Los tests que ya lo cubrían
>   (`root_is_never_a_persistence_identity`,
>   `a_system_account_is_never_a_persistence_identity` con `allow_root: true`,
>   `without_root_drops_the_image_opt_in`) quedan citados en D-C05.
> - Gates: `uv run pytest tests/unit` **1082 passed, 3 skipped** (antes
>   1075/3); `uv run ruff check .` → `All checks passed!`;
>   `uv run ruff format --check .` → `143 files already formatted`;
>   `uv run mypy src tests` → `Success: no issues found in 141 source files`;
>   `uv run pytest ../../scripts/tests` **73 passed**; mkdocs estricto →
>   `Documentation built in 1.74 seconds`;
>   `openspec validate m8-security-fixes --strict --no-interactive` →
>   `Change 'm8-security-fixes' is valid`. Rust y TypeScript no se tocaron.

## 11. [gate] Cierre local de M8 (2026-09-22)

- [x] 11.1 Cerrar el punto (1) de la nota de hand-off: borrar
      `lambda:DeleteMicrovmImage` de `spike/m0/iam.yaml` (`CallerPolicy`,
      statement `ImagesAndMicrovms`), la mitad de código de C-09 que el
      reparto no había asignado a ninguno de los dos cambios
- [x] 11.2 Dar prueba a las tres filas de IAM, que hasta ahora sólo tenían
      `cfn-lint` (que valida la forma, no la política): módulo nuevo
      `scripts/tests/test_iam_template.py` con H-01, C-13 y C-09
- [x] 11.3 Barrido completo de puertas sobre el árbol corregido y registro
      del estado de las 16 filas en `docs/SECURITY_AUDIT.md` §9,
      `SECURITY.md` («Auditoría interna»), `MILESTONES.md` (bloque M8) y los
      tres changelogs de paquete (`Unreleased → Security`)


> **Evidencia del cierre.** `spike/m0/iam.yaml`: `ImagesAndMicrovms` ya no
> concede `lambda:DeleteMicrovmImage`; `lambda:DeleteMicrovmImageVersion`
> sigue (lo usa `_prune.py:271`) y `rayito doctor` nunca simuló el primero
> (`clients/python/src/rayito/cli/_checks.py:99` sólo lista el de versión).
> `scripts/tests/test_iam_template.py`: cuatro tests sobre la plantilla ya
> parseada (cargador con las etiquetas cortas de CloudFormation); con la
> plantilla mutada a la forma anterior (acción devuelta, `Default:
> rayito/home`, `Sid` del `Deny` renombrado, `AllowedPattern` permisivo) los
> cuatro fallan, y con la plantilla real pasan.
>
> **Puertas, todas verdes.** `cargo fmt --all --check`; `cargo clippy
> --workspace --all-targets -- -D warnings`; `cargo test --workspace
> --locked` **388 passed**; `cargo deny check` → `advisories ok, bans ok,
> licenses ok, sources ok`. Python: `pytest tests/unit` **1082 passed, 3
> skipped**; `ruff check .` → `All checks passed!`; `ruff format --check .`
> → `143 files already formatted`; `mypy src tests` → `Success: no issues
> found in 141 source files`; `pytest ../../scripts/tests` **77 passed**.
> TypeScript: `pnpm lint`, `pnpm typecheck`, `pnpm test` **388 passed | 2
> skipped (390)**, `pnpm build`, `pnpm pack:check`. Infra y CI: `uvx
> cfn-lint==1.56.3 spike/m0/iam.yaml infra/egress-connector.yaml
> infra/ci-oidc-role.yaml` exit 0; `actionlint .github/workflows/*.yml`
> exit 0 (el repo aún no es un repositorio git, así que se le pasan los
> ficheros); `python scripts/check_pins.py` → `OK 7 fichero(s)`;
> `gen_limits.py --check` y `check_license.py` → `OK`. Paquete: `uv build`
> + `check_wheel.py` → `OK (95 entradas)` + `uvx twine==7.0.0 check` →
> `PASSED`. Docs: `mkdocs build --strict` construido sin avisos.
> `openspec validate --all --strict --no-interactive` → 31 passed.
>
> **Lo que sigue abierto** (sin cambio respecto al hand-off, menos el punto
> 1, ya cerrado): el gemelo TypeScript del aviso de C-08 en
> `clients/typescript/src/sandbox/launch.ts`, la medición del `/etc/passwd`
> de la imagen base, y los residuos de M8 del triaje (§8). `scripts/check_wheel.py`
> arrastra una deriva de formato anterior a esta auditoría (`uvx ruff format
> --check scripts`), que ninguna puerta de CI comprueba y que no se toca aquí
> por no ensanchar el cambio.
