# Server-side deadline

A sandbox can have a **logical deadline** that `rayd` enforces inside the
MicroVM, even if your process dies: when it expires, the sandbox is
terminated (`on_timeout="kill"`) or suspended (`on_timeout="pause"`). The
deadline is moved live with `set_timeout()` and extended with
`connect(timeout=)`, as in E2B. The platform **cap** (`max_lifetime`, the
`maximumDurationInSeconds` of `run-microvm`) is set in `create()` and does
not move: there is no `UpdateMicrovm`. The table of the three clocks
(deadline, cap, idle) is in [Concepts](concepts.md#plazo-tope-e-idle).

!!! note "Requires an image 0.3.0 or later"
    Requesting a lifecycle (`max_lifetime` or `on_timeout`) from an image
    earlier than 0.3.0 terminates the VM (except with `keep_on_failure`) and
    raises `LifecycleUnsupportedException` (TS `LifecycleUnsupportedError`),
    the same absent feature as any other: it is a subclass of
    `UnimplementedError` (TS `UnimplementedError`), not of
    `InvalidArgumentException`; catch `rayito.UnimplementedError` (or the
    specific name) if you want to tell the case apart. Without
    `max_lifetime` or `on_timeout` there is no logical deadline: `timeout`
    is the platform lifetime and does not move.

## Create with a deadline { #crear-con-plazo }

| Parameter (Python / TS) | Value | What it does |
|---|---|---|
| `timeout` / `timeoutMs` | ≥ 1 s (default 3600 s) | the logical deadline from startup |
| `max_lifetime` / `maxLifetimeMs` | 120-28,800 s (TS: multiple of 1000 ms); default `timeout + 60` (at least 120) | the platform cap, running + suspended; the deadline never exceeds `max_lifetime − 60 s` from startup |
| `on_timeout` / `onTimeout` | `"kill"` (default) or `"pause"` | what `rayd` does on expiry |
| `idle` | `IdlePolicy(...)` (default 300 s and `auto_resume=True`) | in `pause` mode it is required (`idle=None` is `InvalidArgumentException`), `max_idle_seconds` must be less than `max_lifetime` and `auto_resume` is E2B's rule after the deadline; without `idle`, if the 300 s do not fit under `max_lifetime`, it is turned off in `kill` and drops to 60 s in `pause` |

=== "Python"

    ```python
    from rayito import Sandbox

    sbx = Sandbox.create(timeout=600, max_lifetime=7200, on_timeout="kill")
    print(sbx.get_info().expires_at)          # the logical deadline (now + 600 s)
    sbx.set_timeout(1800)                     # EXACT: now + 1800 s; it can shorten
    sbx.connect(timeout=900)                  # AT_LEAST: never shortens
    print(sbx.get_health().lifecycle)         # SandboxLifecycle(phase='active', ...)

    token, sandbox_id = sbx.access_token, sbx.sandbox_id
    # From another process, without a handle: the access token is needed
    Sandbox.set_timeout(sandbox_id, 3600, access_token=token)
    sbx.close()                               # releases this handle; the sandbox keeps going
    again = Sandbox.connect(sandbox_id, access_token=token, timeout=1200)
    again.kill()
    ```

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncSandbox


    async def main() -> None:
        sbx = await AsyncSandbox.create(timeout=600, max_lifetime=7200, on_timeout="kill")
        try:
            await sbx.set_timeout(1800)
            await sbx.connect(timeout=900)
            info = await sbx.get_info()
            print(info.expires_at, info.lifecycle)
            await AsyncSandbox.set_timeout(sbx.sandbox_id, 3600, access_token=sbx.access_token)
        finally:
            await sbx.kill()


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    await using sbx = await Sandbox.create({
      timeoutMs: 600_000,
      maxLifetimeMs: 7_200_000,
      onTimeout: "kill",
    });
    console.log((await sbx.getInfo()).expiresAt); // the logical deadline
    await sbx.setTimeout(1_800_000); // EXACT
    await sbx.connect({ timeoutMs: 900_000 }); // AT_LEAST
    console.log((await sbx.getHealth()).lifecycle?.phase); // "active"

    await Sandbox.setTimeout(sbx.sandboxId, 3_600_000, { accessToken: sbx.accessToken });
    const again = await Sandbox.connect(sbx.sandboxId, {
      accessToken: sbx.accessToken,
      timeoutMs: 1_200_000,
    });
    again.close();
    ```

## `set_timeout` and `connect(timeout=)` { #set_timeout-y-connecttimeout }

- **`set_timeout(t)`** (`SetTimeout` `EXACT`): the deadline becomes now +
  `t`, longer or shorter. Above `max_lifetime − 60 s` from startup it is
  `InvalidArgumentException` and the deadline stays intact; beyond the cap
  only `reincarnate()` is left ([Persistence](persistence.md#reincarnate-mas-alla-de-max_lifetime)). On
  a sandbox created without `max_lifetime` or `on_timeout` it is also
  `InvalidArgumentException`. In `pause` mode, it reopens a sandbox whose
  deadline expired but which has not been suspended yet.
- **`connect(timeout=t)`** (`SetTimeout` `AT_LEAST`, E2B's semantics):
  resumes if needed and the deadline becomes **at least** now + `t`; it
  never shortens it.
- The class forms (`Sandbox.set_timeout(id, t)`, `Sandbox.connect(id,
  timeout=t)`; TS `Sandbox.setTimeout(id, ms, { accessToken })`) need the
  sandbox's access token (`access_token=` or `RAYITO_ACCESS_TOKEN`).
- `get_info().expires_at` is the current logical deadline (it rereads it
  from `Health` if another client moved it) and `platform_expires_at` the
  cap; `remaining_seconds()` counts what is left. `get_health().lifecycle`
  carries `phase` (`unmanaged`, `active`, `resume_grace`, `expired`),
  `deadline`, `cap`, `timeout_seconds`, `on_timeout`, `auto_resume` and
  `extensions`.

## On expiry: `kill` { #al-vencer-kill }

`rayd` closes the streams with `sandbox_timeout`, sends `SIGTERM`/`SIGKILL`
to all processes and exits with code 124; the VM goes to `TERMINATED` ≈ 15 s
later without any IAM call. During those seconds every call except
`Health` and `SetTimeout` fails with `TimeoutException` (TS
`TimeoutError`). Afterwards, `Sandbox.get_info(id).timed_out` is `True`
(code 124 in the `stateReason`: the platform propagates it as `Container
Stopped with Exit Code: 124`, measured, with `TERMINATED` between ≈ 15 and
19 s after the deadline). Those seconds of 502 are billed.

## On expiry: `pause` { #al-vencer-pause }

=== "Python"

    ```python
    from rayito import IdlePolicy, Sandbox

    sbx = Sandbox.create(
        timeout=600,
        max_lifetime=7200,
        on_timeout="pause",
        idle=IdlePolicy(max_idle_seconds=300, auto_resume=True),
    )
    print(sbx.get_info().expires_at)
    sbx.kill()
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    await using sbx = await Sandbox.create({
      timeoutMs: 600_000,
      maxLifetimeMs: 7_200_000,
      onTimeout: "pause",
      idle: { maxIdleSeconds: 300, autoResume: true },
    });
    console.log((await sbx.getInfo()).expiresAt);
    ```

**Honest-partial** mode: with a live client (a `Sandbox` open in some
process) the SDK suspends the sandbox ≈ 1 s after the deadline; **without a
client**, the platform's idle policy suspends it once `max_idle_seconds`
pass without traffic. Between expiry and suspension processes keep running
(≈ 3 s with a live client, up to `max_idle` without one), and the idle
policy can suspend before the deadline if there is no traffic.

With `auto_resume=True` the next call resumes it and the deadline reopens
to `max(timeout, 300 s)` from that moment (E2B's rule), bounded by the cap.
`rayd` applies it when it detects the freeze; if the real suspension lasted
less than 2 s, the SDK that suspended applies it with a `SetTimeout` on the
first call after resuming. With `auto_resume=False` the sandbox stays
`expired` after a loose resume until `connect(timeout=)` or
`set_timeout()` reopens it. A paused sandbox still dies at the cap
(`max_lifetime`), even when suspended.

## In the E2B shim { #en-el-shim-de-e2b }

`rayito.e2b` / `rayito/e2b` always request a logical deadline:
`timeout=300` (TS `timeoutMs: 300_000`) with `on_timeout="kill"`,
`max_lifetime` defaulting to `max(3600, min(timeout + 60, 28800))`, and
`lifecycle={"on_timeout": "pause", "auto_resume": True}` as in E2B
([E2B compatibility](e2b-compat.md)).

=== "Python"

    ```python
    from rayito.e2b import Sandbox

    sbx = Sandbox.create(timeout=300, lifecycle={"on_timeout": "pause", "auto_resume": True})
    sbx.set_timeout(900)
    token = sbx.native.access_token  # E2B has no such token; store it next to the sandbox_id
    Sandbox.set_timeout(sbx.sandbox_id, 1200, access_token=token)
    same = Sandbox.connect(sbx.sandbox_id, timeout=600, access_token=token)
    same.kill()
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito/e2b";

    const sbx = await Sandbox.create({
      timeoutMs: 300_000,
      lifecycle: { onTimeout: "pause", autoResume: true },
    });
    await sbx.setTimeout(900_000);
    const token = sbx.native.accessToken; // E2B has no such token; store it next to the sandboxId
    await Sandbox.setTimeout(sbx.sandboxId, 1_200_000, { accessToken: token });
    const same = await Sandbox.connect(sbx.sandboxId, { timeoutMs: 600_000, accessToken: token });
    await same.kill();
    ```

## Differences from E2B { #diferencias-con-e2b }

- The cap counts suspended time: 8 h at most from startup; E2B keeps paused
  sandboxes without a limit. E2B Pro's 86,400 s do not exist.
- In `pause` mode processes keep running between expiry and suspension, and
  without a live client the suspension arrives at `max_idle`.
- The class forms need the sandbox's access token.
- A real suspension of less than 2 s that crosses the deadline is not
  recognized by `rayd` (the SDK that suspended covers it, above).

??? info "Sources and measurements"
    - Design: ADR-011 in [`ARCHITECTURE.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/ARCHITECTURE.md).
    - Measurements Q58 (exit and `TERMINATED` ≈ 15 s later) and Q63 (code 124
      in the `stateReason`) in [`AWS_API_NOTES.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/AWS_API_NOTES.md).
