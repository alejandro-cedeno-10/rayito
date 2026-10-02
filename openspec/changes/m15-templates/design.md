## Context

m15-templates is one of the eight M15 features built in parallel on top of
`v06-foundations`. It owns no `ConfigureSandbox` section (building an image
is a plane-of-build operation, not a per-sandbox one) and introduces no
proto changes. The architecture handed to this change is Option A of the
templates research (`docs/research/2026-10-e2b-out-of-scope.md` §3): a DSL
compiled entirely client-side to a Dockerfile and a zip, no CodeBuild, no
control plane of Rayito's own. This `design.md` records the decisions
internal to this change.

## Decisions

### T1. `fromBaseImage()` is the only supported base in 0.6; `fromImage`/`fromTemplate`/`fromDockerfile` raise `UnimplementedError`

E2B's `Template` can start `FROM` an arbitrary public or private image.
Rayito's `rayd` (and its boot hooks, `/ready`/`/suspend` wiring, IMDS
credential broker, etc.) only exists inside images that already went
through `rayito image publish`: there is no supported path to inject it
into an external base. `fromBaseImage(name, version?)` therefore always
means "fetch the zip of an already-published Rayito image and layer on top
of its own Dockerfile", never "start a brand-new Dockerfile". The other
three base-selection methods are stubs that name this limitation and point
at the workaround (publish the external image's content as a layer of an
already-published Rayito image via `copy()`/`runCmd()`, or — longer term —
option B of the research, a `CodeBuildPrebuilder` adapter behind the same
`ImageBuildGateway`-shaped port, not built here).

### T2. Composition reads and rewrites the *entire* base zip, not just its Dockerfile

A naive design would fetch only the base's Dockerfile text and zip only the
new layer's files. That breaks the moment the base Dockerfile's own
`COPY`/`RUN --mount` lines reference files that live in its zip but not in
the new one (every `rayito-base` zip at minimum needs its own `rayd`
binary present for the final repeated `CMD` to work). `_build.py`
`_compose()`/`build.ts` `compose()` therefore unzip the base artifact
completely, replace only the `Dockerfile` entry with the composed text,
and add the caller's context files and `template.json` on top — the new
zip is a strict superset of the base's own files. `assemble_artifact`/
`assembleArtifact` do this; `read_zip_entries`/`readZipEntries` is the
(new, from-scratch, dependency-free) zip reader behind it.

### T3. A from-scratch, dependency-free ZIP reader/writer in TypeScript instead of a new npm package

The research flagged "a zip writer" as a new TS dependency needing a
license audit (§3.6). Node has no built-in zip-archive writer (only
`node:zlib`'s raw DEFLATE), but the ZIP format's "stored" compression
method (0, no compression) needs nothing beyond a CRC-32 and the standard
local/central-directory/EOCD records — all of which fit in under 200 lines
with no dependency. `templates/zip-node.ts` writes with method 0 (valid,
just not space-optimal) and reads both method 0 and method 8 (DEFLATE,
what `rayito image publish`'s Python `zipfile.ZIP_DEFLATED` writes, via
`node:zlib`'s `inflateRawSync`) so it can unpack a base zip built by
either SDK. This avoids the dependency-audit question entirely rather than
deferring it.

### T4. Templates needs no `ConfigureSandbox` section; it needs no proto changes at all

Looking at `features/template_start.rs`'s stub (added by foundations):
unlike the other five features, `template_start`'s `Cfg`/`Status` are `()`
because there is nothing to configure over the gRPC channel — the
template's `StartSpec` is baked into the image at build time and read by
`rayd` from `/etc/rayito/template.json` at boot, before any RPC is
possible. This change adds no `.proto` file and touches no shared proto
file. `features/template_start.rs` itself is **not touched** in this
change (see the non-blocking follow-up in `proposal.md`): the slot stays
`Unsupported` until the `rayd`-side adapter work lands.

### T5. The e2b shim's `BuildError`/`TemplateError` naming collision (the open item foundations left) is resolved by retiring the stand-ins

`v06-foundations`'s `design.md` E-series decisions left `src/errors.ts`'s
real `BuildError`/`TemplateError` unexported from `index.ts`, and
`src/e2b/errors.ts` still declaring never-thrown stand-ins with a
different shape (`BuildError` there is a plain `Error`, not a
`SandboxError`), with an explicit note that m15-templates decides. Since
`Template.build()` now genuinely throws the native `BuildError`, the
cleanest resolution is the one already used for
`NotEnoughSpaceError`/`FileUploadError`: `rayito/e2b`'s `BuildError`/
`TemplateError` become re-exported bindings of the native classes (so
`instanceof` holds across entries), `src/e2b/errors.ts` is deleted, and
`src/index.ts` finally exports `BuildError`/`TemplateError`. Python's
equivalent (`e2b/exceptions.py`'s own `TemplateException`/`BuildException`
stand-ins, documented as "Rayito nunca la lanza") gets the same treatment:
both become straight re-exports of `rayito.exceptions.{TemplateException,
BuildException}` instead of separate classes. The divergence note that
E2B's `FileUploadException` inherits from `BuildException` while Rayito's
inherits from `TransferException` is unaffected and stays documented.

### T6. `memory_mb`/`memoryMb` validates against `limits.json`'s `supportedMemoryMiB` directly, not through `m15-sizes-catalog`'s `resolve_size()`

The architecture says `Template.build(memory_mb=, cpu_count=)` should go
through `m15-sizes-catalog`'s `resolve_size()` (rounding up, with
`RayitoCompatWarning`). That module (`_sizing.py`/`sizing.ts`) does not
exist yet: sizes-catalog is a sibling branch building in parallel, not a
dependency this change can import. `_build.py`/`build.ts` validate
`memoryMb` directly against `SUPPORTED_MEMORY_MIB` (already seeded in
`limits.json`/`_limits.py`/`limits.ts` by foundations) and reject an
unsupported value with `InvalidArgumentException`/`InvalidArgumentError`
before any AWS call — correct today, but without sizes-catalog's rounding
convenience. `cpu_count`/`cpuCount` is accepted and validated-but-ignored
(ARM64 is the only architecture Lambda MicroVMs supports, Q87): a
follow-up note in the docs page says image build inherits sizes-catalog's
`resolve_size()` once that change merges, per the suggested merge order
(sizes-catalog merges before templates).

### T7. `assignTags`/`removeTags`/`getTags`/`aliasExists` stay `UnimplementedError`, not a real tag API

`create-microvm-image`/`update-microvm-image` tag the **image**, not a
specific **version** (`tags` in the request, `ListTagsForResource` on the
image ARN). E2B's tagging API is per-template-version. Building a
per-version tag emulation (e.g. encoding tags into the description field)
would be a divergence nobody asked for; the four methods raise, naming
`AWS_API_NOTES.md §27` and suggesting the AWS CLI directly on the image
ARN as the workaround.

## Needs the maintainer (not applicable to this change)

None of D1-D4 from the M15 architecture are this change's to decide; D1
(SPEC.md §4 non-goals, the "no declarative templates/CLI" line) is drafted
in `v06-foundations`'s own `design.md`, not here.
