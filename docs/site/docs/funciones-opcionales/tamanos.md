# Tamaños

Un catálogo cerrado de cinco tamaños CPU/RAM (512mb/1gb/2gb/4gb/8gb) que el
SDK resuelve **en cliente, sin ningún RPC**: `size=` decide qué imagen
lanzar (`rayito-base-4gb`), nunca un ajuste del guest en marcha.

!!! info "Coste y activación"
    - **Por defecto**: apagado. Sin `size=`/`size` el SDK no hace ninguna
      llamada nueva a AWS: el comportamiento es exactamente el de 0.5.x.
    - **Activa**: `Sandbox.create(size="4gb")` / `Sandbox.create({ size: "4gb" })`
      (o `SizeRequest(memory_mib=...)`/`{ memoryMib: ... }`); `rayito image
      publish --sizes 512mb,4gb` para publicar las imágenes adicionales;
      `rayito stack deploy sizes-guard` para el guardarraíles IAM opcional.
    - **Recursos y llamadas AWS**: ninguno nuevo para lanzar un sandbox con
      `size=` (el tamaño ya viene en el nombre de la imagen). `get_info()`/
      `getInfo()` hace, como mucho una vez por versión de imagen y por
      proceso (cacheada), una llamada gratuita a `GetMicrovmImageVersion`
      para confirmar `minimumMemoryInMiB`. `rayito image publish --sizes`
      hace los mismos `create`/`update-microvm-image` que una publicación
      normal, uno por tamaño adicional. `rayito image sizes` añade, sólo si
      hay algún tamaño adicional publicado, una `GetMicrovmImageVersion`
      (también gratuita) por imagen publicada para `sameArtifact`; nunca
      lanza ningún sandbox. `sizes-guard` crea una única
      `AWS::IAM::ManagedPolicy`.
    - **Coste aproximado**: $0 por `size=` en sí (una imagen más grande
      cuesta lo mismo por hora que lanzarla sin `size=` con esa misma
      memoria: no hay sobreprecio por el catálogo). Cada tamaño publicado
      añade una semana de storage de snapshot (como cualquier versión de
      imagen, ver [funciones-opcionales/pilas-opcionales.md](pilas-opcionales.md));
      el snapshot crece con el tamaño (desde ≈450 MB a 512 MiB hasta
      ≈1,1 GB a 8192 MiB sobre la misma imagen mínima). `sizes-guard`: $0
      en reposo y por uso (sólo IAM).
    - **IAM**: ninguno adicional para lanzar con `size=`. `sizes-guard`
      (`RayitoRunAllowedSizes`) añade un Deny de `lambda:RunMicrovm` fuera
      de los ARN de imagen que el operador liste (no sólo un Allow: el
      Allow, por sí solo, no restringe nada si la identidad ya tiene el
      `microvm-image:*` de la `CallerPolicy` estándar de `infra/iam.yaml`,
      que es justo el caso típico); adjúntala a la identidad que crea
      sandboxes para impedir que lance un tamaño no publicado, sea cual sea
      el resto de sus permisos.
    - **Cómo apagarla**: no pases `size=`/`size` (por defecto `None`/
      `undefined`); borra la pila `sizes-guard` si la desplegaste (no borra
      ninguna imagen).
    - **Ejemplo**:
      ```python
      from rayito import Sandbox

      with Sandbox.create(size="4gb") as sbx:
          sbx.commands.run("echo hola")
      ```

!!! success "Aceptado en AWS real (2026-10-02)"
    `size=`/`size` (Python sync/async y TypeScript), `rayito image publish
    --sizes`/`--env`, `rayito image sizes` y el componente `sizes-guard` se
    probaron contra AWS real: ver `AWS_API_NOTES.md` §24 (Q106, Q107).

## Cuándo usarlo

- Una carga puntual necesita más RAM/CPU que el baseline (2048 MiB, 4
  vCPU): `size="4gb"` evita publicar y mantener una imagen separada a
  mano.
- Quieres que `Sandbox.create(size="512mb")` siga siendo barato para
  cargas pequeñas sin perder el baseline existente.
- **Cuándo no**: si ya publicas imágenes con nombres propios por tamaño,
  `size=` es azúcar opcional, no una migración obligatoria; seguir
  pasando el nombre completo (`Sandbox.create("mi-imagen-4gb")`) funciona
  igual.

## Ejemplo rápido

Publica el baseline y los tamaños que necesites, desde el mismo artefacto:

```bash
rayito image publish --artifact rayito-image.zip --base-image-version 1 \
  --bucket mi-bucket --sizes 512mb,4gb
```

