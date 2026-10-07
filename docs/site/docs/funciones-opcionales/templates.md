# Templates declarativos

Un DSL fluido (igual al `Template` de E2B v2) que compila a un Dockerfile y
un zip deterministas, compuestos sobre una imagen `rayito-base`/
`rayito-base-caps` ya publicada, y los sube con
`create`/`update-microvm-image`. Sin plano de control propio: 0.6 lo hace
todo en el cliente (investigación §3, `docs/research/2026-10-e2b-out-of-scope.md`).

!!! info "Coste y activación"
    - **Por defecto**: apagado. Sin llamar a `Template.build()`/
      `build_in_background()` (TypeScript: `buildInBackground`), el SDK no
      construye ningún cliente de `lambda-microvms`/S3/Logs: el builder
      (`Template()`, `copy()`, `pip_install()`, `to_dockerfile()`...) es
      puro.
    - **Activa**: `Template.build(t, "mi-template", bucket="...")` /
      `Template.build(t, "mi-template", { bucket: "..." })`.
    - **Recursos y llamadas AWS**: `lambda:GetMicrovmImageVersion` (leer la
      imagen base y, al terminar, sondear el build),
      `CreateMicrovmImage`/`UpdateMicrovmImage`, `GetMicrovmImage`,
      `ListMicrovmImageVersions`; `s3:GetObject` (zip de la imagen base),
      `HeadObject`/`PutObject` (subir el artefacto nuevo, sólo si no existe
      ya por su hash); `logs:DescribeLogStreams`/`GetLogEvents` sólo si el
      build no termina en `SUCCESSFUL`+`ACTIVE` (para explicar el fallo).
      Ver `AWS_API_NOTES.md` §27.
    - **Coste aproximado** (consultado 2026-10-01): cada versión de imagen
      nueva cuesta almacenamiento de snapshot, ≈ $0,04/semana por versión
      (mínimo una semana), igual que publicar con `rayito image publish`;
      el build en sí no se factura aparte (sin CodeBuild, opción A de la
      investigación). Cada zip de contexto queda en tu bucket bajo
      `rayito/templates/` (almacenamiento S3 estándar): ponle una regla de
      ciclo de vida a ese prefijo; ningún build posterior lo vuelve a leer.
    - **IAM**: `RayitoTemplateBuilder` (`infra/templates.yaml`) sobre quien
      llama a `Template.build()`:
      `UpdateMicrovmImage`/`GetMicrovmImage*`/`ListMicrovmImageVersions`
      sobre las imágenes de la cuenta y la región, con un `Deny` explícito
      que impide actualizar las imágenes base publicadas
      (`ProtectedImageNamePrefix`, `rayito-base` por defecto);
      `CreateMicrovmImage` sobre `*`, porque AWS lo autoriza sobre `*` y no
      sobre el ARN de la imagen nueva, así que no se puede acotar por nombre
      (un `create` sobre un nombre que ya existe falla, de modo que tampoco
      sobrescribe una base); `lambda:PassNetworkConnector` sobre los
      conectores gestionados por AWS, que `create`/`update` pasan aunque no
      nombres ninguno (`AWS_API_NOTES.md` Q114); `iam:PassRole` sobre el rol de build (el mismo que usa
      `rayito image publish`); `s3:PutObject`/`GetObject` en
      `rayito/templates/` del bucket de artefactos y `s3:GetObject` sólo en
      `rayito/*` del bucket de la imagen base (por defecto, el mismo; nunca
      el bucket entero, que puede guardar también los `HOME` persistidos y
      las transferencias de los sandboxes); lectura del grupo de logs de la
      imagen. Deliberadamente separada de cualquier política de lanzar
      sandboxes.
    - **Cómo apagarla**: no llames a `Template.build()`/
      `buildInBackground()`. Las versiones de imagen ya construidas se
      borran con `rayito image` (no las borra `Template`). El grupo de logs
      `/rayito/<nombre>` que crea el rol de build en el primer build
      tampoco se borra con la imagen (`delete-microvm-image` no lo toca, y
      ni el SDK ni la CLI borran imágenes): al retirar una imagen, bórralo
      tú con `aws logs delete-log-group --log-group-name /rayito/<nombre>`
      (`logs:DeleteLogGroup`) o ponle una retención.

