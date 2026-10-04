---
title: Novedades
description: Qué trae cada versión de Rayito, cómo actualizar y qué está en desarrollo.
---

# Novedades

Una página por versión con lo que cambia para ti: funciones nuevas con su
ejemplo, correcciones que cambian un comportamiento y los pasos para
actualizar. El detalle completo, cambio a cambio, está en el
[Changelog](../referencia/changelog.md) de cada componente.

<div class="grid cards" markdown>

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

    print(rayito.__version__)  # "0.6.1"
    ```

=== "TypeScript"

    ```ts
    import { VERSION } from "rayito";

    console.log(VERSION); // "0.6.1"
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
corre dentro del sandbox (por ejemplo `mounts=`, `events=` o `gateways=`,
o el reaper de 0.6.1): [Imágenes](../images.md#publicar-las-tres). Sin
republicar, el SDK nuevo sigue funcionando con tu imagen actual, y cada
función que necesita el `rayd` nuevo falla cerrada con
`UnimplementedError` y termina el sandbox que acaba de lanzar.

## En desarrollo

Funciones con un cambio OpenSpec abierto que **todavía no están
disponibles**. Sus opciones ya existen en la firma de `create()` para que la
API no cambie cuando lleguen, y hoy lanzan `UnimplementedError` ("todavía no
disponible") antes de llamar a AWS:

| Función | Opción | Estado |
|---|---|---|
| [Volúmenes EFS](../funciones-opcionales/volumenes-efs.md) | `volumes=` / `volumes` | en desarrollo (`m15-efs-volumes`), sin versión comprometida |
| [Dominio propio](../funciones-opcionales/dominio-propio.md) | `domain=` / `domain` | en desarrollo (`m15-custom-domain`), sin versión comprometida |

Mientras tanto: para datos compartidos entre sandboxes,
[montajes S3](../funciones-opcionales/montajes-s3.md) o
[persistencia en S3](../persistence.md); para exponer un puerto en
desarrollo, [`rayito sandbox proxy`](../funciones-opcionales/proxy-local.md).

## Versiones anteriores

Las notas de las releases 0.1.0 a 0.5.x están en el
[Changelog](../referencia/changelog.md) y en las
[GitHub Releases](https://github.com/alejandro-cedeno-10/rayito/releases).
