# Auditoría de buenas prácticas de librería (SDKs Python y TypeScript, `rayd`)

Auditoría del 2026-10-03 sobre `main` en 4dc0ee0 (0.6.0 más #92 y #93):
compara el repositorio con las fuentes primarias de buenas prácticas para
librerías y SDKs open source en Python, TypeScript y Rust. Se ha leído código,
no solo documentación. Los arreglos de bajo riesgo están en
[#96](https://github.com/alejandro-cedeno-10/rayito/pull/96) (cambio OpenSpec
`best-practices-low-risk-fixes`) y las convenciones del proyecto quedan como
skill en `.claude/skills/rayito-engineering/`. Complementa a
[`2026-10-oss-standards-audit.md`](2026-10-oss-standards-audit.md) (#95:
licencias, cumplimiento de terceros, archivos de comunidad, insignia de
OpenSSF y política de versiones), que se hizo en paralelo sobre el mismo
commit. Los hallazgos que coinciden se señalan abajo y no se repiten.

**Resultado:** el repositorio está por encima de la media de los SDKs open
source en tipado, cadena de suministro, higiene de secretos y paridad entre
lenguajes. Hay **un hallazgo alto**: el mapa `exports` del paquete npm servía
tipos ESM a los consumidores CommonJS (arreglado en #96). Hay **diez medios**:
cuatro están arreglados en #96 y uno en #95; los demás son decisiones de
política para antes de 1.0. Lo que sobra está en lo especulativo: API pública creada
antes que sus funciones, y el peso de la documentación interna.

## Fuentes

Se leyeron en esta auditoría las cinco primeras, el aviso GHSA-8988-4f7v-96qf
de osv.dev y la API pública de OpenSSF Scorecard (2026-10-03). Las demás son
la referencia canónica de cada tema.

| Tema | Fuente primaria |
|---|---|
| Tipado de librerías Python (`py.typed`, `__all__`, completitud, tipos de parámetro amplios) | [Typing guide: libraries](https://typing.python.org/en/latest/guides/libraries.html), PEP 561 |
| `exports`, `types` por condición, formato de las declaraciones | [TypeScript Handbook: Modules reference](https://www.typescriptlang.org/docs/handbook/modules/reference.html), [arethetypeswrong](https://arethetypeswrong.github.io) |
| Logging en una librería (`NullHandler`, logger propio, no al root) | [Logging HOWTO: Configuring Logging for a Library](https://docs.python.org/3/howto/logging.html#configuring-logging-for-a-library) |
| Política de compatibilidad y obsolescencia | [PEP 387](https://peps.python.org/pep-0387/), PEP 702 (`warnings.deprecated`), [SemVer 2.0.0](https://semver.org/) §4 (0.y.z), [Cargo SemVer](https://doc.rust-lang.org/cargo/reference/semver.html) |
| Formato de skills de agente | [Skill authoring best practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices) |
| Metadatos y empaquetado Python | [Packaging guide: pyproject.toml](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/), PEP 621, PEP 639, PEP 735, PEP 740, [Trusted Publishing](https://docs.pypi.org/trusted-publishers/) |
| Paquetes Node, paquete dual ESM/CJS | [Node.js packages](https://nodejs.org/api/packages.html), [calendario de Node.js](https://github.com/nodejs/release) (Node 20 sin soporte desde 2026-04-30) |
| npm | [provenance](https://docs.npmjs.com/generating-provenance-statements), [trusted publishers](https://docs.npmjs.com/trusted-publishers) |
| Rust | [Rust API Guidelines checklist](https://rust-lang.github.io/api-guidelines/checklist.html) |
| Cadena de suministro | [OpenSSF Scorecard checks](https://github.com/ossf/scorecard/blob/main/docs/checks.md), [OpenSSF Concise Guide](https://best.openssf.org/Concise-Guide-for-Developing-More-Secure-Software), [SLSA v1.0](https://slsa.dev/spec/v1.0/levels), Sigstore, CycloneDX |
| Logs y secretos | [OWASP Logging Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html) |
| Pruebas | [The Practical Test Pyramid](https://martinfowler.com/articles/practical-test-pyramid.html) |
| Documentación | [Diátaxis](https://diataxis.fr/), [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/) |
| Arquitectura | [Hexagonal architecture](https://alistair.cockburn.us/hexagonal-architecture/) (Cockburn) |

## Dónde cumplimos

| Área | Evidencia |
|---|---|
| Tipado Python | `py.typed` en el wheel (lo comprueba `check_wheel.py`), `mypy --strict` sobre `src` y `tests`, módulos privados con `_` y `__all__` explícito. En los módulos públicos revisados (`sandbox_sync`, `sandbox_async`, plantillas, secretos e índice), solo 3 parámetros usan `list`/`dict` en vez de `Sequence`/`Mapping` (`_templates/_dsl.py:135,148,175`) |
| Tipado TypeScript | `strict`, `exactOptionalPropertyTypes`, `noUncheckedIndexedAccess`, `verbatimModuleSyntax` y `noExplicitAny` como error en Biome. Los tipos del `.proto` no se re-exportan |
| Paridad sync/async (Python) | Comparadas todas las parejas `X`/`AsyncX` exportadas: 20 parejas con los mismos métodos públicos y los mismos parámetros. La única diferencia es interna (`ListingIo.take`) |
| Paridad Python/TypeScript | Mismas clases con la convención de cada lenguaje (`XException` ↔ `XError`, segundos ↔ ms), vectores compartidos (`fixtures/charts`, templates) y el corpus de E2B en los dos e2e |
| Jerarquía de errores | La de E2B, con `code` cerrados documentados, mapeo por código y nunca por texto, y `cause` saneado (`_aws_sanitize.py`, `aws/sanitize.ts`) |
| Dependencias mínimas | Python: tres dependencias de runtime (`grpcio`, `protobuf`, `boto3`) y extras `cli`/`mcp`/`otel`. TypeScript: clientes AWS opcionales como `peerDependencies` opcionales que se cargan en el primer uso |
| Empaquetado Python | PEP 621/639/735, `uv_build`, wheel puro con `twine check`, Trusted Publishing con attestations PEP 740 y build sin credenciales separado de la publicación |
| Empaquetado npm | ESM + CJS, `files` acotado, trusted publishing con provenance, tarball verificado por sha256 entre build y publish |
| Cadena de suministro | Acciones fijadas por SHA (gate `check_pins.py`), `permissions: contents: read`, harden-runner, `cargo-deny`, `pip-audit`, `pnpm audit`, CodeQL, Dependabot agrupado, `cargo auditable`, SBOM CycloneDX, cosign keyless para `rayd`. Scorecard: 7/10, con 10/10 en Pinned-Dependencies, Token-Permissions, Dangerous-Workflow, SAST y Packaging |
| Secretos y logs | `check_hygiene.py` en CI, redacción en `repr`/`inspect` (`REDACTED`, `hidden.ts`), logger TypeScript inyectado y callado por defecto, "Higiene de logging" de `SECURITY.md` probada en unit y en e2e (SEC-10) |
| Hexagonal | `rayd-core` sin `tonic`/`axum`/`tokio` (impuesto por su `Cargo.toml`), puertos como `Protocol`/`interface` y slices por función (`_domain`, `_section`, `_service`, adaptador) |
| Valores con nombre | El barrido de literales numéricos sin nombre en `clients/python/src` encontró 46, casi todos idiomáticos. Los que no lo eran están arreglados en #96 |
| Pruebas | Pirámide sana: 3000 unit en Python, 1449 en TypeScript y 1219 en Rust, con servidores gRPC reales en loopback sobre servicers falsos generados del `.proto`, e2e en AWS real y el corpus de E2B como contrato de compatibilidad |
| Docs | Diátaxis (primeros pasos, guías, referencia, conceptos), ejemplos Python/TypeScript compilados en CI y `mkdocs --strict` |
| Versionado | SemVer con versiones enlazadas, release-please, Keep a Changelog y tag = versión del manifiesto comprobado en `release.yml` |

## Hallazgos

Por severidad. «#96» = arreglado en ese PR; «pendiente» = requiere una
decisión o un cambio más grande. Referencias a `main` en 4dc0ee0.

| ID | Sev. | Dónde | Hallazgo | Estado |
|---|---|---|---|---|
| BP-01 | Alta | `clients/typescript/package.json:19-37` | `exports` con `types` hermano de `import`/`require`. TypeScript lo elige antes que ambos y un consumidor CommonJS con `node16`/`nodenext` recibía `.d.mts` para un `.cjs`: arethetypeswrong lo marca como "👺 Masquerading as ESM" en las dos entradas | #96 |
| BP-02 | Media | `clients/typescript/package.json:105` | La dev dependency `@opentelemetry/sdk-trace-base` ^1.27 arrastra `@opentelemetry/core` 1.30.1, afectado por GHSA-8988-4f7v-96qf: es la única vulnerabilidad de Scorecard. `pnpm audit --prod` no la ve porque es de dev | #96 |
| BP-03 | Media | `scripts/check_hygiene.py:162-172` | La regla de rutas locales cubría Windows y Git Bash, pero no el home de macOS ni sus temporales, que es justo la plataforma del mantenedor | #96 |
| BP-04 | Media | `.github/workflows/ci.yml` (job `check`), `clients/python/pyproject.toml:20-22` | Los classifiers declaran 3.11–3.13, pero CI solo probaba la 3.12 del runner. En local la suite pasa entera en 3.11, 3.13 y 3.14. Coincide con el punto 5 de §8 de la auditoría de #95 | #96 (falta declarar 3.14) |
| BP-05 | Media | `clients/typescript/package.json:12-14,106`, `ci.yml:117,201`, `src/index.ts:11`, `src/e2b/index.ts:18` | `engines` `>=20` cuando Node 20 ya no tiene soporte (2026-04-30), y CI solo prueba Node 22. El polyfill de `Symbol.asyncDispose` para Node 20.0–20.3 no funciona: en ESM los imports se evalúan antes que el cuerpo, así que las clases de `dist/dsl-*.mjs` se definen antes que el polyfill (verificado en el bundle), y además choca con `"sideEffects": false` | Pendiente (subir el mínimo a 22 en una minor y quitar el polyfill) |
| BP-06 | Media | `docs/site/docs/limits.md` ("Versionado y soporte") | No había una política escrita de estabilidad y obsolescencia. #95 la añadió: API pública, qué puede romper una minor en 0.x, obsolescencia de al menos una minor y soporte de la última línea. Falta la ventana de soporte de las versiones de Python y Node | #95 (falta la ventana de Python/Node) |
| BP-07 | Media | — | La paridad Python/TypeScript solo la garantiza la revisión. Ningún test compara `__all__` con las exportaciones de `src/index.ts` (con una tabla de excepciones explícita) | Pendiente |
| BP-08 | Media | `clients/python/src/rayito/exceptions.py:306-322,369,379`, `clients/typescript/src/errors.ts:337-343,410,420`, `sandbox_sync/main.py:562,567`, `_feature_options.py:181,204` | API pública especulativa. `Volume*`, `CustomDomain*` y `Gateway*` son públicas y nada las lanza. `create(volumes=, domain=)` tipados `Any` lanzan `UnimplementedError` con el texto "llega en 0.6", que en 0.6.0 ya es falso. Contradice la regla 3 de `CLAUDE.md` | Pendiente (lo resuelven las ramas de EFS y dominio, o se retira antes de 1.0) |
| BP-09 | Media | `.github/workflows/release.yml` (job `rayd`) | Los assets de `rayd` van firmados con cosign y traen SBOM, pero no tienen provenance SLSA: Scorecard da 8 en Signed-Releases por eso. PyPI (PEP 740) y npm sí la tienen. Coincide con el punto 4 de §8 de la auditoría de #95 | Pendiente (`actions/attest-build-provenance` en el job `rayd`) |
| BP-10 | Media | `crates/rayd-core/src/{filesystem/path.rs,secret_gateway/route.rs}`, `crates/rayd/src/adapters/tar_archiver.rs` | Sin fuzzing ni pruebas por propiedades en los parsers que deciden la seguridad (normalizar rutas, allowlist de la pasarela, tar), que solo tienen ejemplos. Scorecard da 0 en Fuzzing. Coincide con el punto 8 de §8 de la auditoría de #95 | Pendiente (`cargo-fuzz` o `proptest` en `rayd-core`) |
| BP-11 | Media | `clients/typescript/src/index/dynamodb.ts:13-49,190` | El bloque "Coste y activación" de `DynamoDbIndex` estaba en el comentario de módulo, que tsdown descarta, así que no salía en el hover. La clase tampoco estaba en el registro de `check-dts-cost-blocks` | #96 |
| BP-12 | Baja | `clients/python/src/rayito/__init__.py` | No hay `NullHandler` en `rayito`. Los avisos de seguridad (`IMDS_OPEN_WARNING`, `SHARED_ACCESS_TOKEN_WARNING`, `_sandbox_base.py:158`) llegan al usuario por el `lastResort` de `logging`. Decidir: `NullHandler` + `warnings.warn` para lo accionable, como recomienda el HOWTO | Pendiente |
| BP-13 | Baja | `clients/python/src/rayito/sandbox_{sync,async}/*.py`, `src/sandbox/*.ts` | Los nombres de span y atributo de OpenTelemetry son literales repetidos (`"rayito.sandbox.id"` aparece 19 veces en Python y 10 en TypeScript; `"rayito.files.operation"`, 19 y 10) en vez de constantes junto a la allowlist de `_otel.py`/`otel.ts`. Lo mismo con los nombres de logger (`"rayito.files"` 3 veces) | Pendiente |
| BP-14 | Baja | `clients/python/src/rayito/_aws.py:227-228`, `cli/template.py:48,54,113`, `cli/_checks.py:531` | Valores mágicos: timeouts de botocore sin nombre (TypeScript sí los nombra) y defaults de la CLI que repetían las constantes del SDK | #96 |
| BP-15 | Baja | `_aws_sanitize.py:20`/`_models.py:52`, `aws/sanitize.ts:15`/`pool/core.ts:15` | `REDACTED` definido dos veces en cada SDK | #96 |
| BP-16 | Baja | `crates/rayd-core/src/lib.rs` | El dominio no tenía `unsafe`, pero nada lo impedía | #96 (`forbid(unsafe_code)`) |
| BP-17 | Baja | `docs/site/docs/referencia/typescript.md` | La referencia de TypeScript es una página escrita a mano, mientras la de Python se genera (mkdocstrings): no hay paridad en la referencia | Pendiente (TypeDoc o un generador desde `.d.mts`) |
| BP-18 | Baja | `cli/template.py:48,54` frente a `cli/image.py` | Flags inconsistentes: `--memory-mb`/`--timeout` en `template build` y `--memory-mib`/`--timeout-seconds` en `image publish` | Pendiente (alias y obsolescencia) |
| BP-19 | Baja | `clients/python/tests/unit/test_m15_*.py`, `clients/typescript/tests/unit/m15-*.test.ts` | Tests nombrados por hito en vez de por función. El hito no dice qué cubre el test | Pendiente (solo para tests nuevos; renombrar no compensa) |
| BP-20 | Baja | `clients/python/src/rayito/__init__.py` | `import rayito` carga `boto3` y `grpc` (≈ 0,15–0,2 s). Es aceptable para un SDK de AWS; PEP 562 (`__getattr__` perezoso) solo merece la pena si la CLI necesita arrancar más rápido | Pendiente (opcional) |
| BP-21 | Baja | Ajustes del repositorio | La rama `main` no exige aprobaciones ni revisión de CODEOWNERS (Scorecard: Code-Review 0, Branch-Protection 4). Los patrones no-provider de secret scanning están apagados | Pendiente (decisión de gobernanza) |
| BP-22 | Baja | `clients/python/pyproject.toml:40`, `clients/typescript/package.json` | `Documentation` apuntaba a las fuentes Markdown y no al sitio publicado; npm no tenía `homepage` ni `bugs` | #95 (#96 traía lo mismo y se quedó con lo de `main`) |
| BP-23 | Baja | `docs/RELEASING.md:3-8` | Decía que nada estaba publicado (desde 0.3.0 sí lo está) | #96 |
| BP-24 | Baja | `clients/typescript/src/e2b/types.ts:126`, `src/e2b/client.ts:28` | Las opciones `index` del shim remiten al bloque de coste de `DynamoDbIndex` en vez de llevar el suyo | Pendiente |
| BP-25 | Baja | `scripts/` | Los scripts de la raíz pasan `ruff check`, pero no `ruff format`: 7 ficheros se reformatearían y no hay configuración propia | Pendiente |
| BP-26 | Info | `clients/python/src/rayito/exceptions.py:17,177,241,247` | No hay una raíz única de errores de la librería: `AuthenticationException`, `QuotaExceededException` y `CapacityException` heredan de `Exception` por fidelidad a E2B. Se puede añadir sin romper nada una base mixin `RayitoError` en los dos SDKs | Pendiente (decisión) |
| BP-27 | Info | `clients/typescript/package.json` | Paquete dual ESM/CJS: el riesgo de doble instancia (`instanceof` entre copias) está mitigado con `setPrototypeOf`. Con `require(esm)` sin flag desde Node 22.12 se puede publicar solo ESM a partir de 1.0 | Pendiente (para 1.0) |

### Verificación de BP-01

`@arethetypeswrong/cli` 0.18.5 con `--pack` antes y después de #96:

| Resolución | Antes (`rayito`, `rayito/e2b`) | Después |
|---|---|---|
| `node16` desde CJS | 👺 Masquerading as ESM, 👺 | 🟢 (CJS), 🟢 (CJS) |
| `node16` desde ESM | 🟢, 🟢 | 🟢, 🟢 |
| `bundler` | 🟢, 🟢 | 🟢, 🟢 |
| `node10` | 🟢, 💀 | 🟢, 💀 (legacy, sin `exports`; ya pasaba antes) |

Una instalación del tarball carga las dos entradas con `require` y con
`import`, y `NotEnoughSpaceError === DiskFullError` se sigue cumpliendo.

## Lo que está sobredimensionado

- **API pública antes que su función** (BP-08): excepciones, parámetros
  `Any` y componentes `OptionalStack` con `supported=False`
  (`_stacks/components/custom_domain.py`, `efs_volumes.py`) que se
  publicaron en 0.6.0 para que las ramas paralelas no chocaran. Ahorra
  conflictos de merge, pero deja en el contrato público superficie que no
  hace nada. Retirar una excepción pública es un cambio incompatible, así
  que lo mejor es no volver a hacerlo: el seam se crea en la rama que lo usa.
- **Registros "drop-in" pensados para el desarrollo en paralelo**
  (`cost-declarations/*.json`, el glob del proto, el catálogo de
  componentes): cada uno se justifica por separado, pero juntos añaden
  indirección. Que no se sumen más.
- **Volumen de la documentación interna**: `ARCHITECTURE.md` (≈ 165 KB),
  `AWS_API_NOTES.md` (≈ 242 KB), `MILESTONES.md` (≈ 110 KB) y `SECURITY.md`
  (≈ 54 KB). Es valioso como registro, pero cuesta como lectura obligatoria
  antes de cada cambio (`CLAUDE.md`). Un índice por tema o ADRs en ficheros
  sueltos bajarían ese coste sin perder historia.
- **No sobra**: la separación entre `_*_base.py` y las cáscaras
  sync/async, los gates de higiene, pines y licencia, el split build/publish
  de `release.yml` y la redacción de secretos tienen todos un riesgo concreto
  detrás. La duplicación sync/async a mano (`sandbox_sync/main.py` 3050
  líneas, `sandbox_async/main.py` 2590) es el coste más alto que queda, pero
  los tests de paridad la contienen. Generar una de las dos (estilo
  `unasync`) solo compensaría si la paridad empezara a fallar.

## Skills externas evaluadas

Criterio: se lee el `SKILL.md` completo; sin scripts ni órdenes que ejecuten
código remoto ni llamadas de red; fuente reputada; y que encaje con las reglas
del repo. **No se instaló nada** (ni global ni en el proyecto).

| Skill | Fuente | Veredicto | Motivo |
|---|---|---|---|
| `skill-creator` | `anthropics/skills` | Aceptada como guía, no instalada | Fuente oficial. Se usó solo su guía de autoría, junto con la página "Skill authoring best practices", para escribir `rayito-engineering`. Trae scripts de evaluación que aquí no hacen falta |
| `codebase-design` | `mattpocock/skills` (MIT) | Aceptable, no instalada | Solo texto, autor reputado y muy usada. Vocabulario útil (módulo profundo, seam, "un adaptador = seam hipotético"); su idea central está incorporada en la skill del proyecto |
| `domain-modeling` | `mattpocock/skills` (MIT) | Aceptable, no instalada | Solo texto. Propone `GLOSSARY.md` y ADRs en `docs/adr/`; aquí los ADRs viven en `ARCHITECTURE.md` y adoptarla duplicaría el sitio de las decisiones |
| `tdd` | `mattpocock/skills` (MIT) | Rechazada para este repo | Solo texto, pero exige confirmar con el usuario cada seam antes de cada test, lo que choca con el flujo OpenSpec (decisiones cerradas en `design.md`) y con el trabajo autónomo |
| `hexagonal-architecture` | `kayaman/skills` (CC0) | Rechazada | Contenido correcto y solo texto, pero viene de un repositorio personal sin trayectoria (1 estrella). Se cita en su lugar la fuente primaria (Cockburn) |
| `architecture-patterns` | `wshobson/agents` (MIT) | Rechazada | Orientada a microservicios con repositorios y casos de uso, no a un SDK cliente. Remite a ficheros de referencia que no se leyeron |
| `python-packaging` | `wshobson/agents` (MIT) | Rechazada | Genérica y desfasada respecto al repo: `requires-python >=3.8`, `optional-dependencies` para dev en vez de PEP 735, setuptools; no habla de PEP 639 ni de Trusted Publishing |
| `rust-skills` | `leonardomso/rust-skills` (MIT) | Rechazada | El repo trae scripts (`checks/*.sh`, `*.py`) y varias reglas chocan con los lints del workspace (`expect()` permitido) y con la dependencia mínima (`SmallVec`, `ThinVec`) |
| `ddd-architecture-hexagonal` | `full-stack-skills/ddd-skills` | Rechazada | Centrada en Java/Spring, con poca adopción, y su `SKILL.md` no se leyó entero |
| `superpowers` | `obra/superpowers` (MIT) | Rechazada | Instala hooks de sesión que ejecutan órdenes y skills con scripts (`start-server.sh`) |
| `find-skills` y el instalador `npx skills add` | `vercel-labs/skills` | Rechazados | Ejecutan código descargado de npm o de repositorios de terceros al instalar |

## Arreglos aplicados (#96)

BP-01, BP-02, BP-03, BP-04 (sin declarar 3.14), BP-11, BP-14, BP-15, BP-16
y BP-23, con el cambio OpenSpec `best-practices-low-risk-fixes`
(deltas en `typescript-sdk`, `ci-hardening` y `python-release`). Ninguno
cambia el comportamiento en tiempo de ejecución de Python ni de `rayd`. En
TypeScript solo cambia qué declaraciones recibe un consumidor CommonJS. Se
publican en la próxima release (0.6.1).

## Pendientes priorizados

1. **BP-05, Node ≥ 22**: subir `engines` a `>=22`, `@types/node` a 22, CI en
   22 y 24, quitar el polyfill y anotarlo en el CHANGELOG como cambio de
   plataforma (minor en 0.x). Esfuerzo bajo.
2. **BP-09, provenance de `rayd`**: un paso
   `actions/attest-build-provenance` sobre `rayd` y `rayito-image.zip` en
   el job `rayd` (`id-token: write` ya está), con la verificación en
   `verify.md`. Esfuerzo bajo; hay que ensayarlo con `dry_run`.
3. **BP-07, gate de paridad entre lenguajes**: un script en `scripts/`, sin
   dependencias, que lea `__all__` y las exportaciones de `src/index.ts`,
   normalice (`Exception` → `Error`, snake → camel) y falle ante una
   diferencia que no esté en una tabla de excepciones versionada. Esfuerzo
   medio.
4. **BP-06, completar la política**: añadir a "Versionado y soporte"
   (`limits.md`) la ventana de soporte de Python y Node (por ejemplo, las
   versiones con soporte upstream en la fecha de cada minor) y qué hace el
   SDK cuando una versión sale de esa ventana. Esfuerzo bajo, pero es una
   decisión del mantenedor.
5. **BP-08, API especulativa**: que las ramas de EFS y dominio sustituyan
   los stubs. Lo que no llegue antes de 1.0 se retira. Mientras tanto,
   cambiar "llega en 0.6" por un texto sin versión cuando se toquen esas
   ramas, para no chocar con ellas.
6. **BP-10, fuzzing**: objetivos `cargo-fuzz` (o `proptest`) para
   `filesystem::path`, `secret_gateway::route` y el lector de tar, en un
   job nocturno. Esfuerzo medio.
7. **BP-12 y BP-13, logging y telemetría**: decidir `NullHandler` +
   `warnings.warn` y pasar los nombres de span y logger a constantes.
   Esfuerzo bajo-medio.
8. **BP-04, completar**: declarar 3.14 (la suite pasa entera) y añadirla al
   job `python-versions`.
9. **BP-17, BP-18, BP-24, BP-25 y BP-26**: cuando se toque cada zona.
