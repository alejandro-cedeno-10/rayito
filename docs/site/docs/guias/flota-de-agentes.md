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
| `scale_in_after_seconds` (`--scale-in-after`, desde 0.17) | inactividad tras la que un VM por encima de `min_vms` se termina solo | 900 s | 60 s |
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

Desde 0.17, una flota reconectada sólo con su nombre y el token también puede
crear VMs y arrancar plazas calientes: lee la [receta](#la-receta-de-la-flota)
que guardó `deploy_agent()`. Sin receta (una flota de 0.16, o desplegada con
`--no-save-recipe`) y sin `project=`, la flota enruta y consulta pero no puede
crear un VM: la primera sesión que necesite uno lanza
`InvalidArgumentException`; pasa entonces el proyecto
(`AgentProject.detect(carpeta)` o `AgentProject.load(...)`).

Un id de sesión que elijas tú (el hilo de un chat) sólo se recuerda dentro del
proceso que desplegó la flota. Para encontrarlo desde otro proceso en modo
`dedicated`, activa el índice opcional de metadatos
([Índice de metadatos](../funciones-opcionales/indice-de-metadatos.md), con su
coste); sin él, un id desconocido empieza un VM nuevo y la respuesta trae el
identificador enrutable que debes guardar.

## La receta de la flota { #la-receta-de-la-flota }

<small>Desde 0.17.0.</small>

Tras arrancar la flota, `deploy_agent()` (y `rayito agent deploy`) guarda su
**receta** en Parameter Store, en `/rayito/agents/<nombre>/recipe`: el proyecto
tal como quedó tras las banderas (`--env`, `--secret-env-from`, `--allow-out`,
las pasarelas), la versión de la imagen, los presets de las pasarelas, el
escalado y si se renuevan las claves. Sólo lleva **nombres** de secretos, nunca
sus valores ni el access token.

Con ella, `AgentFleet.connect(nombre, access_token=...)` desde cualquier
proceso (o `rayito agent invoke|status|stop <nombre>`) crea VMs iguales a los
del despliegue, abre las plazas calientes con sus pasarelas y `status()` da el
escalado real. Un `scaling=` o `project=` explícitos ganan a la receta, y
`use_recipe=False` (`useRecipe: false`) la ignora: la flota es la de 0.16.

=== "Python"

    ```python
    from rayito import AgentFleet

    fleet = AgentFleet.connect("mi-agente", access_token="<token-de-la-flota>")
    status = fleet.status()
    print(status.scaling_source, status.min_vms, status.max_vms)
    fleet.invoke("Hola")
    ```

=== "TypeScript"

    ```ts
    import { AgentFleet } from "rayito";

    const fleet = await AgentFleet.connect("mi-agente", { accessToken: "<token-de-la-flota>" });
    const status = await fleet.status();
    console.log(status.scalingSource, status.minVms, status.maxVms);
    await fleet.invoke("Hola");
    ```

- **Estado.** `FleetStatus.scaling_source` (`scalingSource`) dice de dónde
  sale el escalado: `explicit`, `project`, `recipe` o `default`. Con `default`
  (sin proyecto ni receta) `rayito agent status` imprime
  `members: N (escalado desconocido: sin proyecto ni receta)` en vez de un
  `min 1, max 1` que no describe la flota.
- **Borrado.** `fleet.stop()` y `rayito agent stop <nombre>` borran la receta
  si este proceso la guardó o la leyó; `stop(keep_recipe=True)`
  (`--keep-recipe`) la conserva.
- **Avisos.** Un fallo al guardarla nunca hace fallar el despliegue: deja un
  aviso con un código (`recipe_access_denied`, `recipe_not_saved`,
  `recipe_too_large`) en el log y en `warnings` de `--json`. Al leerla, sin
  permiso es `recipe_access_denied`, y una receta ilegible o un Parameter
  Store que no responde (sin endpoint de VPC para SSM, limitación de tasa) es
  `recipe_unreadable`. Una receta que pedía renovar la clave de Bedrock no
  la activa en quien conecta: sin `renew_gateway_keys=True` el aviso es
  `recipe_renewal_ignored` y no se renueva nada. Un nombre de flota `.` o
  `..` no tiene receta (`recipe_not_saved`); en todos los casos la flota sigue como en 0.16, y
  `rayito agent stop <nombre>` termina sus VMs (si tampoco puede borrar la
  receta, lo dice al final con un error). Tras
  `stop`, Parameter Store tarda unos 30 s en dejar crear otro parámetro con el
  mismo nombre: un `deploy` inmediato avisa `recipe_not_saved`.
- **Tamaño.** Una receta va en JSON si cabe en 4 096 bytes (un parámetro
  estándar) y, si no, comprimida; si ni así cabe (instrucciones muy largas), el
  aviso es `recipe_too_large` y se borra la anterior.
- **`--env` y los secretos.** `rayito agent deploy` rechaza un `--env` cuyo
  nombre parece un secreto (`*_TOKEN`, `*_KEY`, `*_SECRET`, `*_PASSWORD`...,
  con un segmento `PASS`, `PASSWD`, `PAT`, `CREDENTIAL` o `CREDENTIALS`, como
  `DB_PASS` o `GITHUB_PAT`, o con `AUTH` o `BEARER` como último segmento o
  junto a `TOKEN`, `KEY` o `HEADER`, como `X_AUTH` o `AUTH_HEADER`; `AUTH_MODE`
  o `AUTHOR_NAME` pasan):
  usa `--secret-env NOMBRE=<secreto>` o `--secret-env-from <secreto>`, así la
  receta sólo guarda el nombre. Es un cambio incompatible con 0.16 para
  `*_SECRET`. Ojo: la regla es de la bandera `--env`; los valores de `env`
  del proyecto (`[env]` de `rayito-agent.toml` o `env=` de la biblioteca) se
  guardan tal cual en la receta, legibles por quien tenga
  `ssm:GetParameter` sobre `parameter/rayito/agents/*`. Nada secreto en
  `env`, tampoco una URL con credenciales: va en `secret_env`.
- **Quién puede escribirla.** La receta decide la imagen, la salida a red,
  `env` y el upstream y los secretos de cada pasarela de todo proceso que
  conecta por nombre: escribir en `parameter/rayito/agents/*` equivale a
  poder desplegar. Por eso `SandboxLauncherPolicy` sólo la lee y la borra, y
  `ssm:PutParameter` está en `ImagePublisherPolicy` y `CallerPolicy`.

!!! info "Coste y activación"
    - **Activa:** `deploy_agent()` / `rayito agent deploy` contra MicroVMs, por
      defecto. Es la única función de Rayito encendida por defecto (ADR-034):
      el parámetro estándar no cuesta y su petición de KMS entra en la capa
      gratuita.
    - **Recursos y llamadas AWS:** un parámetro `SecureString` estándar por
      flota (`PutParameter` al desplegar, `GetParameter` al reconectar sin
      proyecto, `DeleteParameter` al parar), cifrado con la clave gestionada
      `aws/ssm`.
    - **Coste aproximado:** los parámetros estándar no tienen cargo; cada
      escritura y cada lectura es una petición de KMS (20 000 gratis al mes,
      después $0,03 por 10 000). Consultado el 2026-10-22:
      [Systems Manager](https://aws.amazon.com/systems-manager/pricing/) y
      [KMS](https://aws.amazon.com/kms/pricing/).
    - **IAM:** `ssm:PutParameter` (quien despliega: `CallerPolicy` o
      `ImagePublisherPolicy`),
      `ssm:GetParameter` y `ssm:DeleteParameter` sobre
      `parameter/rayito/agents/*` (la sentencia `AgentRecipe` de
      [IAM](../operacion/iam.md)); ningún permiso de KMS.
    - **Cómo apagarla:** `AgentDeployOptions(save_recipe=False)`
      (`saveRecipe: false`, `--no-save-recipe`).
    - **Ejemplo:** `rayito agent deploy ./mi-agente --no-save-recipe`.

## Plazas calientes y pasarelas { #plazas-calientes-y-pasarelas }

Una plaza aparcada nace sin las pasarelas del proyecto. Desde 0.17, la flota
las abre al tomarla, también cuando la plaza la aparcó otro proceso (una flota
reconectada con la receta o con `project=`), con el mismo camino que
`SandboxPool.take(gateways=)`; si no puede abrirlas, termina la plaza y usa
otro VM. Antes, una flota reconectada podía arrancar el agente sin la pasarela
del modelo y había que desplegar con `--warm 0`.

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
  llamada del plano de control. Mientras atiende alguna invocación, el plazo
  nunca es menor que el de la petición, y sólo vuelve a
  `scale_in_after_seconds` cuando termina la última, también si falla. Los
  cambios de plazo de cada VM van en orden: soltar una invocación nunca acorta
  el de otra que empezó entre medias. Un VM acortado que pasa a ser base
  recupera toda su vida hasta el tope de `rayd`, aunque la flota no lo
  conozca (pide el máximo y `rayd` responde con su tope); desde 0.17.1. Para que `rayd` acepte ese plazo, una
  flota que puede crecer (`max_vms > min_vms`) crea todos sus VMs y plazas
  con plazo lógico (`on_timeout="kill"` y `max_lifetime` =
  `serve.timeout_seconds + 60`); un VM base sigue viviendo
  `serve.timeout_seconds`. Una flota de tamaño fijo crea sus VMs como
  siempre. Hasta 0.16 la reducción no ocurría: los VMs se creaban sin plazo
  lógico y `rayd` rechazaba el `set_timeout`. Un VM creado así (por 0.16)
  no se reduce: la flota avisa una vez; termínalo con `stop()` o vuelve a
  desplegar. Mientras un VM se termina, la plataforma aún lo lista como
  `RUNNING` aunque su `rayd` ya no responda: al listar la flota
  (`AgentFleet.connect()`, `status()`, `refresh_members()`), un VM cuyo
  `Health` falla o que deja de estar `RUNNING` al sondearlo se trata como un
  VM que termina (no es miembro ni cuenta para el cupo, y se avisa con su id)
  en vez de hacer fallar la llamada; desde 0.17.1. Los VMs `RUNNING` se
  sondean hasta 8 a la vez (`AGENT_FLEET_PROBE_CONCURRENCY`), con los
  resultados y los avisos en el orden del listado.
- **Versión de la imagen:** los VMs que crea una flota usan la versión que
  construyó o reutilizó el despliegue, o la de la receta. Una flota
  reconectada sin receta (`AgentFleet.connect(nombre, project=...)`) usa la
  del miembro más reciente, nunca la más reciente de la imagen, que puede
  ser otro build; desde 0.17.1.
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
      todos los VMs de la flota (y borran su receta); sin llamar a `deploy_agent()` no se crea nada,
      y los valores por defecto no dejan plazas ni VMs extra. Por nombre sólo se
      terminan VMs que son de un agente: un `Health` sin token lee las etiquetas
      de los `RUNNING`, y si la imagen tiene alguno que no las lleva, no se
      termina ninguno de los suspendidos (no se pueden sondear sin reanudarlos).
      Si la sonda de algún `RUNNING` falla, o ese VM deja de estar `RUNNING`
      al sondearlo, tampoco: podría ser ajeno, así que los suspendidos de ese
      listado cuentan para el cupo pero ni `stop()` ni la reducción los
      terminan; se avisa con su id y se dejan a su propio plazo y a su
      política de `idle`. `stop()` termina igual sin error y devuelve cuántos
      VMs terminó (`rayito agent stop <nombre>` imprime ese número). Los VMs
      que creó el propio proceso siempre se terminan.
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

## En AgentCore no hay flota { #en-agentcore }

Con `--target agentcore` el agente corre en
[Amazon Bedrock AgentCore Runtime](desplegar-en-agentcore.md), que crea una
microVM por sesión y escala solo: `min_vms`, `max_vms`, `warm_vms` y el resto
de las opciones de esta página que dimensionan una flota no existen allí. Si las
pasas en la llamada es un error (`option_not_supported`) y, si vienen del
fichero de proyecto, un aviso: Rayito no las aproxima en silencio. `invoke()`,
`stream()` y el `session_id` sí son los mismos. La tabla completa está en
[Desplegar en AgentCore](desplegar-en-agentcore.md#opciones-que-no-aplican).

## Ver también { #ver-tambien }

- [Despliega tu agente](despliega-tu-agente.md): la guía completa y el fichero de proyecto.
- [Desplegar en AgentCore](desplegar-en-agentcore.md): el mismo agente en Amazon Bedrock AgentCore Runtime.
- [Límites](../limits.md): las cuotas y los topes de esta página.
- [Pool](../pool.md): las plazas aparcadas que usa una flota.
