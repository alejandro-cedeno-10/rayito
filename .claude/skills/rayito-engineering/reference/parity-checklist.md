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
- [ ] Shim E2B: nombres y semántica de E2B. Lo que AWS no puede dar lanza
      `UnimplementedError` con enlace a `e2b-compat.md`. Un kwarg de E2B que
      se ignora emite `RayitoCompatWarning` con el nombre, nunca el valor.
- [ ] Errores: la misma clase (`XException` ↔ `XError`), los mismos campos
      (`code`, `reason`, `aws_code` ↔ `awsCode`) y el mapeo por código.
- [ ] Constantes: el mismo valor con nombre equivalente (`*_SECONDS` ↔
      `*_MS`). Los límites compartidos, en `limits.json`.
- [ ] Lógica pura compartida: vectores JSON comunes en las fixtures de los
      dos SDKs (como `fixtures/charts`).
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
