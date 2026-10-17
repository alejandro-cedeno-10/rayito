---
title: Varias sesiones y varias VMs
description: Sirve tu agente con varios MicroVMs, reparte las sesiones entre ellos, pon una cola delante del límite y deja plazas calientes, sin servidor propio.
---

# Varias sesiones y varias VMs

<small>Desde 0.14.0.</small>

Un MicroVM con tu agente atiende pocas sesiones a la vez. Cuando necesitas más,
`deploy_agent()` te da una **flota** (`AgentFleet`): varios MicroVMs de la misma
imagen y un reparto de sesiones que hace el cliente, sin ningún servidor de
Rayito en medio. Con los valores por defecto es un solo VM, igual que
[`AgentProject.deploy()`](despliega-tu-agente.md#3-despliegalo-como-servicio).

## Las opciones { #las-opciones }

Cada opción tiene su clave en `rayito-agent.toml`, su argumento en
`ServeOptions` y `ScalingOptions`, y su bandera en `rayito agent deploy`.

| Opción | Qué hace | Por defecto | Límite |
|---|---|---|---|
| `max_concurrency` (`--max-sessions`) | sesiones a la vez en un VM | 1 | las conexiones del tamaño del VM: 7 con 2 GB, 15 con 4 GB, 31 con 8 GB |
| `queue_max` (`--queue`) | peticiones que esperan en la cola del host cuando el VM está lleno | 0 (un 429 al momento) | 256 |
| `queue_timeout_seconds` (`--queue-timeout`) | lo que espera una petición en la cola | 30 s | 900 s |
| `min_vms` (`--min-vms`) | VMs siempre vivos | 1 | |
| `max_vms` (`--max-vms`) | VMs como máximo | 1 | 64 |
| `warm_vms` (`--warm`) | plazas aparcadas (suspendidas) que absorben un pico | 0 | `max_vms - min_vms` |
| `sessions` (`--sessions`) | `shared` (varias sesiones por VM) o `dedicated` (un VM por sesión) | `shared` | |
| `scale_in_after_seconds` | inactividad tras la que un VM por encima de `min_vms` se termina solo | 900 s | 60 s |
| `idle` (`--idle`) | inactividad tras la que un VM se suspende y se reanuda al llamarlo | sin suspensión | ver [Pausar y reanudar](pausar-reanudar.md) |

El límite de `max_concurrency` sale de las conexiones que admite el MicroVM
(8 por vCPU de base) menos una que el servicio se reserva para `/ping`: con 1 GB
se cuenta una vCPU, un valor conservador. Subir la memoria sube el tope.

Lo más corto es el fichero (las claves que no pongas las decide la detección):

```toml
# rayito-agent.toml
[serve]
max_concurrency = 2
queue_max = 4

[scaling]
min_vms = 1
max_vms = 3
warm_vms = 1
sessions = "shared"

[image]
memory_mib = 4096
```

=== "CLI"

    ```bash
    rayito agent deploy ./mi-agente --memory 4096 --max-sessions 2 --queue 4 \
      --min-vms 1 --max-vms 3 --warm 1 --sessions shared
    ```

=== "Python"

    ```python
    from rayito import AgentDeployOptions, ScalingOptions, deploy_agent

    fleet = deploy_agent(
        "./mi-agente",
        options=AgentDeployOptions(
            bucket="amzn-s3-demo-bucket",
            scaling=ScalingOptions(min_vms=1, max_vms=3, warm_vms=1, sessions="shared"),
        ),
    )
    try:
        first = fleet.invoke("Hola")
        again = fleet.invoke("¿Qué te dije?", session_id=first.session_id)
        print(again.text)
    finally:
        fleet.stop()
    ```

=== "TypeScript"

    ```ts
    import { deployAgent, ScalingOptions } from "rayito";

    const fleet = await deployAgent("./mi-agente", {
      options: {
        bucket: "amzn-s3-demo-bucket",
        scaling: new ScalingOptions({ minVms: 1, maxVms: 3, warmVms: 1, sessions: "shared" }),
      },
    });
    try {
      const first = await fleet.invoke("Hola");
      const again = await fleet.invoke("¿Qué te dije?", { sessionId: first.sessionId });
      console.log(again.text);
    } finally {
      await fleet.stop();
    }
    ```

## Sesiones compartidas y dedicadas { #sesiones-compartidas-y-dedicadas }

- **`shared`** (por defecto): una sesión nueva va al VM con hueco; si todos
  están llenos, a una plaza caliente; si no hay, a un VM nuevo mientras no
  llegues a `max_vms`; y si ya estás en `max_vms`, espera en la cola.
- **`dedicated`**: cada sesión nueva tiene su propio MicroVM y un VM nunca
  atiende a dos sesiones. Es lo que quieres si el agente guarda estado en el
  disco del VM o si no te fías de que dos usuarios compartan proceso.

## El id de sesión y volver a una conversación { #el-id-de-sesion }

Con `max_vms > 1`, `AgentResult.session_id` es un identificador que nombra su
VM (`rs1.<sandbox>.<sesión>`): pasarlo de vuelta lleva la llamada al mismo VM sin
consultar a nadie. Con `max_vms = 1` el id de sesión no cambia. Desde otro
proceso, `AgentFleet.connect(nombre, access_token=...)` reconstruye la flota con
los MicroVMs no terminados de su imagen sin reanudar ninguno, y un id de sesión
de la primera llamada sigue llevando a su VM. Para que las respuestas de la
flota reconectada también devuelvan el asa, pasa el mismo `scaling` con el que
la desplegaste.

Un id de sesión que elijas tú (el hilo de un chat) sólo se recuerda dentro del
proceso que desplegó la flota. Para encontrarlo desde otro proceso en modo
`dedicated`, activa el índice opcional de metadatos
([Índice de metadatos](../funciones-opcionales/indice-de-metadatos.md), con su
coste); sin él, un id desconocido empieza un VM nuevo y la respuesta trae el
identificador enrutable que debes guardar.

## La cola y el 429 { #la-cola-y-el-429 }

Con `max_concurrency = 1` y `queue_max = 1`, de tres peticiones simultáneas
una corre, una espera su turno y la tercera recibe `429` con `Retry-After`. El
cliente de la flota respeta ese `Retry-After` hasta `queue_timeout_seconds` y,
pasado el plazo, lanza `CapacityException` (`code = "agent_capacity"`;
`CapacityError` en TypeScript).

## Reducir, suspender y reiniciar { #reducir-suspender-y-reiniciar }

- **Reducir:** los VMs por encima de `min_vms` llevan un plazo de
  `scale_in_after_seconds` que la flota renueva cada vez que los usa; un VM
  ocioso se termina solo (el `rayd` del VM ya sabe terminarse), sin ninguna
  llamada del plano de control.
- **Suspender:** `idle` suspende un VM sin uso y lo reanuda al llamarlo; un VM
  suspendido no cobra cómputo, sólo el almacenamiento de su snapshot.
- **Reiniciar:** si la app del usuario se cae, el host la relanza con espera
  creciente (hasta 3 veces en 5 minutos). Mientras tanto `/ping` dice
  `HealthyBusy` y las invocaciones reciben `503` con `Retry-After`. Pasado el
  presupuesto, `/ping` falla, el marcador de fallo lleva `app_crashloop` y la
  flota sustituye el VM; las llamadas en curso de ese VM fallan con
  `SandboxException(code="agent_vm_lost")`.

!!! info "Coste y activación"
    - **Activa:** `deploy_agent()` con más de un VM posible (`max_vms > 1`) o
      con plazas calientes; con los valores por defecto es un solo VM.
    - **Recursos y llamadas AWS:** `min_vms` MicroVMs en marcha, `warm_vms`
      plazas aparcadas en un pool, y los VMs extra que cree el reparto hasta
      `max_vms`; `list-microvms` para encontrar los miembros de la flota.
    - **Coste aproximado:** cada VM en marcha cuesta lo de un despliegue (≈
      $0,126 por hora con 2 GB, $0,25 con 4 GB); una plaza aparcada sólo paga el
      almacenamiento de su snapshot (≈ $0,011 por día); un VM suspendido, igual.
      Precios de lista, us-east-1, consultados el 2026-10-06:
      [Precios](../cost.md#coste-de-un-agente-desplegado).
    - **IAM:** los de `Sandbox.create()`, `Sandbox.list()`, `Sandbox.connect()` y
      `Sandbox.kill()`; con índice de sesiones, los del
      [índice de metadatos](../funciones-opcionales/indice-de-metadatos.md).
    - **Cómo apagarla:** `fleet.stop()` o `rayito agent stop <nombre>` terminan
      todos los VMs de la flota; sin llamar a `deploy_agent()` no se crea nada,
      y los valores por defecto no dejan plazas ni VMs extra. Por nombre sólo se
      terminan VMs que son de un agente: un `Health` sin token lee las etiquetas
      de los `RUNNING`, y si la imagen tiene alguno que no las lleva, no se
      termina ninguno de los suspendidos (no se pueden sondear sin reanudarlos).
    - **Ejemplo:** `deploy_agent("./mi-agente", options=AgentDeployOptions(scaling=ScalingOptions(max_vms=3)))`.

!!! info "Medido en AWS real"
    En la aceptación de 0.14 (2026-10-16, us-east-1, Claude Haiku 4.5) los quince
    agentes de ejemplo se desplegaron sin tocar su código, en Python y en
    TypeScript, sin opciones y con 4096 MiB, 2 sesiones por VM, cola de 2 y de
    1 a 2 VMs con una caliente. Cinco llamadas a la vez se repartieron entre dos
    VMs; por la URL de un solo VM, el sobrante recibió `429`. Reanudar un VM
    suspendido y hacer la primera llamada tardó entre 1,9 y 4,0 s. Todo el
    ciclo (65 imágenes) costó unos $5 y terminó con el inventario de AWS igual
    que al empezar. Detalle en `AWS_API_NOTES.md` (Q170 a Q172).

## Ver también { #ver-tambien }

- [Despliega tu agente](despliega-tu-agente.md): la guía completa y el fichero de proyecto.
- [Límites](../limits.md): las cuotas y los topes de esta página.
- [Pool](../pool.md): las plazas aparcadas que usa una flota.