!!! warning "Publica antes todos los nombres protegidos que vayas a usar"
    Como `CreateMicrovmImage` no se puede acotar por nombre, quien tenga
    `RayitoTemplateBuilder` puede **crear** una imagen con un nombre
    protegido que todavía no exista en esa cuenta y región (por ejemplo una
    variante de tamaño o `-caps` que no hayas publicado). Si luego un
    servicio pide ese nombre (`Sandbox.create(size=...)` lo resuelve por
    sufijo), correría el código del builder con el rol de ejecución de ese
    servicio. Publica con `rayito image publish`, en cada región que uses,
    todas las variantes que tus servicios puedan pedir.

## Cuándo usarlo

- Migras un agente que ya usa `Template` de E2B: el DSL es el mismo
  (`fromBaseImage`/`from_base_image` en vez de `fromDockerImage`,
  `copy`/`pipInstall`/`setEnvs`/`workdir`/`setUser`/`setStartCmd`).
- Quieres que tu CI construya una imagen nueva a partir de un `Dockerfile`
  declarativo en vez de mantener el pipeline de `rayito image publish` a
  mano.
- **Cuándo no**: un único cambio puntual en una imagen ya publicada —
  `rayito image publish` sigue siendo el camino directo.

## Divergencias con E2B

- **Sin caché de capas entre builds.** 0.6 no tiene una (investigación
  §3.4): cada `Template.build()` reconstruye todo lo que cambió desde la
  imagen base. Una versión ya construida con exactamente el mismo
  artefacto y la misma configuración se reusa sin llamar a
  `create`/`update-microvm-image`; `skip_cache()`/`skipCache()` equivale a
  `force=True`/`force: true` y envía un build nuevo de todos modos.
- **Sólo ARM64, CPU según la memoria.** La API nativa acepta `cpu_count=`
  pero lo ignora; el shim de E2B avisa con `RayitoCompatWarning` si se da
  (la CPU sale de `memory_mb`, Q87). `memory_mb`/`memoryMb` se valida
  contra los cinco tamaños soportados (RES-1); el shim de E2B redondea al
  tamaño siguiente con `RayitoCompatWarning`.
- **La imagen compuesta hereda la configuración de la base.** Hooks, rol
  de build, imagen gestionada y `additionalOsCapabilities` salen de la
  versión de `rayito-base`/`rayito-base-caps` sobre la que compones: un
  template sobre `rayito-base-caps` sigue teniendo capabilities.
- **Nombres sin `:tag`.** El nombre de un template es el de la imagen de
  Lambda MicroVMs: 1-64 caracteres de `[A-Za-z0-9_-]`
  (`TemplateException`/`TemplateError` si no).
- **Sólo `fromBaseImage()` compone de verdad.** `from_image`/`fromImage`,
  `from_template`/`fromTemplate`, `from_dockerfile`/`fromDockerfile` y
  `from_gcp_registry`/`fromGcpRegistry` lanzan `UnimplementedError`:
  inyectar `rayd` y sus hooks en una imagen externa no tiene todavía un
  camino soportado (`rayd` sólo existe ya instalado dentro de una imagen
  `rayito-*` publicada). `apt_install`/`aptInstall` también lanza:
  `rayito-base` es Amazon Linux 2023 (`dnf`, no `apt`).
- **Sin streaming en vivo de los pasos.** El log de cada paso se relee
  **al terminar** el build (TPL-1/Q83: el grupo de logs recibe la salida
  completa de BuildKit de golpe, no en vivo), nunca durante.
- **El etiquetado de E2B no tiene análogo.**
  `alias_exists`/`assign_tags`/`remove_tags`/`get_tags` (y sus
  equivalentes TS) lanzan `UnimplementedError`:
  `create`/`update-microvm-image` sólo etiqueta la imagen entera, no una
  versión concreta.
- **`set_start_cmd`/`setStartCmd` necesita una imagen base con `rayd`
  0.6.** El `StartSpec` se hornea en `/etc/rayito/template.json`; un
  `rayd` 0.6 o posterior lo lee al arrancar, lanza `start_cmd` como
  proceso gestionado antes del `/ready` del build, así que el snapshot ya
  lo lleva en marcha (aparece en `commands.list()`), y
  responde 503 en `/ready` hasta que `ready_cmd` sale con 0; si se agota
  su plazo, `/ready` falla y el build termina con
  `BuildException(reason="ready_server_error")`. Una imagen base publicada
  con un `rayd` anterior ignora el fichero.
