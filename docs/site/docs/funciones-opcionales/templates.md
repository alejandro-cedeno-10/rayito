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
      `ListMicrovmImageVersions`/`ListMicrovmImageBuilds`/
      `GetMicrovmImageBuild`; `s3:GetObject` (zip de la imagen base),
      `HeadObject`/`PutObject` (subir el artefacto nuevo, sólo si no existe
      ya por su hash); `logs:DescribeLogStreams`/`GetLogEvents` sólo si el
      build no termina en `SUCCESSFUL`+`ACTIVE` (para explicar el fallo).
      Ver `AWS_API_NOTES.md` §27.
    - **Coste aproximado** (consultado 2026-10-01): cada versión de imagen
      nueva cuesta almacenamiento de snapshot, ≈ $0,04/semana por versión
      (mínimo una semana), igual que publicar con `rayito image publish`;
      el build en sí no se factura aparte (sin CodeBuild, opción A de la
      investigación).
    - **IAM**: `RayitoTemplateBuilder` (`infra/templates.yaml`) sobre quien
      llama a `Template.build()`:
      `CreateMicrovmImage`/`UpdateMicrovmImage`/`GetMicrovmImage*`/
      `ListMicrovmImageVersions`, `iam:PassRole` sobre el rol de build (el
      mismo que usa `rayito image publish`), `s3:PutObject`/`GetObject`/
      `HeadObject` en el bucket de artefactos y lectura del grupo de logs
      de la imagen. Deliberadamente separada de cualquier política de
      lanzar sandboxes.
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
  imagen base; `skip_cache()`/`skipCache()` sólo fuerza un rebuild con
  `force=True`/`force: true` en vez de reusar una versión idéntica ya
  construida.
- **Sólo ARM64.** `cpu_count=`/`cpuCount` se acepta pero se ignora: Lambda
  MicroVMs sólo construye para `ARM_64` (Q87). `memory_mb`/`memoryMb` sí se
  valida contra los cinco tamaños soportados (RES-1); `m15-sizes-catalog`
  aportará el redondeo con aviso una vez esa función aporte
  `resolve_size()`.
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
- **`setStartCmd`'s `ready_cmd` todavía no gatea nada en el agente.** El
  `StartSpec` se hornea correctamente en `/etc/rayito/template.json`, pero
  `rayd` leer ese fichero y arrancar/sondear el proceso es un seguimiento
  no bloqueante de este cambio (ver el `proposal.md` de `m15-templates`):
  hoy `rayd` arranca igual con o sin `setStartCmd()`.

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
        .set_start_cmd("python -m http.server 8000", wait_for_port(8000))
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
            "python -m http.server 8000", wait_for_port(8000)
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
      .setStartCmd("python -m http.server 8000", waitForPort(8000));

    const info = await Template.build(t, "mi-template", { bucket: "mi-bucket-de-artefactos" });
    console.log(info.templateId);

    await using sbx = await Sandbox.create({ template: info.templateId });
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
