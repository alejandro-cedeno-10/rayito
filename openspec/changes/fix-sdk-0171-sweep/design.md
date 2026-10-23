## D1. A member whose Health fails is terminating, not fatal

`Sandbox.list(metadata=...)` keeps its contract: a failed `Health` is an
error, never a silent gap. The fleet directory stops using it for
membership and probes each `RUNNING` VM with `Sandbox.get_info(sandbox_id)`
(the same `get-microvm` plus tokenless `Health`, one per VM, as before).
The outcome has three classes:

- labels of an agent deployment: owned;
- agent not ready or no labels: not owned (as before, proof of a shared
  image);
- probe failed with a `SandboxException`/`SandboxError`, or the VM is no
  longer `RUNNING`: unreadable.

`select_members(items, owned, unreadable)` / `selectMembers` drops
unreadable VMs and does not let them mark the image as shared. Errors
outside the root (authentication, quota) and a rate limit or a timeout of
`get-microvm` still fail the listing: they say nothing about the VM, and
dropping a healthy member would make the fleet grow. An unreadable VM is not
stopped by `stop()`: its ownership is unknown and it is already going away.

Because an unreadable VM might be a foreign sandbox, it does not prove the
image is agent-only either: the non-`RUNNING` VMs of that listing are
selected with `proven=False`. They count for capacity (the fleet does not
grow because one member is dying), but terminating refuses an unproven
member with a warning and trimming skips it, leaving it to its own deadline
and idle policy. Membership by data that needs no probe was rejected: a
MicroVM cannot be tagged, the recipe does not list member ids and the
session index is optional. A member this process created, or that a later
listing proves, is proven. The probes run with at most
`AGENT_FLEET_PROBE_CONCURRENCY` (8) at a time, and the results and warnings
keep the listing order.

## D2. Template environment file (ADR-035)

`/etc/rayito/template-env.json` holds `{"envs": {...}, "version":
"rayito.template-env/1"}` with sorted keys, indent 2 and a final newline,
the same bytes in both SDKs. It is written only when the template has
`set_envs()`, with a `COPY` right after the template's own steps. `rayd`
reads it once at boot, before any spawn, and `SandboxSession::spawn_defaults`
layers it under the payload envs: template, sandbox, command, the E2B
order. Any failure reading it is an empty map with a warning that names the
path and the error class only.

## D3. Base image only when building

The preflight resolves the bucket first (its step is still reported after
the base and secret steps, so the step order does not change). `bucket is
None` means the deploy will not build (`build_image=False`, or no bucket and
the project image exists); then the base image is not read. When it is
missing and the project image exists, the error adds the reuse hint.

## D4. The entrypoint is checked at host start

The host already turns a failed `warm()` into the failed-start marker that
makes `deploy()` fail with its code. The deepagents and Strands drivers now
import the entrypoint in `warm()`. The Node driver runs `node
node_bridge.mjs <config> --check-entry`, which imports the file and checks
the named export; a missing file or export is `entrypoint_not_found`, any
other import failure keeps the bridge's classification. A static check
before the build was rejected: a Python module can come from an installed
dependency and a Node file can be produced by the install plan.

## D5. CLI output

A closed output exits with code 1 and no traceback in both CLIs (the code
Click already gives Python). The TypeScript CLI logger without `--verbose`
writes `warn` and `error` records as `<message>: <field values>`, the line
the Python last-resort handler writes; the shared line is
`cliWarningLine` in `testdata/fleet-recipe/renewal.json`.

## D6. Deterministic e2e

The custom-domain e2e requires `STABLE_OBSERVATIONS` consecutive answers
for visibility, polls every expected status, waits for the sandbox server
to listen, and its WebSocket echo serves several connections so a rejected
upgrade is retried. The dashboard read-only e2e compares the estimate with
a listing that keeps terminated sandboxes and checks the image filter on
its own views. The renewal e2e runs the production renewer once with its
injectable clock moved forward by `gatewayKeyRenewAfterSeconds`.