=== "Python"

    ```python
    from rayito import Sandbox

    with Sandbox.create(size="4gb") as sbx:  # (1)!
        info = sbx.get_info()
        print(info.size, info.baseline_memory_mib, info.baseline_cpu)  # (2)!
        sbx.commands.run("python entrena.py")
    ```

    1. Resuelve `rayito-base` + `4gb` -> `rayito-base-4gb` en cliente; cero
       llamadas nuevas a AWS en `create()`.
    2. `"4gb"`, `4096`, `8`: una única `GetMicrovmImageVersion` cacheada.
       `info.memory_mb` sigue siendo lo que el guest reporta de verdad
       (hasta 4x `baseline_memory_mib`).

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncSandbox


    async def main() -> None:
        async with await AsyncSandbox.create(size="4gb") as sbx:
            info = await sbx.get_info()
            print(info.size, info.baseline_memory_mib, info.baseline_cpu)


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    await using sbx = await Sandbox.create({ size: "4gb" });
    const info = await sbx.getInfo();
    console.log(info.size, info.baselineMemoryMib, info.baselineCpu);
    await sbx.commands.run("python entrena.py");
    ```

Un tamaño que no encaja exacto se redondea siempre hacia arriba, nunca
hacia abajo, y avisa:

=== "Python"

    ```python
    from rayito import Sandbox, SizeRequest

    # RayitoCompatWarning: redondea a 4096 MiB (4gb)
    with Sandbox.create(size=SizeRequest(memory_mib=3000)) as sbx:
        sbx.commands.run("echo hola")
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    // avisa con process.emitWarning(..., { type: "RayitoCompatWarning" })
    await using sbx = await Sandbox.create({ size: { memoryMib: 3000 } });
    await sbx.commands.run("echo hola");
    ```

## Cómo funciona

1. `resolve_size`/`resolveSize` (dominio puro) redondea lo pedido hacia
   arriba al primer valor del catálogo cerrado que lo cubra
   (`SUPPORTED_MEMORY_MIB`: 512/1024/2048/4096/8192 MiB); por encima de
   8192 MiB es `InvalidArgumentException`/`InvalidArgumentError` **antes de
   cualquier llamada a AWS**.
2. `apply_size_suffix`/`applySizeSuffix` antepone el sufijo al nombre de la
   plantilla (`"rayito-base"` + `4gb` -> `"rayito-base-4gb"`); el baseline
   (2048 MiB) nunca lleva sufijo. Un template dado por **ARN** con `size=`
   es `InvalidArgumentException`/`InvalidArgumentError`: la imagen la
   nombra su ARN, no la convención `<variant>[-<size>]`.
3. `create()` resuelve el ARN de la imagen ya sufijada y lanza
   `run-microvm` exactamente como sin `size=`.
4. `get_info()`/`getInfo()`, sólo si se usó `size=`, confirma
   `minimumMemoryInMiB` con `GetMicrovmImageVersion` (cacheada por versión
   de imagen) y rellena `size`/`baseline_memory_mib`/`baseline_cpu`.
   `cpu_count`/`memory_mb` (`cpuCount`/`memoryMb`) siguen siendo lo que el
   guest reporta de verdad vía `Health`: pueden ver hasta 4x
   `baseline_memory_mib` (medido para los cinco tamaños del catálogo).
5. `create(pool=, size=)`/`create({ pool, size })` es
   `InvalidArgumentException`/`InvalidArgumentError`: una plaza del pool ya
   salió de una imagen fija, no puede cambiar de tamaño al tomarla.
6. `size`/`baseline_memory_mib`/`baseline_cpu` sólo aparecen en el handle
   que de verdad llamó a `create(size=...)`: `Sandbox.connect(id)` nunca
   los rellena, aunque la imagen tenga el sufijo de un tamaño (no se
   adivina a partir del nombre a propósito, para no confirmar un tamaño
   que nadie pidió en *este* proceso).

## Guest vs. imagen (lo medido)

| `minimumMemoryInMiB` | vCPU del guest | `MemTotal` del guest | Disco raíz |
|---|---|---|---|
| 512 | 1 | ≈1 989 MiB | 8,3 GB |
| 1024 | 2 | ≈3 998 MiB | 8,3 GB |
| 2048 (baseline) | 4 | ≈8 016 MiB | 8,3 GB |
| 4096 | 8 | ≈16 052 MiB | 16,7 GB |
| 8192 | 16 | ≈32 123 MiB | 33,6 GB |

El guest siempre ve más que `minimumMemoryInMiB` (hasta ≈4x): es el pico
facturado, no la línea base. `baseline_memory_mib`/`baselineMemoryMib` es
el valor declarado de la imagen (lo que facturas); `memory_mb`/`memoryMb`
sigue siendo lo que el guest reporta.

## Publicar imágenes por tamaño

```bash
rayito image publish --artifact rayito-image.zip --base-image-version 1 \
  --bucket mi-bucket --sizes 512mb,4gb \
  --env RAYITO_ALLOWED_MOUNT_BUCKETS=mi-bucket
