## Why

A sweep of the 0.17.1 tree against real AWS found defects that are older than
0.17.1 and that 0.17.1 should not ship with:

- `AgentFleet.connect()`, `status()` and `refresh_members()` fail with
  «Health no respondió» when one member is being terminated: the platform
  still lists it as `RUNNING` while its `rayd` already exited (1 of 147 calls
  during a scale-in).
- The variables of `Template.set_envs()` are not visible to `commands.run`,
  PTYs or `run_code`: `rayd` starts every child with an environment built
  from scratch, so the image `ENV` only reaches `rayd` itself. E2B makes
  them visible.
- A deploy that reuses the project image (`build_image=False`, or no bucket
  and the image already built) still requires `rayito-base-caps`.
- `rayito agent deploy --entry <wrong>` succeeds and the error
  (`entrypoint_not_found`) only shows at the first invocation.
- The TypeScript CLI crashes with an unhandled `EPIPE` when its output is
  piped to `head`, and never prints the fleet recipe warnings (such as
  `recipe_renewal_ignored`) that the Python CLI prints.
- Several e2e tests are wrong or nondeterministic: the renewal e2e deploys
  over a secret too young to be renewed, the account-setup e2e expects three
  steps instead of four, the agent-project CLI e2e omits `[image] base` and
  leaves the fleet recipe behind, the TypeScript template e2e throws without
  its bucket, and the custom-domain and dashboard read-only e2e depend on
  edge propagation order and on sandboxes of parallel runs.

## What Changes

- The fleet directory probes each `RUNNING` VM on its own
  (`Sandbox.get_info(sandbox_id)`); a VM whose probe fails with a
  `SandboxException`/`SandboxError`, or that left `RUNNING` before the probe,
  is unreadable: not a member, not counted for capacity and not proof of a
  shared image. A warning names its id. Shared vector in
  `testdata/agent-fleet/members.json`.
- `Template.build()` bakes `/etc/rayito/template-env.json`
  (`rayito.template-env/1`) when the template has `set_envs()`, and `rayd`
  reads it once at boot and layers it under the `/run` envs (ADR-035).
  Shared vectors in `testdata/templates/template-env.json`.
- The deploy preflight resolves the bucket first and checks the base image
  only when it will build; a missing base with the project image present
  says how to reuse it (`--no-build`).
- The deepagents and Strands host drivers import the entrypoint in
  `warm()`, and the Node bridge gains `--check-entry`, so a wrong `--entry`
  fails the host start and therefore the deploy.
- Both CLIs exit with code 1 and no traceback when the reader closes the
  output; the TypeScript CLI always passes a logger that writes the SDK's
  warnings and errors to stderr (INFO only with `--verbose`), with the same
  line as Python for `recipe_renewal_ignored`.
- The e2e fixes listed above, in both languages where the test exists.

## Impact

- `rayd`: new boot read of `/etc/rayito/template-env.json`; no proto change.
- Python and TypeScript SDKs: fleet listing, deploy preflight, template
  artifact, CLI entry points; the agent host assets (`node_bridge.mjs`,
  drivers) shared by both SDKs.
- A template that already used `set_envs()` (and agent project images)
  builds a new version on its next `build()`.
- No new AWS API call or parameter.
