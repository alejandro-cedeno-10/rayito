## ADDED Requirements

### Requirement: Route hostnames and KeyValueStore keys are pure and validated before any AWS call
`route_host(alias, port, public_domain)` (Python `rayito._custom_domain._domain`, TypeScript `custom-domain/domain.ts`) SHALL compute `{port}-{alias}.{public_domain}` and SHALL reject, before any network call: a `port` outside `1..65535` or in `limits.json`'s `reservedPorts` (M15 foundations); an `alias` that is not a lowercase DNS-safe label; and a `public_domain` whose labels are not all DNS-safe. `kvs_json_key`/`kvs_meta_key` SHALL prefix the computed label with `j:`/`m:`.

#### Scenario: shared fixture round-trips in both SDKs
- **WHEN** every `valid` case in `testdata/custom-domain/hostnames.json` is passed to `route_host`/`routeHost`
- **THEN** the computed host equals the fixture's `host` field, in both the Python and TypeScript test suites

#### Scenario: reserved and out-of-range ports are rejected
- **WHEN** `route_host`/`routeHost` is called with a port from `limits.json`'s `reservedPorts`, or with `0`, a negative number or a number above `65535`
- **THEN** `InvalidArgumentException`/`InvalidArgumentError` is raised and no AWS call is made

### Requirement: A route's KeyValueStore value never exceeds the real limit
`RouteMetadata.encode()` (Python) / `encodeRouteMetadata` (TypeScript) and the JWE write path SHALL reject, before calling `PutKey`, any value whose UTF-8 encoding exceeds 1024 bytes (`MAX_KVS_VALUE_BYTES`), raising `CustomDomainException`/`CustomDomainError`.

#### Scenario: a real-sized JWE is accepted
- **WHEN** `check_kvs_value_size`/`checkKvsValueSize` is called with an 823-byte string (the measured size of a `create-microvm-auth-token` JWE, DOM-1)
- **THEN** it does not raise

#### Scenario: an oversized value is rejected before touching the KVS
- **WHEN** `CustomDomain.register(...)` is called with a `jwe` or an `endpoint` whose encoded metadata exceeds 1024 bytes
- **THEN** `CustomDomainException`/`CustomDomainError` is raised and the fake `KeyValueStoreWriter` records no call

### Requirement: the KeyValueStore adapter needs its SigV4A dependency, and fails clearly without it
`cloudfront-keyvaluestore`'s endpoint rules require a SigV4A signer regardless of its `service-2.json` `signatureVersion`. Python SHALL make this work through the optional extra `rayito[custom-domain]` (`awscrt`); TypeScript SHALL load the optional peer `@aws-sdk/signature-v4a` lazily, before building the KeyValueStore client. Either adapter SHALL translate the SDK's own missing-dependency failure into a `CustomDomainException`/`CustomDomainError`/`InvalidArgumentError` that names the exact package to install, never surfacing the SDK's own low-level error.

#### Scenario: a real client signs with SigV4A when the dependency is present
- **WHEN** a `CloudFrontKvsWriter` built with dummy AWS credentials calls `describe` against a real boto3 `cloudfront-keyvaluestore` client, with `awscrt` installed and a `before-send` hook that captures the request and stops it short of the network
- **THEN** the captured `Authorization` header starts with `AWS4-ECDSA-P256-SHA256`

#### Scenario: a missing dependency is mapped to a clear error
- **WHEN** the SDK's own missing-dependency exception (`MissingDependencyException` in botocore, a failed dynamic `import` of `@aws-sdk/signature-v4a` in TypeScript) is raised during a KVS call
- **THEN** the caller sees `CustomDomainException`/`CustomDomainError`/`InvalidArgumentError` naming the package to install, not the SDK's own exception type

### Requirement: Constructing CustomDomain makes no AWS call
`CustomDomain(public_domain=...)` / `new CustomDomain({publicDomain})` (Python sync, Python `AsyncCustomDomain`, TypeScript) SHALL build no `cloudformation` or `cloudfront-keyvaluestore` client at construction time; both the `OptionalStacks` facade and the `KeyValueStoreWriter` adapter SHALL be constructed lazily.

#### Scenario: construction is free
- **WHEN** `CustomDomain(public_domain="sbx.example.com")` is constructed with no explicit `provisioner=`/`kvs_writer=`, with `boto3.session.Session.client` spied on
- **THEN** zero calls are recorded

### Requirement: deploy/status/destroy are a thin facade over OptionalStacks
`CustomDomain.deploy(certificate_arn=...)` SHALL call `OptionalStacks.deploy("custom-domain", parameters={"PublicDomain": ..., "CertificateArn": ...}, ...)` and SHALL cache the stack's `KvsArn` output for later `register`/`unregister`/`refresh` calls. `status()` SHALL do the same caching without deploying. `destroy()` SHALL forget any cached `KvsArn` learned this way (an explicit `kvs_arn=`/`kvsArn` passed at construction is never forgotten). A blocked or failed `OptionalStacks` deploy (e.g. a stack in `ROLLBACK_COMPLETE`) SHALL propagate as `StackException`/`StackError` unchanged.