- **El `start_cmd` ya corre cuando llega el `/run`.** Como va en el
  snapshot, en cada sandbox lanzado desde la plantilla se descongela antes
  que el hook `/run` que instala el token de acceso. `rayd` rechaza un
  `/run` que venga de un proceso del sandbox (cualquier uid ≥ 1000) sin
  consumir el `/run` del arranque, así que un `start_cmd` malicioso o
  comprometido no puede instalar su propio token ni dejar el sandbox sin
  él; sólo uno que corra como root (`user="root"` con
  `RAYITO_ALLOW_ROOT=1` en la imagen) queda fuera de esa protección
  (`SECURITY.md` T2).

## Contexto de build

`copy("app/", "/srv/app/")` lee `app/` relativo a `context_dir=`/
`contextDir` (por defecto, el directorio actual) y respeta su
`.dockerignore`. Una ruta que sale del contexto (`../secreto`, o un `src`
que es un enlace simbólico hacia fuera) da
`BuildException(reason="context_path_outside")`. Dentro de un directorio
copiado, los enlaces simbólicos (a fichero o a directorio) **nunca se
siguen**, igual que en Docker y en los dos SDKs: un `config ->
~/.aws/credentials` dentro de `app/` no acaba en el artefacto que se sube a
S3 ni en la imagen, donde el código del sandbox lo podría leer. Si
necesitas ese contenido, cópialo como fichero real dentro del contexto.
Los ficheros van bajo `__rayito_context/` dentro del zip: nunca sustituyen
el `Dockerfile` compuesto ni el binario de `rayd` de la imagen base.

### `.dockerignore`

Los dos SDKs siguen la semántica de `.dockerignore` de Docker (con los
mismos vectores de prueba):

- Cada patrón queda **anclado en la raíz del contexto**: `README.md` no
  excluye `docs/README.md`, y `/build`, `build` y `build/` son lo mismo.
- `*` y `?` **no cruzan `/`**: `*.pyc` sólo excluye los de la raíz; para
  cualquier profundidad, `**/*.pyc`.
- `**` es cero o más directorios: `**/.env` excluye el `.env` de la raíz y
  los de cualquier subdirectorio (el idioma que genera `docker init`).
- Un patrón que casa con un directorio excluye todo lo que hay dentro, y
  el último patrón que casa gana: `node_modules` seguido de
  `!node_modules/keep.js` deja pasar `keep.js`.

```text
**/.env
**/.env.*
**/.git
**/node_modules
**/*.pem
```

Si el contexto va a empaquetar algo que suele llevar secretos (`.env`,
`.env.*`, `.git/`, `.aws/`, `.ssh/`, `*.pem`, `*.key`), el SDK avisa con
las rutas (nunca el contenido) antes de subir nada: en la imagen, cualquier
código del sandbox podría leerlo. Para que el aviso pare un build en CI:

=== "Python"

    ```python
    import warnings

    from rayito import Template

    warnings.filterwarnings("error", message="el contexto de build empaqueta")
    t = Template().from_base_image().copy(".", "/app")
    Template.build(t, "mi-template", bucket="mi-bucket-de-artefactos")
    ```

=== "TypeScript"

    ```ts
    import { Template } from "rayito";

    process.on("warning", (warning) => {
      if (warning.name === "RayitoContextWarning") {
        console.error(warning.message);
        process.exit(1);
      }
    });
    const t = new Template().fromBaseImage().copy(".", "/app");
    await Template.build(t, "mi-template", { bucket: "mi-bucket-de-artefactos" });
    ```

!!! warning "Cambio de comportamiento en 0.6.x"
    Antes, `*` cruzaba `/` (`*.pyc` excluía también `sub/x.pyc`) y `**/X`
    no casaba en la raíz. Si tu `.dockerignore` contaba con lo primero,
    añade `**/` delante del patrón.

## Errores

| `reason` | Qué pasó |
|---|---|
| `None` + `step`/`command`/`exit_code`/`log_tail` | un `RUN` del Dockerfile compuesto salió con error |
| `ready_client_error` / `ready_server_error` | el proceso detrás de `/ready` respondió 4xx/5xx durante el build (Q85) |
| `build_quota` | ya hay 10 builds en marcha en este proceso, o AWS rechazó el undécimo de la cuenta (Q83) |
| `aws_error` | AWS rechazó el build por otro motivo; el mensaje y la causa llevan sólo el código y el mensaje saneados (sin la cadena canónica de un error de firma) |
| `build_timeout` | el build no terminó en `timeout`; sigue en AWS y `get_build_status()` lo consulta |
| `context_path_missing` / `context_path_outside` | un `copy()` nombra algo que no existe, o fuera del contexto |
| `base_image_not_s3` / `base_image_missing_artifact` / `base_image_missing_entrypoint` | la imagen base no es una imagen `rayito-*` publicada con `rayito image publish` |

