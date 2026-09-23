## 0. [docs] Ground truth first (hard rule 1)

- [x] 0.1 Check the live numbering and record the mapping in the task note:
  - the next free `AWS_API_NOTES.md` §16 rows, used for QE1 and QE2;
  - that ADR-012 and T17 are still free (ADR-010/011 and T16 belong to siblings);
  - whether a sibling already added the native `UnimplementedError` (Python `rayito/exceptions.py`, TS `src/errors.ts`);
  - whether a sibling already archived a MODIFIED version of the two `e2b-compat` requirements; if so, rebase this change's MODIFIED blocks per the design's rebase rule.
  - Verificado (2026-09-23, reintento de specs-docs, sobre el árbol vivo): §16 llega a la fila 62 y luego salta a 70–75 (`m9-file-transfer`); 63–65 están reservadas por `m9-server-timeout` (su 9.4). **QE1 = Q66 y QE2 = Q67**; Deno, observabilidad y `git-core` toman los siguientes libres (68, 69, 76…) al escribir sus filas. ADR-012 y T17 estaban libres (ARCHITECTURE llega a ADR-013 con los hermanos; SECURITY a T17). El `UnimplementedError` nativo ya existía (`rayito/exceptions.py`, `src/errors.ts`, de `m9-file-transfer`). Ningún hermano ha archivado aún: los bloques MODIFIED se re-basaron sobre la cadena de archivo simulada (ver 10.5).
- [x] 0.2 `AWS_API_NOTES.md` §2 (design D20):
  - `HTTP_INGRESS` added to the managed connectors as the default ingress (Q60);
  - `NO_EGRESS` recorded as nonexistent (`ValidationException`, Q60);
  - `egressNetworkConnectors: []` ≡ omitted.
  - Placeholders only.
  - Verificado (2026-09-23, reintento de specs-docs): la fila de conectores de la tabla de entrada nombra `HTTP_INGRESS` como ingress por defecto y «Hechos que cambian el diseño» gana los tres hechos citando Q60 (ARN con `<region>`, sin cuenta). La regla de orden («antes del código») llega tarde: el adaptador ya estaba; lo registra esta nota. `test_m9_docs.py::test_platform_egress_facts_are_recorded` rojo → verde.
- [x] 0.3 `AWS_API_NOTES.md` §16: rows QE1 and QE2 with the "Medida" column empty. The "Desde docs" column cites Q48, §7 and the E2B docs.
  - Verificado (2026-09-23, reintento de specs-docs): filas 66 (QE1, las seis preguntas de D17 y su regla de parada) y 67 (QE2, `https_ports`), «Medida» = *Sin medir* nombrando el test e2e que la rellena (9.1/9.2, *pendiente de aceptación en AWS*).

## 1. [contract] Proto (applied by the Contract agent, design D2)

