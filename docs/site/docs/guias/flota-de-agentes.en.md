---
title: Several sessions and several VMs
description: Serve your agent with several MicroVMs, spread the sessions across them, put a queue in front of the limit and keep warm slots, with no server of your own.
---

# Several sessions and several VMs { #varias-sesiones-y-varias-vms }

<small>Since 0.14.0.</small>

A MicroVM with your agent serves a few sessions at a time. When you need more,
`deploy_agent()` gives you a **fleet** (`AgentFleet`): several MicroVMs of the
same image and a session spread done by the client, with no Rayito server in
between. With the defaults it is a single VM, the same as
[`AgentProject.deploy()`](despliega-tu-agente.md#3-despliegalo-como-servicio).

## The options { #las-opciones }

Each option has its key in `rayito-agent.toml`, its argument in `ServeOptions`
and `ScalingOptions`, and its flag in `rayito agent deploy`.

| Option | What it does | Default | Limit |
|---|---|---|---|
| `max_concurrency` (`--max-sessions`) | sessions at a time in one VM | 1 | the connections of the VM size: 7 with 2 GB, 15 with 4 GB, 31 with 8 GB |
| `queue_max` (`--queue`) | requests that wait in the host queue when the VM is full | 0 (an immediate 429) | 256 |
| `queue_timeout_seconds` (`--queue-timeout`) | how long a request waits in the queue | 30 s | 900 s |
| `min_vms` (`--min-vms`) | VMs always alive | 1 | |
| `max_vms` (`--max-vms`) | VMs at most | 1 | 64 |
| `warm_vms` (`--warm`) | parked (suspended) slots that absorb a spike | 0 | `max_vms - min_vms` |
| `sessions` (`--sessions`) | `shared` (several sessions per VM) or `dedicated` (one VM per session) | `shared` | |
| `scale_in_after_seconds` | idle time after which a VM above `min_vms` ends by itself | 900 s | 60 s |
| `idle` (`--idle`) | idle time after which a VM is suspended and resumed when called | no suspension | see [Pause and resume](pausar-reanudar.md) |

The `max_concurrency` limit comes from the connections a MicroVM admits (8 per
base vCPU) minus one the service keeps for `/ping`: below 2 GB one vCPU is
counted, a conservative value. Raising the memory raises the cap.

The shortest way is the file (the keys you leave out are decided by detection):

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
            scaling=ScalingOptions(min_vms=1, max_vms=3, warm_vms=1, sessions="dedicated"),
        ),
    )
    try:
        first = fleet.invoke("Hello")
        again = fleet.invoke("What did I say?", session_id=first.session_id)
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
        scaling: new ScalingOptions({ minVms: 1, maxVms: 3, warmVms: 1, sessions: "dedicated" }),
      },
    });
    try {
      const first = await fleet.invoke("Hello");
      const again = await fleet.invoke("What did I say?", { sessionId: first.sessionId });
      console.log(again.text);
    } finally {
      await fleet.stop();
    }
    ```

## Shared and dedicated sessions { #sesiones-compartidas-y-dedicadas }

- **`shared`** (the default): a new session goes to the VM with room; if all of
  them are full, to a warm slot; if there is none, to a new VM while you have
  not reached `max_vms`; and if you are already at `max_vms`, it waits in the
  queue.
- **`dedicated`**: each new session gets its own MicroVM and a VM never serves
  two sessions. It is what you want if the agent keeps state on the VM disk or
  if you do not trust two users to share a process.

## The session id and coming back to a conversation { #el-id-de-sesion }

With `max_vms > 1`, `AgentResult.session_id` is an identifier that names its
VM (`rs1.<sandbox>.<session>`): passing it back takes the call to the same VM
without asking anyone. With `max_vms = 1` the session id does not change. From
another process, `AgentFleet.connect(name, access_token=...)` rebuilds the fleet
from the non-terminated MicroVMs of its image without resuming any, and a
session id from the first call still leads to its VM. For the reconnected fleet's
replies to return the handle too, pass the same `scaling` you deployed it with.

A session id you choose yourself (a chat thread) is only remembered inside the
process that deployed the fleet. To find it from another process in `dedicated`
mode, turn on the optional metadata index
([Metadata index](../funciones-opcionales/indice-de-metadatos.md), with its
cost); without it, an unknown id starts a new VM and the response carries the
routable identifier you must keep.

## The queue and the 429 { #la-cola-y-el-429 }

With `max_concurrency = 1` and `queue_max = 1`, of three simultaneous requests
one runs, one waits for its turn and the third gets `429` with `Retry-After`.
The fleet client honours that `Retry-After` up to `queue_timeout_seconds` and,
after the deadline, raises `CapacityException` (`code = "agent_capacity"`;
`CapacityError` in TypeScript).

## Scaling in, suspending and restarting { #reducir-suspender-y-reiniciar }

- **Scaling in:** VMs above `min_vms` carry a deadline of
  `scale_in_after_seconds` that the fleet renews every time it uses them; an
  idle VM ends by itself (the VM's `rayd` already knows how to terminate
  itself), with no control-plane call.
- **Suspending:** `idle` suspends an unused VM and resumes it when called; a
  suspended VM bills no compute, only its snapshot storage.
- **Restarting:** if the user's app dies, the host relaunches it with growing
  waits (up to 3 times in 5 minutes). Meanwhile `/ping` says `HealthyBusy` and
  invocations get `503` with `Retry-After`. Past the budget, `/ping` fails, the
  failure marker carries `app_crashloop` and the fleet replaces the VM; calls in
  flight on that VM fail with `SandboxException(code="agent_vm_lost")`.

!!! info "Cost and activation"
    - **Activated by:** `deploy_agent()` with more than one possible VM
      (`max_vms > 1`) or with warm slots; with the defaults it is a single VM.
    - **AWS resources and calls:** `min_vms` MicroVMs running, `warm_vms` slots
      parked in a pool, and the extra VMs the spread creates up to `max_vms`;
      `list-microvms` to find the fleet members.
    - **Approximate cost:** each running VM costs what a deployment costs (≈
      $0.126 per hour with 2 GB, $0.25 with 4 GB); a parked slot only pays for
      its snapshot storage (≈ $0.011 per day); a suspended VM, the same. List
      prices, us-east-1, checked on 2026-10-06:
      [Pricing](../cost.md#coste-de-un-agente-desplegado).
    - **IAM:** those of `Sandbox.create()`, `Sandbox.list()`, `Sandbox.connect()`
      and `Sandbox.kill()`; with a session index, those of the
      [metadata index](../funciones-opcionales/indice-de-metadatos.md).
    - **How to turn it off:** `fleet.stop()` or `rayito agent stop <name>`
      terminate every VM; without calling `deploy_agent()` nothing is created,
      and the defaults leave no slots or extra VMs.
    - **Example:** `deploy_agent("./mi-agente", options=AgentDeployOptions(scaling=ScalingOptions(max_vms=3)))`.

!!! info "Measured on real AWS"
    In the 0.14 acceptance (2026-10-16, us-east-1, Claude Haiku 4.5) the fifteen
    sample agents were deployed without touching their code, in Python and in
    TypeScript, with no options and with 4096 MiB, 2 sessions per VM, a queue of
    2 and 1 to 2 VMs with one warm. Five simultaneous calls spread over two VMs;
    through the URL of a single VM, the overflow got `429`. Resuming a suspended
    VM and making the first call took between 1.9 and 4.0 s. The whole cycle (65
    images) cost about $5 and ended with the AWS inventory the same as at the
    start. Details in `AWS_API_NOTES.md` (Q170 to Q172).

## See also { #ver-tambien }

- [Deploy your agent](despliega-tu-agente.md): the full guide and the project file.
- [Limits](../limits.md): the quotas and caps on this page.
- [Pool](../pool.md): the parked slots a fleet uses.