Los mensajes nombran el template que pasaste, nunca un ARN ni el texto
libre de AWS sin sanear.

## Ejemplo rápido

=== "Python"

    ```python
    from rayito import Sandbox, Template, wait_for_port

    t = (
        Template()
        .from_base_image("rayito-base")  # (1)!
        .pip_install(["pandas==2.2.3"])
        .copy("app/", "/srv/app/")
        .set_envs({"MODE": "prod"})
        .set_start_cmd("python3 -m http.server 8000", wait_for_port(8000))
    )
    info = Template.build(t, "mi-template", bucket="mi-bucket-de-artefactos")  # (2)!
    print(info.template_id)  # arn:aws:lambda:...:microvm-image:mi-template

    with Sandbox.create(info.template_id) as sbx:
        sbx.commands.run("curl -s localhost:8000")
    ```

    1. Puro: no llama a AWS hasta `Template.build()`.
    2. Resuelve la última versión `ACTIVE` de `rayito-base`, compone el
       Dockerfile, sube el zip y espera a que el build termine.

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncTemplate, Template, wait_for_port


    async def main() -> None:
        t = Template().from_base_image().set_start_cmd(
            "python3 -m http.server 8000", wait_for_port(8000)
        )
        info = await AsyncTemplate.build(t, "mi-template", bucket="mi-bucket-de-artefactos")
        print(info.template_id)


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { Sandbox, Template, waitForPort } from "rayito";

    const t = new Template()
      .fromBaseImage("rayito-base")
      .pipInstall(["pandas==2.2.3"])
      .copy("app/", "/srv/app/")
      .setEnvs({ MODE: "prod" })
      .setStartCmd("python3 -m http.server 8000", waitForPort(8000));

    const info = await Template.build(t, "mi-template", { bucket: "mi-bucket-de-artefactos" });
    console.log(info.templateId);

    await using sbx = await Sandbox.create({ template: info.templateId });
    ```

=== "Shim E2B"

    ```python
    from rayito.e2b import E2B, Template

    client = E2B(region="us-east-1", bucket="mi-bucket-de-artefactos")  # (1)!
    t = Template().from_base_image().pip_install(["pandas==2.2.3"])
    info = client.Template.build(t, alias="mi-template", memory_mb=2048, skip_cache=False)
    ```

    1. `bucket=` es una extensión de Rayito; también vale en cada llamada.

    En TypeScript, con la forma de opciones de E2B JS:

    ```ts
    import { E2B, Template } from "rayito/e2b";

    const client = new E2B({ region: "us-east-1", bucket: "mi-bucket-de-artefactos" });
    const t = new Template().fromBaseImage().pipInstall(["pandas==2.2.3"]);
    const info = await client.Template.build(t, { alias: "mi-template", memoryMB: 2048 });
    ```

=== "CLI"

    ```bash
    rayito template build mi_template.py --name mi-template --bucket mi-bucket-de-artefactos
    rayito template status mi-template
    rayito template logs mi-template
    ```

## Un build que falla

```python
from rayito import BuildException, Template

t = Template().from_base_image().pip_install(["no-existe-este-paquete"])
try:
    Template.build(t, "mi-template-rota", bucket="mi-bucket-de-artefactos")
except BuildException as exc:
    print(exc.step, exc.command, exc.exit_code)  # 1 'RUN pip install ...' 1