- [x] 1.1 `proto/rayito/v1/network.proto` exactly as in design D2. `health.proto`: the import plus `EgressEnforcement egress_enforcement = 13;`.
  - verificado aquí (2026-09-23): el primer bloque ```proto de D2 extraído de `design.md` y `proto/rayito/v1/network.proto` son idénticos (`diff` sin líneas en blanco → sin salida). `health.proto` lleva `import "rayito/v1/network.proto";` y `EgressEnforcement egress_enforcement = 13;` con el comentario del segundo bloque de D2.
- [x] 1.2 `buf lint` clean without touching `buf.yaml`. `buf breaking --against <copy of the pre-change proto dir>` clean. Commands and output go in the task note.
  - verificado aquí (2026-09-23): `buf lint` (buf 1.73.0) → sin salida, exit 0; `buf.yaml` y `buf.gen.yaml` sin cambios desde 0.2.0 (`git diff dde681c --stat -- buf.yaml buf.gen.yaml` vacío). `git archive dde681c proto` a un directorio temporal y `buf breaking proto --against <copia>/proto` → sin salida, exit 0.
- [x] 1.3 Regeneration, with no hand edits to generated files:
  - `crates/rayito-proto/build.rs` `PROTO_FILES` += `"network"`;
  - `python scripts/gen_python.py` → `clients/python/src/rayito/v1/network_pb2*.py` and `health_pb2*`;
  - `buf generate` → `clients/typescript/src/gen/rayito/v1/network_pb.ts` and `health_pb.ts`.
  - verificado aquí (2026-09-23): `crates/rayito-proto/build.rs` `PROTO_FILES: [&str; 8]` incluye `"network"`. `buf generate --template <plantilla temporal>` con los mismos plugins remotos de `buf.gen.yaml` (python/pyi v36.1, grpc/python v1.84.0, es v2.15.0) hacia un directorio temporal: `diff -rq` contra `clients/python/src/rayito/v1` (24 ficheros) y `clients/typescript/src/gen` → sin diferencias, así que no hay ediciones a mano. `scripts/gen_python.py` no sirve de comprobación (grpcio-tools 1.84.0 emite gencode protobuf 7.35.1, no el 7.36.1 comprometido).
- [x] 1.4 `limits.json` gains the four `egress*` keys of design D16. `python scripts/gen_limits.py` regenerates `_limits.py` and `limits.ts`, and `--check` passes.
  - verificado aquí (2026-09-23): `limits.json` tiene `egressMaxEntriesPerList` 256, `egressMaxHostnameEntries` 64, `egressHostnameMaxChars` 253 y `egressProxyCredentialMaxBytes` 255 (las cuatro claves de D16); `uv run --no-project python scripts/gen_limits.py --check` → exit 0.

## 2. [rayd] Domain (`crates/rayd-core/src/network/`, no new dependency)

- [x] 2.1 `cidr.rs`: `Cidr` parse, normalize, `contains` and `subtract` (design D3), with the table tests of design D18.
  - Evidencia (2026-09-23, recuento estático; cargo lo corre sólo el agente de `rayd`): 10 tests en `cidr.rs`; en verde dentro de `cargo test --workspace --locked` de 3.10 (rayd-core 483 passed).
- [x] 2.2 `entry.rs`: `EgressEntry` and `HostPattern` (`ALL_TRAFFIC` → both families, apex rule, LDH checks), plus `NetworkError` with messages that carry the list name and index only.
  - Evidencia: 5 tests en `entry.rs` (más 2 en `network/mod.rs`); verdes en 3.10.
- [x] 2.3 `policy.rs`:
  - `EgressPolicy::parse` with the caps;
  - normalization (allow sets ignored without `deny_out`), `ip_verdict`, `deny_by_default`, `mode`, `requires_enforcement`;
  - the two-phase target decision (design D9);
  - `UpstreamProxy::parse` with `ProxyCredentials` (`Zeroizing`, redacted `Debug`).
  - Evidencia: 12 tests en `policy.rs`; verdes en 3.10.
- [x] 2.4 `guard.rs`: `TargetGuard` and `UpstreamGuard` (design D9/D10), including IPv4-mapped canonicalization.
  - Evidencia: 2 tests de tabla en `guard.rs` (y la guardia ejercida por los 17 tests del proxy de `crates/rayd/src/network/tests.rs`); verdes en 3.10.
- [x] 2.5 `route_plan.rs`: slot constants (101/150, 102/151, 103/149), `RoutePlan::for_policy` and the 4096 cap. `swap.rs`: `plan_swap`, rollback and `plan_recovery`, with the "never without a complete table" simulation test.
  - Evidencia: 4 tests en `route_plan.rs` y 7 en `swap.rs` (la simulación incluida); verdes en 3.10.
- [x] 2.6 `probe.rs`: `samples`, `classify_route_get` and `rule_present`.
  - Evidencia: 9 tests en `probe.rs` (incluye `interface_addresses` de 3.2 y `table_missing` de 3.9); verdes en 3.10. Las formas reales de `ip route get` del guest las fija QE1 (*pendiente de aceptación en AWS*).
- [x] 2.7 `proxy_protocol.rs`:
  - HTTP CONNECT and absolute-form parse/rewrite, plus the fixed responses;
  - SOCKS5 server parse/encode;
  - the SOCKS5 client handshake (RFC 1928/1929) encoders and decoders.
  - `proxy_env.rs`: `egress_proxy_env(port)` with the eight keys.
  - Evidencia: 10 tests en `proxy_protocol.rs` y 1 en `proxy_env.rs`; verdes en 3.10.
- [x] 2.8 `state.rs`: `EgressEnforcement` and `NetworkSnapshot`. `health.rs`: `HealthSnapshot.egress_enforcement` plus a builder method. `run_payload.rs`: `network.enforce` with `RunPayloadError::InvalidNetwork`. `process/env.rs`: the `build_child_env` egress layer, with all call sites updated.
  - Evidencia: 3 tests en `state.rs`, `run_payload.rs::network_enforce_is_an_optional_boolean` y `process/env.rs::the_egress_layer_sits_under_the_payload_and_request_envs`; verdes en 3.10.
- [x] 2.9 The `limits.json` agreement test.
  - Evidencia: el test de acuerdo con `limits.json` de `network/mod.rs` (las cuatro claves `egress*` de D16); `python scripts/gen_limits.py --check` → exit 0 (2026-09-23).
- [x] 2.10 Gates:
  - `cargo test -p rayd-core` green on Windows;
  - `cargo clippy --workspace --all-targets -- -D warnings` clean;
  - `crates/rayd-core/Cargo.toml` unchanged.
  - verificado aquí (2026-09-23), sin host Windows: `cargo test -p rayd-core` (dentro de `cargo test --workspace --locked`, Linux aarch64 en la VM) → 483 passed; `cargo clippy --workspace --all-targets --locked -- -D warnings` → limpio; `git diff dde681c -- crates/rayd-core/Cargo.toml` vacío. El dominio no tiene código por plataforma, pero la ejecución en Windows no se hizo aquí.

## 3. [rayd] Adapters, proxy, gRPC and hooks

- [x] 3.1 `adapters/ip_command.rs`: the shared `ip` runner extracted from `imds_block.rs`, with no behaviour change (the `imds_block` tests and `crates/rayd/tests/m6_imds.rs` stay green).
  - Evidencia: 2 tests en `ip_command.rs`; `m6_imds` 3 passed en la corrida de 3.10. El runner captura ahora también stderr (nunca se registra), por 3.9; design D5 actualizado.
- [x] 3.2 `adapters/egress_routes.rs`:
  - step executor (`ip -batch` fills, rule add/del, flush);
  - IPv6 presence;
  - `getifaddrs` local addresses (workspace `nix` += `net` feature; `cargo deny check` clean);
  - the route-get probe runner.
  - Note (verified here): the local addresses come from `ip -o addr show` through the shared runner (pure parser `rayd_core::network::probe::interface_addresses`), not `getifaddrs`. The `nix` `net` feature enables `socket`, which pulls `memoffset`, a crate that is not in `Cargo.lock`; the design assumed it added none. No manifest changed, so `cargo deny` sees the same graph. A failed read fails the apply (`egress_update_failed: local_addresses`), so the own-address guard never runs without its list.
- [x] 3.3 `network/manager.rs`:
  - `NetworkManager` with its mutex, the enforcement atomic and the `EgressEnv` `RwLock`;
  - `enforce_deny_all_at_run`, `update`, `snapshot` and `reverify_after_resume`;
  - the recovery path and the budgets of design D16.
  - Note (verified here): the enforcement atomic and the `EgressEnv` map live in `SandboxSession` (`egress_enforcement()` / `egress_env()`), which the manager publishes to. `Health`, the process and PTY planners and the sidecar supervisor already hold the session, so none of their constructors changed. Every public manager operation runs in its own task, so an expired hook budget or an abandoned RPC never drops a swap half-way. When the `/resume` verification fails, the stored plan is reinstalled with `plan_recovery(&stored_plan)` (emergency deny-all at 149 while slot `A` is rebuilt) rather than `plan_swap`, and deny-all is the fallback.
- [x] 3.4 `network/proxy.rs`:
  - listener, semaphore of 128, first-byte sniffing, HTTP CONNECT/forward, SOCKS5;
  - dial to the checked `SocketAddr`, `copy_bidirectional`;
  - the `Resolve`/`Dial` test seams.
  - `network/upstream.rs`: the SOCKS5 client chain.
  - `network/stats.rs`: `DecisionCounters` and the 60 s `egress_proxy_stats` logger.
  - Evidencia: 17 tests del proxy en `crates/rayd/src/network/tests.rs` (resolvedor y marcador falsos, cadena SOCKS5 contra un servidor que graba, higiene de logs) y 1 en `stats.rs`; verdes en 3.10 (rayd lib 166 passed).
- [x] 3.5 `grpc/network.rs` (the `UpdateNetwork`/`GetNetwork` error table of design D11). `grpc/mod.rs`: `add_service`. `grpc/health.rs`: field 13.
  - Evidencia: 3 tests en `grpc/network.rs` y los 4 de `crates/rayd/tests/m9_network.rs`; verdes en 3.10.
- [x] 3.6 `hooks/mod.rs`:
  - `/run`: deny-all, proxy start and verification before the 200, within the 1.5 s budget, answering 200 always;
  - `/resume`: synchronous re-verification within 3 s.
  - `main.rs` wiring (the capability comes from the existing `capabilities` detection).
  - Evidencia: `m9_network.rs` (`/run` con `network.enforce` sin `CAP_NET_ADMIN` responde 200 y `Health` dice `NONE`; políticas que restringen → `FAILED_PRECONDITION`) y `m9_egress.rs` como root en `unshare --net` (deny-all en `/run`, verificación, `/resume`); verdes en 3.10 y 3.9.
- [x] 3.7 `code/manager.rs`: the egress map is merged under the context envs of every `CreateContext`/`RestartContext`/rotation/post-resume restart op. Process and PTY managers pass the egress layer.
  - Note (verified here): the merge is `SidecarOp::with_egress_env`, applied once in `SidecarSupervisor::call` (the choke point every create, restart, rotation and post-resume restart goes through). The registry keeps only the caller's envs, and `Execute` envs are untouched. Pinned by `code::supervisor::tests::kernel_starting_ops_leave_with_the_egress_proxy_env_under_their_envs`.
- [x] 3.8 `rayd` unit tests of design D18: gRPC in-process with a fake executor, `/run` and `/resume`, the proxy suite with the fake resolver and dialer, upstream chaining against a recording SOCKS5 server, and the log-hygiene capture.
  - Evidencia: los de 3.4–3.6 más 3 en `adapters/egress_routes.rs` y `code::supervisor::tests::kernel_starting_ops_leave_with_the_egress_proxy_env_under_their_envs` (3.7); recuento de la revisión: rayd-core `network` 67, rayd lib `network` 21, hooks de egress 9, `grpc::network` 3; todos verdes en 3.10.
- [x] 3.9 `crates/rayd/tests/m9_egress.rs` (Linux, root, network namespace, self-skip when not root), as in design D18.
  - verificado aquí (2026-09-23): la prueba existía; al correrla de verdad (`sudo env RAYITO_REQUIRE_EGRESS_NETNS=1 unshare --net -- <binario m9_egress> --test-threads=1`, kernel 6.8, iproute2 6.1) salía roja: `enforce_deny_all_at_run` devolvía `None` con `egress_update_failed step="fill_table"`. Causa: en un guest recién arrancado las tablas 101/102/103 no existen, y `ip route flush table N` / `ip route show table N` fallan con exit 2 y "FIB table does not exist" en vez de tratarla como vacía, así que el deny-all de `/run` fallaba siempre con el `ip` real (el kernel en memoria de los tests unitarios no modela ese caso).
    Arreglo: `IpOutput` guarda también stderr (nunca se registra), `rayd_core::network::probe::table_missing` reconoce esa frase fija (test `a_never_created_table_is_told_apart_from_other_failures`), y `IpEgressRoutes::flush`/`show_table` la leen como tabla vacía. Rojo → verde: la misma invocación pasa a `1 passed`. Además, con `RAYITO_REQUIRE_EGRESS_NETNS=1` el self-skip es un fallo (así el paso de CI no puede quedar verde sin comprobar nada): como root fuera de un namespace con la variable → panic con el mensaje; como usuario sin la variable → `skipped`, `1 passed`. `ip rule show` de la VM queda intacto después.
- [x] 3.10 Gates:
  - `cargo fmt --all --check`;
  - `cargo clippy --workspace --all-targets -- -D warnings`;
  - `cargo test --workspace --locked` (WSL2 or CI for `cfg(unix)`);
  - the auditable aarch64-musl build + `scripts/check_auditable.py`, with the binary size delta in the task note.
  - verificado aquí (2026-09-23), VM Lima Ubuntu aarch64: `cargo fmt --all --check` exit 0; `cargo clippy --workspace --all-targets --locked -- -D warnings` limpio; `cargo test --workspace --locked` con los binarios ejecutados como un usuario uid 1500 (como el runner de CI): rayd lib 166, bin 5, m1 14, m2 25, m3 30, m4 19 (+1 ignored), m5_pty 15, m5_suspend_resume 7, m6_hooks 7, m6_imds 3, m6_limits 5, m7_poly 9, m9_deno 4, m9_egress 1, m9_metrics_history 6, m9_network 4, m9_timeout 11, m9_transfer 21, s3_store 0 (+1 ignored), rayd-core 483, todo verde; más `m9_egress` como root en `unshare --net` → 1 passed. Con el usuario por defecto de la VM (uid 501 < 1000) 146 tests de integración fallan con `PermissionDenied` de la política C-05: es el host, no el código.
    `cargo auditable zigbuild --release --locked --target aarch64-unknown-linux-musl -p rayd` (zig 0.16.0, cargo-zigbuild 0.23.4, cargo-auditable 0.7.6) → 13 755 256 B, `ELF 64-bit LSB executable, ARM aarch64, statically linked, stripped`; `python3 scripts/check_auditable.py` → `.dep-v0: 257 packages, root rayd 0.2.0`. Delta frente a 0.2.0 (12 524 384 B auditable, `rayito-base` 18.0): +1 230 872 B para todo M9 (egress, transfer, timeout, métricas, deno). zigbuild sin auditable: 13 751 344 B.

## 4. [sidecar] Kernel environment pin

- [x] 4.1 `kernel-sidecar/tests/test_kernels.py::test_op_envs_proxy_variables_reach_the_kernel_environment` (a pin test; no sidecar code change, design D7/D18). `cd kernel-sidecar && uv run pytest` green.
  Verificado aquí (2026-09-23, macOS arm64): archivo nuevo `kernel-sidecar/tests/test_kernels.py` (no marcado `kernel`, corre en cualquier host): `KernelContext` real detrás del `SidecarServer` con un `AsyncKernelManager` falso que registra el `env` de `start_kernel`; `create_context` y `restart_context` (contexto nuevo y `default`) con las ocho claves de `egress_proxy_env` llegan tal cual al entorno del kernel, y el entorno del sidecar no las tiene. Sin cambios en `src/`.
  Mutaciones (restauradas): quitar `env.update(envs)` de `kernel_environment` → falla; quitar `self.envs = dict(envs)` de `restart` → falla.
  Puertas: `uv run --with-requirements requirements.txt pytest` → 85 passed, 14 deselected; `uvx ruff==0.16.7 check .` y `format --check .` limpios; `uv run mypy src` sin errores.

## 5. [python] SDK (sync and async identical)

- [x] 5.1 `rayito/exceptions.py`: `UnimplementedError(NotImplementedError)`, unless 0.1 found it. `rayito/e2b/exceptions.py`: the shim class subclasses it, and the test proves it.
  Verificado aquí (2026-09-23, macOS arm64, `clients/python`, reintento) (parte del shim): `rayito.e2b.exceptions.UnimplementedError` es la misma clase que `rayito.exceptions.UnimplementedError` (D13 de `m9-e2b-v2-surface` sustituye la subclase por la re-exportación, así que `issubclass` se cumple trivialmente); lo prueban `test_e2b_v2_exports.py::test_the_shim_unimplemented_error_is_the_native_class` y `test_exceptions.py`.
  Mutación: volver a una subclase → 2 fallos.
- [x] 5.2 `_models.py`:
  - Evidencia de 5.2–5.7 (2026-09-23, reintento de specs-docs): `test_network_base.py`, `test_network_sync.py`, `test_network_async.py`, `test_payload.py`, `test_models.py`, `test_exceptions.py` y `test_e2b_compat_{base,sync,async}.py` dan 347 passed en 17 s (la revisión contó 368 en su selección); el gate completo, en 5.8.
  - `ALL_TRAFFIC`, `EgressProxy` (password `repr=False`), `NetworkSelectorContext`, `NetworkSelector`, `NetworkOptions`, `NetworkPolicy`, `EgressEnforcement`, `NetworkState`;
  - `SandboxHealth.egress_enforcement`.
  - `_sandbox_base.health_from_proto` fills it.
- [x] 5.3 `_network_base.py` (design D13), with `tests/unit/test_network_base.py`.
- [x] 5.4 `_payload.py` `network_enforce` and `build_launch_plan(network_enforce=)`, with the `test_payload.py` cases.
- [x] 5.5 `sandbox_sync/main.py`:
  - `create(network=, allow_internet_access=)` with the fail-closed gate: terminate even with `keep_on_failure`;
  - `_readiness_health`;
  - `pool=` refusal;
  - `LaunchOptions.network` and `reincarnate`;
  - `update_network` (instance and class variant: connect → update → close);
  - `get_network`.
- [x] 5.6 `sandbox_async/main.py`: the identical async surface.
- [x] 5.7 `rayito/__init__.py` exports. `tests/unit/fake_network.py` servicer wired into `conftest.py`. `test_network_sync.py` and `test_network_async.py`, plus the model and base tests (design D18).
  - Fixes de revisión (2026-09-23): (a) la `feature` de la compuerta sigue la regla de TS `egressFeature`: `allow_internet_access=False` sólo cuando la política resuelta es la del flag solo (`deny_out=[ALL_TRAFFIC]`, sin `allow_out` ni proxy) y el flag es `False`; `network={allow_out:[x]}` + flag da ya `network` (D13: «came only from that flag»). Nuevo `egress_feature` en `_network_base.py`; `test_launch_plan_names_the_flag_only_when_it_alone_restricts` ampliado (flag solo, `deny_out` = flag, `allow_out`, proxy, `deny_out` sin flag): rojo con el código anterior (`'network' == 'allow_internet_access=False'`), verde después. (b) Gemelos async que faltaban: `test_async_proxy_credentials_travel_only_in_update_network` y `test_async_update_network_checks_the_shape_before_the_rpc`; rojo en una copia con el proxy quitado del `UpdateNetwork` inicial y sin comprobar la forma en `update_network` (2 fallos), verde en el árbol (`test_network_{base,sync,async}.py` 89 passed).
- [x] 5.8 `uv run pytest tests/unit && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests` green.
  - Gate de Python (2026-09-23, macOS arm64, reintento de specs-docs): `uv run --with pytest-timeout pytest tests/unit --timeout 60` da 2090 passed en 321 s, exit 0; `ruff check .` limpio; `ruff format --check .` 198 ficheros (el `README.md` ya formateado); `mypy src tests` sin errores en 196 ficheros.
  Corrida del fixer python (2026-09-23, macOS arm64, tras los fixes de revisión): `uv run --with pytest-timeout pytest tests/unit --timeout 60` 2090 passed, 0 failed (332 s; los `is_running` ya no fallan); `ruff check .` limpio; `mypy src tests` sin errores en 196 ficheros; `ruff format --check .` sólo marca `clients/python/README.md` (bloques de código de la doc, área de docs, en edición por otro agente). Queda sin marcar hasta que ese fichero pase el formato.

## 6. [ts] SDK (camelCase mirror)

- [x] 6.1 `src/errors.ts` `UnimplementedError`, unless 0.1 found it. `src/models.ts` types and `ALL_TRAFFIC`. `SandboxHealth.egressEnforcement`.
  - verified here: no sibling had added the TS `UnimplementedError`, so this change adds it (outside the `SandboxError` hierarchy, with `feature` and `reason`). `EgressEnforcement` is a const object plus a string-union type, like `FileType`, so `rayito.EgressEnforcement.GUEST_ROUTES` works.
- [x] 6.2 `src/sandbox/network.ts` pure helpers. `src/payload.ts` and `src/sandbox/launch.ts` `networkEnforce`.
  - verified here: `launch.test.ts` gains the `networkEnforce` case.
- [x] 6.3 `src/sandbox/sandbox.ts`:
  - create options and the gate with `keepOnFailure` termination;
  - `updateNetwork` (instance and static), `getNetwork`;
  - `pool` refusal.
  - `src/sandbox/readiness.ts`: keep the readiness health.
  - `src/index.ts` exports.
  - verified here: `healthFromProto` fills `egressEnforcement`. The `Health` that satisfied readiness is kept in `Sandbox.#readinessHealth`. `reincarnate()` re-sends the resolved policy through `LaunchOptions.network`.
