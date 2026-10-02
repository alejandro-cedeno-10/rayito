# docs-delta: sizes-catalog

Exact replacement content for the shared tables `m15-docs-integration`
owns. Features never edit these files directly (shared-file protocol,
M15 architecture §5); this file carries the rows this change needs, for
`m15-docs-integration` to apply in its own PR.

## `docs/site/docs/e2b-parity.md` — replace row 82

Current row 82 (`cpu_count` / `memory_mb` por sandbox) predates `size=` and
describes `rayito image publish --memory-mib`, which `--sizes` now
supersedes (both still work: `--memory-mib` sets the baseline's own
memory, `--sizes` publishes additional named images from the same
artifact). Replace it with:

```
| 82 | `cpu_count` / `memory_mb` por sandbox | divergente | por imagen, como E2B por build de template: `Sandbox.create(size="4gb")` resuelve un catálogo cerrado de 5 tamaños (512mb/1gb/2gb/4gb/8gb, Q87) y `rayito image publish --sizes` publica una imagen por tamaño desde el mismo artefacto; `Template.build(cpu_count=, memory_mb=)` sigue en `UnimplementedError`; `cpu_count`/`memory_mb` de `SandboxInfo` siguen siendo la vista real del guest (Q88), nunca el tamaño elegido — para eso está `SandboxInfo.baseline_memory_mib`/`baseline_cpu` | [Límites](limits.md#tamano-cpuram) |
```

## `docs/site/docs/limits.md` — replace the "Tamaño (CPU/RAM)" section

Replace the section starting at `## Tamaño (CPU/RAM)` (through the
paragraph ending "...Q68) es la fila de 2048...") with:

```markdown
## Tamaño (CPU/RAM) {#tamano-cpuram}

El tamaño (memoria y vCPU) **es propiedad de la imagen**
(`resources[0].minimumMemoryInMiB` en `create-microvm-image`,
`AWS_API_NOTES.md` §4 y §24), no un parámetro libre de `Sandbox.create()`.
Desde `m15-sizes-catalog`, Rayito resuelve esto con un **catálogo cerrado
de cinco tamaños** (`size="512mb"|"1gb"|"2gb"|"4gb"|"8gb"`, o
`SizeRequest(memory_mib=...)`/`{ memoryMib }` redondeado siempre hacia
arriba): `Sandbox.create(size="4gb")` publica/usa `rayito-base-4gb`. Sigue
sin existir un `resources=` arbitrario en `create()`: sólo estos cinco
valores aceptan `create-microvm-image` (Q87, medido: 256, 3072, 10240 y
16384 MiB dan `ValidationException` síncrona sin crear nada).

Tabla medida en cuenta real para los cinco tamaños (RES-2/Q88,
`docs/research/2026-10-e2b-out-of-scope.md`; confirma el punto suelto de
Q61/Q68 en 2048 MiB), con el coste de la hora en baseline derivado de los
precios de §12 (`AWS_API_NOTES.md` §12 / [Costes](cost.md)):

| `minimumMemoryInMiB` | vCPU del guest | `MemTotal` del guest | disco raíz | `/dev/shm` | $/h en baseline |
|---|---|---|---|---|---|
| 512 | 1 | ≈1 989 MiB | 8,3 GB | 64 MiB | $0.0315 |
| 1024 | 2 | ≈3 998 MiB | 8,3 GB | 64 MiB | $0.0631 |
| 2048 (baseline de `rayito image publish`) | 4 | ≈8 016 MiB | 8,3 GB | 64 MiB | $0.1261 |
| 4096 | 8 | ≈16 052 MiB | 16,7 GB | 64 MiB | $0.2522 |
| 8192 | 16 | ≈32 123 MiB | 33,6 GB | 64 MiB | $0.5044 |

El guest siempre ve más que `minimumMemoryInMiB` (memoria/512 vCPU, hasta
≈4x la memoria nominal): es el pico facturado, no la línea base.
`SandboxInfo.cpu_count`/`memory_mb` (`cpuCount`/`memoryMb`) reportan ese
pico real, leído de `Health`; `SandboxInfo.baseline_memory_mib`/
`baselineMemoryMib` y `baseline_cpu`/`baselineCpu` (sólo con `size=`)
reportan en cambio `minimumMemoryInMiB` y su vCPU de la tabla de arriba —
usa estos últimos para decidir qué tamaño elegir, no `memory_mb`. El
snapshot de memoria de una misma imagen mínima crece con el tamaño (446 MB
a 512, 503 a 1024, 597 a 2048, 782 a 4096, 1 146 a 8192 MiB): cada tamaño
adicional publicado cuesta más storage de snapshot. Ver [Tamaños](funciones-opcionales/tamanos.md).
```

(Keep everything in the file after that section — the `## Compatibilidad
SDK ↔ rayd ↔ imagen` heading and on — unchanged.)

## `docs/site/docs/optional-features.md` — add a row to "Funciones con coste AWS"

Add this row (after the "Índice de metadatos" row, before "Trazas
OpenTelemetry del SDK", keeping the table's existing row order otherwise
unchanged):

