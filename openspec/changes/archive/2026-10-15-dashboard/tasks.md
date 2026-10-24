## 0. Phase 0: decision

- [x] 0.1 ADR-026 in `ARCHITECTURE.md`
- [x] 0.2 `SPEC.md` §4 non-goal adjusted
- [x] 0.3 OpenSpec change `dashboard` validated

## 1. Measurements and contract

- [x] 1.1 Measure in real AWS: `ListMicrovms`/`ListMicrovmImages` TPS, Service Quotas utilization for MicroVMs, Cost Explorer lag and usage types by region (run 2026-10-14 with the agnostic script `scripts/measure/dashboard_apis.py`: about 5 calls/s per List operation, no usage metric published for any of the 26 MicroVM quotas, current-day cost already visible as estimated; Cost Explorer usage types outside us-east-1 not measured: no usage there)
- [x] 1.2 Record facts in `AWS_API_NOTES.md` (§30, Q163 to Q165)

## 2. Phase 1: domain, ports, SDK services

- [x] 2.1 Constants in `limits.json` and `gen_limits.py` (quota catalog, thresholds, cache TTL, compute prices; page sizes and polling intervals arrive with the server in 3.x)
- [x] 2.2 Pure domain and the six ports (Python and TypeScript)
- [x] 2.3 `Quotas` and `UsageCost` services, Python sync and async plus TypeScript, with fakes and parity tests
- [x] 2.4 Adapters with lazy imports; errors mapped by code
- [x] 2.5 Zero-cost golden trace test in both SDKs
- [x] 2.6 Cost clarity: every public SDK class, method and option says free or billable (price, source, trigger), with the same wording in Python and TypeScript, `resume` warning that compute bills again, async classes documented, and zero-cost tests in both SDKs

## 3. Phase 1: local dashboard

- [x] 3.1 Versioned `/api/v1` JSON schema and contract tests
- [x] 3.2 `rayito dashboard` server (loopback, Host/Origin, CSRF, session token)
- [x] 3.3 Build-free SPA (ES modules, web components, design tokens, overview with SVG charts, dense table, dark default, light and auto)
- [x] 3.3b User theme and logo (`--theme`, `--logo`, validated, same origin), reusable by the phase 2 stack
- [x] 3.4 Actions behind `--allow-actions`, confirmation and TPS-aware queue
- [x] 3.5 Security tests (including `Sec-Fetch-Site`, the paid GET and the queue on close); hygiene over fixtures
- [x] 3.6 Cost clarity in the CLI and SPA: `--allow-actions`, `--cost-explorer`, `--events-stack` and `--log-group` help say free or billable (`resume` bills compute again, action prices from `limits.json`), the refresh button shows the price, the banner announces the paid mode, and the proxy strips the dashboard cookie whatever the separator
- [x] 3.7 Built-in Help view (sidebar entry, no external call, link to the guide), illustrated step-by-step guide with demo-data screenshots, and `scripts/check_dashboard_images.py` (no metadata, size limit) in tests, `make lint` and CI
- [x] 3.8 Screenshot privacy review: every guide capture re-rendered from the panel over invented demo data with the default theme (the optional example theme moved to a violet palette), `meta.deployment` (`local`/`stack`) so Help shows stack-appropriate hints, ETA under one second reads "menos de 1 s", and singular/plural agreement in the SPA text

## 4. Phase 1: docs and acceptance

- [x] 4.1 Spanish docs: guide, API reference, security page, "Coste y activación" box, `optional-features.md` row, `clients/typescript/scripts/cost-declarations/dashboard.json`, nav
- [x] 4.2 `check_docs_examples.py`, `mkdocs --strict`, all gates
- [x] 4.3 Acceptance in real AWS and cleanup of the run's resources only (Q158-Q162)
- [x] 4.4 Step-by-step guide with real screenshots (identifiers replaced, no account data) in the panel page, the docs home and the README

## 5. Phase 2: in-account stack

