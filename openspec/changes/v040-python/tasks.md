# Tasks — v040-python

## 1. Un solo tipo para feature ausente (ruptura)

- [x] 1.1 `exceptions.py`: `LifecycleUnsupportedException` pasa a heredar de `UnimplementedError` (cuerpo vacío + docstring); se mueve debajo de la definición de `UnimplementedError`
- [x] 1.2 `_lifecycle_base.py`: `connect_extension`, `older_agent_error` y la rama `UNIMPLEMENTED` de `translate_set_timeout_error` construyen `LifecycleUnsupportedException(feature, reason)` con la firma de `UnimplementedError`
- [x] 1.3 `_transport.py`: nuevo `unimplemented_rpc_error(exc, feature=GENERIC_RPC_FEATURE)`; la rama `UNIMPLEMENTED` de `translate_rpc_error` lo usa; `translate_rpc_error`/`_translated_unary`/`_code_call`/`_open_stream`/`_open_failure`/`_stream_failure` (sync y async) ganan un parámetro `feature`
- [x] 1.4 `_code_base.py`: nuevo `code_feature(call, language)`; `sandbox_sync/code.py` y `sandbox_async/code.py` lo pasan a `_open_stream` (`run_code`) y `_code_call` (`create_context`)
- [x] 1.5 `_metrics_base.py`: se borra `is_history_unimplemented`; `history_unimplemented_error` toma `UnimplementedError` como causa; las dos rutas de `get_metrics_history` (sync y async, instancia y clase) capturan `except UnimplementedError` en vez de `except SandboxException` + chequeo
- [x] 1.6 `e2b/_compat.py`: `unimplemented_language(error: UnimplementedError, feature, language) -> UnimplementedError` sin chequeo de `grpc_code` ni retorno `None`; `e2b/_sync.py` y `e2b/_async.py` (`run_code`, `create_code_context`) pasan a `except UnimplementedError as exc: raise unimplemented_language(exc, ...) from exc`
- [x] 1.7 Docstrings actualizados: `sandbox_sync/main.py` (`run_code`), `sandbox_sync/code.py` (`create_context`)
- [x] 1.8 Auditoría de `except SandboxException`/`except InvalidArgumentException` alrededor de RPCs afectados (`sandbox_sync/main.py:1276/1319` y gemelos async): migrados a `except UnimplementedError`
- [x] 1.9 Unit: `test_exceptions.py` (jerarquía + tabla UNIMPLEMENTED de `_transport.py`), `test_lifecycle_base.py`/`_sync.py`/`_async.py`, `test_code_sync.py`/`_async.py`, `test_metrics_base.py`, `test_metrics_history_sync.py`/`_async.py`, `test_e2b_compat_base.py`/`_sync.py`/`_async.py`
- [x] 1.10 e2e: `test_m7_poly_kernels.py`, `test_m9_deno_kernels.py` (expectativa nativa a `UnimplementedError`)

## 2. Avisos de ApiParams en llamadas de instancia

- [x] 2.1 `e2b/_connection.py`: `InstanceCall` (dataclass) + `instance_call(params, *, call, applies_request_timeout)` + `INSTANCE_UNAPPLIED_REASON`
- [x] 2.2 `e2b/_sync.py`: `instance_request_timeout` sustituye a `request_timeout_of`; `connect` (instancia) pasa `applies_request_timeout=True`, `kill`/`pause` pasan `False`
- [x] 2.3 `e2b/_async.py`: importa `instance_request_timeout` de `_sync.py` (como antes importaba `request_timeout_of`)
- [x] 2.4 Auditoría: ninguna otra llamada de instancia acepta `**ApiParams` en ninguno de los dos árboles
- [x] 2.5 Unit: tabla pura de `instance_call` (headers/proxy/retries/request_timeout, `applies` en `True`/`False`, ningún valor en los avisos); `test_e2b_compat_sync.py`/`_async.py` (`kill`/`pause`/`connect` avisan lo esperado; las variantes de clase no avisan)

