# Rayito — Project Constitution

## Purpose

Rayito is an E2B-compatible sandbox SDK built on AWS MicroVMs (Firecracker via
AWS's own microvm control plane), not on E2B's infrastructure. It ships:

- `rayd`: the in-VM agent (Rust, gRPC) that runs inside each MicroVM and
  exposes process, filesystem, code-execution and PTY services to the client.
- Client SDKs (Python first, TypeScript later) generated from a shared
  `.proto` contract, matching the E2B SDK surface (`Sandbox`, `commands`,
  `files`, `run_code`, `pty`, `pause`/`resume`) wherever AWS's primitives allow
  it, and declaring `unimplemented` where they do not.

Full context: `SPEC.md` (why/what/scope), `ARCHITECTURE.md` (design + ADRs),
`AWS_API_NOTES.md` (verified AWS API ground truth), `MILESTONES.md`
(per-milestone acceptance criteria), `SECURITY.md` (threat model).

## Tech stack

- **Agent (`rayd`)**: Rust, hexagonal workspace —
  `crates/rayito-proto` (generated from `.proto`, via `tonic-prost-build` +
  `protox`, no `protoc` on the dev machine), `crates/rayd-core` (domain +
  port traits, no `tonic`/`axum`/`tokio-process` types), `crates/rayd`
  (adapters + `main`). Compiles for `aarch64-unknown-linux-musl` only
  (Graviton/Firecracker is ARM64-only); static binary, no TLS on the
  listeners (the AWS proxy terminates TLS); the only TLS client is the S3
  adapter of ADR-009 (`rustls` + `aws-lc-rs`, `aws-sdk-s3`).
- **Protocol**: gRPC h2c via `tonic` 0.14 with `x-aws-proxy-force-h2`
  (ADR-001). Only server-stream and unary RPCs; no bidi streaming (ADR-005).
- **Kernel sidecar**: Python 3.12, `jupyter_client` `AsyncKernelManager`
  (`ipc` transport), JSON lines over stdio (ADR-002).
- **PTY**: `nix::pty::openpty` + `tokio::process::Command`, not
  `portable-pty` (ADR-005).
- **Hooks**: dedicated port 9000, `axum` HTTP/1.1, never in `allowedPorts`
  (ADR-006).
- **Client SDKs**: Python (`grpcio` + `protobuf` + `boto3`, `uv_build`,
  `>=3.11`, hand-written sync and async trees with identical surfaces over
  shared pure helpers) first; TypeScript (Connect-ES v2) later. Both
  generated from `.proto` via `buf`, never hand-written request/response
  structs.
- **Control plane**: a library inside the client SDK (boto3), not a separate
  service — each MicroVM is its own endpoint.

## Hard rules (non-negotiable)

1. **Never invent AWS API parameters.** If a parameter name, field, or
   response shape is not literally documented in `AWS_API_NOTES.md`, stop and
   ask. Never infer by analogy with other AWS APIs or by guessing from a
   command's name. This is the single largest source of wasted work in this
   repo.
2. **The `.proto` is the source of truth.** Never hand-write request/response
   structs in Rust, Python, or TypeScript. To change the API, change the
   `.proto` and regenerate (`make proto`, or `python scripts/gen_python.py`
   for Python / `cargo build` for Rust).
3. **One milestone at a time.** Work only on the active milestone
   (`MILESTONES.md`). Do not add modules, features, or "for later"
   abstractions. See `SPEC.md` §4 for the explicit non-goals list (no
   desktop/GUI, no declarative templates/CLI, no multi-cloud, no non-Python
   kernels, no pre-warmed VM pools before M6, no billing/dashboard, no
   per-sandbox metadata or size params, no `set_timeout`, no control-plane
   service).
4. **A milestone never closes on mocks.** Its acceptance test runs against
   real AWS.
5. **ARM64 only.** Everything compiles for `aarch64-unknown-linux-musl`. If a
   dependency does not cross-compile, find another dependency — never switch
   architecture.
6. **Security defaults live in the milestone that ships the surface they
   protect, not in a later hardening pass.** M2 ships process spawning with
   its final security posture already in place: non-root default user (uid
   1000; root only if explicitly allowed by the image), a from-scratch child
   environment (nothing inherited from `rayd`), `setrlimit` limits, process
   groups + `killpg`, bounded output channels with backpressure, a per-sandbox
   process/PTY cap, and `Health` as the only RPC without `x-access-token`.
7. **E2B parity, honestly declared.** Client SDK surface matches E2B's SDK
   (naming, method shapes) wherever AWS's primitives support it; anything
   that cannot be supported is `unimplemented`, never silently approximated
   or faked.
8. **When the architecture doesn't fit reality, stop — don't improvise.** If
   implementing `ARCHITECTURE.md`'s design turns out to be unworkable, do not
   silently invent a replacement. Stop, explain the conflict, and propose a
   new ADR. Architecture decisions change in writing.

## Conventions

- **Naming**: identifiers (files/modules, classes, functions, variables,
  constants, field names, test names) are always English. Spanish is for
  user-facing strings, docstrings/comments, and product vocabulary that
  already crosses a wire (e.g. proto enum values), never for identifiers.
- **Comments**: WHY, not WHAT. No inline comments inside function bodies —
  extract a descriptively named helper instead, or put the rationale in a
  doc comment above the signature. No issue/ticket tags in code; traceability
  lives in OpenSpec changes, Linear, and commit messages.
- **Rust**: `clippy` pedantic clean, no warnings. `thiserror` for domain
  errors, `anyhow` only in `main`. No `unwrap()`/`expect()`/`panic!()` outside
  tests. Hexagonal boundaries are enforced: `rayd-core` never depends on
  `tonic`, `axum`, or `tokio::process`; those live only in `rayd`'s adapters.
  A port trait is introduced only in the milestone that ships its adapter,
  never ahead of time.
- **Python**: full type hints, `ruff` (lint + format), `mypy`. Sync and async
  client trees share the same public surface over shared pure helpers.
- **Errors**: in-stream agent errors use `StreamError` with string codes
  (`not_found`, `permission_denied`, `unimplemented`, …); unary RPCs use
  standard gRPC status codes.
- **Never log file contents, executed code, PTY bytes, or tokens/hook
  bodies** (`SECURITY.md`).
- **Gates**: `make lint` (buf lint + clippy + ruff), `make test` (unit),
  `make test-e2e` (real AWS) must pass before a milestone is considered
  implemented; only the acceptance agent closes a milestone, after the real
  AWS e2e is green.

## Acceptance process

Each milestone (`MILESTONES.md`) is tracked as an OpenSpec change under
`openspec/changes/<milestone-slug>/` with `proposal.md`, `design.md`,
`tasks.md`, and delta specs under `specs/<capability>/spec.md`. Every change
must pass `openspec validate <change> --strict` before implementation starts.
Implementers follow `tasks.md`, ticking boxes as they go. A milestone's
acceptance test is a real-AWS end-to-end test under
`clients/python/tests/e2e`, run with real credentials against a real
MicroVM — never mocked. Only after that e2e test is green does the
acceptance agent archive the change with `openspec archive <change> --yes`.
