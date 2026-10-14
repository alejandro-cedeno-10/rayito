# Panel de gestión: cuotas, coste y flota

Los servicios del SDK que alimentan el panel de gestión de MicroVMs
(ADR-026 en [`ARCHITECTURE.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/ARCHITECTURE.md)): `Quotas` (cuotas aplicadas de Lambda MicroVMs y
memoria asignada contra la cuota de la región), `UsageCost` (coste facturado
por Cost Explorer y coste estimado por sandbox) y `Fleet` (sandboxes,
imágenes, eventos y acciones masivas). Funcionan solos, sin ningún servidor:
lees tus propios datos con tus credenciales.

!!! note "Qué hay y qué llega después"
    Esta página cubre los **servicios del SDK** (Python síncrono y asíncrono,
    y TypeScript, con la misma superficie). El panel web local y la pila
    opcional `dashboard` se construyen encima de ellos en las fases
    siguientes del cambio OpenSpec `dashboard`; todavía no existen.

!!! info "Coste y activación"
    - **Por defecto**: apagado. Construir `Quotas`, `UsageCost` o `Fleet` no
      llama a AWS, no crea ningún cliente ni importa ningún peer opcional.
      Sin llamar a un método no pasa nada, y un test en cada SDK lo
      comprueba.
    - **Activa**: una llamada explícita: `Quotas.report()`,
      `UsageCost.billed()` (la única que usa Cost Explorer; `estimate()` no),
      o cualquier método de `Fleet`. Las acciones masivas
      (`Fleet.run_bulk`/`runBulk`) exigen además `allow_actions=True`/
      `allowActions: true`: por defecto el servicio es de sólo lectura.
    - **Recursos y llamadas AWS**: sólo lecturas, ningún recurso nuevo.
      `ListServiceQuotas` (código `lambda`), `ListMicrovms`,
      `GetMicrovmImageVersion` (como mucho una por versión de imagen y
      proceso), `ListMicrovmImages` y `ListMicrovmImageVersions`; con
      `billed()`, `ce:GetCostAndUsage` en us-east-1; con acciones,
      `TerminateMicrovm`, `SuspendMicrovm` y `ResumeMicrovm` sobre los
      sandboxes que indiques. En TypeScript, los clientes de Service Quotas
      y Cost Explorer son peers opcionales que se importan en la primera
      llamada. Detalle: `AWS_API_NOTES.md` §30.
    - **Coste aproximado** (us-east-1, consultado 2026-10-13): $0 las
      lecturas de Lambda MicroVMs (`sandboxes`, `images`, `image_versions`),
      `estimate()` y `Quotas.report()`; Service Quotas no publica ninguna
      tarifa por consulta ([guía](https://docs.aws.amazon.com/servicequotas/latest/userguide/intro.html)),
      y `plan_bulk` es un cálculo local. **De pago**: `UsageCost.billed()`
      cobra **$0,01 por petición** a Cost Explorer
      ([precios](https://aws.amazon.com/aws-cost-management/aws-cost-explorer/pricing/)),
      una por ventana (una por página si hubiera más, y `DAILY` sobre
      ventanas largas pagina) en cada fallo de caché, y `refresh=True`
      siempre paga. La caché está en memoria, es de cada instancia (se pierde
      al terminar el proceso o al crear otra `UsageCost`), guarda como mucho
      64 ventanas y se indexa por inicio, fin, granularidad y región:
      reutiliza una sola instancia y un mes ronda las 30 peticiones, ≈
      $0,30; con una instancia nueva por llamada (cron, Lambda, script)
      cada `billed()` paga $0,01. `Fleet.events()` también es de pago,
      aunque mínimo: lecturas bajo demanda de DynamoDB sobre la tabla de
      `events-webhooks`, sin sondeo. Las acciones no se facturan por la
      llamada, pero cambian la factura (`AWS_API_NOTES.md` §12): `kill` deja
      de facturar cómputo; `suspend` escribe un snapshot ($0,0038/GB) y paga
      almacenamiento ($0,08/GB-mes) mientras esté suspendido; `resume` lee el
      snapshot ($0,00155/GB) y **vuelve a facturar cómputo** al precio de su
      tamaño ($0,1261/h con 2 GB y 1 vCPU), así que reanudar muchos sandboxes
      a la vez sube la factura.
    - **IAM**: `servicequotas:ListServiceQuotas`, `lambda:ListMicrovms`,
      `lambda:GetMicrovmImageVersion`, `lambda:ListMicrovmImages` y
      `lambda:ListMicrovmImageVersions` para leer; `ce:GetCostAndUsage` sólo
      para `billed()`; `lambda:TerminateMicrovm`, `lambda:SuspendMicrovm` y
      `lambda:ResumeMicrovm` sólo para las acciones. Ver
      [IAM mínimo](#iam-minimo).
    - **Cómo apagarla**: no llames a los métodos (no queda nada que borrar).
      Para quitar Cost Explorer basta con no llamar a `billed()`; para
      impedir las acciones, no pases `allow_actions=True` y no concedas los
      tres permisos de escritura.
    - **Ejemplo**:
      ```python
      from rayito import Quotas

      report = Quotas(region="us-east-1").report()
      print(report.memory.level)
      ```

## Cuándo usarlo

- Quieres saber cuánta memoria de tu cuota regional ocupan los sandboxes
  `RUNNING` y suspendidos, y que algo te avise antes de topar con el límite.
- Quieres ver el coste de MicroVMs de la cuenta por tipo de uso, o una
  estimación por sandbox, sin salir del código.
- Operas una flota y necesitas listar sandboxes con la memoria de su imagen,
  imágenes y versiones, o suspender y matar en bloque respetando los TPS.
- **Cuándo no**: para facturar o repartir costes. El coste por sandbox es una
  estimación y nunca sustituye a la factura; para eso, Cost Explorer o el
  informe de costes y uso de tu cuenta.

## Cuotas y memoria asignada

`Quotas.report()` junta tres cosas: las cuotas aplicadas de la región
(Service Quotas), los sandboxes `RUNNING`, `SUSPENDING` y `SUSPENDED` de
`ListMicrovms` con la memoria mínima de su imagen, y los avisos.

=== "Python"

    ```python
    from rayito import Quotas

    report = Quotas(region="us-east-1").report()
    memory = report.memory
    print(memory.allocated_gb, memory.quota_gb, memory.level)  # (1)!
    for warning in report.warnings:
        print(warning.kind, warning.message, warning.increase_url)  # (2)!
    print(report.applied_rate("SuspendMicrovm"))  # (3)!
    ```

    1. `level` es `ok`, `warning` (desde el 80 % de la cuota) o `critical`
       (desde el 95 %); los umbrales son constantes de `limits.json`.
    2. Cada aviso trae el enlace de la consola de Service Quotas para pedir
       el aumento: pedirlo no es una acción del SDK.
    3. La tasa de TPS aplicada de una operación (2 para `SuspendMicrovm` por
       defecto), o `None` si Service Quotas no la lista.

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncQuotas


    async def main() -> None:
        report = await AsyncQuotas(region="us-east-1").report()
        print(report.memory.level, len(report.warnings))


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { Quotas } from "rayito";

    const report = await new Quotas({ region: "us-east-1" }).report();
    const { memory } = report;
    console.log(memory.allocatedGb, memory.quotaGb, memory.level);
    for (const warning of report.warnings) {
      console.log(warning.kind, warning.message, warning.increaseUrl);
    }
    console.log(report.appliedRate("SuspendMicrovm"));
    ```

| Campo | Qué es |
|---|---|
| `memory` | memoria asignada (GB) contra la cuota (`ARM_64`), la fracción, el nivel, los sandboxes contados y cuántos tienen la memoria de imagen sin resolver (`unresolved`: el uso real puede ser mayor) |
| `entries` | las 18 cuotas del catálogo con su valor aplicado (`None`/`undefined` si Service Quotas no la lista), el valor por defecto de la región y el enlace de aumento |
| `tps` | tasa y ráfaga aplicadas de cada operación |
| `warnings` | `memory_high`, `memory_critical`, `below_default` (las cuentas nuevas parten de cuotas reducidas) y `not_listed` |

!!! warning "Pendiente de medir en AWS real"
    Se está comprobando en una cuenta real si la cuota de memoria cuenta la
    memoria mínima de la imagen (lo que suma el SDK) o la que ve el guest, y
    si Service Quotas publica la utilización de estas cuotas. Hasta entonces
    el uso de memoria es el calculado por el SDK. Los TPS de `ListMicrovms` y
    `ListMicrovmImages` no están documentados por AWS. Detalle:
    `AWS_API_NOTES.md` §30.

## Coste facturado y coste estimado

Son dos cosas distintas y cada resultado lleva su rótulo.

| | `billed()` | `estimate()` |
|---|---|---|
| Rótulo | `billed` | `estimated` |
| De dónde sale | Cost Explorer, agrupado por tipo de uso (`Lambda-MicroVM-*`) | tiempo transcurrido desde el arranque de cada sandbox `RUNNING` por el precio de cómputo baseline |
| Retraso | unas 24 h; `partial` avisa de periodos aún estimados por AWS | ninguno |
| Cuesta | $0,01 por petición, con caché local de 24 h | $0 |
| Qué no incluye | otros servicios | snapshots (lectura, escritura, almacenamiento), consumo sobre el baseline, y el tiempo activo de los suspendidos |

=== "Python"

    ```python
    from rayito import UsageCost

    cost = UsageCost()
    billed = cost.billed()  # (1)!
    print(billed.total_usd, billed.partial, billed.requests)
    for line in billed.lines:
        print(line.component, line.quantity, line.unit, line.cost_usd)

    estimate = cost.estimate()  # (2)!
    print(estimate.label, estimate.total_usd)
    ```

    1. Por defecto, del día 1 del mes en curso (UTC) a mañana. Acepta
       `start`, `end` (`datetime.date` o `AAAA-MM-DD`), `granularity`
       (`MONTHLY` por defecto), `region` y `refresh=True` para saltarse la
       caché.
    2. La estimación nunca llama a Cost Explorer.

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncUsageCost


    async def main() -> None:
        cost = AsyncUsageCost()
        billed = await cost.billed(start="2026-10-01", end="2026-10-13")
        print(billed.total_usd, billed.label)
        print((await cost.estimate()).label)


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { UsageCost } from "rayito";

    const cost = new UsageCost();
    const billed = await cost.billed({ start: "2026-10-01", end: "2026-10-13" });
    console.log(billed.totalUsd, billed.partial, billed.requests);
    for (const line of billed.lines) {
      console.log(line.component, line.quantity, line.unit, line.costUsd);
    }
    const estimate = await cost.estimate();
    console.log(estimate.label, estimate.totalUsd);
    ```

`estimate().items` lleva un elemento por sandbox contado; los suspendidos
y los de memoria desconocida salen con `cost_usd`/`costUsd` vacío. En
TypeScript, `billed()` necesita el paquete opcional
`@aws-sdk/client-cost-explorer`, y `Quotas.report()`
`@aws-sdk/client-service-quotas`; sin ellos el error nombra el paquete a
instalar.

## Sandboxes, imágenes, eventos y acciones

`Fleet` es la vista de operación: el inventario con la memoria de cada
imagen, el catálogo de imágenes y sus versiones, el historial de eventos y
las acciones masivas.

=== "Python"

    ```python
    from rayito import Fleet, Quotas

    fleet = Fleet(region="us-east-1", allow_actions=True)  # (1)!
    running = fleet.sandboxes(states={"RUNNING"})
    print([(view.sandbox_id, view.memory_mib) for view in running])

    rate = Quotas(region="us-east-1").report().applied_rate("SuspendMicrovm")
    plan = fleet.plan_bulk("suspend", len(running), applied_rate=rate)  # (2)!
    print(plan.tps, plan.estimated_seconds)

    result = fleet.run_bulk(
        "suspend",
        [view.sandbox_id for view in running],
        applied_rate=rate,
        on_progress=lambda progress: print(progress.done, progress.total),
    )
    print(result.count("done"), result.count("failed"))
    ```

    1. Sin `allow_actions=True`, `run_bulk` lanza `InvalidArgumentException`
       antes de llamar a nadie.
    2. La cola va a la menor de las dos tasas: la de `apiTps`
       (`SuspendMicrovm`: 2 TPS) y la cuota aplicada de tu cuenta.

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncFleet


    async def main() -> None:
        fleet = AsyncFleet(region="us-east-1")
        images = await fleet.images()
        for image in images:
            versions = await fleet.image_versions(image.name)
            print(image.name, [version.image_version for version in versions])


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { Fleet, Quotas } from "rayito";

    const fleet = new Fleet({ region: "us-east-1", allowActions: true });
    const running = await fleet.sandboxes({ states: ["RUNNING"] });
    console.log(running.map((view) => [view.sandboxId, view.memoryMib]));

    const rate = (await new Quotas({ region: "us-east-1" }).report()).appliedRate("SuspendMicrovm");
    const result = await fleet.runBulk(
      "suspend",
      running.map((view) => view.sandboxId),
      {
        appliedRate: rate,
        onProgress: (progress) => console.log(progress.done, progress.total),
      },
    );
    console.log(result.count("done"), result.count("failed"));
    ```

- **Inventario**: `ListMicrovms` sólo trae el id, el estado, la imagen, la
  versión y `startedAt` (sin etiquetas ni filtros por metadatos). La vista
  añade la memoria mínima de la imagen. Nunca lleva `envs`, el
  `runHookPayload`, tokens ni el endpoint.
- **Imágenes**: `images()` y `image_versions()`/`imageVersions()` (la
  versión más nueva primero). De cada artefacto sólo se conserva el nombre
  del fichero, nunca el bucket ni la ruta.
- **Eventos**: `events()` lee el historial de la pila
  [`events-webhooks`](eventos-y-webhooks.md) si se pasa
  `events=LifecycleEvents(...)`; sin ella lanza
  `InvalidArgumentException`, porque el SDK no despliega nada por su cuenta.
- **Acciones masivas**: matar (`kill`), suspender (`suspend`) y reanudar
  (`resume`), de uno en uno y al ritmo de la cuota de TPS de su operación.
  Un fallo de un sandbox no detiene a los demás: queda `failed` con el
  **código** de AWS (nunca su mensaje): `ServiceQuotaExceededException` o
  `InsufficientCapacityException` si la cuenta se queda sin cuota o sin
  capacidad, y `UnknownError` si el error no trae código. Una cuota
  aplicada no positiva en `applied_rate` es `InvalidArgumentException`. Una acción sobre un sandbox que ya
  no necesita nada (por ejemplo, suspender uno que no está `RUNNING`) queda
  `unchanged`. Un id repetido se aplica una sola vez.

## IAM mínimo { #iam-minimo }

Concede sólo lo que uses.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadFleet",
      "Effect": "Allow",
      "Action": [
        "servicequotas:ListServiceQuotas",
        "lambda:ListMicrovms",
        "lambda:ListMicrovmImages",
        "lambda:ListMicrovmImageVersions",
        "lambda:GetMicrovmImageVersion"
      ],
      "Resource": "*"
    },
    {
      "Sid": "BilledCost",
      "Effect": "Allow",
      "Action": "ce:GetCostAndUsage",
      "Resource": "*"
    },
    {
      "Sid": "FleetActions",
      "Effect": "Allow",
      "Action": [
        "lambda:TerminateMicrovm",
        "lambda:SuspendMicrovm",
        "lambda:ResumeMicrovm"
      ],
      "Resource": "*"
    }
  ]
}
```

## Errores

Los errores se traducen por **código** de AWS y el mensaje sólo lo lleva a
él: el mensaje de AWS puede nombrar recursos de tu cuenta.

| Código de AWS | Excepción (Python / TypeScript) | Qué hacer |
|---|---|---|
| `AccessDeniedException` | `AuthenticationException` / `AuthenticationError` | falta el permiso de la sección anterior que corresponda a la llamada |
| `TooManyRequestsException`, `LimitExceededException`, `ThrottlingException` | `RateLimitException` / `RateLimitError` | reintenta más tarde; con Cost Explorer, usa la caché en vez de `refresh=True` |
| cualquier otro (por ejemplo `DataUnavailableException`, si Cost Explorer no está activado en la cuenta) | `DashboardException` / `DashboardError`, con `aws_code`/`awsCode` | depende del código |

## Ver también

- [Funciones opcionales y su coste](../optional-features.md)
- [Límites](../limits.md): cuotas y TPS de Lambda MicroVMs
- [Precios](../cost.md): de dónde salen los precios del cómputo
- [Eventos y webhooks](eventos-y-webhooks.md)
- [Referencia Python: opcionales](../referencia/python/opcionales.md#panel-de-gestion) y
  [Referencia TypeScript](../referencia/typescript.md#opcionales)