## 3. Test de descarga S3 determinista

- [x] 3.1 `_s3.py`: `ObjectFetch.run` comprueba `_cancelled` bajo `_lock` antes de `open_chunks`
- [x] 3.2 `tests/unit/stalled_s3.py` (nuevo, compartido): `StalledS3` con `open_gate`/`read_gate`, contadores `opened`/`closed` y `fetch_finished`
- [x] 3.3 `test_transfer_sync.py`/`test_transfer_async.py`: los tres tests de `stalled_s3` usan el helper compartido; el de `request_timeout` retrasa `open_chunks` más allá del plazo (repro permanente) y afirma `opened == closed` + `s3.keys() == []`; el de `stream_idle_timeout` retrasa la lectura y afirma el mismo invariante
- [x] 3.4 `tests/unit/test_s3.py` (nuevo): tests puros de `ObjectFetch` con `threading.Event`, sin sleeps (cancelar antes de `run`, durante `open_chunks`, durante `next()`)
- [x] 3.5 200 repeticiones sin fallos (bucle de shell; `pytest-repeat` no está instalado)

## 4. Documentación

- [x] 4.1 `docs/site/docs/lifecycle.md`: `LifecycleUnsupportedException`/`LifecycleUnsupportedError` como subclases de `UnimplementedError`
- [x] 4.2 `docs/site/docs/images.md`: la nota de "sí (M9)" nombra `UnimplementedError` como el tipo único
- [x] 4.3 `docs/site/docs/kernels.md`: un kernel ausente es `UnimplementedError` en Python y TypeScript
- [x] 4.4 `docs/site/docs/e2b-compat.md`: fila nueva para los avisos de `ApiParams` en llamadas de instancia
- [x] 4.5 `clients/python/README.md:145`, `ARCHITECTURE.md:1451`
- [x] 4.6 `clients/python/CHANGELOG.md`: `[Unreleased]` con "Cambios que rompen" (item 1), "Changed" (item 2) y "Fixed" (item 3)

## 5. OpenSpec y cierre

- [x] 5.1 Deltas MODIFIED: `sandbox-timeout` ("SDK error mapping for the deadline"), `e2b-compat` ("E2B features without an AWS primitive raise UnimplementedError"), `code-execution` ("Execute selects a per-language default context and creates it lazily")
- [x] 5.2 `openspec validate v040-python --strict`
- [x] 5.3 `cd clients/python && uv run pytest tests/unit -q` (verde)
- [x] 5.4 `cd clients/python && uv run pytest ../../scripts/tests -q` (verde)
- [x] 5.5 `cd clients/python && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests` (verde)
- [x] 5.6 `python3 scripts/check_pins.py`, `python3 scripts/check_hygiene.py` (verde)
- [ ] 5.7 CI verde en el PR (GitHub Actions; no se puede correr en local)

## 6. Hallazgos del revisor

