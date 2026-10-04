---
name: rayito-engineering
description: Convenciones de ingeniería de Rayito, el SDK Python/TypeScript compatible con E2B y su agente Rust rayd: capas hexagonales por lenguaje, paridad entre Python, TypeScript y el shim E2B, valores con nombre, funciones con coste apagadas por defecto y el convenio OptionalStack, mapeo de errores, qué no se escribe nunca en ficheros ni logs, gates locales (con la VM Linux de rayd), flujo OpenSpec, release y aceptación en AWS, y documentación en español (Diátaxis). Úsala antes de diseñar, implementar, revisar o publicar cualquier cambio en este repositorio.
---

# Ingeniería en Rayito

Reglas para cambiar código, docs o infra de este repositorio. Las duras están
en `CLAUDE.md`; esta skill las concreta y añade el cómo. Detalle en:

- [reference/gates.md](reference/gates.md): las órdenes exactas de cada gate,
  incluida la VM Linux para `rayd`.
- [reference/parity-checklist.md](reference/parity-checklist.md): checklist
  de paridad Python / TypeScript / shim E2B, errores y docs.
- [reference/release-and-aws.md](reference/release-and-aws.md): release,
  aceptación en AWS real, inventario y limpieza.

## Antes de escribir código

1. Lee lo que toque de `SPEC.md`, `ARCHITECTURE.md` (ADRs),
   `AWS_API_NOTES.md` y `MILESTONES.md`.
2. **No inventes parámetros de AWS.** Si un campo, parámetro o forma de
   respuesta no aparece literalmente en `AWS_API_NOTES.md`, para y pregunta.
3. **El `.proto` es la fuente de verdad** (`proto/rayito/v1/`). Ningún struct
   de request o response se escribe a mano: `make proto`.
4. Un cambio no trivial empieza como cambio OpenSpec (ver abajo).
5. Trabaja en un worktree y una rama propios. Nunca en `main`.
6. Si el diseño de `ARCHITECTURE.md` no encaja, no improvises: para, explica
   el conflicto y propón un ADR.

## Arquitectura hexagonal por lenguaje

| Capa | Rust (`crates/`) | Python (`clients/python/src/rayito/`) | TypeScript (`clients/typescript/src/`) |
|---|---|---|---|
| Dominio puro | `rayd-core` (sin `tonic`, `axum`, `tokio`; `forbid(unsafe_code)`) | `_<feature>/_domain.py`, `_models.py` | `<feature>/domain.ts`, `models.ts` |
| Puertos | traits de `rayd-core` | `Protocol` (`ControlPlane`, `DynamoDbApi`, `EventsGateway`, `StackProvisioner`…) | `interface` (`ControlPlane`, `DynamoDbApi`…) |
| Aplicación | `rayd/src/<área>/manager.rs`, `features/` | `_<feature>/_service.py` + `_service_async.py`, `_section.py` (sección de `ConfigureSandbox`) | `<feature>/service.ts`, `section.ts` |
| Adaptadores | `rayd/src/adapters/`, `rayd/src/grpc/` | `_<feature>/_aws.py`, `_dynamodb.py`, `sandbox_sync/`, `sandbox_async/` | `<feature>/dynamodb.ts`, `aws/`, `sandbox/`, `transport/` |

- El dominio no importa `grpc`, `boto3`/`botocore`, `@aws-sdk/*` ni el código
  generado del proto. Lo que se comparte entre sync y async en Python (mapeo
  proto ↔ modelos, errores) vive en `_*_base.py`, y `sandbox_sync/` y
  `sandbox_async/` son las dos cáscaras de I/O.
- Las dependencias llegan por constructor (`session=`, `control_plane=`,
  `credentials`, clientes); los tests usan fakes, nunca AWS.
- El adaptador traduce los errores técnicos a excepciones propias. Ningún
  tipo de botocore, smithy o tonic cruza hacia el dominio.
- **Nada especulativo**: un adaptador es un seam hipotético, dos son uno real.
  No añadas módulos, clases públicas ni excepciones "para más adelante"
  (`CLAUDE.md`, regla 3).
- Clientes AWS opcionales: el import es perezoso, dentro de la función
  activada (Python: `_optional.py`; TypeScript: `loadOptionalPeer`, con peers
  opcionales en `peerDependenciesMeta`). Los extras de Python son `cli`, `mcp`
  y `otel`.

## Nombres y estilo

- Identificadores en inglés. Docstrings, TSDoc, comentarios, mensajes al
  usuario y docs, en español.
- Se comenta el porqué, no el qué. Nada de comentarios en línea dentro de una
  función: extrae un helper con nombre o cuéntalo en el docstring/TSDoc. Sin
  etiquetas de ticket en el código.
