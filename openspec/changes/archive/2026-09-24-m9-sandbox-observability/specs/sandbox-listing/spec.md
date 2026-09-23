## ADDED Requirements

### Requirement: Control plane exposes one list-microvms page
The Python `ControlPlane` port SHALL gain `list_microvms_page(*, image_arn, image_version, max_results, next_token) -> MicrovmListPage(items, next_token)` and the TypeScript port `listMicrovmsPage({ imageArn?, imageVersion?, maxResults, nextToken? }) → Promise<MicrovmListPage>`, each issuing exactly one `ListMicrovms` call with `maxResults` and, only when given, `nextToken`, `imageIdentifier` and `imageVersion` (the parameter names of `AWS_API_NOTES.md` §6), and returning every item unfiltered plus the response `nextToken` (`None`/`undefined` on the last page). The listing paginators SHALL always request `maxResults` 50. The existing `list_microvms`/`listMicrovms` SHALL keep their behaviour.

#### Scenario: one page, exact input
- **WHEN** the Stubber (Python) or the recording sender (TypeScript) expects a `ListMicrovms` request with `maxResults 50`, `nextToken "t1"` and `imageIdentifier <arn>`, answering `RUNNING` and `TERMINATED` items with `nextToken "t2"`
- **THEN** the page holds both items (the terminated one included) and `next_token == "t2"`, and a call without `next_token`/`image_version` sends neither key

### Requirement: The next_token is opaque, validated and shared by both SDKs
A listing `next_token` SHALL be the unpadded base64url of the canonical JSON (sorted keys, no spaces, UTF-8 without escaping) of either `{"v": 1, "f": <fingerprint>, "a": <AWS nextToken of the cursor's page or null>, "s": [<sorted 12-hex sha256 prefixes of the raw items already consumed from that page>]}` or, for ordered listings, `{"v": 1, "f": <fingerprint>, "k": [<startedAt ms>, <sandbox id>]}` of the last item served. The fingerprint SHALL be the first 16 hex chars of the sha256 of the canonical JSON of `{"image", "version", "states", "started_after_ms", "metadata", "order"}`, so a token never contains metadata values. Decoding SHALL raise `InvalidArgumentException` (`InvalidArgumentError` in TypeScript) without echoing the token for: more than 8 192 chars, invalid base64url or JSON, `v != 1`, a malformed fingerprint or digest, more than 50 digests, or neither/both cursor forms; a token whose fingerprint differs from the listing's filters SHALL raise the same error at the first page. Python and TypeScript SHALL produce byte-identical tokens (the golden vectors of design D8). Neither SDK SHALL log a token.

#### Scenario: golden vector
- **WHEN** both SDKs encode a page cursor with `a = null` and the digest of `microvm-00000000-0000-0000-0000-000000000001` for filters with image `arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base` and metadata `{"run": "ñ1"}`
- **THEN** both produce fingerprint `8a197ac8111983d7` and token `eyJhIjpudWxsLCJmIjoiOGExOTdhYzgxMTE5ODNkNyIsInMiOlsiMDU1OWU1MzI5OTZmIl0sInYiOjF9`, and each decodes the other's token to the same cursor

#### Scenario: foreign or broken tokens
- **WHEN** a paginator is built with `next_token="%%%"`, another with a valid token from a listing without `order` but with `order="asc"`, and a third with a valid token from an `order="asc"` listing but with `order="desc"`
- **THEN** the first two constructions raise `InvalidArgumentException` with no AWS call (a malformed token, a cursor form that does not match `order`), and the third raises it on its first `next_items()` before any `ListMicrovms` call

### Requirement: Python paginator with limit, next_token, order and filters
`Sandbox.paginate(*, template, template_version, states, metadata, started_after, order, limit, next_token, region, session, control_plane, transport, request_timeout) -> SandboxListPaginator` and the identical `AsyncSandbox.paginate(...) -> AsyncSandboxListPaginator` SHALL validate `limit` (`None` or an integer ≥ 1), `order` (`None`, `"asc"` or `"desc"`), `metadata` (only with `states ⊆ {RUNNING}`), `started_after` and the token at construction, before any AWS call, and SHALL resolve the template ARN lazily. The paginator SHALL expose `has_next`, `next_token` (the given token before the first call, the encoded cursor while `has_next`, else `None`) and `next_items()` (a coroutine on the async class), which SHALL raise `SandboxException` when `has_next` is `False`. Without `order`, `next_items()` SHALL consume raw items of 50-item pages until `limit` matches (all when `limit` is `None`), keeping an item when its state passes (explicit `states`, or the default that drops `TERMINATING|TERMINATED`), its `started_at >= started_after`, and, with `metadata`, the M6 probe matches (filling `metadata`); resuming from a token SHALL re-request the cursor's page and skip the items already consumed by identity. With `order`, the first `next_items()` SHALL walk every page (documented O(pages), plus the O(n) probe with `metadata`), sort by `(started_at, sandbox_id)` ascending or reversed, skip up to the token's key, and serve `limit` items per call. `template` SHALL travel as the server-side `imageIdentifier`. `Sandbox.list(...)` and `AsyncSandbox.list(...)` SHALL accept `started_after` and `order` with the same semantics, keep their return types, stream lazily without `order`, and keep their request sequence when the new kwargs are absent.