- [x] 6.4 `tests/unit/fake/network.ts` plus the fake server wiring. `tests/unit/network.test.ts`, and additions to `sandbox.test.ts` and `payload.test.ts`.
  - verified here: 11 mutations were applied to a scratch copy of the TS client. Each one turned the targeted tests red, and reverting it made them green again. The mutations were: gate without terminate, payload without the `network` block, `healthFromProto` ignoring field 13, `FailedPrecondition` left unmapped, pool accepting a policy, `allowInternetAccess` not merged, create skipping `UpdateNetwork`, static `updateNetwork` not closing, `denyOut` hostname check removed, `reincarnate` dropping the policy, and `buildLaunchPlan` not forwarding `networkEnforce`.
- [x] 6.5 `pnpm lint && pnpm typecheck && pnpm test && pnpm build` green.
  - verificado aquí (2026-09-23): Gate completo del paquete el 2026-09-23 en este árbol: `pnpm lint` (biome, 114 ficheros, 0 errores), `pnpm typecheck` exit 0, `pnpm test` 35 ficheros / 762 passed, `pnpm build` ok y `pnpm pack:check` ok (lista `package/dist/index.{mjs,cjs,d.mts,d.cts}`). `filesystem.test.ts` "3 MB file" pasa sin carga del host.
  - verified here on the files of this change: biome, `tsc --noEmit` and the 7 egress-related unit files (150 tests) are clean, and `pnpm build` passes. The package-wide gate is still red. Its failures are in sibling M9 files that are mid-implementation (`transfer.ts`/`transfer.test.ts`, `metrics.test.ts`, the format of `fake/filesystem.ts`, `fake/s3.ts` and `listing.test.ts`), plus `filesystem.test.ts` "3 MB file", which also times out on a pristine `HEAD` export under the current host load.
  - reverificado (2026-09-23, reintento): `pnpm lint` (biome, 123 ficheros, 0 errores), `pnpm typecheck` exit 0, `pnpm test` 38 ficheros / 797 passed, `pnpm build` exit 0 y `pnpm pack:check` exit 0.

