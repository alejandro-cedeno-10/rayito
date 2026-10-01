# docs-delta: m15-templates

Applied only by `m15-docs-integration`, in one PR together with every other
feature's delta. Each block below names the file and gives the exact
replacement text for the row(s)/line(s) templates owns. Nothing here is
applied by this change itself.

## `docs/site/docs/e2b-parity.md`

### Row 56

Replace:

```
| 56 | cliente ligado `E2B(api_key, domain, ...)` con `.Sandbox` / `.AsyncSandbox` / `.Template` / `.Volume` / `.Secret` | implementado (0.3.0) | liga `region`/`session`/`control_plane`; desde 0.5.0 `.Secret`/`.AsyncSecret` funcionan (Secrets Manager con esa `region`/`session`; aceptado en AWS real); `.Template` y `.Volume` lanzan `UnimplementedError` | [Compatibilidad](e2b-compat.md) |
```

With:

```
| 56 | cliente ligado `E2B(api_key, domain, ...)` con `.Sandbox` / `.AsyncSandbox` / `.Template` / `.Volume` / `.Secret` | divergente (0.3.0) | liga `region`/`session`/`control_plane`; desde 0.5.0 `.Secret`/`.AsyncSecret` funcionan (Secrets Manager con esa `region`/`session`; aceptado en AWS real); desde 0.6.0 `.Template` construye de verdad ([Templates](funciones-opcionales/templates.md)); `.Volume` lanza `UnimplementedError` (pendiente de `m15-efs-volumes`) | [Compatibilidad](e2b-compat.md) |
```

### Row 81

Replace:

```
| 81 | API de build de templates (`TemplateBase`, `TemplateBuilder`, `Template.build`, ...) | fuera por SPEC | `SPEC.md` §4 excluye los templates declarativos; el análogo es el Dockerfile más `rayito image publish` | [Compatibilidad](e2b-compat.md) |
```

With:

```
| 81 | API de build de templates (`TemplateBase`, `TemplateBuilder`, `Template.build`, ...) | divergente (0.6.0) | DSL compilado en el cliente a Dockerfile + zip determinista, compuesto sobre una imagen `rayito-base` ya publicada (sin caché de capas, sólo ARM64, sin streaming en vivo de los pasos: el log se relee al terminar el build); `from_image`/`from_template`/`from_dockerfile`/`from_gcp_registry` siguen sin equivalente | [Templates](funciones-opcionales/templates.md) |
```

### Row 89

Replace:

```
| 89 | `TemplateException` / `BuildException` | fuera por SPEC | las clases existen y nunca se lanzan (no hay API de templates) | [Compatibilidad](e2b-compat.md) |
```

With:

```
| 89 | `TemplateException` / `BuildException` | divergente (0.6.0) | ambas se lanzan de verdad desde `Template.build()`; `BuildException` lleva `reason`/`step`/`command`/`exit_code`/`log_tail` cuando aplica; a diferencia de E2B, `FileUploadException` de Rayito no hereda de `BuildException` (hereda de `TransferException`, por una importación/escritura grande, no por un build de template) | [Templates](funciones-opcionales/templates.md) |
```

### Row 112

Replace:

```
| 112 | CLI de E2B (`auth`, `sandbox list/create/connect/exec/kill/metrics`, `template`, `snapshots`, `fork`) | divergente (0.3.0) | añade `sandbox create/connect/exec/metrics` a los `list/kill/logs` que ya había ([CLI](cli.md)); `auth`, `template`, `snapshots` y `fork` siguen fuera | [CLI](cli.md) |
```

With:

```
| 112 | CLI de E2B (`auth`, `sandbox list/create/connect/exec/kill/metrics`, `template`, `snapshots`, `fork`) | divergente (0.3.0 / 0.6.0) | añade `sandbox create/connect/exec/metrics` a los `list/kill/logs` que ya había ([CLI](cli.md)); desde 0.6.0, `rayito template build/status/logs` ([Templates](funciones-opcionales/templates.md)); `auth`, `snapshots` y `fork` siguen fuera | [CLI](cli.md) |
```

## `docs/site/docs/optional-features.md`

Add one row to the "Funciones con coste AWS" table (after the OpenTelemetry
row, before the closing "Una fila pasa a..." sentence):

