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
`CustomDomain.register(alias, port, endpoint=, jwe=, traffic_token=None, ttl_seconds=)` SHALL validate `ttl_seconds > 0` and the encoded metadata size before any KVS call, then call `DescribeKeyValueStore` once and `PutKey` twice (`j:<label>` with the JWE, `m:<label>` with `{endpoint, sha256(traffic_token) or "", expires_at}`), chaining each call's returned `ETag` into the next. `traffic_token`, when given, SHALL be stored only as its sha256 hex digest, never in clear text. `unregister(alias, port)` SHALL call `DescribeKeyValueStore` once and `DeleteKey` on both keys, and SHALL be idempotent: a `ResourceNotFoundException` (surfaced as `aws_code`/`awsCode` on the raised exception) from a missing key SHALL NOT propagate.

#### Scenario: register writes exactly two chained PutKey calls
- **WHEN** `register("ws-7", 8000, endpoint=..., jwe=..., ttl_seconds=2400)` runs against a fake KVS already seeded for the stack's `KvsArn`
- **THEN** the fake's calls are exactly `describe, put, put`, the `j:8000-ws-7` value equals the given JWE, and the `m:8000-ws-7` value's `endpoint` field equals the given endpoint

#### Scenario: a traffic token is never stored in clear text
- **WHEN** `register(..., traffic_token="a-secret-token")` runs
- **THEN** the stored `m:<label>` value does not contain the substring `"a-secret-token"`

#### Scenario: unregistering a route that was never registered is a no-op
- **WHEN** `unregister("never-registered", 8000)` runs against a deployed (but not yet used for this route) fake KVS
- **THEN** it does not raise

### Requirement: refresh rewrites only the JWE key
`CustomDomain.refresh(route, jwe=, ttl_seconds=)` SHALL rewrite only `j:<label>` (one `DescribeKeyValueStore` + one `PutKey`) and SHALL leave `m:<label>` untouched, returning a `CustomDomainRoute` with the same `alias`/`port`/`public_domain` and a new `expires_at`.

#### Scenario: metadata is unchanged after a refresh
- **WHEN** a route is registered and then `refresh(route, jwe="new-jwe", ttl_seconds=2400)` runs
- **THEN** `j:<label>` equals `"new-jwe"` and `m:<label>` is byte-identical to its value before the refresh

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

### Requirement: the custom-domain stack creates no Lambda and no IAM beyond CloudFront's own
`infra/custom-domain.yaml` SHALL declare exactly `AWS::CloudFront::Distribution`, `AWS::CloudFront::Function` and `AWS::CloudFront::KeyValueStore`, require no `Capabilities`, and pass `cfn-lint`. The Function's `FunctionConfig.Runtime` SHALL be `cloudfront-js-2.0` and its `KeyValueStoreAssociations` SHALL reference the template's own `AWS::CloudFront::KeyValueStore`. `DefaultCacheBehavior` SHALL use the managed `CachingDisabled` cache policy (routing is per sandbox and must never be cached) and SHALL associate the Function on `viewer-request`.

#### Scenario: the template's resource set is exactly these three types
- **WHEN** `infra/custom-domain.yaml` is parsed
- **THEN** the set of `Resources[*].Type` is exactly `{AWS::CloudFront::Distribution, AWS::CloudFront::Function, AWS::CloudFront::KeyValueStore}`

### Requirement: the embedded Function source never drifts from its own file
`infra/custom-domain.yaml`'s `RouterFunction.Properties.FunctionCode` SHALL be byte-identical (ignoring a single trailing newline) to `infra/functions/custom_domain_router.js`, since CloudFormation has no file-include mechanism for this property.

#### Scenario: the YAML and the JS file match
- **WHEN** both files are read and compared
- **THEN** the embedded `FunctionCode` string equals the `.js` file's content