## 7. [python] E2B shim

- [x] 7.1 `e2b/_compat.py`:
  - `map_create_kwargs(allow_internet_access=, network=)` forwarding without raising;
  - `NO_EGRESS_REASON` removed;
  - `map_network` with the key table of design D14 and the selector context wrapper.
  - `e2b/_sync.py` and `_async.py`: `network=` on create/constructor/`beta_create`, `update_network` instance and class forms returning `None`, gate error re-raised as the shim class.
  - `ALL_TRAFFIC` importable from `rayito.e2b` (not added to `__all__`).
  Verificado aquí (2026-09-23, macOS arm64, `clients/python`, reintento): `map_create_kwargs(allow_internet_access=, network=)` reenvía sin lanzar, `NO_EGRESS_REASON` borrado, `map_network` con la tabla D14 y el contexto de selector; `network=` en create/constructor/`beta_create`, `update_network` de instancia y de clase devolviendo `None`.
  `ALL_TRAFFIC` se importa desde `rayito.e2b`; está en `__all__` porque D4 de `m9-e2b-v2-surface` (posterior) lo añade explícitamente.
  Mutaciones: `rules` aceptado → 6 fallos; `allow_internet_access` forzado a `True` → 5 fallos y 2 errores.
- [x] 7.2 After QE2 (task 9.2): set `HTTPS_PORTS_SUPPORTED` and the reason text citing the real §16 row number. Unit tests for both branches, with the constant patched.
  - Medida hecha, texto pendiente (2026-09-24): QE2 (Q67) dio **no soportado**, así que `HTTPS_PORTS_SUPPORTED` se queda en `False` y los tests unitarios de las dos ramas ya existen (`test_https_ports_follow_the_measurement_constant`). Falta sólo el motivo, que aún dice «pendiente»: en `clients/python/src/rayito/e2b/_compat.py` (`HTTPS_PORTS_REASON`) y `clients/typescript/src/e2b/compat.ts`, «el proxy de Lambda MicroVMs no reenvía TLS extremo a extremo a un puerto del guest (medido, fila Q67 de AWS_API_NOTES.md §16); get_host(puerto) sirve HTTP en claro», y la fila literal de `e2b-compat.md` a la vez. Es código fuente: lo hace el dueño de `clients/*/src` cuando acaben los refactors; requerido para M9.
  - Hecho (2026-09-24): `HTTPS_PORTS_REASON` en Python y TS y la fila de `e2b-compat.md` citan ahora la fila 67 (QE2) medida; `HTTPS_PORTS_SUPPORTED` sigue en `False`. `test_e2b_compat_base.py` + `test_e2b_v2_exports.py` 181 passed; `e2b-compat.test.ts` + `e2b-shim.test.ts` 54 passed.
