## Context

The SDKs already accept an injected `ControlPlane` and `TransportSettings`
(`control_plane=`/`transport=` in Python, `controlPlane`/`transport` in
TypeScript), and both SDKs' unit tests already talk to a fake `rayd` over
loopback (`grpc.local_channel_credentials` in Python, `scheme: "http"` in
TypeScript, which `openTransport` only allows towards loopback). Floci 2.1.0
emulates the Lambda MicroVMs control plane (images, versions,
`run-microvm`, `get-microvm`, `list-microvms`, `terminate-microvm`) but not
the per-VM endpoint, auth tokens or suspend/resume.

## Goals / Non-Goals

- Goal: one command runs the real SDKs against the real `rayd` in the real
  product image and against emulated AWS for every optional feature that
  can run without privileges.
- Goal: no SDK, `rayd` or proto change, and no SDK safety check relaxed.
- Non-goal: replacing AWS acceptance; emulating the AWS proxy, snapshots,
  IMDS, egress enforcement or S3 mounts.

## Decisions

### D1. Floci, pinned by digest and confined

Floci (MIT, maintained, no auth token) over LocalStack Community (token
required and frozen security updates since March 2026). Pinned to
`2.1.0@sha256:f5aa8c18…`, which fixes the published critical advisories. Its
API is unauthenticated, so it runs as uid 1001 with `cap_drop: [ALL]`,
`no-new-privileges`, a read-only root filesystem, no Docker socket (the
socket is what turned a Floci bug into host RCE in GHSA-6f92-9q2p-fmpj),
only on an `internal: true` network and with no published ports.
Container-backed services and the console are disabled. Rejected: mounting
the Docker socket to get real Lambda execution.

### D2. The guest is the unchanged product image

`make local-guest-context` stages `image/Dockerfile`, a copy of
`kernel-sidecar/` (`scripts/copy_sidecar.py`) and a `rayd` binary, and
compose builds the guest from that context, so there is no second
Dockerfile to drift. The binary comes from `LOCAL_RAYD_BIN` or from
`dev/local/rayd/Dockerfile`, which compiles on the same al2023 base (same
digest) so the glibc binary links against the glibc it runs on. Works with
the classic builder (no BuildKit-only syntax).

### D3. Shared network namespace instead of TLS

The guest joins the runner's network namespace
(`network_mode: service:runner`), so `rayd` is on `127.0.0.1` for the
SDKs and the existing loopback-only cleartext channels apply. The runner
owns the namespace (not the guest) because the guest restarts after every
`/terminate`. Rejected: a TLS terminator with a local CA (more moving parts)
and relaxing the SDKs' loopback rule (a security regression).

### D4. A test-only decorator of the `ControlPlane` port

`LocalGuestControlPlane(inner, address)` lives in `tests/local/` of each
SDK. `run_microvm` calls the inner (real) adapter against Floci, then the
guest's `/ready` and `/run` with `{microvmId, runHookPayload}`, and returns
the info with the guest's host as endpoint. `get`/`list`/`terminate` go to
Floci; `suspend`/`resume` call the hooks and overlay `SUSPENDED`;
`create_auth_token` returns a fixed placeholder (the proxy, not `rayd`,
validates the JWE). One sandbox per guest: `run_microvm` terminates a live
one first, and `/run` only counts when it answers `installed` (an
`already_ran`/`illegal` reply is the previous `rayd`, recycled with
`/terminate`).

### D5. Events Lambdas run in process

The forwarder, deliverer and reconciler code (`infra/lambdas/events_webhooks`)
is imported by the Python local test and invoked against Floci's DynamoDB,
Streams and Secrets Manager. The forwarder receives a CloudWatch Logs
subscription payload with a line signed with the stack key read from Floci;
the deliverer reads the table's stream and posts through a test
`HttpSender` to a loopback receiver, which checks the E2B signature. The
real HTTPS client and SSRF guard stay covered by the Lambdas' unit tests.

### D6. CI only where it pays

The job needs arm64 (`ubuntu-24.04-arm`) and compiles `rayd` in Docker, so
it runs on PRs that touch the environment, nightly and on
`workflow_dispatch`, with `permissions: contents: read` and pinned actions.