#### Scenario: deploy delegates with the right parameters and caches KvsArn
- **WHEN** `CustomDomain(public_domain=...).deploy(certificate_arn=...)` runs against a fake `StackProvisioner` that returns a `KvsArn` output
- **THEN** the fake provisioner's calls are exactly `describe, create, wait, describe` (or `describe, update, wait, describe` for an existing stack) and `CustomDomain.kvs_arn()` afterwards returns that output without another call

#### Scenario: a blocked stack surfaces StackException
- **WHEN** `deploy()` runs against a fake stack already in `ROLLBACK_COMPLETE`
- **THEN** `StackException`/`StackError` is raised naming the stack, and no `KeyValueStoreWriter` call is made

### Requirement: register/unregister write and remove both KeyValueStore keys with a chained ETag
`CustomDomain.register(alias, port, endpoint=, jwe=, traffic_token=None, public=False, ttl_seconds=)` SHALL validate, before any KVS call: `ttl_seconds > 0`; the encoded metadata size; and that `traffic_token` is given unless `public=True` (a route is never public by omission, SEC-T25), raising `InvalidArgumentException`/`InvalidArgumentError` otherwise. It SHALL then call `DescribeKeyValueStore` once and `PutKey` twice (`j:<label>` with the JWE, `m:<label>` with `{endpoint, sha256(traffic_token) or "", expires_at}`), chaining each call's returned `ETag` into the next. `traffic_token`, when given, SHALL be stored only as its sha256 hex digest, never in clear text. If the second `PutKey` fails, the first SHALL be deleted best-effort before the error propagates, so a route never ends up with a live `j:` and no `m:`. The whole describe-then-write sequence SHALL be retried, up to a bounded number of times, when a write is rejected for an `ETag` conflict with another writer of the same route. `unregister(alias, port)` SHALL call `DescribeKeyValueStore` once and `DeleteKey` on both keys, and SHALL be idempotent: a `ResourceNotFoundException` (surfaced as `aws_code`/`awsCode` on the raised exception) from a missing key SHALL NOT propagate.

#### Scenario: register writes exactly two chained PutKey calls
- **WHEN** `register("ws-7", 8000, endpoint=..., jwe=..., public=True, ttl_seconds=2400)` runs against a fake KVS already seeded for the stack's `KvsArn`
- **THEN** the fake's calls are exactly `describe, put, put`, the `j:8000-ws-7` value equals the given JWE, and the `m:8000-ws-7` value's `endpoint` field equals the given endpoint

#### Scenario: a traffic token is never stored in clear text
- **WHEN** `register(..., traffic_token="a-secret-token")` runs
- **THEN** the stored `m:<label>` value does not contain the substring `"a-secret-token"`

#### Scenario: a route is never public by omission
- **WHEN** `register("ws-7", 8000, endpoint=..., jwe=..., ttl_seconds=60)` runs with neither `traffic_token` nor `public=True`
- **THEN** `InvalidArgumentException`/`InvalidArgumentError` is raised and the fake `KeyValueStoreWriter` records no call

#### Scenario: a failed metadata write rolls back the JWE
- **WHEN** `register(...)` runs against a fake KVS configured to fail the `m:<label>` `PutKey` with a non-conflict error
- **THEN** the raised exception propagates, and neither `j:<label>` nor `m:<label>` is present in the fake's store afterwards

#### Scenario: unregistering a route that was never registered is a no-op
- **WHEN** `unregister("never-registered", 8000)` runs against a deployed (but not yet used for this route) fake KVS
- **THEN** it does not raise

### Requirement: refresh rewrites both KeyValueStore keys, keeping the route's endpoint and token hash
`CustomDomain.refresh(route, jwe=, ttl_seconds=)` SHALL rewrite `j:<label>` and `m:<label>` (one `DescribeKeyValueStore` and the same chained-write/retry/rollback behavior as `register`), rebuilding `m:<label>` from `route.endpoint`/`route.traffic_token_sha256` with a new `expires_at` — so a route's stored expiry never goes stale after a refresh — and returning a `CustomDomainRoute` with the same `alias`/`port`/`public_domain`/`endpoint`/`traffic_token_sha256` and the new `expires_at`.

#### Scenario: a refresh updates the stored expiry without changing endpoint or token hash
- **WHEN** a route is registered and then `refresh(route, jwe="new-jwe", ttl_seconds=2400)` runs
- **THEN** `j:<label>` equals `"new-jwe"`, `m:<label>`'s `x` field equals the new expiry, and the refreshed route's `endpoint`/`traffic_token_sha256` equal the original route's