- [x] 7.3 `test_e2b_compat_base.py`, `_sync.py` and `_async.py` cases of design D18. The Python gates of 5.8 are green.
  Gate de Python (2026-09-23, macOS arm64, reintento de specs-docs): `uv run --with pytest-timeout pytest tests/unit --timeout 60` da 2090 passed en 321 s, exit 0; `ruff check .` limpio; `ruff format --check .` 198 ficheros (el `README.md` ya formateado); `mypy src tests` sin errores en 196 ficheros.
  Casos D18 en `test_e2b_compat_base.py` (`test_allow_internet_access_false_is_forwarded_with_the_connector`, `test_network_mapping_passes_the_policy_keys_and_the_e2b_selector`, `test_network_keys_without_primitive_are_unimplemented`, `test_https_ports_follow_the_measurement_constant` con las dos ramas) y en `_sync.py`/`_async.py` (`beta_create(network=)`, `update_network` de instancia y clase, el error de la puerta sin enforcement).
  Sin marcar sólo por el gate de 5.8: última corrida (2026-09-23, macOS arm64, reintento): `uv run --with pytest-timeout pytest tests/unit --timeout 60` 2058 passed, 2 failed; los dos fallos son nativos y ajenos al shim: `test_sandbox_sync.py::test_is_running_request_timeout_bounds_the_health_deadline` y su gemelo async (el plazo de `Health` llega como `0.501 <= 0.5` con la máquina cargada; la revisión los vio fallar también aislados; los corrigió después el fixer de python y la corrida completa de hoy da 2090 passed). `ruff check .` limpio, `ruff format --check .` 198 ficheros, `mypy src tests` sin errores en 196 ficheros. Los ficheros del shim (`test_e2b_compat_{base,sync,async}.py`, `test_e2b_v2_{base,sync,async,exports}.py`, `test_packaging.py`) dan 530 passed.

