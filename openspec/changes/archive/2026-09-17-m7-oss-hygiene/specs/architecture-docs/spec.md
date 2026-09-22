## ADDED Requirements

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
