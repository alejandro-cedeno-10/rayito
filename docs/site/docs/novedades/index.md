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

-   :material-tag:{ .lg .middle } **0.10.0** · 2026-10-08 · actual

    ---

    El agente puede usar OpenAI, Gemini, Azure OpenAI, OpenRouter, Groq,
    Mistral, DeepSeek, xAI o tu proxy de LiteLLM; el timeout y `abort()`
    paran todo lo que lanzó; y deepagents funciona fuera de Bedrock y
    cuenta sus tokens. Sin cambios que rompan.

    [:octicons-arrow-right-24: Novedades de 0.10.0](0.10.0.md)

-   :material-tag:{ .lg .middle } **0.9.1** · 2026-10-07

    ---

    `Sandbox.create()` con un `timeout` de 300 s o menos y sin `idle`
    vuelve a funcionar: la auto-suspensión por defecto se adapta al plazo.
    Sin cambios de API.

    [:octicons-arrow-right-24: Novedades de 0.9.1](0.9.1.md)

-   :material-tag:{ .lg .middle } **0.9.0** · 2026-10-07

    ---

    Limpieza y correcciones: se retira la opción D del agente y las
    excepciones de la pasarela que nada lanzaba, `connect()` recupera
    `sbx.gateways` desde otro proceso y el SDK de Python honra
    `AWS_REGION`. Cambios que rompen, con su migración.

    [:octicons-arrow-right-24: Novedades de 0.9.0](0.9.0.md)

-   :material-tag:{ .lg .middle } **0.8.0** · 2026-10-07

    ---

    Un agente de IA dentro del sandbox (`sbx.agent` con OpenCode o
    deepagents), arranque rápido con `AgentTemplate` y el calentamiento
    del pool, presets de pasarela para modelos y la página de precios.
    Un cambio que rompe en `register_webhook`.

    [:octicons-arrow-right-24: Novedades de 0.8.0](0.8.0.md)

-   :material-tag:{ .lg .middle } **0.7.1** · 2026-10-06

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

    print(rayito.__version__)  # "0.10.0"
    ```

=== "TypeScript"

    ```ts
    import { VERSION } from "rayito";

    console.log(VERSION); // "0.10.0"
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
`gateways=`, el endurecimiento de `rayd` 0.7.0, los avisos de licencia
de `rayd` 0.7.1 en `/usr/share/doc/rayd/` o el timeout del agente que
para todo su árbol de procesos de `rayd` 0.10.0):
[Imágenes](../images.md#publicar-las-tres). Desde 0.10, `rayito doctor`
exige el `rayd` del tag `rayd-v0.10.0`, y el agente de IA (`sbx.agent`)
necesita además una imagen con su runtime, construida con
[`AgentTemplate`](../funciones-opcionales/templates-de-agente.md)
(reconstrúyela en 0.10.0: lleva las correcciones de deepagents). Sin
republicar, el SDK nuevo sigue funcionando con tu imagen actual, y cada
función que necesita el `rayd` nuevo falla cerrada con
`UnimplementedError` y termina el sandbox que acaba de lanzar. Al pasar de
0.9.x a 0.10.0 no hay cambios que rompan
([Cómo actualizar desde 0.9.x](0.10.0.md#como-actualizar-desde-09x)); de
0.8.x a 0.9.0, revisa los
[cambios que rompen](0.9.0.md#como-actualizar-desde-08x) (opción D del
agente, `GatewayException`/`GatewayError` y, en Python, `AWS_REGION`); de
0.7.x a 0.8.0, el
[cambio que rompe en `register_webhook`](0.8.0.md#como-actualizar-desde-07x);
de 0.6.x a 0.7.0, los
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

Ninguna por ahora: `AgentTemplate`, `PoolConfig.warmup` y el runtime
deepagents se publicaron en [0.8.0](0.8.0.md).

## Versiones anteriores

Las notas de las releases 0.1.0 a 0.5.x están en el
[Changelog](../referencia/changelog.md) y en las
[GitHub Releases](https://github.com/alejandro-cedeno-10/rayito/releases).