```

- Sin `--sizes`, sólo se publica el baseline, exactamente como antes de
  esta función.
- Cada tamaño adicional se publica como una imagen con nombre
  `<image-name>-<size>` desde el **mismo artefacto**; `--env KEY=VALUE`
  (repetible) añade variables de imagen (`environmentVariables`) a todas
  las que se publiquen en la invocación, baseline incluido — nunca un
  interruptor de activación del SDK (ADR-014 regla 4), sólo configuración
  horneada en la imagen. `rayito image publish` hornea además
  `RAYITO_BASELINE_MEMORY_MIB` en cada imagen con sufijo. Ninguna de las
  dos llega al entorno de `commands.run` (medido, Q107): son información
  declarada de la imagen, que lees con `GetMicrovmImageVersion`.
  **Nunca pongas secretos en `--env`**: cualquiera con `GetMicrovmImageVersion`
  y todo proceso del guest los leen en claro; usa `SecretStore`/`secrets=`
  para eso.
- Con `--sizes`, `--memory-mib` sólo admite el valor por defecto (2048 MiB):
  la imagen sin sufijo de `--sizes` siempre es el baseline.
- Las construcciones de más de un tamaño se piden todas antes de esperar a
  que ninguna se asiente (una oleada de hasta 10 construcciones
  simultáneas, el límite del servicio), así AWS las construye en paralelo.
- Una versión ya construida con el mismo artefacto y configuración se
  reutiliza, como en una publicación normal. Con `--env` o `--sizes` la
  comparación de variables usa una `GetMicrovmImageVersion` (gratuita) por
  versión candidata, porque `list-microvm-image-versions` no las devuelve
  (Q106); sin ninguno de los dos, la comprobación es la de 0.5.x, sin
  llamadas nuevas, y una versión publicada antes *con* `--env` se
  reutiliza tal cual: usa `--force` para reconstruirla sin variables.
- `rayito image sizes [--variant | --image-name]` lista, por tamaño del catálogo cerrado,
  qué imagen de la variante ya se publicó (o si ninguna) y si comparte
  artefacto con el baseline (`sameArtifact`): siempre una
  `list-microvm-images`, y si hay algún tamaño adicional publicado además
  una `GetMicrovmImageVersion` (sin cuota propia) por imagen ya publicada —
  con sólo el baseline publicado, ninguna llamada adicional. Nunca lanza
  ningún sandbox. Ver [CLI](../cli.md#image-sizes).

## Guardarraíles de coste (`sizes-guard`, opcional)

```bash
rayito stack deploy sizes-guard \
  --param ImageArns=<ARN del baseline>,<ARN de rayito-base-4gb>
```

Crea una política IAM (`RayitoRunAllowedSizes`) con un Allow de
`lambda:RunMicrovm` sobre los ARN listados y, sobre todo, un Deny de
`lambda:RunMicrovm` con `NotResource` para cualquier otro ARN: una
identidad con esta política adjunta no puede lanzar un tamaño que no esté
en la lista, aunque exista, **aunque también tenga** el `microvm-image:*`
de la `CallerPolicy` estándar de `infra/iam.yaml` (un Deny explícito gana
siempre sobre cualquier Allow, venga de la política que venga). `rayito
stack destroy sizes-guard` borra la política; ninguna imagen se toca.

## Errores y solución de problemas

| Python | TypeScript | Cuándo | Qué hacer |
|---|---|---|---|
| `InvalidArgumentException` | `InvalidArgumentError` | `size=` pide más de 8192 MiB, un nombre que no existe en el catálogo, o se combina con un template dado por ARN | usa un nombre del catálogo o `SizeRequest(memory_mib=...)`/`{ memoryMib }`, y pasa el nombre de la imagen, no su ARN |
| `InvalidArgumentException` | `InvalidArgumentError` | `create(pool=, size=)` | fija el tamaño publicando la imagen del `PoolConfig` con ese sufijo, no en `take()` |
| `SandboxNotFoundException` | `SandboxNotFoundError` | `size=` pide un tamaño válido del catálogo que nunca publicaste (`No active version found for MicroVM image ...-8gb`); `RunMicrovm` lo rechaza sin crear ningún MicroVM | publícalo con `rayito image publish --sizes 8gb`, o pide un tamaño que exista (`rayito image sizes`) |
| `RayitoCompatWarning` | aviso de `process.emitWarning` | `size=` no coincide exacto con un valor del catálogo | informativo: el sandbox se lanza igual, redondeado hacia arriba |

## Diferencias con E2B

E2B no tiene un catálogo de tamaños con nombre: cada plantilla declara su
propia CPU/RAM al construirse. `size=` es una conveniencia de Rayito sobre
la convención de nombres `<variant>[-<size>]`; el shim de E2B no expone
ningún kwarg nuevo para esto.

## Ver también

- [Funciones opcionales](../optional-features.md) (fila "sizes-catalog"
  pendiente de `m15-docs-integration`)
- [Pilas opcionales (`rayito stack`)](pilas-opcionales.md)
- [Coste](../cost.md)
- [IAM](../operacion/iam.md)