## 8. [infra] CI and images

- [x] 8.1 `.github/workflows/ci.yml`, job `arm`: the root network-namespace step of design D19 after `cargo test --workspace`. `actionlint` clean and `python scripts/check_pins.py` clean.
  - verificado aquí (2026-09-23): paso `m9_egress policy routes as root in a fresh network namespace (ADR-012)` en el job `arm`, tras `cargo test --workspace --locked`. En vez del `ls target/debug/deps/m9_egress-* | head -1` de D19 (con la caché de `rust-cache` puede elegir un binario viejo), toma el ejecutable de `cargo test -p rayd --test m9_egress --locked --no-run --message-format=json` con `jq`, comprueba `test -x` y corre `sudo env RAYITO_REQUIRE_EGRESS_NETNS=1 unshare --net -- "$binary" --test-threads=1`, así que el self-skip falla el paso. `actionlint` no está instalado en el host; `uvx --from actionlint-py actionlint .github/workflows/ci.yml` (actionlint 1.7.12) → exit 0, sin salida (sin `shellcheck` instalado, así que los `run:` no pasaron por shellcheck). `uv run --no-project python scripts/check_pins.py` → OK, 8 ficheros.
- [x] 8.2 [AWS, maintainer] Publish M9 `rayito-base` and `rayito-base-caps` from this tree (the `image-publish` / `image-publish-caps` steps by hand; `make` is not installed). Versions go in the task note, placeholders only in tracked files.
  - Republicado sólo `rayito-base-caps` (2026-09-23, carril caps) con el arreglo de 9.1, desde un zip en un directorio temporal (no se tocó `image/`): **10.0** (un primer intento que subía `RUN_ENFORCE_BUDGET` a 10 s, descartado porque `Health` llega durante `/run`; versión huérfana, se puede borrar) y **11.0** (arreglo final; `rayd` 13 756 408 B, zip 13 874 844 B, `check_auditable.py` 257 paquetes; `snapshotBuild` 933 363 712 / 1 361 211 392 / 36 409 344 B; publicación 3 min 45 s). `rayito-base` y `rayito-base-poly` siguen con el `rayd` anterior (no aplican egress, pero conviene republicarlos desde este árbol).
  - Hecho (2026-09-23, accept-prep): `rayd` musl con `cargo auditable zigbuild` en la VM Lima (13 756 408 B), `copy_sidecar.py` + `image_zip.py` → `image/rayito-image.zip` 13 874 844 B; `publish_image.py --artifact image/rayito-image.zip --base-image-version 1` → `rayito-base` **21.0** (216,6 s; `snapshotBuild` 927 043 584 / 1 360 146 432 / 39 071 744 B) y el mismo zip con `--os-capabilities ALL --image-name rayito-base-caps` → `rayito-base-caps` **9.0** (237,9 s; 931 262 464 / 1 362 808 832 / 33 214 464 B), ambas `UPDATED`/`SUCCESSFUL`/`ACTIVE`. Versiones previas: 20.0 y 8.0. Sin conector de egress propio desplegado en la cuenta de pruebas (ver 9.x).

## 9. [e2e] Real-AWS measurements and acceptance

- [x] 9.1 QE1 (`test_guest_network_facts`) on caps. Fill the §16 row. Apply the design D17 stop rule: with a loopback resolver, stop and write an ADR-012 addendum proposal instead of continuing. With an unknown `ip route get` form, extend `classify_route_get` and its test.
  - Medido (2026-09-23, `rayito-base-caps` 11.0, 1 passed en 10,7 s, `create()` con la política 6,37 s): fila 66 rellena. **La regla de parada de D17 se dispara**: `nameserver 127.0.0.2` (loopback) y `169.254.53.53`, ambos escuchando UDP 53 dentro del guest, y `getaddrinfo("aws.amazon.com")` como uid 1000 bajo deny-all sale con 0. Las tres formas de `ip route get` son conocidas (exit 2 `RTNETLINK answers: Invalid argument` → `Blocked`, `local ...` → `Local`, `... via <ip> dev eth0` → `Routable`), así que `classify_route_get` no cambia. **Parado aquí**: 9.2–9.4 esperan al anexo de ADR-012 (decisión del orquestador).
  - El test pedía `user="root"` para `ip route get`/`ip rule show`/IMDS y la imagen prohíbe root (`running as root is not allowed by this image`): ahora las sondas corren como uid 1000 (no necesitan privilegio) y la parte root de (f) sale de `ip route get 169.254.169.254 uid 0` y del log `imds_probe` de `rayd`.
  - Bug de `rayd` encontrado y arreglado para poder medir: con `allow_internet_access=False` la compuerta terminaba el VM 2 de cada 3 veces. Logs: el primer `ip` tras restaurar tardó 2,76 s (`egress_enforce_failed step=budget`, `egress_policy_applied duration_ms=2763`) y un `Health` trazado desde el SDK llegó 1,3 s después de empezar `/run` con `sandbox_id` ya puesto y `egress_enforcement=NONE`. Arreglo: `SandboxSession::egress_settling` (puesto al aceptar un payload con `network.enforce`, quitado al acabar la tarea del deny-all o sin `CAP_NET_ADMIN`) y `Health.agent_ready` falso mientras dura. Tests rojos sin el arreglo: `hooks::tests::egress::health_waits_for_a_deny_all_that_outlives_the_run_budget` (retraso de 2 763 ms en `FakeKernel::local_addresses`, tiempo pausado) y `session::tests::health_is_not_ready_while_the_run_deny_all_settles`; más `an_image_without_net_admin_is_ready_after_run` y `a_run_without_enforce_never_settles`. Puertas en la VM: `cargo fmt --check`, `clippy -D warnings` limpios, rayd-core 485, rayd lib 168, integración como uid 1500 todo verde, `m9_egress` como root en `unshare --net` 1 passed. Tras republicar caps 11.0: 8 de 8 `create(allow_internet_access=False)` dan `guest_routes` (el primer `Health` llega con `agent_ready=false`). Queda una corrida (1 de 10) que terminó en 3,8 s con el VM parado a mitad de `/run`, sin traza: posible `Health` anterior a `/run` (sin `sandbox_id`), no reproducida en 8 trazas.
  - Decisión (2026-09-23, dueño): **opción C**, escrita como «Adenda: DNS bajo deny-all en caps» de ADR-012 (`ARCHITECTURE.md`) y en design D17. Sin cambio de aplicación en M9: bajo deny-all en caps los nombres pueden resolverse por los resolvedores de la plataforma dentro del guest y toda conexión fuera del VM sigue fallando. La aceptación de DNS pasa a ser **«resuelve (o no), la conexión falla»**; el delta de spec (`egress-control`, «Internet off»), T17, `network.md` y `e2b-compat.md` lo reflejan. La regla de parada queda resuelta: 9.2–9.4 pueden seguir.
