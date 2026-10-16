# Paridad Python / TypeScript / shim E2B

Copia la checklist en el PR o en tus notas y márcala.

```
Paridad:
- [ ] Python sync y async: mismos nombres, orden y valores por defecto de
      parámetros (segundos). Test de firmas con `inspect.signature`.
- [ ] TypeScript: el mismo método en camelCase, con milisegundos y opciones
      en un objeto. Mismo comportamiento y mismos códigos de error.
- [ ] Exportado en `rayito/__init__.py` (`__all__`) y en `src/index.ts`.
      Si E2B tiene el nombre, también en `rayito.e2b` y `rayito/e2b`.
      `python3 scripts/check_sdk_parity.py` (en `make lint` y en CI) lo
      comprueba; una diferencia intencional entra en su lista de permitidos,
      con el motivo, nunca para tapar un hueco. Añadir un valor exportado
      obliga además a tocar `tests/unit/public-exports.test.ts`.
- [ ] Shim E2B: nombres y semántica de E2B. Lo que AWS no puede dar lanza
      `UnimplementedError` con enlace a `e2b-compat.md`. Un kwarg de E2B que
      se ignora emite `RayitoCompatWarning` con el nombre, nunca el valor.
- [ ] Errores: la misma clase (`XException` ↔ `XError`), los mismos campos
      (`code`, `reason`, `aws_code` ↔ `awsCode`) y el mapeo por código.
- [ ] Constantes: el mismo valor con nombre equivalente (`*_SECONDS` ↔
      `*_MS`). Los límites compartidos, en `limits.json`.
- [ ] Lógica pura compartida: vectores JSON comunes que leen los tests de los
      dos SDKs (como `fixtures/charts` o `testdata/image-variants/`), nunca
      dos copias que puedan divergir.
- [ ] Coste: bloque "Coste y activación" en los dos docstrings, y un test
      en cada SDK de que sin la opción no hay llamadas.
- [ ] Docs: ejemplo con pestañas Python/TypeScript, la referencia Python
      (mkdocstrings) y `referencia/typescript.md`, y las tablas
      `e2b-compat.md`/`e2b-parity.md` si cambia la compatibilidad.
- [ ] CHANGELOG de los dos paquetes, en `## [Unreleased]`.
```

## Mapeo de errores

| Origen | Cómo se clasifica | Destino |
|---|---|---|
| Status gRPC unario | `grpc.StatusCode` / `Code` de Connect | `NotFoundException`, `InvalidArgumentException`, `AuthenticationException`… |
| Error dentro de un stream | `StreamError.code` (`not_found`, `permission_denied`, `unimplemented`…) | La misma tabla que los unarios |
| AWS (botocore/smithy) | Nombre del código (`aws_code`/`awsCode`) | Excepción propia; el `cause` pasa por `sanitize_aws_error` |
| 403 del proxy de AWS | `proxy_rejected=True` | El SDK vuelve a acuñar el token y reintenta una vez |
| Función ausente | Versión de imagen o configuración | `UnimplementedError(feature, reason, doc)` |

Un `except SandboxException` no debe tragarse una función ausente:
`UnimplementedError` hereda de `NotImplementedError`.

## Shim E2B

- El contrato está fijado a las versiones de E2B que cita `e2b-compat.md`.
  Si el corpus de E2B en `tests/e2e/e2b_corpus` corre igual cambiando solo
  el import, el shim es correcto.
- Las extensiones de Rayito dentro del shim (`index`, `bucket`, `region`…)
  son opcionales y van apagadas por defecto, como en el SDK nativo.

## Qué se prueba en cada sitio

La paridad no es "el mismo número de tests". Cada comportamiento se prueba
donde vive su código, una vez:

| Qué | Dónde se prueba | Por qué |
|---|---|---|
| Comportamiento del guest y de `rayd` (sidecar, kernels, `Health`, límites del agente, hooks, formato de las líneas de evento) | **una vez**, en Python (`tests/e2e`, `tests/local`) y en las pruebas de `rayd` | No depende del SDK que lo llame: repetirlo en TypeScript sólo duplica coste de AWS |
| CLI (`rayito ...`), MCP (`rayito.mcp`) y `sandbox proxy` | **los dos SDK**, con un golden compartido | ADR-027: TypeScript también entrega `rayito` y `rayito-mcp`. La superficie de la CLI es `testdata/cli/surface.json`, que pytest genera y vitest comparará (fase 1); `python3 scripts/check_sdk_parity.py --cli` lista lo que falta en TypeScript. Hasta que cierren las fases, lo pendiente se prueba sólo en Python |
| Los duplicados síncrono/asíncrono de Python (`Sandbox` y `AsyncSandbox`) | **sólo Python** | TypeScript es sólo asíncrono: un caso por comportamiento, no uno por forma |
| Todo lo demás: código propio del SDK (validación, planes, servicios, transporte, shim E2B, plantillas, pool, formas de clase) | **los dos SDK**, con unit tests y, si toca AWS, e2e | Es el único código que puede divergir |

Reglas que salen de ahí:

- Una función nueva de SDK lleva tests unitarios en Python **y** TypeScript.
  Si usa AWS, un e2e en cada uno; si lo que se prueba es el guest, basta uno.
- Un e2e de Python que prueba guest o `rayd` no necesita espejo en
  TypeScript; dilo en la cabecera del test ("guest: se prueba una vez").
- Una brecha real es un caso sobre código que sólo existe en TypeScript
  (sus servicios, sus adaptadores, su shim) sin e2e: se cubre.
- Contar casos entre los dos SDK no es una métrica de paridad: compara los
  exports (`check_sdk_parity.py`), los vectores compartidos y esta tabla.
