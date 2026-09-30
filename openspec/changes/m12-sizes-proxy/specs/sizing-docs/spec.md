## ADDED Requirements

### Requirement: Sandbox size is documented as a property of the image, not a create() parameter
`docs/site/docs/limits.md` SHALL carry a "## Tamaño (CPU/RAM)" section, placed before "## Compatibilidad SDK ↔ rayd ↔ imagen", stating that:

- size (CPU/RAM) is fixed by the image (`resources[0].minimumMemoryInMiB` in `create-microvm-image`), never a `Sandbox.create()` parameter, the same shape E2B uses (`Template.build(cpu_count=, memory_mb=)` / `e2b template create --cpu-count --memory-mb`, also per build rather than per `Sandbox.create()`);
- the verified size table from `AWS_API_NOTES.md` (`512`/`1024`/`2048`/`4096`/`8192` → baseline/peak/disk/bandwidth), citing that section, and that other `minimumMemoryInMiB` values are unmeasured (RES-1);
- how to choose a size today: publish one image per size with a distinguishing name (`rayito image publish --memory-mib N --image-name <name>`) and create sandboxes against that image;
- the cost angle: the baseline row is what AWS bills while `RUNNING`, and every published image version additionally costs its snapshot storage (at least one week);
- the guest-view caveat: `SandboxInfo.cpu_count`/`memory_mb` report the guest's view (`nproc`/`MemTotal`), not the contracted baseline — measured `Q68` (`AWS_API_NOTES.md`): a 2048 MiB image showed `cpu_count=4`/`memory_mb=8016`, the peak of the range;
- there is no size catalog and no `resources=` resolver on `create()` (out of scope for this change).

`docs/site/docs/images.md`'s "Publicar las tres" section SHALL cross-reference the size section with the same `--memory-mib`/`--image-name` example. `docs/site/docs/e2b-compat.md`'s cpu/memory footnote row SHALL name the same per-image alternative.

The CLI SHALL NOT gain new `--memory-mib` validation as part of this requirement: unmeasured sizes remain accepted without a reference row.

#### Scenario: the size section cites its source and the guest-view caveat
- **WHEN** a reader opens `limits.md`'s "## Tamaño (CPU/RAM)" section
- **THEN** it cites `AWS_API_NOTES.md` and `Q68`, shows the five measured `minimumMemoryInMiB` rows, and states that `SandboxInfo.cpu_count`/`memory_mb` are the guest's view, not the baseline

#### Scenario: images.md points at the same mechanism
- **WHEN** a reader opens `images.md`'s "Publicar las tres" section
- **THEN** it names `--memory-mib`/`--image-name` and links to `limits.md#tamano-cpuram`

#### Scenario: mkdocs still builds in strict mode
- **WHEN** `mkdocs build --strict` runs against `docs/site/mkdocs.yml`
- **THEN** it exits 0 with no warnings