- [x] 9.2 QE2 (`test_https_ports_measurement`, measurement half). Fill the §16 row, then do task 7.2.
  - Sin correr (2026-09-23): parado por la regla de parada de 9.1 (resuelta con la adenda de ADR-012; pendiente de correr).
  - Hecho (2026-09-24): `test_https_ports_measurement` en `rayito-base` 23.0: `get_host(8443)` hacia un servidor TLS de uid 1000 no devuelve respuesta HTTP, ni con `x-aws-proxy-force-h2` → `https_ports supported: False`, igual que la constante (el test lo comprueba). Fila **Q67** rellena. 7.2 queda con sólo el texto del motivo.
- [x] 9.3 `clients/python/tests/e2e/test_m9_egress.py` tests 2–11 of design D19 green against real AWS. Output summary, timings (update within 1 s, the 5 s refusal) and the CloudWatch log check go in the task note.
  - Aceptación de DNS (adenda de ADR-012, opción C): `test_internet_off` ya no exige que `getaddrinfo` falle; resuelve el nombre como uid 1000 y exige que un TCP connect a cada dirección resuelta falle (`unresolved` o `blocked`, nunca `connected`). El e2e de TS de 9.4 (`unreachable` en `m9-egress.e2e.test.ts`) hace lo mismo.
  - Hecho (2026-09-24, aceptación): `test_m9_egress.py` **14/14** (QE1, QE2, tests 2–11 y el shim sync/async) dentro de la regresión e2e de Python de 2026-09-24 sobre `rayito-base` 23.0, `rayito-base-caps` 12.0 y `rayito-base-poly` 7.0: **62 passed de 62** (server-timeout 12, observability 4, deno 8, egress 14, transfer 14, M4 1, M5 2, M6 2, M7 poly 5). QE1 repetido en caps 12.0 con los mismos hechos que Q66. `create()` con política 4,4–11,8 s; `urllib` https de uid 1000 rechazado en 1,75 s; resolver + conectar bajo deny-all: `blocked` (opción C); imagen por defecto falla cerrado y queda `TERMINATED` en 1,3 s con y sin `keep_on_failure`; `update_network` deny-all efectivo en 0,002 s y allow-all en 0,072 s (< 1 s); `curl` a un host denegado → `CONNECT tunnel failed, response 403`; proxy → IMDS 403 y `CONNECT` al puerto de hooks 403; 8 líneas del log de `rayd` revisadas sin datos del proxy.
- [x] 9.4 `clients/typescript/tests/e2e/egress.e2e.test.ts` green against real AWS.
  - written by the [ts] implementer as `clients/typescript/tests/e2e/m9-egress.e2e.test.ts`, the M9 e2e naming the orchestrator set. It has 6 tests: 3 on `RAYITO_TEMPLATE_CAPS` (`allowInternetAccess: false` with urllib, DNS and loopback through `getHost`; the hostname allowlist 200/403 plus the bypasses; the `updateNetwork` instance and static cycle, each within 1 s) and 3 on `RAYITO_TEMPLATE` (fail-closed with and without `keepOnFailure`, `TERMINATED` within 60 s; `updateNetwork` restrictions → `UnimplementedError`). It is collected and skips without `RAYITO_E2E`. It has not been run against AWS yet (Accept step).
  - Hecho (2026-09-24): `m9-egress.e2e.test.ts` verde dentro de los **26/26** de TS sobre `rayito-base` 24.0 y `rayito-base-caps` 13.0.
- [ ] 9.5 CI `arm` job green, including the `m9_egress` network-namespace step.
  - Diferido con razón escrita (2026-09-24): el job `arm` sólo corre en GitHub Actions y el repositorio aún no tiene el PR de M9; no necesita AWS y se comprueba en el primer run de CI de ese PR (gate de merge, no de archivo). Equivalente local ya verde: `m9_egress` como root en `unshare --net` en la VM Lima, 1 passed (9.1). Anotado en `MILESTONES.md` (M9, diferidos).

Seguimiento para el siguiente ciclo (no es una tarea de este cambio): **egress: bloquear DNS (opción A) en deny-all de caps**, una regla `ip rule` con `uidrange 1000-65535`, `ipproto udp`/`tcp`, `dport 53` y `prohibit` antes de la regla `local` de prioridad 0 (moviéndola, con cambio atómico y rollback que nunca dejen el guest sin enrutamiento local); al llegar, la aceptación de DNS vuelve a «la resolución falla». Anotado en `MILESTONES.md` (M9, diferidos) y en la adenda de ADR-012.

## 10. [docs] Documentation (design D20)

- [x] 10.1 `ARCHITECTURE.md`: ADR-012, the `NetworkService` row, the "Política de egress (M9)" subsection, the hexagonal tables and the variants paragraph.
  - Verificado (2026-09-23, reintento de specs-docs): ADR-012 (contexto Q44/Q60/Q48, decisión de dos capas, modos, sólo con `CAP_NET_ADMIN`, consecuencias); fila `NetworkService`; subsección «Política de egress (M9)» con la tabla de huecos 101/102/103 y prioridades 150/151/149, los modos, el proxy y su guardia y la exportación de entorno; `network` en las tablas hexagonales y los adaptadores `ip_command`, `egress_routes` y `network/`; «Variantes de imagen» dice que caps aplica la política. `test_m9_docs.py::test_the_m9_adrs_are_written` rojo → verde.