- [x] 6.1 `cli/_console.py`: `translated_failures()` captura también `UnimplementedError` (salida 1, mismo mensaje); `mcp/_server.py`: `UnimplementedError` se suma a `SDK_MESSAGE_EXCEPTIONS` (`ToolError`). Unit: `test_app.py::test_unimplemented_feature_exits_1_with_its_message`, `test_mcp_server.py::test_unimplemented_feature_is_a_tool_error_without_resetting_the_lease`
- [x] 6.2 Auditoría de los `except SandboxException` de reconexión (gate retry) alrededor de RPCs: `sandbox_sync/commands.py` y `sandbox_async/commands.py` (`_resubscribe_through_the_gate`) amplían a `except (SandboxException, UnimplementedError)` porque envuelven la causa en `self._progress.fail(exc)` (si no, el fallo no queda registrado en el `CommandProgress` y una `resolve()` posterior vería "terminó sin EndEvent" en vez del error real); los gate-retry de `filesystem.py`/`code.py`/`transfer.py` (sync y async) sólo hacen `raise` desnudo tras un `retry_delay` que ya da `None` para cualquier no-gate-refusal, así que no cambian (documentado aquí, no en el código)
- [x] 6.3 Delta ADDED para `sandbox-observability` (`specs/sandbox-observability/spec.md`, nuevo): el historial pre-M9 encadena `UnimplementedError` genérico (no `SandboxException`) y éste, a su vez, el `grpc.RpcError` original; tests reforzados en `test_metrics_history_sync.py`/`_async.py` (`cause.__cause__.code() is grpc.StatusCode.UNIMPLEMENTED`) y en `test_e2b_compat_sync.py`/`_async.py` (`test_kernel_not_shipped_is_unimplemented_from_the_native_error`)
- [x] 6.4 Deltas MODIFIED que faltaban: `sandbox-timeout` "The SDK fails closed on agents older than M9" (jerarquía `UnimplementedError` + `feature` de `create()`/`connect()`) y `e2b-compat` "The E2B timeout surface maps to the server-enforced deadline" (la re-excepción de `LifecycleUnsupportedException` ya es `isinstance(_, UnimplementedError)`); delta ADDED nueva "Instance calls on rayito.e2b warn about ApiParams they cannot apply" (los avisos de `sbx.kill`/`pause`/`connect`, ausentes de las specs); `openspec validate v040-python --strict` en verde
- [x] 6.5 `e2b/_connection.py`: `instance_call` ya no marca `headers={}` (u otro mapping vacío) como "dado" — usa `settings.headers` validado en vez de `is_given` para ese parámetro. Unit: `test_e2b_v2_base.py::test_instance_call_empty_headers_is_not_given`, `test_e2b_compat_sync.py`/`_async.py::test_*_does_not_warn_for_empty_headers`
- [x] 6.6 `clients/python/CHANGELOG.md`: reescrita la entrada "Fixed" (M9.4) — el síntoma real era un `get_object` de más tras una cancelación ya vista, nunca una excepción distinta de `TimeoutException` para el llamante
- [x] 6.7 `_s3.py`: docstring de `ObjectFetch` corregido ("se cierra al menos una vez", no "exactamente una vez"); el chequeo posterior a `open_chunks` se factoriza en `_register_or_close`/`_is_cancelled` en vez de repetir el `with self._lock: if self._cancelled` inline
- [x] 6.8 `test_s3.py`: `assert not thread.is_alive()` tras cada `join(5)`; `test_transfer_sync.py`/`_async.py`: docstring de los dos tests de `request_timeout_bounds_a_stalled_s3_download` ya no afirma que ejercen el pre-check bajo cualquier interleaving (el `get_object` tardío puede encontrar `NoSuchKey` antes de que `opened` suba, dando `opened == closed == 0` trivial); remite al test puro `test_s3.py::test_cancel_before_run_never_calls_get_object` para esa garantía

## 7. Hallazgos del barrido cruzado entre los tres PRs de 0.4.0

