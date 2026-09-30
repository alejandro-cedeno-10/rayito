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
