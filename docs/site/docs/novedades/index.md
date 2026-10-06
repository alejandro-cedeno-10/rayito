---
title: Novedades
description: Qué trae cada versión de Rayito, cómo actualizar, qué es experimental y qué está en desarrollo.
---

# Novedades

Una página por versión con lo que cambia para ti: funciones nuevas con su
ejemplo, correcciones que cambian un comportamiento y los pasos para
actualizar. El detalle completo, cambio a cambio, está en el
[Changelog](../referencia/changelog.md) de cada componente.

<div class="grid cards" markdown>

-   :material-flask-outline:{ .lg .middle } **0.8.0** · borrador, sin publicar

    ---

    Agente de IA dentro del sandbox (`sbx.agent` con OpenCode, ya en
    `main`), presets de pasarela para modelos y la página de precios.
    Aún no está en ningún paquete publicado.

    [:octicons-arrow-right-24: Borrador de 0.8.0](0.8.0.md)

-   :material-tag:{ .lg .middle } **0.7.1** · 2026-10-06 · actual

    ---

    `rayd` lleva sus avisos de licencia de terceros en la release, en
    `rayito-image.zip` y en la imagen (`/usr/share/doc/rayd/`), y
    `SHA256SUMS` va firmado. Sin cambios de API.

    [:octicons-arrow-right-24: Novedades de 0.7.1](0.7.1.md)

-   :material-tag:{ .lg .middle } **0.7.0** · 2026-10-05

    ---

    Volúmenes EFS en tu VPC, dominio propio (experimental), pruebas en
    local con Docker y Floci, y un endurecimiento de seguridad de `rayd`,
    los SDK, la infraestructura y la release.

    [:octicons-arrow-right-24: Novedades de 0.7.0](0.7.0.md)

-   :material-tag:{ .lg .middle } **0.6.1** · 2026-10-04

    ---

    Redesplegar una pila opcional conserva sus parámetros,
    `reincarnate()` reaplica todas las opciones 0.6 y `rayd` recoge los
    procesos zombi como PID 1.

    [:octicons-arrow-right-24: Novedades de 0.6.1](0.6.1.md)

-   :material-tag:{ .lg .middle } **0.6.0** · 2026-10-03

    ---

    Montajes S3, catálogo de tamaños, eventos y webhooks, exportación
    OTLP, templates declarativos, pasarela de secretos y `rayito stack`.

    [:octicons-arrow-right-24: Novedades de 0.6.0](0.6.0.md)

</div>

## Qué versión tienes

=== "Python"

    ```python
    import rayito

    print(rayito.__version__)  # "0.7.1"
    ```

=== "TypeScript"

    ```ts
    import { VERSION } from "rayito";

    console.log(VERSION); // "0.7.1"
    ```

=== "CLI"

    ```bash
    rayito doctor --template rayito-base  # la comprobación "compatibility" compara SDK y rayd
    ```

Los SDK de Python y TypeScript y el agente `rayd` comparten la serie
`MAJOR.MINOR`. Una función que vive dentro del sandbox necesita además una
imagen publicada con un `rayd` de esa serie:
[Compatibilidad SDK ↔ rayd ↔ imagen](../limits.md#compatibilidad-sdk-rayd-imagen).

## Cómo actualizar

=== "Python"

    ```bash
    pip install -U "rayito[cli]"    # con uv: uv lock --upgrade-package rayito
    ```

=== "TypeScript"

    ```bash
    pnpm add rayito@latest          # o: npm i rayito@latest
    ```

Después, republica tu imagen sobre el `rayd` nuevo si quieres usar lo que
corre dentro del sandbox (por ejemplo `volumes=`, `mounts=`, `events=` o
`gateways=`, el endurecimiento de `rayd` 0.7.0 o los avisos de licencia
de `rayd` 0.7.1 en `/usr/share/doc/rayd/`):
[Imágenes](../images.md#publicar-las-tres). Sin republicar, el SDK nuevo
sigue funcionando con tu imagen actual, y cada función que necesita el
`rayd` nuevo falla cerrada con `UnimplementedError` y termina el sandbox
que acaba de lanzar. Al pasar de 0.6.x a 0.7.0, revisa además los
[cambios de comportamiento](0.7.0.md#como-actualizar-desde-06x).

## Disponible como experimental

Funciones publicadas, apagadas por defecto, cuya API puede cambiar en una
minor.

| Función | Desde | Cómo se usa | Por qué es experimental |
|---|---|---|---|
| [Volúmenes EFS](../funciones-opcionales/volumenes-efs.md) | 0.7.0 | `volumes=` / `volumes`, `VolumeStore`, `EfsVolumes` (`rayito stack deploy efs-volumes`), sobre la imagen opcional `rayito-base-caps-efs` (`rayito image publish --with-efs`) | aceptados en AWS real, pero dependen de una imagen opcional y de tu VPC |
| [Dominio propio](../funciones-opcionales/dominio-propio.md) | 0.7.0 | `CustomDomain` (`rayito domain deploy`) y `register()` de cada ruta | sin verificar de punta a punta en AWS real (crear la distribución y servir tráfico); `domain=` en `Sandbox.create()` sin cablear |

Si pruebas una, cuéntanos cómo te fue en un
[issue de GitHub](https://github.com/alejandro-cedeno-10/rayito/issues/new/choose).

## En desarrollo

Partes con un cambio pendiente que **todavía no están disponibles**. Su
opción ya existe en la firma de `create()` para que la API no cambie cuando
lleguen, y hoy lanza `UnimplementedError` antes de llamar a AWS:

| Función | Opción | Estado |
|---|---|---|
| Dominio propio integrado en el sandbox | `domain=` / `domain` en `Sandbox.create()`, y `get_host()`/`expose()` devolviendo la URL | sin versión comprometida; mientras tanto, [`CustomDomain`](../funciones-opcionales/dominio-propio.md) (experimental) |

Mientras tanto, para exponer un puerto en desarrollo,
[`rayito sandbox proxy`](../funciones-opcionales/proxy-local.md).

## Diseñado, sin código todavía

Partes con un diseño ya aceptado pero sin ninguna línea fusionada en
`main`: no existen en ningún SDK publicado, ni como opción ni como
`UnimplementedError`.

| Función | Estado |
|---|---|
| `AgentTemplate`, `PoolConfig.warmup` y el runtime deepagents | diseño aceptado (`ai-agent-fast-start`, `ai-agent-deepagents`), sin fusionar; ver el [borrador de 0.8.0](0.8.0.md) |

## Versiones anteriores

Las notas de las releases 0.1.0 a 0.5.x están en el
[Changelog](../referencia/changelog.md) y en las
[GitHub Releases](https://github.com/alejandro-cedeno-10/rayito/releases).