```
| [Catálogo de tamaños](funciones-opcionales/tamanos.md) | implementado en 0.6.0, pendiente de aceptación en AWS real | `size=` | `size` | `None` / `undefined` | Resuelve, en cliente y sin RPC, un catálogo cerrado de 5 tamaños (redondea hacia arriba) y antepone el sufijo a la imagen antes de lanzar; `rayito image publish --sizes` publica una imagen adicional por tamaño; `get_info()`/`getInfo()` confirma el tamaño real con `GetMicrovmImageVersion` (cacheada) | Ninguno nuevo para lanzar con `size=` (el tamaño ya está en el nombre de la imagen); `get_info()`/`getInfo()` hace como mucho una `GetMicrovmImageVersion` por versión de imagen y por proceso; `rayito image publish --sizes` hace los mismos `create`/`update-microvm-image` que una publicación normal, uno por tamaño | $0 por `size=` en sí; cada tamaño publicado añade una semana de storage de snapshot (crece con el tamaño, de ≈450 MB a ≈1,1 GB sobre la misma imagen mínima), como cualquier versión de imagen (ver [precios de Lambda](https://aws.amazon.com/lambda/pricing/), consultado 2026-09-30) | Ninguno adicional para `size=`; el guardarraíles opcional `sizes-guard` (`RayitoRunAllowedSizes`) limita `lambda:RunMicrovm` a los ARN de imagen listados | No pasar `size=` / `size` (o pasar `None`/`undefined`); borrar la pila `sizes-guard` si se desplegó (no borra ninguna imagen) | `clients/python/src/rayito/_sizing.py` / `clients/typescript/src/sizing/sizing.ts` |
```

(This row's first column links straight to the guide page, with no
fragment — same pattern as any other row here whose guide page has no
internal anchor.)

## `docs/site/docs/cost.md` — add a sizes-catalog entry

Add, in whatever per-feature cost breakdown section `cost.md` keeps for
optional features (next to metadata-index's), a short paragraph:

```markdown
### Catálogo de tamaños

`size=`/`size` en sí no cuesta nada aparte del cómputo normal de la
MicroVM a ese tamaño (idéntico a lanzar esa misma imagen sin `size=`).
Publicar tamaños adicionales con `--sizes` multiplica el storage de
snapshot semanal por el número de tamaños publicados (cada uno crece con
el tamaño, de ≈450 MB a ≈1,1 GB sobre la misma imagen mínima, RES-2/Q88).
El guardarraíles opcional `sizes-guard` es $0 (sólo IAM).
```

## `SECURITY.md` and `docs/site/docs/security.md` — add threat T27

Add to `SECURITY.md`'s threat table (same row in the site's
`security.md` page if it mirrors the table, or a short cross-reference if
it only links out):

```
| T27 | coste por tamaño | Una identidad con `lambda:RunMicrovm` sin acotar puede lanzar el tamaño más caro del catálogo (8192 MiB) en vez del baseline previsto, multiplicando el coste por hora hasta ≈4x (RES-13/Q90: midió que una política acotada por ARN de imagen sí bloquea tamaños mayores) | Opcional, apagado por defecto: `rayito stack deploy sizes-guard` crea `RayitoRunAllowedSizes`, una política IAM con un Deny de `lambda:RunMicrovm` fuera de los ARN de imagen explícitamente listados por el operador (el baseline y cada tamaño que de verdad se quiera permitir); el Deny es lo que hace la restricción efectiva aunque la identidad ya tenga el `microvm-image:*` de la `CallerPolicy` estándar (un Allow por sí solo no restringiría nada en ese caso, que es el típico). Sin desplegarla, el comportamiento es el mismo de antes de `m15-sizes-catalog`: cualquier imagen que la identidad ya pudiera lanzar sigue lanzable | M15 |
```

## `SPEC.md` §4 — draft replacement for the "Tamaño por sandbox" non-goal (D1)

Not applied by this change (D1 leaves the exact wording to the maintainer,
proposal.md); this is a draft in the same style as the "Metadatos por
sandbox" non-goal this same section already struck through and redirected,
for the maintainer to accept, edit or reject:

```markdown
- ~~**Tamaño por sandbox** (`cpu=`/`memory=`): el tamaño es propiedad de la imagen
  (`resources[0].minimumMemoryInMiB`) tanto en Rayito como, de hecho, en E2B.
  Un template = un tamaño, como en E2B (tamaño por build de template, por
  ejemplo `base-2gb`, `base-4gb`); ver [`limits.md`](docs/site/docs/limits.md).
  No hay, ni se promete, un resolvedor de tamaño por sandbox.~~: entregado en
  M15 (`m15-sizes-catalog`, ADR-019) con un catálogo **cerrado** de cinco
  tamaños nombrados (`512mb`/`1gb`/`2gb`/`4gb`/`8gb`) resuelto por completo
  en el SDK, sin RPC: `size="4gb"` sigue siendo "un template = un tamaño"
  (antepone el sufijo de la convención `<variant>[-<size>]` antes de
  lanzar), nunca un ajuste del guest en marcha ni un `resources=`/`cpu=`/
  `memory=` arbitrario en `create()` — eso sigue sin existir, porque
  `create-microvm-image` sólo acepta esos cinco valores (Q87). Ver
  [Tamaños](docs/site/docs/funciones-opcionales/tamanos.md).
```

## `docs/site/docs/limits.md` reference check

`e2b-parity.md`'s row 82 and `optional-features.md`'s new row both link
to `limits.md#tamano-cpuram`: the replacement section above keeps that
exact anchor (`{#tamano-cpuram}`) so neither link breaks.
