## Why

Operating Rayito today means knowing four scripts by heart:
`scripts/publish_image.py` (upload + `create/update-microvm-image` + the
three-state gate), `scripts/image_prune.py` (serialised version deletes),
`scripts/image_zip.py` and `scripts/copy_sidecar.py` (the artifact), each
with its own `argparse` surface and its own `uv run --with boto3` incantation
in the `Makefile`. Nothing lists images or sandboxes, kills a stray MicroVM
or tails its CloudWatch stream without hand-written boto3, and nothing tells
a newcomer on a fresh AWS account *why* `Sandbox.create()` fails
(credentials, region, quotas, IAM, bucket, image not launchable, SDK older
than the agent). E2B ships `e2b template build` and `e2b sandbox list|kill|logs`
for exactly this. The M7 research report
(`docs/research/2026-09-m7-oss-readiness.md` §3e and §4 "`rayito doctor`",
change 6 of §5) recommends a thin `typer` CLI over the existing scripts plus
a `doctor` command, and `MILESTONES.md` lists it as `m7-cli`.

`SPEC.md` §4 rejects "templates declarativos con CLI propia
(`rayito template build`)": a config-file template builder. This change
ships an **operational** CLI over the Dockerfile flow that already exists
and adds no `rayito.toml`; `design.md` D15 records the distinction and the
one-line clarification added to `SPEC.md` §4.

## What Changes

Track 6 of M7, every decision closed in `design.md`:

- **`rayito` CLI as an optional extra.** `rayito[cli]` adds `typer` to the
  Python SDK; console entry point `rayito` (and `python -m rayito.cli`).
  Without the extra the entry point prints how to install it and exits 2.
- **Scripts become library code.** The logic of `publish_image.py`,
  `image_prune.py`, `image_zip.py` and `copy_sidecar.py` moves to
  `rayito.cli._publish`, `rayito.cli._prune` and `rayito.cli._artifact`
  (behaviour preserved: content-addressed S3 key, version reuse with the
  numeric `baseImageVersion` comparison of Q52, the three-state gate, the
  hooks table, build-log tail on failure, the keep set and 5/10/20/40/80 s
  backoff of prune, the deterministic zip and the `slim` marker). The four
  files under `scripts/` stay as thin shims with the same argv, so
  `make image-zip`, `image-publish*` and `image-prune` keep working; the
  `Makefile` runs the two AWS shims through `uv run --project clients/python`
  instead of `uv run --with boto3`, and passes the artifact bucket explicitly
  (`BUCKET`) because the library no longer hard-codes the maintainer's
  bucket.
- **`rayito image publish|list|prune|zip`.** `publish` is the script;
  `list` shows images or, with a name, that image's versions; `prune` is the
  script; `zip` builds the artifact (optionally copying the sidecar first).
- **`rayito sandbox list|kill|info|logs`.** Over the SDK's own control plane
  (`Sandbox.list/kill/get_info`, shared token buckets); `logs` resolves the
  CloudWatch stream `YYYY/MM/DD[<imageVersion>]<microvmId>` of a sandbox
  from `get-microvm` and prints its events.
- **`rayito doctor`.** Ten checks with `OK | WARN | FAIL | SKIP`:
  credentials/region, `list-managed-microvm-images` reachability, applied
  Service Quotas versus the published defaults, `iam:SimulatePrincipalPolicy`
  over the caller's actions (advisory), bucket access, the image three-state
  gate, live/orphan `RUNNING` MicroVMs, a minted auth token, the agent's
  `Health` (`agent_version`, `kernel_ready`, `imds_blocked`) and the SDK /
  `agent_version` / image compatibility table. `--launch` creates and kills a
  300 s sandbox so the last three run on a fresh account (about $0.002);
  without it they run against the newest `RUNNING` sandbox of the template or
  `SKIP`. Exit 1 only on `FAIL`.
- **Never prints secrets.** No JWE, access token or `runHookPayload` in any
  output, human or `--json` (`SECURITY.md`).
- **Tests.** Unit tests with `typer.testing.CliRunner` + `botocore.stub.Stubber`
  under `clients/python/tests/unit/cli/` (the three script test files migrate
  there); shim smoke tests stay in `scripts/tests/`; e2e
  `tests/e2e/test_m7_cli.py` runs `rayito doctor --launch`, `rayito image
  list`, `rayito sandbox list|info|logs` and `rayito image publish` (reuse
  path, no build) against the account.
- **Docs.** `docs/site/docs/cli.md` (nav entry), the compatibility table in
  `limits.md` with a drift test, README/quickstart pointers, changelog and
  `MILESTONES.md` row.

## Capabilities

### New Capabilities

- `cli`: the `rayito` command-line tool: packaging (extra, entry point,
  shims), `image`, `sandbox` and `doctor` commands, output and secret rules,
  tests and the real-AWS acceptance.

### Modified Capabilities

- `image-lifecycle`: the prune requirement names `rayito.cli._prune` as
  the implementation and `rayito image prune` / `scripts/image_prune.py` as
  its two equivalent entry points.
- `python-release`: the docs site nav gains `cli.md` (and records the
  already-present `verify.md`).

## Impact

- `clients/python/pyproject.toml`: `[project.optional-dependencies] cli`,
  `[project.scripts] rayito`, `typer` in the `dev` group (same pin), new
  `mypy`/`ruff` coverage of `src/rayito/cli` and `tests/unit/cli`.
- `clients/python/src/rayito/cli/` (new package), one internal refactor in
  `sandbox_sync/main.py` and `sandbox_async/main.py` (`probe_metadata` split
  into a `Health` probe plus the metadata projection; public surface
  unchanged).
- `scripts/publish_image.py`, `image_prune.py`, `image_zip.py`,
  `copy_sidecar.py` reduced to shims; `scripts/tests/test_publish_image.py`,
  `test_image_prune.py`, `test_image_variant.py` move to
  `clients/python/tests/unit/cli/`; `scripts/tests/test_shims.py` added.
- `Makefile` (`image-publish*`, `image-prune`, `BUCKET`, `BOTO3_SPEC`
  removed, `test-scripts` comment), `.github/workflows/ci.yml` step name of
  the scripts test (the `build` job and `release.yml` keep calling the two
  stdlib shims with bare `python3`: unchanged).
- Docs: `docs/site/docs/cli.md`, `mkdocs.yml` nav, `limits.md`,
  `quickstart.md`, `README.md`, `clients/python/README.md`,
  `clients/python/CHANGELOG.md`, `ARCHITECTURE.md` script references,
  `SPEC.md` §4 clarification, `MILESTONES.md` M7 row.
- No change to `rayd`, the proto, the image, the sidecar or the TypeScript
  SDK. No new AWS parameter: every `lambda-microvms` name used is in
  `AWS_API_NOTES.md` §1–§4, §6, §10, §11, §14 or
  `docs/aws-api/model_summary.md`; the IAM, STS, Service Quotas, S3 and
  CloudWatch Logs calls are standard boto3 operations whose shapes
  `design.md` lists from botocore 1.43.94.
- Cost of acceptance: about $0.03 for the e2e sandbox fixtures plus about
  $0.002 for the doctor's own sandbox; `rayito image publish` in the
  acceptance reuses the current version (no build, no storage).
