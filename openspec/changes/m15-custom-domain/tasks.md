## 1. Contract (`AWS_API_NOTES.md` §29)

- [x] 1.1 §29: `DescribeKeyValueStore`/`PutKey`/`DeleteKey` (only parameters
      used, output fields read, `ETag` chaining), verified offline against
      botocore 1.43.103's `cloudfront-keyvaluestore` model — its
      `endpoint-rule-set-1.json` requires SigV4A despite `service-2.json`
      declaring `signatureVersion: v4` (D2 of `design.md`; an earlier pass
      of this task read the wrong field and concluded the opposite).
      CloudFormation resource shapes used
      by `infra/custom-domain.yaml` (`AWS::CloudFront::{Distribution,
      Function,KeyValueStore}`), `cf.updateRequestOrigin`/`cf.kvs()`
      contract for the Function. DOM-1 re-derived (823 B JWE) and
      DOM-2/3/5/7/8 marked "pendiente de D3 + aceptación AWS".

## 2. Pure domain + port/adapter

- [x] 2.1 `_custom_domain/_domain.py` / `custom-domain/domain.ts`: hostname
      format, alias/port/public-domain validation (`reservedPorts` from
      `limits.json`), KVS key builders, `RouteMetadata` encode/decode, the
      1 KiB value-size guard.
- [x] 2.2 `testdata/custom-domain/hostnames.json`, consumed by both SDKs'
      unit tests.
- [x] 2.3 `_custom_domain/_kvs.py` / `custom-domain/kvs.ts`:
      `KeyValueStoreWriter` port, `CloudFrontKvsWriter` adapter (lazy
      client, sanitized errors, `aws_code`/`awsCode` carrying
      `ResourceNotFoundException` for idempotent deletes).
- [x] 2.4 Unit tests: `test_m15_custom_domain_domain.py`,
      `m15-custom-domain.test.ts` (domain section);
      `tests/unit/fake_custom_domain.py` / `m15-fake-custom-domain.ts`.

## 3. Service `CustomDomain`

- [x] 3.1 Python `_custom_domain/_service.py` (`CustomDomain`,
      `CustomDomainRoute`) and `_service_async.py` (`AsyncCustomDomain`,
      over `asyncio.to_thread`, mirroring `AsyncOptionalStacks`).
      TypeScript `custom-domain/service.ts` (one async class).
      `deploy`/`status`/`destroy` as a thin facade over `OptionalStacks`;
      `register`/`unregister`/`refresh`/`hostFor`/`kvsArn` for route
      management, with a chained `ETag` on every write (`register`/
      `refresh` share a `_write_route`/`#writeRoute` that retries a bounded
      number of times on an `ETag` conflict and rolls back best-effort if a
      later write in the chain fails). `register` requires `traffic_token`
      unless `public=True` is passed explicitly — a route is never public
      by omission (SEC-T25).
- [x] 3.2 "Coste y activación" docstring/TSDoc block on the class;
      registered in `check-dts-cost-blocks.mjs` (TypeScript).
