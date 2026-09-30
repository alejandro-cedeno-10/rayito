## Why

Una feature ausente en un sandbox concreto ("esta imagen/agente no lo
implementa") sale hoy por tres tipos sin relación en el SDK Python: (a)
`UnimplementedError` (transferencias, red, historial de métricas), (b)
`LifecycleUnsupportedException`, subclase de `InvalidArgumentException` (un
agente anterior a M9 que no impone el plazo del servidor), y (c)
`InvalidArgumentException` con `grpc_code` `UNIMPLEMENTED` (un kernel ausente
en `run_code`/`create_code_context` y cualquier otro RPC que el agente no
implemente, vía la tabla unaria genérica de `_transport.py`). El usuario
necesita tres `except` distintos para el mismo concepto, y el shim de E2B
tiene tres rutas de traducción (`except LifecycleUnsupportedException`,
`except InvalidArgumentException` + un chequeo de `grpc_code` en
`unimplemented_language`, e `isinstance` para `MetricsHistoryUnavailable`).
El docstring de `_metrics_base.py` ya afirma que el plazo del servidor es
`UnimplementedError`, cosa que hoy es falsa (item 1 la hace verdadera).

Por separado, las variantes de instancia de `rayito.e2b` que aceptan
`**ApiParams` (`sbx.kill()`, `sbx.pause()`, `sbx.connect()`) validan
`headers`, `proxy`, `retries` y a veces `request_timeout` y luego los
descartan en silencio, porque operan sobre el canal y el plano ya
construidos de este sandbox y no pueden reconstruirlos. Eso contradice el
contrato del módulo (`rayito.e2b._connection`), que promete un
`RayitoCompatWarning` por cada `ApiParam` no aplicado; las variantes de
clase (`Sandbox.kill(id, retries=, proxy=)`) sí los aplican.

Por último, `test_async_request_timeout_bounds_a_stalled_s3_download` (y su
gemelo sync) es un test flaky con causa raíz verificada durante el
planning: si el plazo vence antes de que el hilo de `ObjectFetch.run` vuelva
de `get_object`, `run_bounded` llama a `cancel()`, que sólo marca
`_cancelled` porque todavía no hay cuerpo abierto. El `finally:
delete_quietly` de `read_routed` borra el objeto de staging casi al
instante. Cuando el `get_object` tardío llega, S3 responde `NoSuchKey`,
nunca se abre un cuerpo y la aserción original (que el cuerpo se llegó a
abrir) falla — no porque haya una fuga, sino porque el test afirmaba algo
que depende del reloj. El SDK, además, sigue lanzando un `GET` a S3 tras
cancelar si el hilo aún no había empezado a leer.

## What Changes

1. **Un solo tipo para "esta feature no está disponible en este sandbox".**
   `UnimplementedError` (`rayito.exceptions.UnimplementedError`, subclase de
   `NotImplementedError`, nunca de `SandboxException`) pasa a ser el único
   tipo del concepto. `LifecycleUnsupportedException` deja de heredar de
   `InvalidArgumentException`/`SandboxException` y pasa a ser
   `LifecycleUnsupportedException(UnimplementedError)`: **ruptura
   deliberada y aprobada**. La rama genérica `UNIMPLEMENTED` de
   `translate_rpc_error` (`_transport.py`) devuelve ahora `UnimplementedError`
   (construido por el nuevo `unimplemented_rpc_error(exc, feature=...)`) en
   vez de `InvalidArgumentException`. `CodeService` (`run_code`,
   `create_code_context`) pasa un `feature` concreto (con el `language`
   pedido cuando lo hay) a esa misma tabla, sin copiarla. `_metrics_base.py`
   pierde `is_history_unimplemented` (ya no hace falta inspeccionar
   `grpc_code`: el historial captura `UnimplementedError` directamente) y su
   docstring pasa a ser cierto. El shim de E2B se simplifica en la misma
   medida: `unimplemented_language` ya no comprueba `grpc_code` ni devuelve
   `None`, porque el caller sólo la invoca tras un `except
   UnimplementedError`.
2. **Avisos de `ApiParams` en las llamadas de instancia.** Una función pura
   nueva, `instance_call` (`rayito.e2b._connection`), calcula qué `ApiParams`
   de una llamada de instancia se pierden (`headers`, `proxy`, `retries`
   siempre; `request_timeout` según la llamada) y produce el
   `RayitoCompatWarning` correspondiente, nombrando el parámetro nunca su
   valor. `sbx.kill()`, `sbx.pause()` y `sbx.connect()` la usan a través de
   un envoltorio fino (`instance_request_timeout`), que sustituye a
   `request_timeout_of`.
3. **Test de descarga S3 determinista.** `ObjectFetch.run` (`_s3.py`)
   comprueba la cancelación bajo `_lock` **antes** de `open_chunks`, así una
   cancelación ya vista nunca dispara un `get_object` de más; el invariante
   es "todo cuerpo abierto se cierra exactamente una vez, y tras cancelar no
   se abre ninguno". Los tests de descarga colgada pasan a afirmar ese
   invariante (`opened == closed`) en vez de "el cuerpo se llegó a abrir",
   que dependía del reloj, con un helper compartido
   (`tests/unit/stalled_s3.py`) entre los árboles sync y async; se añaden
   tests puros de `ObjectFetch` (nuevos, `tests/unit/test_s3.py`) que
   secuencian las tres franjas de la carrera con `threading.Event`, sin
   sleeps.

Documentación compartida por ambos SDK (Python y TypeScript): este cambio es
el único dueño de `docs/site/docs/{lifecycle,images,kernels,e2b-compat}.md`
y de los requisitos OpenSpec `sandbox-timeout` ("SDK error mapping for the
deadline") y `e2b-compat` (kernels ausentes), y documenta también los
nombres TypeScript (`UnimplementedError extends Error`,
`LifecycleUnsupportedError extends UnimplementedError`) para que ambos
árboles queden simétricos sin que el cambio de TypeScript archive un
requisito en conflicto.

## Non-goals

- `PersistenceException(code="unimplemented")` para `Checkpoint`/`Restore`
  contra un agente antiguo: tiene un `code` cerrado y documentado, y queda
  como está — cambiarlo sería otra ruptura, fuera de este cambio.
- Ningún cambio en `clients/typescript/**`, `crates/**`, `proto/**` ni
  `kernel-sidecar/**`. El equivalente TypeScript del item 1 es un cambio
  hermano (`v040-typescript`), coordinado para no archivar deltas en
  conflicto sobre los mismos requisitos compartidos.
- `rayd` no cambia: los tres items son de cliente.

## Impact

- Specs afectadas: `sandbox-timeout` ("SDK error mapping for the deadline"),
  `e2b-compat` ("E2B features without an AWS primitive raise
  UnimplementedError"), `code-execution` ("Execute selects a per-language
  default context and creates it lazily").
- Código afectado: `clients/python/src/rayito/exceptions.py`,
  `_transport.py`, `_lifecycle_base.py`, `_metrics_base.py`, `_s3.py`,
  `_code_base.py`, `sandbox_sync/code.py`, `sandbox_sync/main.py`,
  `sandbox_async/code.py`, `sandbox_async/main.py`, `e2b/_compat.py`,
  `e2b/_connection.py`, `e2b/_sync.py`, `e2b/_async.py`.
- Tests: `clients/python/tests/unit/**` (nuevos `test_s3.py` y
  `stalled_s3.py`), `clients/python/tests/e2e/test_m7_poly_kernels.py`,
  `test_m9_deno_kernels.py`.
- Docs: `clients/python/README.md`, `ARCHITECTURE.md`,
  `docs/site/docs/{lifecycle,images,kernels,e2b-compat}.md`.
- **Ruptura real** (item 1): `except InvalidArgumentException` o `except
  SandboxException` alrededor de un `run_code`/`create_code_context` con un
  kernel ausente, o de un `set_timeout`/`create()` con lifecycle contra un
  agente anterior a M9, deja de capturar el error. Ver "Migración".

## Migración

- `LifecycleUnsupportedException` y el kernel ausente en
  `run_code`/`create_code_context` (y cualquier RPC que el agente no
  implemente) ya no son `InvalidArgumentException`/`SandboxException`:
  captura `rayito.UnimplementedError` (o `LifecycleUnsupportedException`,
  que ahora es subclase suya). `MetricsHistoryUnavailable` no cambia — ya
  era `UnimplementedError`.
- `rayito.e2b` no cambia de superficie: seguía lanzando `UnimplementedError`
  en los tres casos (lifecycle, historial, kernels) con el mismo `feature`,
  `reason` y `doc`; sólo se simplifica cómo lo traduce internamente.
- `sbx.kill()`, `sbx.pause()` y `sbx.connect()` (`rayito.e2b`) ahora avisan
  con `RayitoCompatWarning` si les pasas `headers=`, `proxy=` o `retries=`
  (y `sbx.kill()`/`sbx.pause()` también con `request_timeout=`), en vez de
  perderlos en silencio; no hay cambio de comportamiento, sólo de
  visibilidad. Usa `Sandbox.<kill|pause|connect>(sandbox_id, ...)` si
  necesitas que esos parámetros se apliquen de verdad.
- El fix de `_s3.py` no tiene migración: sólo evita un `get_object` inútil
  tras un timeout ya decidido; tipos y mensajes no cambian.
