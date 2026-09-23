# architecture-docs Specification

## Purpose
TBD - created by archiving change m7-oss-hygiene. Update Purpose after archive.

## Requirements

### Requirement: ARCHITECTURE.md explains what runs where
`ARCHITECTURE.md` SHALL contain a section `## Qué corre dónde` placed after "Vista general" and before "Capa 1 — Imagen base" with: (1) an ASCII process diagram of one MicroVM showing `rayd` (Rust, static musl, root) with `:8080` gRPC h2c called by the SDK through the AWS proxy and `:9000` HTTP/1.1 called only by Lambda for the hooks, its child kernel-sidecar (Python 3.12, uid 1000) speaking JSON lines over stdin/stdout, the sidecar's `ipykernel` children (one per context, uid 1000) over ZMQ `ipc://` under `/run/rayito/k/<context>/` marked as where user code runs, and the `commands` / `pty` processes as direct children of `rayd` as uid 1000; (2) a table with the columns Pieza / Lenguaje / Dónde corre / Quién la usa and exactly these rows: `rayd`, kernel-sidecar, ipykernel, procesos y PTYs, SDK Python, SDK TypeScript, llamadas al plano de control (boto3 / AWS SDK JS v3 inside the SDK process), stating for the two SDKs and the control-plane calls that they run **outside** the MicroVM in the client's process; (3) one paragraph "Por qué el sidecar es Python" restating ADR-002 (the kernel is Python anyway; `jupyter_client` is the battle-tested wire-protocol client; formatters, `e2b_charts`, warm-up, supervision and reseed are Python; the stdio hop is negligible; the `.proto` is backend-independent so the decision is reversible; Rust consolidation via `jupyter-zmq-client` remains an evaluated option) and linking to ADR-002. The existing "Vista general" diagram SHALL be unchanged.

#### Scenario: section present and complete
- **WHEN** a reviewer opens `ARCHITECTURE.md`
- **THEN** `## Qué corre dónde` sits between "Vista general" and "Capa 1", the diagram names `:8080`, `:9000`, JSON lines, `ipc://` and the uid of each process, the table has the seven rows and marks the SDKs as running outside the VM, and the paragraph cites ADR-002 and `jupyter-zmq-client`