- [x] 5.1a Implement the `AWS_IAM` variant behind the explicit `AuthMode=aws-iam` stack parameter: API Function URL with `AuthType AWS_IAM` and Cognito identity pool credentials, page function without permissions on its own public URL, SigV4 in the SPA (checked against `botocore`), `X-Rayito-Token`, template and handler tests, docs
- [x] 5.1b Verify on real AWS: `AuthMode=aws-iam` end to end (CORS preflight on an `AWS_IAM` URL, unsigned request rejected by the URL, identity pool credentials invoke the API) versus the default `AuthType NONE` with handler-side Cognito JWT (run 2026-10-14, Python and TypeScript, AWS_API_NOTES §30 Q169: unsigned request rejected with 403 and without invoking the function, preflight answered with the page origin, identity pool credentials invoke the API; the default variant stays, see ADR-026 rule 7)
- [x] 5.2 `infra/dashboard.yaml` (Lambda Function URL serving SPA and API, DynamoDB cache and audit, read-only function role plus an actions role assumed after the group check, Cost Explorer stack parameter off by default, security headers and CORS), `cfn-lint`
- [x] 5.3 `OptionalStacks` component and `rayito stack deploy|status|destroy dashboard`; the theme (JSON text) enters as a stack or SSM parameter and goes through `parse_theme`/`parse_logo` (3.3b)
- [x] 5.4 Lambda handler over the same services; shared contract tests
- [x] 5.5 Audit rows per action; security review
- [x] 5.6 Optional CloudFront plus S3 variant, documented untested (no template: some organizations allow neither)
- [x] 5.7a Docs: guide, security page and `SECURITY.md` T32, `optional-features.md` rows, `infra/README.md`
- [x] 5.7c Review fixes: bounded billed-cost windows, regions and daily budget; docs and T32 on token revocation plus a live Cognito check before actions; SPA keeps the refresh token and does not loop on 401; SPA client found by name and `/config.json` 5xx surfaced; pinned `limits.json` values
- [x] 5.7d Cost clarity and review fixes: Lambda price and worked example, public Function URL warning and `ReservedConcurrency`, Cost Explorer worst case in dollars, SSM Advanced parameter cost, jobs longer than the function timeout refused before the audit, a single owner for the read-cache invalidation, and a Python and TypeScript parity test of the stack cost text
- [x] 5.7e Screenshot privacy review: the in-account captures re-rendered from the panel over invented demo data, and the stack reports `meta.deployment: stack` so the shared Help view shows stack hints instead of CLI flags
- [x] 5.7f Review polish: the multi-account startup line agrees in number (`1 cuenta`/`N cuentas`) and the accounts capture is re-rendered from demo data
- [x] 5.7b Acceptance in real AWS (deploy, sign in, read, act, audit rows, destroy) and cleanup of the run's resources only (Q162); the screenshot guide of the in-account panel is in its docs page

## 6. Phase 3 (optional)

- [x] 6.1 Generated CloudWatch dashboard: `rayito dashboard cloudwatch`, the `dashboard-cloudwatch` stack from the same generator (no-drift test), docs and cost box
- [x] 6.2 Multi-account read roles: `--account`/`--external-id`, `AccountsApi`, `/api/v1/accounts`, SPA selector, the `dashboard-read-role` stack for target accounts
- [x] 6.3 Quota alerts to SNS: the `quota-alerts` stack (Lambda, EventBridge, SNS, state table, custom metric), tests with fakes, bundle and template tests
- [x] 6.4 Docs (guide, security summary and SECURITY.md T33, `optional-features.md` rows, `infra/README.md`), TypeScript component parity, changelogs
- [x] 6.4b Cost clarity and review fixes: CloudWatch prices from `limits.json` in the CLI help and both SDK components, `--quota` help naming the `quota-alerts` cost, alert hysteresis (highest level kept until usage is ok again), explicit expiry check on assumed credentials, line-height and font-weight design tokens, and a Python and TypeScript parity test of the four dashboard stack cost texts
- [x] 6.5 Acceptance in real AWS (partial): the CloudWatch dashboard deploys and is destroyed from both SDKs, the quota alert reaches an SNS topic of the run (Q160, Q161) and the multi-account template lints and validates. Not run: PromQL OTLP charts rendering and a real `AssumeRole` into a second account (no second account was assumed)
- [x] 6.6 Gaps left by the acceptance, closed locally: target accounts from the environment (`RAYITO_DASHBOARD_ACCOUNTS`), documented recipe to test the OTLP charts, a unique default name for the CloudWatch dashboard (the stack name, existing stacks keep theirs), read-only e2e for `Quotas`, `UsageCost` (billed behind `RAYITO_E2E_BILLED_COST=1`) and `Fleet` in both SDKs
- [x] 6.7 Run those e2e and the OTLP recipe in real AWS, a real `AssumeRole` into a second account, and confirm the `AuthMode=aws-iam` behaviours of 5.1b; then update the status cells of `optional-features.md` and the "Pendiente de confirmar" bullets of `AWS_API_NOTES.md` §30 (done 2026-10-14: Q163 to Q169; the OTLP chart rendering in the console and the quota increase link are not verifiable through an API and stay noted)
