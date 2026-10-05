# Pruebas punta a punta en local con Docker y Floci

Investigación del cambio OpenSpec `local-e2e-floci`, 2026-10-04. Objetivo:
correr el SDK de Python y el de TypeScript contra un `rayd` de verdad y
contra un AWS emulado, en un portátil o en CI, sin cuenta de AWS ni
credenciales. El resultado es `make local-up`, `make local-e2e` y
`make local-down` (`dev/local/compose.yaml`) y la guía
`docs/site/docs/guias/probar-en-local.md`.

**Resultado:** Floci 2.1.0 se adopta como emulador, fijado por digest y
encerrado (sin privilegios, sin capabilities, sin socket de Docker, sin
salida a Internet y sin puertos en el host). Primera corrida completa en
local (macOS, Docker en una VM Lima arm64 de 2 vCPU y 3 GiB): **38 tests,
38 en verde** (22 de Python y 16 de TypeScript) en ~100 s, con el entorno
ya levantado. Tras fusionar los montajes S3 en `rayd`, esa VM ya no compila
`rayd` dentro de Docker (rustc muere por memoria con `aws-sdk-s3`); con el
binario musl de `cargo zigbuild` en macOS vía `LOCAL_RAYD_BIN`, la corrida
sigue en 38 de 38.

## Por qué Floci

LocalStack Community dejó de recibir actualizaciones de seguridad y exige un
token desde marzo de 2026, según el propio README de Floci y el anuncio de
LocalStack que enlaza. Floci (`floci-io/floci`, licencia MIT) es la
alternativa sin token ni límites por servicio.

| Comprobado (2026-10-04) | Valor |
|---|---|
| Licencia | MIT (`LICENSE`, etiqueta OCI `org.opencontainers.image.licenses`) |
| Release elegida | `2.1.0` (2026-09-15), la última estable |
| Imagen | `floci/floci:2.1.0@sha256:f5aa8c18302cedb4f2385f5c4e455b3efc77fee6bf7b6e5d1712b2817ba102db` (índice OCI con amd64, arm64 y dos manifests de atestación) |
| Base | `registry.access.redhat.com/ubi9-micro:9.7`, binario nativo de Quarkus/GraalVM |
| Usuario de la imagen | `floci` (uid 1001); el entrypoint solo usa root para leer el grupo del socket de Docker |
| Procedencia | atestación SLSA v1 de BuildKit: GitHub Actions de `floci-io/floci`, revisión `560b3e4` (la etiqueta `2.1.0`), `Dockerfile.native-package`. Sin firma cosign: la integridad la da el digest fijado |
| SBOM | atestación SPDX con los 22 RPM de la base; las dependencias Java compiladas en el binario nativo no aparecen |
| Telemetría | ninguna llamada saliente encontrada en el código; da igual, porque en el compose no tiene ruta a Internet |

## Qué emula y qué no

Lo que este repositorio usa, probado con el SDK real contra Floci 2.1.0:

| Servicio | Uso en Rayito | Estado en Floci |
|---|---|---|
| CloudFormation | `OptionalStacks` y `rayito stack` | Despliega y borra las siete plantillas de `infra/` que el SDK publica (`metadata-index`, `secrets-access`, `otlp-export`, `s3-mounts`, `sizes-guard`, `templates`, `events-webhooks`), con `UsePreviousValue` al redesplegar |
| Lambda MicroVMs (plano de control) | `run-microvm`, `get-microvm`, `list-microvms`, `terminate-microvm`, imágenes y versiones | Emulado: imágenes, versiones, builds y MicroVMs con convergencia instantánea |
| Lambda MicroVMs (datos) | endpoint por VM, `create-microvm-auth-token`, suspend/resume, el guest | **No emulado** en ningún emulador; lo sustituye el contenedor `guest` |
| S3 | artefactos de `Template.build`, zip de los Lambdas | Emulado (direccionamiento por ruta y por host virtual con alias de red) |
| DynamoDB y Streams | índice de metadatos, tabla de eventos, disparador del deliverer | Emulado, con Streams leíbles por `dynamodbstreams` |
| Secrets Manager | `SecretStore`, `secrets=`, `gateways=`, clave de la pila de eventos | Emulado (ver diferencias) |
| STS | resolver un template por nombre | Emulado (cuenta `000000000000`) |
| CloudWatch Logs | grupo de logs de la pila de eventos | Emulado; la suscripción al forwarder no se ejecuta |
| IAM, SQS, EventBridge Scheduler | recursos de las plantillas | Se crean como registros; no se aplican permisos (`FLOCI_SERVICES_IAM_ENFORCEMENT_ENABLED=false`) |
| Lambda (ejecución) | forwarder, deliverer, reconciler | Requiere el socket de Docker: **rechazado** (abajo). Los handlers se ejecutan en proceso desde los tests |
| CloudFront | dominio propio (pendiente) | Floci lo emula, pero `custom-domain` aún no existe en `main` |

