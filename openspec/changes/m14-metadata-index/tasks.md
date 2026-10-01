## 1. Contract (`AWS_API_NOTES.md` §20, hard rule 1)

- [x] 1.1 §20 "DynamoDB (M14, contrato de parámetros)": `PutItem` and `BatchGetItem` with only their used parameters and output fields, item schema, TTL format and the "within a few days" caveat, limits (100 keys, 16 MB), on-demand prices, error codes, IAM; verified offline against botocore 1.43.103 and `@aws-sdk/client-dynamodb` 3.1144.0.
- [x] 1.2 IDX-1 marked "A MEDIR" with a "SIN MEDIR" status line.
- [x] 1.3 `scripts/tests/test_metadata_index_template.py::test_the_sdks_only_name_operations_documented_in_section_20` greps `_index.py` and `src/index/**` against §20.

## 2. Core and adapter

- [x] 2.1 Python `rayito/_index.py`: `IndexRecord` (allow-listed attributes, `repr` without values), `record_for` (deterministic TTL), `joined` (pure, applied item by item by the listing), `DynamoDbIndex` (lazy client, conditional put, chunked batch get with bounded `UnprocessedKeys` retry, expired rows dropped, errors by code without AWS messages).
- [x] 2.2 TypeScript `src/index/{record,join,dynamodb}.ts` with the same contract; `@aws-sdk/client-dynamodb` optional peer + devDependency + `tsdown` external, loaded only through `loadOptionalPeer`.
- [x] 2.3 `SandboxIndexException`/`IndexWriteException`; TS `SandboxIndexError`/`IndexWriteError`; exported from both packages.
- [x] 2.4 Unit tests: `test_index_record.py`, `test_index_dynamodb.py` (incl. botocore `Stubber` against the service model); `index-record.test.ts` (incl. the shared golden fingerprint and lazy peer).

## 3. SDK option `index=` / `index`

- [x] 3.1 Python sync/async `create(index=)` (after `run-microvm`, before readiness; terminate/warn; `keep_on_failure`; `pool=` + `index=` rejected), `list`/`paginate(index=)` via `_listing_base` (`ListFilters.index_table`, `indexed_list_states`, `ListingSession.index_candidates/load_records/joined`, `PageWalk.buffered`).
- [x] 3.2 `PoolConfig(index=)` (validated; launch kwargs carry it only when set) for the sync and async pools.
- [x] 3.3 TypeScript `SandboxCreateOptions.index`, `SandboxListOptions.index`, `listing.ts`, `paginator.ts`, `PoolConfig.index`.
- [x] 3.4 "Coste y activación" blocks with runnable examples on `DynamoDbIndex`, `create`, `list`, `PoolConfig` (Python docstrings and TSDoc).
- [x] 3.5 Unit tests: `test_index_sync.py`, `test_index_async.py`, `test_index_pool.py`, `index-sandbox.test.ts` (one conditional put, failed put terminates before any token, warn continues, pool rejection, refill writes, SUSPENDED listing with 0 token/get/Health calls and 0 probe channels, rows without index excluded, index without metadata never calls DynamoDB, resumable token bound to the table, read failure raises, kill untouched, no client/peer without index, logs without metadata values). Existing listing tests unchanged.
- [x] 3.7 `reincarnate()` carries `index` into the successor (Python `LaunchOptions.index`, TypeScript `LaunchContext.index`); tests `test_index_reincarnate.py`, `index-reincarnate.test.ts`.
- [x] 3.8 I/O-free preflight before `run-microvm` (also on pool refill, which goes through `create()`): Python `DynamoDbIndex.prepare()` builds the boto3 client and raises `InvalidArgumentException` without a region; TypeScript `DynamoDbIndex.prepare()` (internal) resolves the region and loads the optional peer, raising `InvalidArgumentError`. Tests assert `RunMicrovm` is never called (`test_index_sync.py`, `test_index_async.py`, `index-sandbox.test.ts`).
- [ ] 3.6 e2e `clients/python/tests/e2e/test_metadata_index_e2e.py` and `clients/typescript/tests/e2e/metadata-index.e2e.test.ts` against real AWS (`RAYITO_E2E=1`, `RAYITO_E2E_INDEX_TABLE`): 3 sandboxes, 2 paused, the indexed listing returns exactly those 2 and `get-microvm` still shows them `SUSPENDED`; one is resumed and keeps its `startedAt` (±1 s) and stays in the indexed listing; IDX-1 (a, b, c) recorded. **Gate for archive; not run in this branch (no real AWS).**

## 4. Infra (`infra/metadata-index.yaml`)

- [x] 4.1 Table (`PAY_PER_REQUEST`, `pk` HASH, TTL `expires_at`, default SSE, PITR/deletion protection off by default) + `RayitoIndexWriter`/`RayitoIndexReader` on the table ARN; outputs.
- [x] 4.2 `infra/README.md` section (deploy, $0 at rest, delete to turn off) and `make infra-lint` lists the template.
- [x] 4.3 `scripts/tests/test_metadata_index_template.py`; `cfn-lint` 1.56.3 clean.

## 5. E2B shim and CLI

- [x] 5.1 Python `states_for`/`list_mapping(indexed=)`, `Sandbox.list(index=)`/`AsyncSandbox.list(index=)`, `E2B(index=)` (class attribute, not merged into other calls); reason text names `index=`.
- [x] 5.2 TypeScript `mapListOptions` with `index`, `SandboxListOpts.index`, `E2BClientOpts.index`; reason text names `index`.
- [x] 5.3 CLI `rayito sandbox list --metadata/--state/--index-table`; `--index-table` without `--metadata` is a usage error (exit 2) with no DynamoDB call.
- [x] 5.4 Tests: `test_index_e2b.py`, the e2b block of `index-sandbox.test.ts`, `cli/test_index_cli.py`.

## 6. Docs

- [x] 6.1 `observability.md` "Listado por metadatos con índice (opcional)"; `cli.md`; `api.md`.
- [x] 6.2 `optional-features.md` row 3 → "implementado, pendiente de aceptación en AWS real (M14)" + examples; flips to "disponible (0.5.0)" after 3.6.
- [x] 6.3 `e2b-parity.md` row 40 → divergente, counts 72/21/9/11; `e2b-compat.md` table and section.
- [x] 6.4 `SECURITY.md` T19 and `docs/site/docs/security.md`; docs tests in `test_metadata_index_template.py`.
- [x] 6.5 CHANGELOG `[Unreleased]` of both packages; READMEs.

## 7. Gates

- [x] 7.1 Python unit + ruff + format + mypy; TS install/lint/typecheck/build/test/pack:check; `scripts/tests`; `check_pins.py`; `check_hygiene.py`; `cfn-lint`.
- [x] 7.2 `openspec validate m14-metadata-index --strict`.
- [ ] 7.3 Archive after 3.6 passes on real AWS (not in this branch).
