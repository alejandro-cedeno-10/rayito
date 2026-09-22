# Gobernanza

Este documento es deliberadamente mínimo: describe cómo se toman las
decisiones en Rayito hoy, con un solo mantenedor, y se revisará cuando el
proyecto llegue a **tres mantenedores**. Hasta entonces no hay comité, ni
votaciones, ni `MAINTAINERS.md` aparte: la lista vive aquí.

## Mantenedores

| Persona | Desde | Ámbito |
|---|---|---|
| [@alejandro-cedeno-10](https://github.com/alejandro-cedeno-10) | 2026-09 | todo el repositorio |

El mantenedor fusiona los PRs, corta las releases (`docs/RELEASING.md`), es
el propietario de `.github/CODEOWNERS` y el contacto de `SECURITY.md` y
`CODE_OF_CONDUCT.md`.

## Cómo se decide

- **Las decisiones de arquitectura se toman por escrito, como ADR**, en
  `ARCHITECTURE.md`. Un ADR tiene Contexto, Decisión, Razón y Consecuencia;
  está numerado (`ADR-001`, `ADR-002`, ...); **nunca se reescribe**: si
  cambia de opinión, se añade un ADR nuevo que reemplaza al anterior y lo
  cita. Ejemplos: ADR-002 (sidecar Python), ADR-007 (sin `set_timeout`),
  ADR-008 (pool de suspendidos).
- **El trabajo acotado se propone como cambio OpenSpec**
  (`openspec/changes/<nombre>/`: `proposal.md`, `design.md` con todas las
  decisiones cerradas, `tasks.md`, especificaciones delta). Un cambio se
  implementa cuando `openspec validate --strict` pasa y se archiva cuando
  su aceptación es verde contra AWS real (o su conjunto de gates, si no toca
  runtime). El detalle del flujo está en `CONTRIBUTING.md` §2.
- **Las fuentes de verdad mandan**: `proto/rayito/v1/` para el contrato y
  `AWS_API_NOTES.md` para la API de AWS. Si un documento las contradice, se
  corrige el documento. Nada de lo que no esté en `AWS_API_NOTES.md` se
  asume de la plataforma.
- **Si el diseño no encaja con la realidad, se para**: se explica el
  conflicto y se propone un ADR nuevo; no se improvisa una alternativa en
  silencio.

## Cómo se llega a mantenedor

Por contribuciones sostenidas: al menos **tres cambios OpenSpec no triviales
aceptados** que toquen al menos dos de `crates/`, `clients/python`,
`clients/typescript` y `kernel-sidecar`, y el acuerdo de los mantenedores
existentes. La incorporación se registra en la tabla de arriba con un PR a
este fichero y se refleja en `.github/CODEOWNERS`.

Un mantenedor deja de serlo cuando lo pide o tras doce meses sin actividad,
con el mismo procedimiento.

## Cuándo se revisa este documento

Al alcanzar tres mantenedores: entonces se decidirá si hace falta un proceso
formal de decisión (mayorías, plazos de revisión, áreas de propiedad) y se
escribirá aquí. Mientras tanto, cualquier duda de gobernanza se plantea como
issue con la plantilla de propuesta.