```
| [Templates declarativos](funciones-opcionales/templates.md) | nuevo en 0.6.0, pendiente de aceptación en AWS real | `Template.build(...)` | `Template.build(...)` | — (sin llamar a `build()`, ningún cliente) | Compila un `Template`/DSL a Dockerfile + zip determinista sobre una imagen `rayito-base` ya publicada y lo sube con `create`/`update-microvm-image`; `build_in_background`/`get_build_status`/`exists` para builds largos; el shim `Template`/`AsyncTemplate` de E2B ya funciona | `lambda:GetMicrovmImageVersion` (leer la imagen base y sondear el build), `CreateMicrovmImage`/`UpdateMicrovmImage`, `GetMicrovmImage`, `ListMicrovmImageVersions`/`ListMicrovmImageBuilds`/`GetMicrovmImageBuild`; `s3:GetObject` (zip base), `HeadObject`/`PutObject` (subir el artefacto, sólo si no existe ya por hash); `logs:DescribeLogStreams`/`GetLogEvents` sólo si el build no termina `SUCCESSFUL`+`ACTIVE` | cada versión de imagen nueva cuesta almacenamiento de snapshot, ≈ $0,04/semana por versión (mínimo una semana); el build en sí no se factura aparte (sin CodeBuild) | `RayitoTemplateBuilder` (`infra/templates.yaml`): `CreateMicrovmImage`/`UpdateMicrovmImage`/`GetMicrovmImage*`/`ListMicrovmImageVersions`, `iam:PassRole` sobre el rol de build, S3 del artefacto, lectura de logs de build | No llamar a `Template.build()`/`buildInBackground()`; borrar versiones de imagen con `rayito image` | `clients/python/src/rayito/_templates/_build.py` / `clients/typescript/src/templates/build.ts` |
```

Add one row to the "De un vistazo" table too (short form):

```
| [Templates declarativos](funciones-opcionales/templates.md) | apagada | `Template.build(...)`: compila un DSL a Dockerfile + zip y publica una imagen nueva sobre una `rayito-base` ya publicada | ≈ $0,04/semana por versión de imagen nueva; el build no se factura aparte | construir imágenes (`RayitoTemplateBuilder`) | no llamar a `Template.build()` |
```

## `docs/site/docs/cost.md`

Replace the "Reglas prácticas" bullet (exact text match on the parenthetical):

```
- Todo lo de esta página es lo que **crea o llama el propio `create()` /
  `connect()`**. Las funciones opcionales (secretos, índice de metadatos)
  tienen su propio coste, apagado por defecto y activado sólo con una opción
  explícita del SDK: [Funciones opcionales](optional-features.md).
```

With:

```
- Todo lo de esta página es lo que **crea o llama el propio `create()` /
  `connect()`**. Las funciones opcionales (secretos, índice de metadatos,
  templates declarativos) tienen su propio coste, apagado por defecto y
  activado sólo con una opción explícita del SDK: [Funciones
  opcionales](optional-features.md).
```

## `SECURITY.md`

Add a new row after T19, before the closing of the threat table:

```
| T26 | cadena de suministro del template | `Template.build()` compone un Dockerfile nuevo sobre el zip de una imagen `rayito-base` ya publicada y lo construye con el build role del operador (`infra/iam.yaml` `BuildRoleArn`): un `Template` malicioso (`runCmd`/`pipInstall` con un paquete comprometido, un `copy()` que trae un binario no auditado) o una imagen base manipulada producen una imagen de sandbox igual de comprometida, con las mismas credenciales de build que `rayito image publish` | El DSL nunca acepta credenciales (`set_envs` se hornea en la imagen y en todos sus snapshots: documentado como "nunca secretos"); `RayitoTemplateBuilder` (`infra/templates.yaml`) es una política separada de la de lanzar sandboxes, para que un agente que crea sandboxes no pueda también publicar imágenes; el artefacto se sube por sha256 (contenido-direccionado, `s3:PutObject` sólo bajo `rayito/templates/*`) y el build falla en el acto (`BuildException` con el paso, el comando y el código de salida, leídos del grupo de logs de BuildKit) en vez de producir una imagen a medias; `fromImage`/`fromTemplate`/`fromGcpRegistry` (fuentes externas sin la cadena de confianza de un `rayito-base` ya publicado) lanzan `UnimplementedError` en vez de aceptar un origen no verificado. Residual: Rayito no verifica la integridad del contenido que el `Template` copia (`copy()` trae lo que haya en el disco del llamante, igual que un `COPY` de Docker normal); eso es responsabilidad del pipeline de CI que invoca `Template.build()`, igual que con cualquier Dockerfile hoy | pendiente (m15-templates, sin aceptar en AWS real) |
```

## `docs/site/docs/security.md`

Add a new row to the short "Amenaza | Mitigación" table, after the T19 row:

```
| Cadena de suministro del template (T26) | `Template.build()` nunca acepta credenciales en el DSL (`set_envs` se hornea en la imagen); política de build (`RayitoTemplateBuilder`) separada de la de lanzar sandboxes; el artefacto se sube por sha256 y un build fallido nunca produce una imagen a medias; `fromImage`/`fromTemplate`/`fromGcpRegistry` (fuentes externas) siguen sin equivalente ([Templates](funciones-opcionales/templates.md), `SECURITY.md` T26) |
```

## `openspec/project.md`

No change: templates does not touch the non-goals list (that is D1,
drafted by `v06-foundations`, not this change's).