### Requirement: README explains how it works in one screen
The root `README.md` SHALL contain a section `## Cómo funciona`, after the `rayito.e2b` snippet and before the "Estado" paragraph, with a client ↔ VM ASCII diagram (client process with the SDK on the left, the AWS proxy and the MicroVM with `rayd → sidecar → ipykernel` on the right, HTTPS/2 in between) and three explicit statements: the SDK **never runs inside the sandbox** (it lives in the client's process and only sends RPCs); code sent with `run_code` runs in a Jupyter kernel inside the MicroVM as uid 1000 and `commands` / `pty` spawn ordinary processes there; Rayito can be used from Python and TypeScript today and from any other language by generating a gRPC client from `proto/rayito/v1/`. The section SHALL end with pointers to `ARCHITECTURE.md` "Qué corre dónde" and to the docs site concepts page.

#### Scenario: the three statements are present
- **WHEN** a reader searches the README section for "nunca" and for "proto/rayito/v1"
- **THEN** they find the statement that the SDK never runs inside the sandbox and the statement that other languages generate a client from the `.proto`, and the diagram shows the SDK outside the VM

### Requirement: Docs site concepts page carries the expanded version and the languages table
`docs/site/docs/concepts.md` SHALL begin with `## Qué corre dónde` (the same diagram and table as `ARCHITECTURE.md`, a three-sentence ADR-002 summary, and the line that `ARCHITECTURE.md` wins if they drift) followed by `## Desde qué lenguajes se usa Rayito` containing a table with the columns Producto / Agente dentro de la VM / SDKs oficiales de cliente and the rows E2B (`envd`, Go; Python, JS/TS; Go as community forks), Daytona (Go daemon; Python, TypeScript, Ruby, Go, Java + CLI), Modal (proprietary; Python; JS and Go via libmodal / modal-client) and Rayito (`rayd`, Rust; Python, TypeScript; any other language from `proto/rayito/v1/`), the three requirements for another language (call the `lambda-microvms` control plane; gRPC over HTTPS/2 with the four lowercase metadata headers; readiness on `Health` and the reconnection contract if pause/resume is used), and the position that a Rust client ships when asked, a Go client is not planned, and publishing `rayito-proto` is deferred. Every pre-existing section of the page SHALL remain below, unchanged, and `mkdocs build --strict` SHALL pass without a nav change.

#### Scenario: strict build with the expanded page
- **WHEN** CI runs `cd clients/python && uv run --group docs mkdocs build -f ../../docs/site/mkdocs.yml --strict`
- **THEN** the build succeeds and the rendered `concepts` page shows the four-row languages table before "Vida del sandbox vs. política de idle"

#### Scenario: no drift in the existing sections
- **WHEN** the pre-change `concepts.md` is compared with the post-change file from the heading "## Vida del sandbox vs. política de idle" onward
- **THEN** there is no difference

### Requirement: ADR-011 records the server-enforced logical deadline
`ARCHITECTURE.md` SHALL contain a section `ADR-011 — Plazo lógico impuesto por rayd; el tope de la plataforma se elige en create()` stating:

- the context: ADR-007, the Q58 measurement that `rayd` exiting terminates the VM about 15 s later, and the monotonic clock of `AWS_API_NOTES.md` §15
- the decision: the payload block, the `sandbox_timeout` domain module, the watcher thread, kill and pause behaviour, the resume grace, E2B's auto-resume rule, `SetTimeout`, `Health.lifecycle` and the SDK deadline trigger
- the honest costs: the cap counts suspended time; processes run between the pause deadline and the suspension; idle suspension happens before the deadline; about 15 s of 502 is billed after a kill-mode exit; the 60 s cap margin

The ADR-007 section SHALL keep its body and gain the line `**Sustituida por ADR-011**`. `SPEC.md` §4 SHALL NOT list `set_timeout` as a non-goal. `openspec/project.md` hard rule 3 SHALL NOT list "no `set_timeout`".

#### Scenario: docs gate
- **WHEN** `scripts/tests/test_lifecycle_docs.py` reads `ARCHITECTURE.md`, `SPEC.md` §4 and `openspec/project.md`
- **THEN** ADR-011 exists, ADR-007 carries the superseded line, and neither SPEC §4 nor hard rule 3 names `set_timeout`

### Requirement: ADR-012 records the in-guest egress policy
`ARCHITECTURE.md` SHALL carry ADR-012 "Política de egress en el guest sobre rayito-base-caps; conectores de plataforma sólo por VPC del cliente".

The ADR SHALL contain:
- the context (Q44 and Q60: `[]` ≡ omitted, no `NO_EGRESS`; Q48: uidrange routing; no `UpdateMicrovm`);
- the decision (routes plus the local proxy, three modes, capability-gated, fail-closed default image);
- the consequences (guest-kernel residual, proxy-unaware clients fail closed, DNS not blocked on caps because the platform resolvers listen inside the guest, VPC connector as the only platform control);
- the addendum "Adenda: DNS bajo deny-all en caps" with context (QE1), decision (option C: names may resolve under deny-all, every connection outside the VM fails, hostname rules and proxy mode unchanged), consequences (the DNS-exfiltration residual channel), the alternatives considered (A: a port-53 `ip rule` before the `local` rule, deferred to the next cycle; B: a per-process `resolv.conf`, rejected) and reversibility.

The "Capa 2 — Agente rayd" services table SHALL list `NetworkService` (`UpdateNetwork`, `GetNetwork`). A subsection "Política de egress (M9)" SHALL give:
- the slot map (tables 101/102/103 at priorities 150/151/149 after `local` and the IMDS rule at 100);
- the mode table;
- the proxy protocols and guard;
- the environment export.

The hexagonal tables SHALL list the `network` domain module and the `egress_routes` and `network/` adapters. The image-variants paragraph SHALL say that `rayito-base-caps` also enforces the egress policy. A new `docs/site/docs/network.md` page SHALL be linked from the `mkdocs.yml` nav and SHALL list the divergences from E2B.

#### Scenario: ADR and page present
- **WHEN** a reviewer opens `ARCHITECTURE.md` and the docs site navigation
- **THEN** ADR-012 follows ADR-011 with context, decision, consequences and the DNS addendum, the services table lists `NetworkService`, the egress subsection shows the priority map, and the "Red saliente" page lists the proxy-unaware, ports 80/443, DNS, UDP, new-connections-only, lazily-started-proxy, root-not-filtered and caps-only divergences
