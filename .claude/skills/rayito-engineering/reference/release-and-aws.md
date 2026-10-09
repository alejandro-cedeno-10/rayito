# Release y aceptación en AWS

## Contenido

- Flujo de release
- Reglas para cualquier ejecución en AWS
- Inventario y limpieza
- Coste

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
- **Serializado**: nunca dos sesiones de e2e contra la misma imagen a la vez,
  porque el sweeper del `conftest` termina todos los MicroVMs vivos de la
  imagen. Usa imágenes con un nombre propio para la ejecución.
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
