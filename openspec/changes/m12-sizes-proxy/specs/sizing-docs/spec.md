## ADDED Requirements

### Requirement: Sandbox size is documented as a property of the image, not a create() parameter
`docs/site/docs/limits.md` SHALL carry a "## Tamaño (CPU/RAM)" section, placed before "## Compatibilidad SDK ↔ rayd ↔ imagen", stating that:

- size (CPU/RAM) is fixed by the image (`resources[0].minimumMemoryInMiB` in `create-microvm-image`), never a `Sandbox.create()` parameter, the same shape E2B uses (`Template.build(cpu_count=, memory_mb=)` / `e2b template create --cpu-count --memory-mb`, also per build rather than per `Sandbox.create()`);
- the size table from `AWS_API_NOTES.md` §4 (`512`/`1024`/`2048`/`4096`/`8192` → baseline/peak/disk/bandwidth), attributed to AWS's own documentation, not to a measurement — only the 2048 bandwidth (§7, measured 4.54 MB/s) and the guest view at 2048 (`Q68`) carry a "medido" label; a `$/h` column derived from `AWS_API_NOTES.md` §12 / `cost.md`; and that other `minimumMemoryInMiB` values are neither documented nor measured (RES-1);
- how to choose a size today: publish one image per size with a distinguishing name (`rayito image publish --memory-mib N --image-name <name>`) and create sandboxes against that image;
- the cost angle: AWS bills the baseline row per second while `RUNNING`, and separately bills any consumption above baseline (up to the peak) at the vCPU/GB actually consumed, not at the next size's price; every published image version additionally costs its snapshot storage (at least one week);
- the guest-view caveat: `SandboxInfo.cpu_count`/`memory_mb` report the guest's view (`nproc`/`MemTotal`), not the contracted baseline — measured `Q68` (`AWS_API_NOTES.md`): a 2048 MiB image showed `cpu_count=4`/`memory_mb=8016`, the peak of the range — and that code sizing its parallelism from `nproc` or total memory can cross the baseline and, per the point above, pay for it, not just run faster for free;
- there is no size catalog and no `resources=` resolver on `create()` (out of scope for this change).

`docs/site/docs/images.md`'s "Publicar las tres" section SHALL cross-reference the size section with the same `--memory-mib`/`--image-name` example, without calling the table "measured". `docs/site/docs/e2b-compat.md`'s cpu/memory footnote row SHALL name the same per-image alternative.

The CLI SHALL NOT gain new `--memory-mib` validation as part of this requirement: unmeasured sizes remain accepted without a reference row.

#### Scenario: the size section cites its source and never overstates what is measured
- **WHEN** a reader opens `limits.md`'s "## Tamaño (CPU/RAM)" section
- **THEN** it cites `AWS_API_NOTES.md` and `Q68`, shows the five `minimumMemoryInMiB` rows with their `$/h` baseline cost, attributes the table to AWS's documentation rather than a measurement, and states that `SandboxInfo.cpu_count`/`memory_mb` are the guest's view, not the baseline

#### Scenario: images.md points at the same mechanism
- **WHEN** a reader opens `images.md`'s "Publicar las tres" section
- **THEN** it names `--memory-mib`/`--image-name` and links to `limits.md#tamano-cpuram`

#### Scenario: mkdocs still builds in strict mode
- **WHEN** `mkdocs build --strict` runs against `docs/site/mkdocs.yml`
- **THEN** it exits 0 with no warnings