```

## Build en segundo plano y logs

`Template.build()` espera a que el build termine (como mucho `timeout`, 30
minutos por defecto). Para no bloquear, `build_in_background()`
(TypeScript: `buildInBackground()`) devuelve en cuanto AWS acepta el build
un `BuildHandle`, que `get_build_status()` consulta cuando quieras, también
desde otro proceso:

=== "Python"

    ```python
    import time

    from rayito import Template

    t = Template().from_base_image().pip_install(["pandas==2.2.3"])
    handle = Template.build_in_background(t, "mi-template", bucket="mi-bucket-de-artefactos")
    status = Template.get_build_status(handle)
    while status.state == "IN_PROGRESS":
        time.sleep(15)
        status = Template.get_build_status(handle)
    if status.info is not None:
        print(status.info.template_id)  # SUCCESSFUL
    else:
        print(status.error_message)  # FAILED
    ```

=== "TypeScript"

    ```ts
    import { setTimeout as sleep } from "node:timers/promises";
    import { Template } from "rayito";

    const t = new Template().fromBaseImage().pipInstall(["pandas==2.2.3"]);
    const handle = await Template.buildInBackground(t, "mi-template", {
      bucket: "mi-bucket-de-artefactos",
    });
    let status = await Template.getBuildStatus(handle);
    while (status.state === "IN_PROGRESS") {
      await sleep(15_000);
      status = await Template.getBuildStatus(handle);
    }
    console.log(status.info?.templateId ?? status.errorMessage);
    ```

=== "CLI"

    ```bash
    rayito template status mi-template            # la versión más reciente
    rayito template status mi-template --version 3
    rayito template logs mi-template --limit 200
    ```

El log de BuildKit llega **al terminar** el build, nunca en vivo (el grupo
de logs de la imagen lo recibe de golpe, Q83). Para recibirlo línea a línea
desde `Template.build()`, pasa `on_build_logs` (TypeScript:
`onBuildLogs`); sin él, el SDK no lee el grupo de logs salvo para explicar
un fallo:

=== "Python"

    ```python
    from rayito import Template

    t = Template().from_base_image().pip_install(["pandas==2.2.3"])
    Template.build(t, "mi-template", bucket="mi-bucket-de-artefactos", on_build_logs=print)
    ```

=== "TypeScript"

    ```ts
    import { Template } from "rayito";

    const t = new Template().fromBaseImage().pipInstall(["pandas==2.2.3"]);
    await Template.build(t, "mi-template", {
      bucket: "mi-bucket-de-artefactos",
      onBuildLogs: (line) => console.log(line),
    });
    ```

En TypeScript, `onBuildLogs` y la explicación de un build fallido leen el
log con el peer opcional `@aws-sdk/client-cloudwatch-logs`
(`pnpm add @aws-sdk/client-cloudwatch-logs`); sin él instalado, el build
funciona igual, pero `onBuildLogs` no recibe ninguna línea y
`BuildError` llega sin `step`/`command`/`logTail`.

## Del `Template` de E2B a Rayito

El shim (`rayito.e2b` / `rayito/e2b`) acepta la firma de build de E2B y la
traduce a la nativa; el DSL es la misma clase en los dos.

| E2B | Rayito nativo | Nota |
|---|---|---|
| `Template().from_image("python:3.12")`, `from_template`, `from_dockerfile`, `from_gcp_registry` | `Template().from_base_image("rayito-base")` | sólo se compone sobre una imagen `rayito-*` publicada; esos cuatro orígenes lanzan `UnimplementedError` |
| `copy`, `run_cmd`, `pip_install`, `set_envs`, `set_user`, `skip_cache` | los mismos métodos | `skip_cache()` equivale a `force=True` |
| `set_workdir(path)` | `workdir(path)` | — |
| `apt_install([...])` | `run_cmd("dnf install -y ...")` | `rayito-base` es Amazon Linux 2023; `apt_install` lanza `UnimplementedError` |
| `set_start_cmd(cmd, wait_for_port(8000))` | igual, también `wait_for_url`/`wait_for_process`/`wait_for_file` | necesita una imagen base con `rayd` 0.6 |
| `Template.build(t, alias="x", cpu_count=, memory_mb=, skip_cache=, on_build_logs=)` | `Template.build(t, "x", bucket=, memory_mb=, force=, on_build_logs=)` | el shim pide `bucket` en la llamada o en `E2B(bucket=...)`; `memory_mb` se redondea al tamaño siguiente y `cpu_count` se ignora, los dos con `RayitoCompatWarning` |
| `Template.build_in_background(...)` + `Template.get_build_status(...)` | igual, con un `BuildHandle` | — |
| `Template.exists(name)` | igual | — |
| `alias_exists`, `assign_tags`, `remove_tags`, `get_tags` | — | lanzan `UnimplementedError`: Lambda MicroVMs etiqueta la imagen entera, no una versión |
| `TemplateException`, `BuildException` | las mismas clases | `BuildException` trae `reason`, `step`, `command`, `exit_code` y `log_tail` |

## Referencia

- [`rayito.Template`/`rayito.AsyncTemplate`](../referencia/python/opcionales.md#templates-declarativos)
- `AWS_API_NOTES.md` §27 (logs de build y contrato de `codeArtifact`)
- `ARCHITECTURE.md` ADR-022