- [x] 3.3 Unit tests: `test_m15_custom_domain_service.py` (incl. "builds no
      boto3 client" and "construction makes no call"),
      `m15-custom-domain.test.ts` (service section).
- [x] 3.4 e2e `clients/python/tests/e2e/test_m15_custom_domain.py` /
      `clients/typescript/tests/e2e/custom-domain.e2e.test.ts` (TypeScript's
      e2e suite only picks up `*.e2e.test.ts`, `vitest.config.ts`): deploy
      the stack, register token routes, DOM-2 (HTTP/1.1), DOM-5 (KVS
      put -> visible and delete -> 404 at the edge, printed), DOM-7 partial
      (`refresh()` keeps a live route serving), DOM-3 (WebSocket upgrade, a
      minimal stdlib-only echo client+server, no new dependency), DOM-8
      (a paused `auto_resume` sandbox answers through the domain), 403
      without the token, 404 after `unregister()` and after the route's
      TTL, destroy the stack and confirm `status()` returns nothing. The
      test environment comes ONLY from `RAYITO_E2E_DOMAIN` (a domain whose
      wildcard the certificate covers) and `RAYITO_E2E_CERT_ARN`, plus an
      optional `RAYITO_ACCEPTANCE_RUN_TAG`; without the first two the module
      skips before any fixture (no AWS call). Each run uses a random id: its
      own stack `rayito-cd-e2e-<id>` and, instead of the wildcard, only its
      routes' exact hostnames (`alternate_domain_names`), so it never
      collides with an existing CloudFront alias. No DNS record is needed:
      the client connects to the distribution's `*.cloudfront.net` sending
      the custom hostname as SNI and `Host` (`curl --connect-to`). The
      teardown unregisters every route and destroys the stack even when
      `deploy()` failed half-way. TypeScript hooks get their own timeout
      (`CUSTOM_DOMAIN_WAIT_TIMEOUT_MS` + 60 s): the generic `hookTimeout`
      (300 s) is shorter than a CloudFront create/delete.
- [x] 3.4b `deploy(alternate_domain_names=)` / `deploy({alternateDomainNames})`
      and `rayito domain deploy --alternate-domain-name` (repeatable):
      replace the default `*.<public_domain>` alias with explicit
      `<label>.<public_domain>` names (template parameter
      `AlternateDomainNames`, `CommaDelimitedList`, default empty = the
      wildcard), validated before AWS against the shared
      `testdata/custom-domain/hostnames.json` cases (one DNS label, inside
      `public_domain`, no duplicates, at most 100 — CloudFront's default
      quota). `rayito domain deploy` now prints the `CNAME` target.
- [x] 3.5 AWS acceptance, part without D3 (2026-10-02, Q121): the
      deployed `FunctionCode` did not compile on `cloudfront-js-2.0`
      (`for...of`, default parameter); fixed, cookie read from
      `request.cookies` first, and re-measured with `TestFunction`
      (403/404/origin as in the Node tests). Off-by-default re-checked on
      real AWS: a plain sandbox only calls `lambda-microvms` (+ `sts`).
      DOM-2/3/5 end to end still need a real distribution (3.4).
- [ ] 3.6 AWS acceptance with a real distribution (DOM-2/3/5/7/8), first
      attempt 2026-10-04 (Q140, Q141). Python and TypeScript e2e, environment
      only from variables: the first stack died in `RouterFunction` (its
      `Comment` was longer than CloudFront's 128 characters; fixed, the
      distribution's `Comment` now names the stack instead of the domain,
      and `test_every_cloudfront_comment_fits_in_128_characters` guards all
      three). With the fix, KVS and Function create fine, but the test
      account's organisation SCP denies `cloudfront:CreateDistribution`, so
      the stack rolls back at `Distribution` and DOM-2/3/5/7/8 stay
      **unmeasured**. Teardown verified in both SDKs (stack gone, no KVS or
      Function left, inventory diff empty). Pending: rerun the two e2e in an
      account where `CreateDistribution` is allowed.

## 4. Stack `infra/custom-domain.yaml`

- [x] 4.1 `AWS::CloudFront::Distribution` (wildcard alias, ACM cert
      parameter, placeholder origin, `CachingDisabled`/
      `AllViewerExceptHostHeader` managed policies — not plain `AllViewer`,
      which would forward `Host` to the dynamically-chosen origin and break
      its TLS/SNI check (DOM-2) —, viewer-request Function association),
      `AWS::CloudFront::Function` (`cloudfront-js-2.0`),
      `AWS::CloudFront::KeyValueStore`. No Lambda, no IAM beyond CloudFront.
- [x] 4.2 `infra/functions/custom_domain_router.js`: strips viewer
      `x-aws-proxy-*` headers, denies reserved ports (`RESERVED_PORTS`,
      kept in sync with `limits.json`) before touching the KVS, treats a
      route whose `m.x` (expiry) already passed as not-found — a 404, not a
      403, so an expired route is indistinguishable from one that never
      existed (T25; a PR #74 review finding: `route()` used to never read
      `m.x` at all, so only the JWE's own AWS-side expiry bounded an
      orphaned route) —, checks the route's `traffic_token` in constant
      time against a lower-cased host label, calls
      `cf.updateRequestOrigin`. The routing decision is a pure
      `route(request, kvsGet, now = Date.now)` function, unit tested with
      `node:test` (`infra/functions/tests/custom_domain_router.test.mjs`,
      via a minimal `cloudfront` module shim since that builtin only
      exists inside CloudFront's own runtime) covering
      404/403/success/header-stripping/expiry-with-an-injected-clock;
      `handler` itself (the thin `cf`-calling wrapper) is not exported,
      matching AWS's own `AWS::CloudFront::Function` example, and is only
      exercised in the AWS acceptance stage (DOM-2/3).
- [x] 4.3 `scripts/tests/test_custom_domain_function_sync.py` keeps the
      YAML's embedded `FunctionCode` in sync with the `.js` source, minus
      its `export` keywords (CloudFormation has no file-include for this
      property, and `cloudfront-js-2.0` has never been documented to
      support `export` in a function's own code).
- [x] 4.4 `cfn-lint` 1.56.3 clean; `Makefile` `CUSTOM_DOMAIN_TEMPLATE`
      added to `infra-lint` (the `aws cloudformation validate-template`
      half of that target needs AWS and was not run from this branch).
- [x] 4.5 `_stacks/components/custom_domain.py` /
      `stacks/components/custom-domain.ts`: real `COMPONENT`
      (`supported: true`), cost statement citing §29.
- [x] 4.6 `scripts/gen_stack_assets.py` run; packaged templates
      (`_templates/custom-domain.yaml`, `stacks/templates/
      custom-domain.gen.ts`) regenerated and committed; TypeScript
      `stacks/packaging.ts`'s `ASSETS` map updated with the new component.
- [x] 4.7 The optional refresher Lambda from the M15 architecture (§7.8,
      `EnableRefresher` parameter, `rate(10 minutes)` schedule) is **not**
      built in this change: deferred, same precedent as the `Sandbox`
      wiring in D6, and for a more concrete reason — `OptionalStacks`'s
      generic deploy mechanism has no conditional-artifact support, so a
      refresher Lambda would need its own packaging path just to stay off
      by default, and it could not be exercised against real AWS here
      anyway (no D3). Recorded as **DOM-14, pending** (not failed) in
      `design.md`'s "What is explicitly unmeasured here", `MILESTONES.md`'s
      M15 section and `docs-delta.md` — a PR #74 review finding noted this
      was previously only in `_stacks/components/custom_domain.py`'s
      module docstring, invisible to whoever runs the acceptance stage.

## 5. CLI and exports

- [x] 5.1 `cli/domain.py`: `deploy` builds a `CustomDomain` (validates
      `--public-domain` before touching AWS, prints the per-use cost
      alongside the idle one); `status`/`destroy` go straight to
      `OptionalStacks` instead, same as `rayito stack`, since neither needs
      `--public-domain`; `destroy` prints what `destroy()` retains.
      `tests/unit/cli/test_m15_domain_cli.py`. Updated the one pre-existing
      foundations test that asserted `domain --help` was still a pending
      stub (`test_m15_stack_cli.py`).
- [x] 5.2 `rayito/__init__.py` / `src/index.ts` export `CustomDomain`,
      `AsyncCustomDomain` (Python), `CustomDomainRoute` and the option/
      result types.
- [x] 5.3 `clients/typescript/package.json`: two new optional peers,
      `@aws-sdk/client-cloudfront-keyvaluestore` and
      `@aws-sdk/signature-v4a` (the data plane needs SigV4A, D2 of
      `design.md`) (+ devDependencies), `pnpm-lock.yaml` updated. Python:
      new `pyproject.toml` extra `custom-domain` (`awscrt`, same reason).

## 6. Off-by-default and zero-cost acceptance (§9 of the M15 architecture)

- [x] 6.1 Constructing `CustomDomain`/`AsyncCustomDomain` makes no AWS call
      (unit test with a `boto3.session.Session.client` spy, mirroring
      foundations' `OptionalStacks` test).
- [x] 6.2 `domain=` on `Sandbox.create()` is unchanged from
      `v06-foundations` (still `UnimplementedError`, before `run-microvm`);
      the zero-cost golden tests (`test_m15_zero_cost.py`,
      `zero-cost-defaults.test.ts`) were not touched and still pass.
- [x] 6.3 Full `uv run pytest` (2619 passed, 1 skipped without `awscrt`),
      `ruff check`, `ruff format --check`, `mypy` clean on the touched
      files. Full `pnpm vitest run --project unit` (1179 passed) + 20
      `node:test` of the Function, `pnpm lint`, `pnpm typecheck`,
      `pnpm pack:check` (incl. `check-dts-cost-blocks.mjs`) clean.

## 7. Docs

- [x] 7.1 `ARCHITECTURE.md` ADR-024 (ADR-016 pattern, D1-D6 above).
- [x] 7.2 `AWS_API_NOTES.md` §29.
- [x] 7.3 `MILESTONES.md`: the `custom-domain` bullet under "M15 — Rayito
      0.6" updated from "pendiente" to what this change actually built,
      with D3/DOM-2/3/5/7/8 named as the acceptance gate.
- [x] 7.4 `CHANGELOG.md` anchor in both `clients/python/CHANGELOG.md` and
      `clients/typescript/CHANGELOG.md` (no rayd component, so no
      `crates/rayd/CHANGELOG.md` entry).
- [x] 7.5 0.6.0 shipped without this change, so its entry lives under
      `[Unreleased]` in both CHANGELOGs, not in `RELEASE_NOTES_0.6.0.md`.
- [x] 7.6 `docs/site/docs/funciones-opcionales/dominio-propio.md`: real
      "Coste y activación" box, Python + TypeScript examples of the
      standalone `CustomDomain` usage, marked experimental with the exact
      gate (D3 + DOM-2/3/5/7/8) and the pending `Sandbox` integration named
      plainly.
- [x] 7.7 `docs-delta.md` in this directory: exact replacement rows for
      `e2b-parity.md` (#15, #16, #110), `optional-features.md`, `cost.md`
      and `security.md` (T25), for `m15-docs-integration` to apply —this
      change does not touch those shared files itself.
- [x] 7.8 `docs/site` `mkdocs build --strict` run locally as a smoke check
      (clean; the page now explains what the feature does, how to activate
      and remove it, explicit names and testing without DNS).

## 8. OpenSpec

- [x] 8.1 This change (`proposal.md`, `design.md`, `tasks.md`,
      `specs/custom-domain/spec.md`, `docs-delta.md`).
- [x] 8.2 `npx -y @fission-ai/openspec@1.10.0 validate --strict` clean.
      Not archived.

## 9. Ship as experimental

- [x] 9.1 Merge `origin/main` (FeatureSet planning and hashed cfn-lint
      kept; `AWS_API_NOTES.md` Q121 was left free by main, Q140/Q141 are
      the next free rows after main's Q139).
- [x] 9.2 Maintainer decision: merge as **experimental** without the
      end-to-end AWS run, because the test account's SCP denies
      `cloudfront:CreateDistribution` (Q141). Verified on real AWS: the
      Function in `cloudfront-js-2.0` (Q121) and the KVS + Function of the
      stack (Q140). Not verified: the distribution and DOM-2/3/5/7/8.
- [x] 9.3 Marked experimental in the feature page (admonition with what
      was and was not verified and how to report issues), nav, docstrings
      and TSDoc, both CHANGELOGs, `e2b-parity.md` rows 15/110,
      Novedades ("Disponible como experimental"), `optional-features.md`,
      the stacks catalog, the CLI reference and `SECURITY.md` T25;
      `rayito domain` is no longer hidden.
- [ ] 9.4 Run the e2e (`RAYITO_E2E_DOMAIN`/`RAYITO_E2E_CERT_ARN`) in an
      account that allows `CreateDistribution`, then drop the experimental
      label and archive this change.
