# CLAUDE.md — instrucciones para Claude Code

## Antes de escribir cualquier código

Leer, en este orden: `SPEC.md`, `ARCHITECTURE.md`, `AWS_API_NOTES.md`,
`MILESTONES.md`. Trabajar sólo en el hito activo.

## Reglas duras

1. **No inventar parámetros de la API de AWS.** Si un nombre de parámetro, campo
   o forma de respuesta no aparece literalmente en `AWS_API_NOTES.md`, parar y
   pedirlo. No deducir por analogía con otras APIs de AWS ni por el nombre del
   comando. Esta es la principal fuente de trabajo desperdiciado en este repo.

2. **El `.proto` es la fuente de verdad.** No escribir structs de request o
   response a mano en Rust, Python ni TypeScript. Si la API necesita cambiar, se
   cambia el `.proto` y se regenera.

3. **Nada fuera del hito activo.** No añadir módulos, features ni abstracciones
   "para más adelante". Ver la lista de no-objetivos en `SPEC.md` §4.

4. **Un hito no se cierra con mocks.** El test de aceptación corre contra AWS real.

5. **ARM64.** Todo compila para `aarch64-unknown-linux-musl`. Si una dependencia
   no cruza, se busca otra, no se cambia de arquitectura.

## Comandos

```bash
make proto     # regenera clientes desde proto/ con buf
make build     # compila el agente para aarch64-musl
make image     # construye la imagen Docker ARM64
make test      # tests unitarios
make test-e2e  # tests de integración contra AWS (requiere credenciales)
make lint      # buf lint + clippy + ruff
```

## Convenciones

- Rust: `clippy` sin warnings. `thiserror` para errores de dominio, `anyhow` sólo
  en `main`. Nada de `unwrap()` fuera de tests.
- Python: type hints completos, `ruff`, sync y async con la misma superficie.
- Errores del agente dentro de streams: usar `StreamError` con códigos string
  (`not_found`, `permission_denied`, `unimplemented`). Errores unarios: status
  gRPC estándar.
- Nunca loguear el contenido de ficheros, código ejecutado ni tokens.

## Cuando algo no encaja

Si el diseño de `ARCHITECTURE.md` resulta inviable al implementarlo, **no
improvisar una alternativa en silencio**. Parar, explicar el conflicto, y proponer
un ADR nuevo. Las decisiones de arquitectura se cambian por escrito.