- [x] 7.1 `_transport.py`: `unimplemented_rpc_error` gana `hint: str | None = UNIMPLEMENTED_IMAGE_HINT`; sólo la rama genérica de `translate_rpc_error` (feature por defecto) sigue llevando la pista de publicar una imagen actual. `_code_base.py`: nuevo `kernel_unimplemented_error(exc, call, language)` reconstruye el `UnimplementedError` de un kernel ausente con `hint=None` (rayd ya nombra `rayito-base-poly`) y la `RpcError` original en `__cause__`, sin la intermedia. Se retira el parámetro `feature` de `_translated_unary`/`_code_call`/`_open_stream`/`_open_failure`/`_stream_failure` (sync y async): `CodeClient.run_code`/`create_context` capturan `except UnimplementedError` de la ruta genérica y la renombran con `kernel_unimplemented_error` en vez de enhebrar `feature` por cinco firmas (paridad con `withKernelFeature` de TypeScript). Unit: `test_exceptions.py::test_unimplemented_rpc_error_hint_is_opt_out_for_a_named_feature`; `test_code_sync.py`/`_async.py::test_language_not_shipped_is_unimplemented` comprueban que la pista no aparece y que `__cause__` es la `RpcError`.
- [x] 7.2 `clients/python/README.md`: el kernel ausente en `rayito-base` ya no se documenta como `InvalidArgumentException`/`UNIMPLEMENTED`; pasa a `UnimplementedError`, como el resto del SDK.
- [x] 7.3 `docs/site/docs/e2b-compat.md`: fila nueva para el comportamiento de TypeScript en llamadas de instancia (`kill`/`pause`/`getInfo`/`isRunning`/`setTimeout`/`connect`/`getMetrics`/`updateNetwork`, la regla de `requestTimeoutMs` y que las variantes estáticas aplican todo); y la cadena de causas del historial de métricas corregida a `__cause__`/`__cause__.__cause__` (TS `cause`/`cause.cause`). `docs/site/docs/observability.md`: misma corrección de la cadena de causas.
- [x] 7.4 `clients/python/CHANGELOG.md`: la entrada de ruptura ahora dice que el `__cause__` de `MetricsHistoryUnavailable` cambia (ya no es la `RpcError` directamente) y explica la pista opcional de `unimplemented_rpc_error`.
- [x] 7.5 `openspec/changes/v040-python/specs/sandbox-timeout/spec.md`: el requisito "The SDK fails closed on agents older than M9" distingue el `feature` de Python (`create(max_lifetime=, on_timeout=)`, `connect(timeout=)`) del de TypeScript (`create({ maxLifetimeMs, onTimeout })`, `connect({ timeoutMs })`), en vez de nombrar sólo el de Python para ambos SDKs.
- [x] 7.6 `clients/python/tests/unit/fake_code.py`: los dos mensajes del agente falso pasan al español de `rayd` 0.4.0 (`"el lenguaje {language} no está instalado en esta imagen; usa rayito-base-poly"`, `"language debe ser python, bash, javascript o typescript"`); `test_e2b_compat_base.py` actualizado a juego.
- [x] 7.7 `openspec/changes/v040-python/.openspec.yaml` (nuevo, faltaba): mismos campos (`schema`, `created`, `goal`) que `v040-typescript` y que los cambios archivados.
- [x] 7.8 `uv run pytest tests/unit -q`, `ruff check`/`format --check`, `mypy --strict`, `pytest ../../scripts/tests -q`, `check_pins.py`, `check_hygiene.py` y `openspec validate v040-python --strict` en verde tras 7.1–7.7.

## 8. Hallazgos de la revisión final

- [x] 8.1 Auditoría 1.8/6.2 ampliada: `_reopen_after_deadline_pause` (`sandbox_sync/main.py` y gemelo async) captura también `UnimplementedError`. Un `SetTimeout` que responde `UNIMPLEMENTED` tras reanudar de la pausa del plazo vuelve a registrar el aviso y deja ver el `TimeoutException('sandbox_timeout')` original, como en 0.3.x (antes era `SandboxException` y ahora `LifecycleUnsupportedException` escapaba). Unit: `test_lifecycle_sync.py::test_an_unimplemented_reopen_leaves_the_original_sandbox_timeout` y su gemelo async
- [x] 8.2 `test_transfer_sync.py`/`_async.py::*stream_idle_timeout_bounds_a_stalled_s3_download`: el test ya no abre `read_gate` a mano; sólo el `close()` del SDK (la guardia de inactividad) desbloquea al lector, así `fetch_finished` y `opened == closed == 1` vuelven a probar que la guardia cierra el cuerpo. Test puro nuevo `test_s3.py::test_idle_guard_closes_a_body_blocked_in_next`. `request_timeout` de la descarga colgada vuelve a 1.0 s: el determinismo lo da `open_gate`, no un plazo corto que recorta la exportación previa
- [x] 8.3 `docs/site/docs/e2b-parity.md`: filas 6, 8, 28 y 103 dicen que desde 0.4.0 esas opciones avisan con `RayitoCompatWarning` en llamadas de instancia y no se aplican