- Python: `snake_case`, tiempos en segundos (`*_seconds`), `mypy --strict`,
  `ruff`, y las superficies sync y async idénticas. Las excepciones se llaman
  `XException`.
- TypeScript: `camelCase`, tiempos en milisegundos (`*Ms`), y `strict` con
  `exactOptionalPropertyTypes`, `noUncheckedIndexedAccess` y
  `verbatimModuleSyntax`. Biome. Los errores se llaman `XError`. Las opciones
  van en un objeto.
- Rust: `clippy` pedantic sin warnings; `thiserror` en dominio y `anyhow` solo
  en `main`; nada de `unwrap`/`expect`/`panic!` fuera de tests. Todo proceso
  hijo nace por `ChildRegistry::spawn` (`clippy.toml` lo impone).
- Lo público es lo que exportan `rayito/__init__.py` (`__all__`),
  `rayito.e2b`, `src/index.ts` y `src/e2b/index.ts`. Lo demás es privado
  (módulos `_` en Python, sin re-exportar en TypeScript).
- Los tests nuevos se nombran por la función o el comportamiento
  (`test_s3_mounts_*.py`, `s3-mounts.test.ts`), no por el hito.

## Sin valores mágicos

- Cada número o cadena con significado es una constante con nombre (`Final`
  en Python, `const` exportada en TypeScript). Su docstring o comentario cita
  la fuente: una sección de `AWS_API_NOTES.md`, la doc de AWS con fecha de
  consulta o una medición.
- Una sola definición por SDK, y el mismo valor con nombre equivalente en el
  otro (`CONNECT_TIMEOUT_SECONDS` ↔ `CONNECTION_TIMEOUT_MS`).
- Los límites compartidos se editan en `limits.json`.
  `scripts/gen_limits.py` genera `_limits.py` y `limits.ts`, que nunca se
  editan a mano. Las plantillas de `infra/*.yaml` se renderizan con
  `scripts/gen_stack_assets.py`.
- Los valores por defecto de la CLI son las constantes del SDK, nunca
  literales repetidos.

## Funciones con coste: apagadas por defecto

- Nada que cree recursos o haga llamadas facturables corre sin una opción
  explícita del SDK o la CLI. Sin la opción no hay ninguna llamada a ese
  servicio ni se carga su peer, y un test en cada SDK lo comprueba (por
  ejemplo, la traza dorada de `test_m15_zero_cost.py`).
- El docstring/TSDoc de la clase u opción que lo activa lleva el bloque
  `Coste y activación` con estas seis entradas: `Activa:`, `Recursos y
  llamadas AWS:`, `Coste aproximado:` (región, fecha de consulta y URL de
  precios), `IAM:`, `Cómo apagarla:` y `Ejemplo:`. En TypeScript va en el TSDoc
  del propio símbolo, porque tsdown descarta el comentario de módulo, y se
  registra en `clients/typescript/scripts/cost-declarations/<función>.json`.
- La infra se monta como componente `OptionalStack`: plantilla en
  `infra/<componente>.yaml`, `OptionalStacks.deploy/status/destroy` y
  `rayito stack deploy|status|destroy <componente>`. Sin servidores propios.
- Datos de un entorno concreto (VPC, subredes, dominio, certificado…): solo
  como argumentos de la llamada o variables de entorno en tiempo de ejecución.
  Nunca como valores por defecto en el código.
- En docs, la página de la función lleva la caja
  `!!! info "Coste y activación"` y una fila en `optional-features.md`.

## Errores

- La jerarquía tiene la forma de E2B. `SandboxException`/`SandboxError` es la
  raíz de lo atribuible a un sandbox. `AuthenticationException`,
  `QuotaExceededException`, `CapacityException` y `UnimplementedError` quedan
  fuera a propósito.
- Se mapea por código (status gRPC, `StreamError.code`, `aws_code`/`awsCode`),
  nunca por el texto del mensaje. Los `code` posibles son una lista cerrada que
  documenta el docstring.
- Los mensajes van en español y nunca llevan URL, bucket, clave, ruta, valor o
  nombre de secreto, token ni el mensaje crudo de AWS. El `__cause__`/`cause`
  pasa por `sanitize_aws_error`/`sanitizeAwsError`.
- Una función que no existe lanza `UnimplementedError(feature, reason, doc)`,
  y el shim de E2B enlaza la página de compatibilidad.
- En `rayd`: dentro de streams, `StreamError` con código string (`not_found`,
  `permission_denied`, `unimplemented`); en unarios, status gRPC estándar.

## Lo que nunca se escribe ni se loguea

