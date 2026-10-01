## Why

E2B's `allow_public_traffic`/`get_host()` give a stable, header-free public
URL per sandbox port. Rayito 0.5.x has no equivalent (`e2b-parity.md` row
15: "imposible en la plataforma" — every request needs the
`x-aws-proxy-auth`/`x-aws-proxy-port` headers that `HostAccess` already
carries). That is fine for programmatic clients but breaks anything that
needs a plain HTTPS URL: a browser tab, a webhook target, an iframe. M15
foundations (`v06-foundations`) pre-added the `domain=` kwarg (still
`UnimplementedError`), the `CustomDomainException` class and the
`custom-domain` `OptionalStack` slot (`supported: false`) for this change to
fill.

This change builds the piece that does not need AWS or a maintainer-owned
domain to build and test: a CloudFront distribution with a wildcard alias
that a viewer-request CloudFront Function routes per sandbox via its
KeyValueStore (`cf.updateRequestOrigin`, JavaScript runtime 2.0), and the
`CustomDomain` SDK class (Python sync/async, TypeScript) that deploys that
stack and manages its routes (`register`/`unregister`/`refresh`/`hostFor`).
D3 (a domain and an ACM certificate the maintainer owns, in `us-east-1`)
blocks everything that needs AWS: DOM-2 (HTTP/1.1 through
`updateRequestOrigin`), DOM-3 (WebSocket upgrade), DOM-5 (KVS propagation
latency), DOM-7 (JWE keep-alive) and DOM-8 (auto-resume) stay unmeasured
here and are documented as pending the AWS acceptance stage.

## What Changes