### Diferencias de Floci frente a AWS encontradas

- `CreateSecret` ignora `ClientRequestToken` y asigna un UUID como
  `VersionId`, así que la versión que `SecretStore.update` deriva de los
  tokens no coincide con la de AWS. El test local solo comprueba el valor.
- `ListMicrovmImageVersions` sobre una imagen que no existe responde
  `ResourceNotFoundException`. `Template.build` busca una versión reutilizable
  antes de crear la imagen, así que el primer build local va con
  `force=True`. No se ha medido qué responde AWS en ese caso; los e2e de
  templates contra AWS usan nombres nuevos y pasan, lo que sugiere una lista
  vacía. Queda como pregunta abierta para la siguiente aceptación en AWS.
- `RunMicrovm` devuelve `maximumDurationInSeconds: 28800` sea cual sea el
  pedido. No afecta a los tests: el plazo lógico lo impone `rayd`.
- `create-microvm-auth-token`, `suspend-microvm` y `resume-microvm`
  responden 404 (documentado por Floci como fuera de alcance).

## Decisión de confianza

Floci es un servidor HTTP **sin autenticación**: cualquiera que alcance su
puerto 4566 puede hacer cualquier cosa que emule. Tres avisos publicados lo
dejan claro (`github.com/floci-io/floci/security/advisories`):

| Aviso | Severidad | Afecta | Relevancia aquí |
|---|---|---|---|
| GHSA-6f92-9q2p-fmpj: bind mount del host vía ECS lleva a RCE en el host Docker | crítica | `< 2.1.0` | Corregido en 2.1.0; además, sin socket de Docker no hay host que alcanzar |
| GHSA-3p4c-wp7w-mgjx: RCE sin autenticar vía reflexión en `$util` de VTL | crítica | `<= 1.5.28` | No afecta a 2.1.0 |
| GHSA-x7jw-8w9c-q4rq: el webhook de tokens de EKS acepta tokens falsos | alta | `< 2.1.0` | Corregido en 2.1.0; EKS está apagado |
| GHSA-x427-4mpm-vff5: bypass de firma del autorizador JWT de API Gateway v2 | alta | `< 1.7.0` | No afecta a 2.1.0 |

Con eso, el compose lo trata como código no confiable:

1. **Sin socket de Docker.** Es lo que convierte un fallo de Floci en root
   sobre el host (el aviso crítico de ECS). Se pierde la ejecución real de
   Lambda (y RDS, ECS, EKS...), que Rayito no necesita: los handlers de
   eventos se prueban en proceso.
2. **Sin privilegios:** `user: 1001:0`, `cap_drop: [ALL]`,
   `no-new-privileges`, sistema de ficheros de solo lectura con `tmpfs` en
   `/tmp` y `/app/data`, `mem_limit` y `pids_limit`. Verificado en el
   contenedor: `Uid 1001`, `CapEff 0`, `NoNewPrivs 1`.
3. **Sin red hacia fuera:** solo vive en la red `aws` (`internal: true`).
   Verificado: `connect: Network is unreachable` hacia una IP pública y sin
   resolución DNS externa.
4. **Sin puertos en el host:** nadie fuera del compose alcanza el 4566.
5. **Servicios con contenedores apagados**, para que tampoco abran
   listeners: consola web, EC2 (y su IMDS en el 9169), ECS, EKS, ECR, RDS,
   ElastiCache, MSK, Amazon MQ, OpenSearch, Managed Flink, CodeBuild e IoT
   (broker MQTT).
6. **Fijado por digest** y actualizado a mano tras leer las notas de la
   release y los avisos nuevos.

### Barrido de vulnerabilidades (2026-10-04)

Con Trivy 0.75.0 (`aquasec/trivy@sha256:af6acf9a…`, sin socket de Docker:
la imagen se exporta con `docker save` y se escanea el tar), severidades
HIGH y CRITICAL:

| Objetivo | Resultado | Lectura |
|---|---|---|
| Imagen de Floci 2.1.0 (SO) | 0 críticas, 8 altas: `libacl` y `libcap` (escalada local, con versión corregida en RHEL 9.8) y `pcre2` (sin corrección) | Las de `libacl`/`libcap` necesitan ejecutar código y operar con ACL o capabilities de ficheros: el proceso no tiene capabilities y el sistema de ficheros es de solo lectura. `pcre2` no la usa el binario nativo (las expresiones regulares de Java no pasan por ella) |
| Dependencias Java de Floci (grafo de dependencias de GitHub del repositorio, que sigue a `main` y no a la etiqueta) | 0 críticas, 5 altas: `wire-runtime` (caída del decodificador), `httpcore5`/`httpcore5-h2` (cliente HTTP), `postgresql` (driver de RDS, apagado) y `assertj` (solo tests) | Denegación de servicio dentro de un emulador local, como mucho. El escaneo de las dependencias de la etiqueta con su `pom.xml` lo cortó Maven Central (HTTP 429); el binario nativo no trae metadatos de dependencias que Trivy pueda leer |
| Secretos en la imagen de Floci | ninguno | |
| Imagen `runner` (nuestra) | 5 críticas sin corrección en paquetes de Debian 12 (`sqlite`, `perl-base`, `zlib`) y `node-tar` dentro del pnpm 9.15.4 que fija `packageManager` | El runner solo ejecuta los tests y las dependencias de los lockfiles; no procesa entradas no confiables. Dependabot propondrá los bumps de las bases |

Lo que no cubre este barrido: las dependencias Java exactas de la etiqueta
`2.1.0` (ver arriba) y una auditoría del código de Floci. La mitigación no
depende de que Floci esté libre de fallos: depende del encierro.

## Diseño del entorno

- `floci`: el emulador, en la red interna `aws`.
- `runner`: Python 3.12 con uv 0.12.18 y Node 22 con pnpm 9.15.4 (todo fijado
  por digest), usuario uid 993 (fuera del rango del sandbox, 1000-65535,
  como el agente de la plataforma: `rayd` rechaza el `/run` y el `/terminate`
  de un uid del sandbox), sistema de ficheros de solo lectura con el
  árbol montado en solo lectura y las dependencias en volúmenes. Es el
  dueño del espacio de red.
- `guest`: la **imagen de producto** (`image/Dockerfile`, sin copiarla) con
  `rayd` como PID 1, construida desde `dev/local/.guest-context`, que prepara
  `make local-guest-context` (el binario, de `LOCAL_RAYD_BIN` o compilado en
  `dev/local/rayd/Dockerfile` sobre la misma base al2023 para enlazar contra
  su glibc). Comparte el espacio de red del runner
  (`network_mode: service:runner`): los SDK hablan con `rayd` por loopback
  con las credenciales locales de grpc (Python) y h2c (TypeScript), los
  mismos canales que ya usan sus tests unitarios contra el `rayd` falso. No
  hizo falta ningún cambio en el SDK publicado ni bajar ninguna de sus
  comprobaciones (TLS fuera de loopback, filtro SSRF del deliverer).
- `restart: always` en el guest: `/terminate` hace salir a `rayd` (como en un
  MicroVM, ADR-011) y Docker lo rearranca para el siguiente sandbox.
- Plano de control local, solo en los tests (`tests/local/guest.py` y
  `tests/local/guest.ts`): un decorador del puerto `ControlPlane` sobre el
  plano real apuntado a Floci. `run-microvm`, `get-microvm`, `list-microvms`
  y `terminate-microvm` pasan por el adaptador real; el endpoint, el token
  del proxy y suspend/resume los sustituyen los hooks del guest.

### Lo que no se cubre en local

- El proxy de AWS: el JWE, el 403 de un token falso, los keepalives y el
  `x-aws-proxy-port`.
- Snapshot y restauración reales del MicroVM: `/suspend` y `/resume` del guest
  pasan por la misma máquina de estados de `rayd`, pero la memoria no se
  congela.
- Bloqueo de IMDS, egress y montajes S3: necesitan `CAP_NET_ADMIN` o
  `CAP_SYS_ADMIN` en el guest y credenciales por IMDS, y dárselas rompería la
  regla de no privilegiar contenedores.
- Persistencia (`Checkpoint`/`Restore`) y transferencias prefirmadas:
  `rayd` resuelve credenciales por IMDS y exige HTTPS.
- La entrega HTTPS del deliverer y su filtro SSRF (los cubren los tests
  unitarios de `infra/lambdas/events_webhooks`), y la suscripción de
  CloudWatch Logs al forwarder.
- Coste, cuotas, permisos IAM y tiempos de arranque reales.

Un hito o una release se siguen cerrando con el e2e contra AWS real
(`CLAUDE.md`, regla 4); el entorno local es el bucle rápido de antes.

## CI

`local-e2e.yml` corre `make local-up` y `make local-e2e` en
`ubuntu-24.04-arm` (la base al2023 solo publica arm64) en los PR que tocan el
entorno local, cada noche y a mano. El primer arranque compila `rayd` dentro
de Docker; el tiempo medido en CI está en el PR del cambio.