### Requirement: the CloudFront Function strips viewer-supplied proxy headers before computing its own
`infra/functions/custom_domain_router.js`'s viewer-request handler SHALL delete every incoming header whose name starts with `x-aws-proxy-` before calling `cf.updateRequestOrigin`, so a viewer can never inject or override the `x-aws-proxy-auth`/`x-aws-proxy-port` headers the Function sets from the route's KeyValueStore entry.

#### Scenario: a forged proxy header is removed
- **WHEN** `stripUpstreamProxyHeaders` runs on a headers object containing `x-aws-proxy-auth` and `x-aws-proxy-port` set by the viewer
- **THEN** neither key remains in the headers object afterwards

### Requirement: a route's traffic token is checked in constant time, and an empty hash means a public route
`trafficTokenAccepted` SHALL return `true` without comparing anything when the route's stored token hash is empty (a public route), and otherwise SHALL compare the sha256 of the token found in the `e2b-traffic-access-token` header or the `rayito_tt` cookie (in that order) against the stored hash using a constant-time comparison, rejecting a request that supplies neither.

#### Scenario: a public route accepts any request
- **WHEN** `trafficTokenAccepted(headers, "")` is called with any `headers`
- **THEN** it returns `true`

#### Scenario: the correct token in the cookie is accepted, the wrong one in the header is not
- **WHEN** `trafficTokenAccepted` is called with a `rayito_tt` cookie matching the stored hash, and separately with an `e2b-traffic-access-token` header that does not
- **THEN** the first call returns `true` and the second returns `false`

### Requirement: the router's pure routing decision denies reserved ports and normalizes the host before any KVS lookup
`route(request, kvsGet)` (`infra/functions/custom_domain_router.js`) SHALL lower-case the viewer's `Host` header before deriving the route label, and SHALL return a 404 decision — without calling `kvsGet` — when the label's port is one of `RESERVED_PORTS` (kept equal to `limits.json`'s `reservedPorts`), as defense in depth on top of the SDKs already refusing to register a route on one of those ports.

#### Scenario: a reserved port is denied without a KVS call
- **WHEN** `route` is called with a host whose port is in `RESERVED_PORTS`, and a `kvsGet` that throws if invoked
- **THEN** the returned decision's `kind` is `"not-found"` and `kvsGet` is never called

#### Scenario: an upper-case host resolves the same route as its lower-case form
- **WHEN** `route` is called with a host whose label is upper-case, against a KVS seeded under the lower-case label
- **THEN** the returned decision's `kind` is `"origin"`

### Requirement: the custom-domain stack creates no Lambda and no IAM beyond CloudFront's own, and never forwards Host to the chosen origin
`infra/custom-domain.yaml` SHALL declare exactly `AWS::CloudFront::Distribution`, `AWS::CloudFront::Function` and `AWS::CloudFront::KeyValueStore`, require no `Capabilities`, and pass `cfn-lint`. The Function's `FunctionConfig.Runtime` SHALL be `cloudfront-js-2.0` and its `KeyValueStoreAssociations` SHALL reference the template's own `AWS::CloudFront::KeyValueStore`. `DefaultCacheBehavior` SHALL use the managed `CachingDisabled` cache policy (routing is per sandbox and must never be cached) and the managed `AllViewerExceptHostHeader` origin request policy — never plain `AllViewer`, which would forward the viewer's `Host` to the per-request origin `RouterFunction` picks and break CloudFront's TLS/SNI check against that origin's own certificate (DOM-2) — and SHALL associate the Function on `viewer-request`.

#### Scenario: the template's resource set is exactly these three types
- **WHEN** `infra/custom-domain.yaml` is parsed
- **THEN** the set of `Resources[*].Type` is exactly `{AWS::CloudFront::Distribution, AWS::CloudFront::Function, AWS::CloudFront::KeyValueStore}`

### Requirement: the deployed Function code never drifts from its source file, and carries no ES module syntax
`infra/custom-domain.yaml`'s `RouterFunction.Properties.FunctionCode` SHALL equal `infra/functions/custom_domain_router.js` with every top-level `export` keyword removed (ignoring a single trailing newline): CloudFormation has no file-include mechanism for this property, and the `cloudfront-js-2.0` runtime has never been documented to support `export` in a function's own code (only `import` of its builtins, which the deployed code keeps).

#### Scenario: the YAML and the JS file match once exports are stripped
- **WHEN** both files are read, and `export` is removed from each top-level declaration in the `.js` file's content
- **THEN** the embedded `FunctionCode` string equals that stripped content

#### Scenario: the embedded FunctionCode has no top-level export
- **WHEN** the embedded `FunctionCode` string is itself run through the same export-stripping transform
- **THEN** the result is unchanged