- **Pure domain** (`_custom_domain/_domain.py`, `custom-domain/domain.ts`):
  the hostname format `{port}-{alias}.{public_domain}`, alias/port/domain
  validation (`reservedPorts` from `limits.json`, M15 foundations), the two
  KeyValueStore keys per route (`j:<label>` the JWE, `m:<label>` compact
  JSON metadata) and the 1 KiB value-size guard (DOM-1: a real JWE measures
  823 B, verified against `create-microvm-auth-token`'s own output). Shared
  test fixture `testdata/custom-domain/hostnames.json` (Python + TypeScript).
- **Port + adapter** (`_custom_domain/_kvs.py`, `custom-domain/kvs.ts`): a
  `KeyValueStoreWriter` port and a `CloudFrontKvsWriter` adapter
  (`DescribeKeyValueStore`/`PutKey`/`DeleteKey`, `ETag`-chained). Verified
  offline against the `cloudfront-keyvaluestore` service model (botocore
  1.43.103): `signatureVersion: v4`, **not** SigV4A as the research assumed
  — no `awscrt`/`@aws-sdk/signature-v4a` dependency needed, a correction
  recorded in `AWS_API_NOTES.md` §29.
- **Service** `CustomDomain`/`AsyncCustomDomain` (Python),
  `CustomDomain` (TypeScript, one async class): `deploy`/`status`/`destroy`
  as a thin facade over `OptionalStacks` (M15 foundations), plus
  `register`/`unregister`/`refresh`/`hostFor` for route management. Every
  method's "Coste y activación" block is in the class docstring/TSDoc and
  (TypeScript) registered in `check-dts-cost-blocks.mjs`.
- **Stack** `infra/custom-domain.yaml`: `AWS::CloudFront::Distribution`
  (wildcard alias, ACM cert parameter, a placeholder origin that
  `RouterFunction` always replaces per request), `AWS::CloudFront::Function`
  (`cloudfront-js-2.0`, viewer-request) and `AWS::CloudFront::KeyValueStore`.
  No Lambda, no IAM beyond CloudFront's own. `infra/functions/
  custom_domain_router.js` is the function's source of truth (its pure
  helpers are unit-tested with `node:test`; `infra/custom-domain.yaml`
  embeds it verbatim under `FunctionCode`, kept in sync by
  `scripts/tests/test_custom_domain_function_sync.py`). `cfn-lint` clean.
  `_stacks/components/custom_domain.py` / `stacks/components/custom-domain.ts`
  now `supported: true`.
- **CLI** `rayito domain deploy|status|destroy` (`cli/domain.py`): a thin
  facade over `rayito stack ... custom-domain` with `CustomDomain`'s own
  parameter names (`--public-domain`, `--certificate-arn`).
- **Exports**: `CustomDomain`, `AsyncCustomDomain`, `CustomDomainRoute` from
  `rayito`; `CustomDomain` and its option/result types from TypeScript's
  `rayito` root.
- **Not in this change (explicit, non-blocking follow-up):** wiring
  `Sandbox.create(domain=)`/`get_host()`/`expose()`/`unexpose()` to this
  service. `_feature_options.py`/`feature-options.ts` keep raising
  `UnimplementedError` for `domain=` — foundations did not end up
  pre-adding the `HostResolver` seam or the `expose()`/`unexpose()`
  delegating members that architecture §1(g) called for, and
  `sandbox_{sync,async}/main.py` is foundations-only for kwargs,
  delegations and exports (§5 of the M15 architecture). Wiring it in
  properly needs a small, focused addition to that shared file — the same
  "don't ship it half-safe" call `v06-foundations` made for the orphan
  reaper. `CustomDomain` is fully usable standalone today (deploy the
  stack, then `register()`/`unregister()` routes yourself using the JWE and
  endpoint your own code already has) and the docs page says so plainly.
  See `design.md` for the reasoning and the exact follow-up shape.

## Impact

- **Python**: new package `rayito/_custom_domain/` (`__init__.py`,
  `_domain.py`, `_kvs.py`, `_service.py`, `_service_async.py`);
  `_stacks/components/custom_domain.py` (real); `cli/domain.py` (real);
  `__init__.py` exports; tests
  `tests/unit/{test_m15_custom_domain_domain,test_m15_custom_domain_service,fake_custom_domain}.py`,
  `tests/unit/cli/test_m15_domain_cli.py`; one pre-existing foundations test
  updated (`test_m15_stack_cli.py`: `domain` is no longer a pending stub).
- **TypeScript**: new `src/custom-domain/{domain,kvs,service}.ts`;
  `src/stacks/components/custom-domain.ts` (real); `src/index.ts` exports;
  `package.json` new optional peer `@aws-sdk/client-cloudfront-keyvaluestore`
  (+ devDependency, + `pnpm-lock.yaml`); `check-dts-cost-blocks.mjs` new
  declaration; tests `tests/unit/{m15-custom-domain.test.ts,m15-fake-custom-domain.ts}`.
- **Infra**: `infra/custom-domain.yaml` (new), `infra/functions/
  custom_domain_router.js` + `infra/functions/tests/
  {custom_domain_router.test.mjs,cloudfront-runtime-shim.mjs}`;
  `scripts/tests/test_custom_domain_function_sync.py`; `Makefile`
  (`CUSTOM_DOMAIN_TEMPLATE`, added to `infra-lint`); `clients/typescript/
  package.json` `test:infra-functions` script (chained into `test`).
- **Docs**: `ARCHITECTURE.md` ADR-024, `AWS_API_NOTES.md` §29,
  `MILESTONES.md` (the `custom-domain` bullet under M15), both `rayito`
  `CHANGELOG.md`, `docs/RELEASE_NOTES_0.6.0.md`, `docs/site/docs/
  funciones-opcionales/dominio-propio.md` (real content, marked
  experimental pending D3/DOM-2/3/5/7/8). `docs-delta.md` in this
  directory has the exact replacement rows for `e2b-parity.md`,
  `optional-features.md`, `cost.md` and `security.md` (T25), for
  `m15-docs-integration` to apply.
- **No change** to any 0.5.x/0.6-foundations behaviour: `domain=` still
  raises `UnimplementedError` before `run-microvm`; the zero-cost golden
  tests are untouched.