#### Scenario: walking and resuming with limit 1
- **WHEN** the fake control plane serves one page of three `RUNNING` items and one `TERMINATED` item and the unit test walks `Sandbox.paginate(limit=1)`, then builds a fresh `Sandbox.paginate(limit=1, next_token=<token after the first page>)` while the fake page now has a fourth `RUNNING` item inserted first
- **THEN** the first walk yields the three items once each with `has_next` `False` at the end, and the resumed walk yields the not-yet-consumed items plus the inserted one, never an item already returned

#### Scenario: order and filters
- **WHEN** the unit test lists items started at 30, 10 and 20 s with `order="asc"`, `order="desc"`, `states=["SUSPENDED"]` and `started_after` at 15 s
- **THEN** the orders are 10, 20, 30 and 30, 20, 10, the state filter keeps only the suspended item, and `started_after` keeps the items at 20 and 30 s

#### Scenario: metadata with a limit probes only what it consumes
- **WHEN** four `RUNNING` sandboxes are listed with `metadata={"env": "ci"}` and `limit=1`, the first one matching
- **THEN** one `next_items()` returns that sandbox with `metadata` filled after exactly one `Health` probe

#### Scenario: past the end
- **WHEN** `next_items()` is called again after it returned the last page
- **THEN** it raises `SandboxException`, in both the sync and the async paginator

### Requirement: TypeScript paginator and metadata filter
The TypeScript `Sandbox` SHALL expose `static paginate(options?: SandboxPaginateOptions): SandboxListPaginator` (`hasNext`, `nextToken`, `nextItems()`) with the Python semantics, and `static list(options?)` SHALL accept `metadata`, `startedAfter`, `order`, `requestTimeoutMs` (probe deadline, default 5 000 ms) and `transport` while still returning `AsyncIterable<SandboxListItem>`. With `metadata` it SHALL, sequentially per candidate, call `getMicrovm` (skipping `SandboxNotFoundError` and states other than `RUNNING`), mint one JWE for port 8080 through a fresh store, send one `Health` without `x-access-token` over a dedicated transport (one retry after a proxy 403, `sessionManager.abort()` afterwards), skip `agentReady === false`, keep subset matches with `metadata` filled, and reject with `SandboxError` naming the sandbox id when a `Health` fails; `metadata` with `states` other than `RUNNING` SHALL throw `InvalidArgumentError` before any AWS call. `SandboxListItem` SHALL gain `metadata?`. No log line SHALL carry metadata or a token.

#### Scenario: metadata filter against the fakes
- **WHEN** the plane lists three `RUNNING` sandboxes backed by three fake `rayd`s echoing `{env: "ci", run: "1"}`, `{env: "ci", run: "2"}` and `{}`, and the test walks `Sandbox.paginate({ metadata: { env: "ci", run: "2" }, limit: 1 })`
- **THEN** the walk yields only the second sandbox with its metadata, three tokens were minted, three `Health` calls were made, and every probe transport was aborted

#### Scenario: paginator parity
- **WHEN** the TypeScript unit test repeats the Python walking, resuming, order and past-the-end scenarios on the fake plane
- **THEN** it observes the same items, the same token strings and `SandboxError` past the end

### Requirement: Listing accepted on real AWS
`clients/python/tests/e2e/test_m9_observability.py::test_pagination_order_and_filters` SHALL create three sandboxes of one template 2 s apart and pause the third, then prove on real AWS: `paginate(template=T, started_after=t_before, limit=1)` starts with `has_next` `True` and its walk yields each created id exactly once and no id twice; a `next_token` from the first page passed to a fresh `paginate()` continues with each created id once overall; `order="asc"` and `"desc"` sort the three by `startedAt`; `states=["SUSPENDING", "SUSPENDED"]` and the shim `SandboxQuery(state=[SandboxState.PAUSED])` contain the third and neither of the others; `started_after` between the first and second creation excludes the first; every item listed with `template=T` has `template` equal to the ARN of `T`. `clients/typescript/tests/e2e/m9-observability.e2e.test.ts` SHALL prove the TypeScript paginator with a metadata filter walks exactly the sandboxes created with that metadata, continues from a `nextToken` in a fresh paginator, and sorts by `startedAt` with `order`.

#### Scenario: listing on AWS
- **WHEN** both e2e suites run with `RAYITO_E2E=1` and the M9 image
- **THEN** every assertion above holds, the printed `pages` and `walk_s` are recorded in the task notes, and no MicroVM created by the run stays alive
