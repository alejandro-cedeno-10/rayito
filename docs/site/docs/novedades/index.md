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

## Disponible como experimental

Funciones que ya puedes usar, apagadas por defecto, pero cuya API puede
cambiar en una minor. Todavía no tienen versión publicada: llegan en la
próxima release (ver `[Unreleased]` en el
[Changelog](../referencia/changelog.md)) o ya están en `main`.

| Función | Cómo se usa | Qué falta |
|---|---|---|
| [Volúmenes EFS](../funciones-opcionales/volumenes-efs.md) | `volumes=` / `volumes`, sobre la imagen opcional `rayito-base-caps-efs` (`rayito image publish --with-efs`) | aceptado en AWS real; experimental por la imagen opcional y la VPC |
| [Dominio propio](../funciones-opcionales/dominio-propio.md) | `CustomDomain` (`rayito domain deploy`) y `register()` de cada ruta | sin verificar de punta a punta en AWS real (crear la distribución y servir tráfico); `domain=` en `Sandbox.create()` sin cablear |

Si pruebas una, cuéntanos cómo te fue en un
[issue de GitHub](https://github.com/alejandro-cedeno-10/rayito/issues/new/choose).

## En desarrollo

Partes con un cambio pendiente que **todavía no están disponibles**. Su
opción ya existe en la firma de `create()` para que la API no cambie cuando
lleguen, y hoy lanza `UnimplementedError` antes de llamar a AWS:

| Función | Opción | Estado |
|---|---|---|
| Dominio propio integrado en el sandbox | `domain=` / `domain` en `Sandbox.create()`, y `get_host()`/`expose()` devolviendo la URL | sin versión comprometida; mientras tanto, [`CustomDomain`](../funciones-opcionales/dominio-propio.md) |

Mientras tanto: para datos compartidos entre sandboxes,
[montajes S3](../funciones-opcionales/montajes-s3.md) o
[persistencia en S3](../persistence.md); para exponer un puerto en
desarrollo, [`rayito sandbox proxy`](../funciones-opcionales/proxy-local.md).

## Versiones anteriores

Las notas de las releases 0.1.0 a 0.5.x están en el
[Changelog](../referencia/changelog.md) y en las
[GitHub Releases](https://github.com/alejandro-cedeno-10/rayito/releases).
