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
      `rayito/templates/` del bucket de artefactos y `s3:GetObject` en el
      bucket de la imagen base (por defecto, el mismo); lectura del grupo
      de logs de la imagen. Deliberadamente separada de cualquier política
      de lanzar sandboxes.
    - **Cómo apagarla**: no llames a `Template.build()`/
      `buildInBackground()`. Las versiones de imagen ya construidas se
      borran con `rayito image` (no las borra `Template`).

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
`.dockerignore`. Una ruta que sale del contexto (`../secreto`, o un enlace
simbólico hacia fuera) da `BuildException(reason="context_path_outside")`.
Los ficheros van bajo `__rayito_context/` dentro del zip: nunca sustituyen
el `Dockerfile` compuesto ni el binario de `rayd` de la imagen base.

## Errores

| `reason` | Qué pasó |
|---|---|
| `None` + `step`/`command`/`exit_code`/`log_tail` | un `RUN` del Dockerfile compuesto salió con error |
| `ready_client_error` / `ready_server_error` | el proceso detrás de `/ready` respondió 4xx/5xx durante el build (Q85) |
| `build_quota` | ya hay 10 builds en marcha en este proceso, o AWS rechazó el undécimo de la cuenta (Q83) |
| `build_timeout` | el build no terminó en `timeout`; sigue en AWS y `get_build_status()` lo consulta |
| `context_path_missing` / `context_path_outside` | un `copy()` nombra algo que no existe, o fuera del contexto |
| `base_image_not_s3` / `base_image_missing_artifact` / `base_image_missing_entrypoint` | la imagen base no es una imagen `rayito-*` publicada con `rayito image publish` |

Los mensajes nombran el template que pasaste, nunca un ARN ni el texto
libre de AWS.

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

## Referencia

- [`rayito.Template`/`rayito.AsyncTemplate`](../referencia/python/opcionales.md#templates-declarativos)
- `AWS_API_NOTES.md` §27 (logs de build y contrato de `codeArtifact`)
- `ARCHITECTURE.md` ADR-022
