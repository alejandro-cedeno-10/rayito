# Release y aceptación en AWS

## Contenido

- Regresión por niveles
- Flujo de release
- Reglas para cualquier ejecución en AWS
- Inventario y limpieza
- Coste

## Regresión por niveles

No todo cambio pide la misma comprobación. El nivel lo marca lo que toca el
diff, y el PR dice cuál corrió. Ante la duda, sube de nivel.

| Qué cambia | Qué corre | AWS real |
|---|---|---|
| Solo docs, tests o CI | gates del lenguaje tocado y `mkdocs --strict` | No |
| SDK o herramientas (CLI, servidor MCP, panel local, proxy) sin tocar `rayd`, imagen, proto, `infra/` ni el plano de control | CI completo y el e2e local (`make local-up` y `make local-e2e`, Docker + Floci; en CI, `local-e2e.yml` se dispara con cambios en `clients/**`, `testdata/**` y `limits.json`, entre otros) | Un chequeo corto y dirigido, solo de la función tocada y en carriles paralelos por lenguaje (por ejemplo, los e2e de la CLI o del MCP que cambiaron), con inventario antes y después |
| `rayd`, imagen, `proto/`, `infra/`, plano de control o una release | CI completo, e2e local y la regresión completa (e2e de Python y de TypeScript, el corpus de E2B) | Sí: regresión completa; la rotación de 60 minutos solo si el mantenedor la pide |

- Los carriles de Python y de TypeScript corren en paralelo, cada uno con su
  imagen y su prefijo de recursos, nunca contra la misma imagen a la vez (ver
  «Reglas para cualquier ejecución en AWS»).
- El e2e local no sustituye al real cuando cambia el runtime: no cubre el proxy
  de AWS, los snapshots, IMDS, los montajes ni el coste
  (`docs/site/docs/guias/probar-en-local.md`).
- Un cambio de SDK que añade una función con coste o toca el protocolo con
  `rayd` sube al nivel de la fila siguiente.

## Flujo de release

1. Los cambios entran en `main` por PR, con CI en verde y merge commit. Cada
   PR deja su entrada en `## [Unreleased]` del CHANGELOG del paquete que toca.
2. release-please mantiene abierto un único PR de release (versiones
   enlazadas: Python, TypeScript y `rayd` suben juntos).
3. `make release-pr` (`scripts/prepare_release_pr.py`) deja ese PR listo:
   arregla los lockfiles (`Cargo.lock`, `uv.lock`), convierte
   `## [Unreleased]` en `## [x.y.z] - fecha` y lo rehace como un único commit
   firmado. Con `--dry-run` solo muestra el diff.
4. CI en verde sobre el PR de release **antes** de gastar en AWS.
5. Aceptación en AWS real sobre el árbol del PR de release: imágenes
   desechables publicadas para esta ejecución, e2e de Python, e2e de
   TypeScript y el corpus de E2B. Un hito o una release no se cierran con
   mocks.
6. Al fusionar, se crean los tags `python-v*`, `typescript-v*` y `rayd-v*`, y
   `release.yml` publica: PyPI y npm por OIDC (sin tokens), y los assets de
   `rayd` firmados con cosign más su SBOM.
7. La publicación se verifica con `docs/site/docs/verify.md`.

No publiques en PyPI ni en npm a mano, y no saltes el paso 4.

## Reglas para cualquier ejecución en AWS

- Solo si la tarea lo pide. Pasa siempre la región explícita. Las
  credenciales vienen del perfil del usuario, que nunca se escribe en un
  fichero ni en un informe. Si la sesión SSO ha caducado, para y dilo.
- El CLI de AWS del sistema puede no conocer `lambda-microvms`: usa boto3
  (`uv run` en `clients/python`) o la CLI `rayito`.
- El e2e nunca corre sin `RAYITO_E2E=1` y `RAYITO_TEMPLATE`. Los datos del
  entorno (VPC, subredes, dominio, certificado, buckets) llegan solo por
  variables `RAYITO_E2E_*` o `RAYITO_*` en tiempo de ejecución.
- **Serializado**: nunca dos sesiones de e2e contra la misma imagen a la vez.
  El sweeper de Python solo termina los MicroVMs que creó su sesión (el
  `control_plane` de sesión apunta cada id que devuelve `run_microvm`); los
  demás vivos de la imagen se informan por número y no se tocan, y lo que nace
  fuera de ese `control_plane` (el corpus E2B, la CLI en subprocesos) lo acota
  el `maximumDurationInSeconds=900`. El de TypeScript termina solo `created`.
  Aun así, usa imágenes con un nombre propio para la ejecución.
- Un e2e nuevo etiqueta lo que crea con `RAYITO_E2E_TAGS` (helper
  `e2e_tags()` / `tagsFromEnvironment()`), registra para la limpieza solo lo
  que demostró crear, arranca los servidores en segundo plano sin timeout
  (`timeout=None` / `timeoutMs: 0`) y sube los scripts con la API de ficheros,
  nunca citados en la shell.
- Nada de mocks para aceptar. Si algo falla en AWS y pasa en local, se
  investiga y se anota en `AWS_API_NOTES.md` (pregunta Q-n con fecha y
  medida), no se ignora.

## Inventario y limpieza

Antes de crear nada, guarda un inventario completo de la cuenta y la región:

- MicroVMs no terminados.
- Imágenes y sus versiones.
- Pilas de CloudFormation.
- Secretos de Secrets Manager.
- Tablas de DynamoDB.
- Funciones Lambda.
- Distribuciones de CloudFront.
- Sistemas de ficheros y access points de EFS.
- ENIs, security groups y conectores de red.
- Certificados de ACM.
- Objetos S3 bajo los prefijos de Rayito.
- Grupos de logs.

Al terminar:

- Borra **solo** lo que creó tu ejecución. Lo que ya existía antes del
  inventario se queda, aunque parezca de Rayito.
- Repite el inventario y compara antes y después. La diferencia tiene que ser
  cero, salvo lo que se quiera dejar a propósito, que se explica.
- La última versión de una imagen solo se puede borrar borrando la imagen.
  Usa imágenes desechables para no tocar las que ya existían.
- Publica las imágenes desechables con un id de ejecución
  (`RAYITO_E2E_RUN_ID=<id> make image-publish*` o `rayito image publish
  --artifact-run-id <id>`): el zip va a `rayito/images/runs/<id>/` y la
  limpieza borra solo ese prefijo. Sin id, borra solo los zips cuyo resumen
  `--json` dijo `artifactUploaded: true`; nunca un
  `rayito/images/rayd-<sha>.zip` que la publicación encontró ya subido,
  porque puede ser el de otra ejecución del mismo commit.

## Coste

- Fija un tope antes de empezar y dilo en el PR: imágenes publicadas,
  MicroVM-segundos y recursos opcionales creados.
- Cost Explorer va con ~24 h de retraso. Estima a partir de los MicroVM-segundos
  y del número de versiones de imagen publicadas.