- [x] 10.2 `SECURITY.md`: the T8 update and the new T17. `docs/site/docs/security.md`: the summary line.
  - Verificado (2026-09-23, reintento de specs-docs): T8 gana la capa del guest en caps, el falla-cerrado y la salvedad DNS del conector VPC; T17 nueva con los puntos de D20 (SSRF del proxy root y su guardia, capas del guest que caen ante un exploit o root, DNS, falla cerrado, credenciales del upstream sólo por RPC, C-05/C-01 sin retroceso); la sección «Execution role» ya no dice que `allow_internet_access=False` sea `UnimplementedError`; `docs/site/docs/security.md` lleva la fila T17 y el párrafo del shim actualizado. `test_m9_docs.py::test_the_threat_model_names_the_m9_surfaces` rojo → verde; `test_security_docs.py` verde.
- [x] 10.3 `docs/site/docs/network.md` (new) plus the `mkdocs.yml` nav entry. `docs/site/docs/e2b-compat.md`: moves and divergences.
  - Verificado (2026-09-23, reintento de specs-docs): `network.md` (modos, semántica, ejemplos Python/TS/shim, `update_network`, `egress_proxy`, cómo llega el proxy a los procesos, las diferencias de D20 y la alternativa de plataforma) con la entrada «Red saliente» tras «Persistencia»; `e2b-compat.md` mueve `allow_internet_access=False`, `network` y `update_network` a «Se mapea», deja `https_ports` según QE2 (fila Q67, pendiente) y mantiene `rules`, `mask_request_host` y `allow_public_traffic=True` como no implementados. `mkdocs build --strict` verde.
- [x] 10.4 `SPEC.md` §3 shim row. `MILESTONES.md` M9 egress acceptance line.
  - Verificado (2026-09-23, reintento de specs-docs): la fila del shim en §3 (y la de §5) ya no lista `allow_internet_access=False` como no implementado: «en `rayito-base-caps`; falla cerrado en otras imágenes»; §3 gana la fila «Red saliente (M9)»; `MILESTONES.md` «M9 — Paridad con E2B» con la línea de `m9-egress-policy` (QE1 primero, QE2, tests 2–11, e2e de TS, paso netns de CI; *pendiente de aceptación en AWS*).
- [x] 10.5 Final gates of design D21. `python scripts/check_hygiene.py` exits 0. `openspec validate m9-egress-policy --strict --no-interactive` passes.
  - Parcial (2026-09-23), sin marcar: solo la mitad openspec. Los bloques `e2b-compat` "E2B create kwargs…" y "E2B features without an AWS primitive…" se re-basaron sobre server-timeout/observability (deadline y `lifecycle`, `set_timeout` mapeado, `beta_create(mcp=…)` en lugar de `auto_pause`, re-export nativo de `UnimplementedError`) y `openspec validate m9-egress-policy --strict --no-interactive` → valid; el archivo secuencial simulado de los seis cambios M9 pasa. `check_hygiene.py` → exit 0 (939 ficheros) en la re-verificación; quedan los gates de código de D21, por eso sigue sin marcar.
  - Pendiente, paso de cierre de M9 (no se difiere, 2026-09-24): se corre sobre el árbol final cuando terminen los refactors sin cambio de comportamiento que están en curso en `crates/` y `clients/*/src`. Última pasada local completa (2026-09-23): Python 2090 passed, TypeScript 831 passed, sidecar 85 passed, `scripts/tests` 184, `check_hygiene`/`check_pins`/`check_license`/`buf lint` OK; faltan los gates de Rust (`cargo fmt`, `clippy -D warnings`, `cargo test --workspace --locked` en la VM Lima como uid 1500) sobre ese árbol.
  - Hecho (2026-09-24, cierre de M9, árbol final): Rust en la VM Lima (Ubuntu aarch64, `CARGO_TARGET_DIR` propio, `-j 2`): `cargo fmt --all --check` exit 0; `cargo clippy --workspace --all-targets --locked -- -D warnings` limpio; `cargo test --workspace --locked` con los binarios como uid 1500 (`setpriv --reuid=1500 --regid=1500 --clear-groups`): rayd lib 172, bin 5, m1 14, m2 25, m3 30, m4 19 (+1 ignored), m5_pty 15, m5_suspend_resume 7, m6_hooks 7, m6_imds 3, m6_limits 5, m7_poly 9, m9_deno 4, m9_egress 1, m9_metrics_history 6, m9_network 4, m9_timeout 11, m9_transfer 21, s3_store 0 (+1 ignored), rayd-core 498: 856 passed, 0 failed, 2 ignored; `m9_egress` como root en `unshare --net` con `RAYITO_REQUIRE_EGRESS_NETNS=1` → 1 passed; `cargo deny check` → advisories, bans, licenses, sources ok; `cargo auditable zigbuild --release --locked --target aarch64-unknown-linux-musl -p rayd` → `rayd` estático de 13 764 024 B y `check_auditable.py` → `.dep-v0: 257 packages, root rayd 0.2.0`; Python (macOS arm64): `uv run --with pytest-timeout pytest tests/unit -q --timeout 60` 2115 passed; `uvx ruff==0.16.7 check` limpio y `format --check` 219 ficheros; `mypy src tests` sin errores en 217 ficheros; sidecar: `pytest` 85 passed (14 deselected), ruff limpio, `mypy src` sin errores; TypeScript: `pnpm lint` (biome, 139 ficheros), `pnpm typecheck` exit 0, `pnpm test` 40 ficheros / 863 passed, `pnpm build` y `pnpm pack:check` exit 0; `buf lint` exit 0; `check_hygiene.py` OK (939 ficheros); `check_pins.py` OK (8 ficheros); `check_license.py` OK; `gen_limits.py --check` exit 0; `scripts/tests` 186 passed; `mkdocs build --strict` (target `docs` del `Makefile`) sin avisos. `openspec validate m9-egress-policy --strict --no-interactive` → valid.
