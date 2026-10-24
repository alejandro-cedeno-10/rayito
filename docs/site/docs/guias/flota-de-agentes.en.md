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
| `scale_in_after_seconds` (`--scale-in-after`, since 0.17) | idle time after which a VM above `min_vms` ends by itself | 900 s | 60 s |
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
            scaling=ScalingOptions(min_vms=1, max_vms=3, warm_vms=1, sessions="shared"),
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
        scaling: new ScalingOptions({ minVms: 1, maxVms: 3, warmVms: 1, sessions: "shared" }),
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

Since 0.17, a fleet reconnected with only its name and token can also create
VMs and start warm slots: it reads the [recipe](#la-receta-de-la-flota) that
`deploy_agent()` saved. Without a recipe (a 0.16 fleet, or one deployed with
`--no-save-recipe`) and without `project=`, the fleet routes and queries but
cannot create a VM: the first session that needs one raises
`InvalidArgumentException`; pass the project then. `AgentProject.detect()`
returns the project and its detection report, so split them
(`AgentProject.load(...)` returns only the project):

=== "Python"

    ```python
    import os

    from rayito import AgentFleet, AgentProject

    project, _report = AgentProject.detect("./my-agent")
    fleet = AgentFleet.connect(
        "my-agent", access_token=os.environ["RAYITO_ACCESS_TOKEN"], project=project
    )
    ```

=== "TypeScript"

    ```ts
    import { AgentFleet, AgentProject } from "rayito";

    const [project] = await AgentProject.detect("./my-agent");
    const fleet = await AgentFleet.connect("my-agent", {
      accessToken: process.env.RAYITO_ACCESS_TOKEN ?? "",
      project,
    });
    ```

A session id you choose yourself (a chat thread) is only remembered inside the
process that deployed the fleet. To find it from another process in `dedicated`
mode, turn on the optional metadata index
([Metadata index](../funciones-opcionales/indice-de-metadatos.md), with its
cost); without it, an unknown id starts a new VM and the response carries the
routable identifier you must keep.

## The fleet recipe { #la-receta-de-la-flota }

<small>Since 0.17.0.</small>

After starting the fleet, `deploy_agent()` (and `rayito agent deploy`) saves its
**recipe** in Parameter Store, at `/rayito/agents/<name>/recipe`: the project as
it ended up after the flags (`--env`, `--secret-env-from`, `--allow-out`, the
gateways), the image version, the gateway presets, the scaling and whether keys
are renewed. It only holds secret **names**, never their values or the access
token.

With it, `AgentFleet.connect(name, access_token=...)` from any process (or
`rayito agent invoke|status|stop <name>`) creates VMs like the deployed ones,
opens the warm slots with their gateways and `status()` gives the real scaling.
An explicit `scaling=` or `project=` wins over the recipe, and
`use_recipe=False` (`useRecipe: false`) ignores it: the fleet is the 0.16 one.

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

- **Status.** `FleetStatus.scaling_source` (`scalingSource`) tells where the
  scaling comes from: `explicit`, `project`, `recipe` or `default`. With
  `default` (no project and no recipe) `rayito agent status` prints
  `members: N (escalado desconocido: sin proyecto ni receta)` instead of a
  `min 1, max 1` that does not describe the fleet.
- **Deletion.** `fleet.stop()` and `rayito agent stop <name>` delete the recipe
  if this process saved or read it; `stop(keep_recipe=True)` (`--keep-recipe`)
  keeps it.
- **Warnings.** A failure to save it never fails the deploy: it leaves a
  warning with a code (`recipe_access_denied`, `recipe_not_saved`,
  `recipe_too_large`) in the log and in `warnings` of `--json`. When reading,
  no permission is `recipe_access_denied`, and an unreadable recipe or a
  Parameter Store that does not answer (no SSM VPC endpoint, throttling) is
  `recipe_unreadable`. A recipe that asked to renew the Bedrock key does not
  turn it on for the connecting process: without `renew_gateway_keys=True`
  the warning is `recipe_renewal_ignored` and nothing is renewed. A fleet
  named `.` or `..` has no recipe (`recipe_not_saved`); in every case the fleet behaves as in 0.16, and
  `rayito agent stop <name>` still terminates its VMs (if it cannot delete
  the recipe either, it reports that error at the end). After
  `stop`, Parameter Store takes about 30 s before it lets another parameter use
  the same name: an immediate `deploy` warns `recipe_not_saved`.
- **Size.** A recipe is stored as JSON if it fits in 4,096 bytes (a standard
  parameter) and compressed otherwise; if it still does not fit (very long
  instructions), the warning is `recipe_too_large` and the previous one is
  deleted.
- **`--env` and secrets.** `rayito agent deploy` rejects an `--env` whose name
  looks like a secret (`*_TOKEN`, `*_KEY`, `*_SECRET`, `*_PASSWORD`..., or
  with a `PASS`, `PASSWD`, `PAT`, `CREDENTIAL` or `CREDENTIALS` segment, such
  as `DB_PASS` or `GITHUB_PAT`, or with `AUTH` or `BEARER` as the last segment
  or next to `TOKEN`, `KEY` or `HEADER`, such as `X_AUTH` or `AUTH_HEADER`;
  `AUTH_MODE` or `AUTHOR_NAME` are accepted): use
  `--secret-env NAME=<secret>` or `--secret-env-from <secret>`, so the recipe
  only keeps the name. This breaks 0.16 command lines for `*_SECRET`. Note:
  the rule belongs to the `--env` flag; the project's `env` values
  (`[env]` in `rayito-agent.toml` or `env=` in the library) are stored as is
  in the recipe, readable by anyone with `ssm:GetParameter` on
  `parameter/rayito/agents/*`. Nothing secret in `env`, not even a URL with
  credentials: it belongs in `secret_env`.
- **Who can write it.** The recipe decides the image, egress, `env` and each
  gateway's upstream and secrets for every process that connects by name:
  write access to `parameter/rayito/agents/*` amounts to deploy rights. That
  is why `SandboxLauncherPolicy` only reads and deletes it, and
  `ssm:PutParameter` is in `ImagePublisherPolicy` and `CallerPolicy`.

!!! info "Cost and activation"
    - **Turns on:** `deploy_agent()` / `rayito agent deploy` to MicroVMs, by
      default. It is the only Rayito feature on by default (ADR-034): the
      standard parameter is free and its KMS request falls in the free tier.
    - **AWS resources and calls:** one standard `SecureString` parameter per
      fleet (`PutParameter` on deploy, `GetParameter` when reconnecting without
      a project, `DeleteParameter` on stop), encrypted with the AWS managed key
      `aws/ssm`.
    - **Approximate cost:** standard parameters have no charge; each write and
      each read is one KMS request (20,000 free per month, then $0.03 per
      10,000). Checked on 2026-10-22:
      [Systems Manager](https://aws.amazon.com/systems-manager/pricing/) and
      [KMS](https://aws.amazon.com/kms/pricing/).
    - **IAM:** `ssm:PutParameter` (the deployer: `CallerPolicy` or
      `ImagePublisherPolicy`),
      `ssm:GetParameter` and `ssm:DeleteParameter` on
      `parameter/rayito/agents/*` (the `AgentRecipe` statement of
      [IAM](../operacion/iam.md)); no KMS permission.
    - **How to turn it off:** `AgentDeployOptions(save_recipe=False)`
      (`saveRecipe: false`, `--no-save-recipe`).
    - **Example:** `rayito agent deploy ./mi-agente --no-save-recipe`.

## Warm slots and gateways { #plazas-calientes-y-pasarelas }

A parked slot is born without the project's gateways. Since 0.17, the fleet
opens them when it takes the slot, also when another process parked it (a fleet
reconnected with the recipe or with `project=`), through the same path as
`SandboxPool.take(gateways=)`; if it cannot open them, it terminates the slot
and uses another VM. Before, a reconnected fleet could start the agent without
the model gateway, and deploys had to use `--warm 0`.

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
  itself), with no control-plane call. While it serves any invocation, the
  deadline is never shorter than the request's, and it goes back to
  `scale_in_after_seconds` only when the last one ends, also if it fails.
  Each VM's deadline changes go in order: releasing an invocation never
  shortens the deadline of another one that started meanwhile. A shortened
  VM that becomes a base VM gets its whole life back up to `rayd`'s cap, even
  when the fleet does not know it (it asks for the maximum and `rayd` answers
  with its cap); since 0.17.1. For `rayd` to accept that
  deadline, a fleet that can grow (`max_vms > min_vms`) creates all its VMs
  and slots with a logical deadline (`on_timeout="kill"` and `max_lifetime` =
  `serve.timeout_seconds + 60`); a base VM still lives
  `serve.timeout_seconds`. A fixed-size fleet creates its VMs as before. Up
  to 0.16 scale-in did not happen: VMs were created without a logical
  deadline and `rayd` rejected the `set_timeout`. A VM created that way (by
  0.16) is not scaled in: the fleet warns once; end it with `stop()` or
  redeploy. While a VM is being terminated, the platform still lists it as
  `RUNNING` although its `rayd` no longer answers: when the fleet is listed
  (`AgentFleet.connect()`, `status()`, `refresh_members()`), a VM whose
  `Health` fails or that stops being `RUNNING` when probed is treated as a
  terminating VM (not a member, not counted for capacity, and a warning names
  its id) instead of failing the call; since 0.17.1. `RUNNING` VMs are probed
  up to 8 at a time (`AGENT_FLEET_PROBE_CONCURRENCY`), with results and
  warnings in listing order.
- **Image version:** the VMs a fleet creates use the version the deploy built
  or reused, or the recipe's. A fleet reconnected without a recipe
  (`AgentFleet.connect(name, project=...)`) uses the newest member's, never
  the image's newest version, which may be another build; since 0.17.1.
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
      terminate every VM of the fleet (and delete its recipe); without calling `deploy_agent()` nothing
      is created, and the defaults leave no slots or extra VMs. By name only VMs
      that belong to an agent are terminated: a tokenless `Health` reads the
      labels of the `RUNNING` ones, and if the image has one without them, none
      of the suspended ones is terminated (they cannot be probed without
      resuming them). Neither are they when the probe of a `RUNNING` one fails,
      or that VM stops being `RUNNING` when probed: it might not be an agent,
      so the suspended ones of that listing count for capacity but neither
      `stop()` nor scale-in terminates them; a warning names each id and they
      are left to their own deadline and `idle` policy. `stop()` still ends
      without an error and returns how many VMs it terminated (`rayito agent
      stop <name>` prints that number). The VMs this process created are
      always terminated.
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

## There is no fleet on AgentCore { #en-agentcore }

With `--target agentcore` the agent runs on
[Amazon Bedrock AgentCore Runtime](desplegar-en-agentcore.md), which creates one
microVM per session and scales on its own: `min_vms`, `max_vms`, `warm_vms` and
the rest of the options on this page that size a fleet do not exist there.
Passing them in the call is an error (`option_not_supported`) and, if they come
from the project file, a warning: Rayito never approximates them silently.
`invoke()`, `stream()` and the `session_id` are the same. The full table is in
[Deploy to AgentCore](desplegar-en-agentcore.md#opciones-que-no-aplican).

## See also { #ver-tambien }

- [Deploy your agent](despliega-tu-agente.md): the full guide and the project file.
- [Deploy to AgentCore](desplegar-en-agentcore.md): the same agent on Amazon Bedrock AgentCore Runtime.
- [Limits](../limits.md): the quotas and caps on this page.
- [Pool](../pool.md): the parked slots a fleet uses.