**En ficheros versionados, docs, tests, fixtures, mensajes de commit,
descripciones de PR e informes**: IDs de cuenta, ARNs con cuenta real,
nombres de bucket, IDs de VPC, subred, SG, ENI o certificado, zonas o
dominios de cualquier organización, nombres de empresa o de empleador,
perfiles SSO, IDs de MicroVM, tokens o JWE, URLs prefirmadas, valores o
nombres de secretos y rutas locales absolutas. Usa los marcadores
`123456789012`, `amzn-s3-demo-bucket`, `vpc-0123456789abcdef0`,
`microvm-00000000-0000-0000-0000-000000000001`, `example.com` y
`<tu-perfil>`. Los datos del entorno de pruebas llegan solo por variables
`RAYITO_E2E_*` en tiempo de ejecución, documentadas de forma genérica en la
cabecera de cada test. `scripts/check_hygiene.py` caza los patrones con forma
fija, pero no puede conocer nombres ni dominios: revisa tu diff.

**En logs y errores del SDK y de `rayd`**: solo ids, estados, duraciones,
bytes y códigos. Nunca contenido de ficheros, código ejecutado, `envs`, bytes
de PTY, tokens, cabeceras ni `runHookPayload` (`SECURITY.md`, "Higiene de
logging"). Los secretos se redactan en `repr`/`inspect` (`REDACTED`,
`hidden.ts`). Python loguea en `rayito.*` y TypeScript usa un logger inyectado,
en silencio por defecto.

## Gates (resumen)

Antes de cada push: los gates del lenguaje que tocaste, más OpenSpec y docs.
Las órdenes exactas están en [reference/gates.md](reference/gates.md).

- Rust: fmt, `clippy -D warnings` y `cargo test --workspace --locked`, dentro
  de la VM Linux como usuario sin privilegios (uid ≥ 1000), con `flock` y
  `-j 2`.
- Python: `pytest tests/unit`, `ruff check`, `ruff format --check` y
  `mypy src tests`.
- TypeScript: `pnpm lint`, `typecheck`, `build`, `test` y `pack:check`.
- Docs: `mkdocs --strict`. OpenSpec:
  `npx -y @fission-ai/openspec@1.10.0 validate --all --strict --no-interactive`.
- Raíz: `check_hygiene.py`, `check_pins.py` y `gen_limits.py --check`.

## Flujo OpenSpec

- `openspec/changes/<nombre>/` lleva `proposal.md` (por qué y qué),
  `design.md` (todas las decisiones cerradas), `tasks.md` (checklist ordenada)
  y `specs/<capacidad>/spec.md` con `## ADDED`/`## MODIFIED Requirements`. Un
  `MODIFIED` copia el requisito entero, escenarios incluidos.
- La CLI va fijada a la 1.10.0 (las 1.11+ rompen los `Purpose` de las specs).
  Valida el cambio antes de implementar y `--all` antes del PR.
- Marca `tasks.md` a medida que avanzas. Se archiva
  (`openspec archive <nombre> --yes`) solo después de la aceptación: AWS real
  si toca runtime, gates si no.
- Las decisiones de arquitectura se escriben como ADR en `ARCHITECTURE.md`.

## Git y PR

- Conventional Commits. Cada commit con `git commit -s -S` (DCO más firma).
- El PR sigue `.github/PULL_REQUEST_TEMPLATE.md`: cambio OpenSpec, gates y en
  qué sistema corrieron, e2e en AWS o "sin cambio de runtime", y CHANGELOG del
  paquete tocado (a mano, en `## [Unreleased]`).
- Se fusiona solo con merge commit (`gh pr merge --merge`), nunca squash ni
  rebase, nunca push a `main`, y solo con CI en verde.
- Los cambios en la API pública siguen "Versionado y soporte"
  (`docs/site/docs/limits.md`). En 0.x una minor puede romper, pero lo dice
  (`feat!:` y entrada en `Changed`/`Removed`). Antes de retirar algo, se marca
  obsoleto (`DeprecationWarning` / `@deprecated` y `Deprecated` en el
  CHANGELOG) al menos una minor antes.

## Documentación

- En español, con Diátaxis: `primeros-pasos/` (tutorial), `guias/` (cómo
  hacer), `referencia/` (API, CLI, errores, límites) y `concepts.md` +
  `ARCHITECTURE.md` (explicación). Cada página nueva entra en el `nav` de
  `docs/site/mkdocs.yml`.
- Los ejemplos van en pestañas `=== "Python"` / `=== "TypeScript"` y
  `scripts/check_docs_examples.py` los compila y comprueba sus tipos.
- Las funciones opcionales llevan la caja "Coste y activación", y lo medido en
  AWS real va en una caja "Medido en AWS real" que cita `AWS_API_NOTES.md`.
