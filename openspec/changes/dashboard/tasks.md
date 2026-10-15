## 0. Phase 0: decision

- [x] 0.1 ADR-026 in `ARCHITECTURE.md`
- [x] 0.2 `SPEC.md` §4 non-goal adjusted
- [x] 0.3 OpenSpec change `dashboard` validated

## 1. Measurements and contract

- [ ] 1.1 Measure in real AWS: `ListMicrovms`/`ListMicrovmImages` TPS, Service Quotas utilization for MicroVMs, Cost Explorer lag and usage types by region
- [ ] 1.2 Record facts in `AWS_API_NOTES.md`

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
- [ ] 4.3 Acceptance in real AWS and cleanup of the run's resources only

## 5. Phase 2: in-account stack

- [ ] 5.1 Verify in the test account: Function URL `AuthType NONE` with handler-side Cognito JWT versus `AWS_IAM` with identity pool and SigV4 (not run yet: needs AWS access; the default variant was chosen by design, see ADR-026 rule 7)
- [x] 5.2 `infra/dashboard.yaml` (Lambda Function URL serving SPA and API, DynamoDB cache and audit, read-only function role plus an actions role assumed after the group check, Cost Explorer stack parameter off by default, security headers and CORS), `cfn-lint`
- [x] 5.3 `OptionalStacks` component and `rayito stack deploy|status|destroy dashboard`; the theme (JSON text) enters as a stack or SSM parameter and goes through `parse_theme`/`parse_logo` (3.3b)
- [x] 5.4 Lambda handler over the same services; shared contract tests
- [x] 5.5 Audit rows per action; security review
- [x] 5.6 Optional CloudFront plus S3 variant, documented untested (no template: the test account denies both)
- [x] 5.7a Docs: guide, security page and `SECURITY.md` T32, `optional-features.md` rows, `infra/README.md`
- [x] 5.7c Review fixes: bounded billed-cost windows, regions and daily budget; docs and T32 on token revocation plus a live Cognito check before actions; SPA keeps the refresh token and does not loop on 401; SPA client found by name and `/config.json` 5xx surfaced; pinned `limits.json` values
- [x] 5.7d Cost clarity and review fixes: Lambda price and worked example, public Function URL warning and `ReservedConcurrency`, Cost Explorer worst case in dollars, SSM Advanced parameter cost, jobs longer than the function timeout refused before the audit, a single owner for the read-cache invalidation, and a Python and TypeScript parity test of the stack cost text
- [x] 5.7e Screenshot privacy review: the in-account captures re-rendered from the panel over invented demo data, and the stack reports `meta.deployment: stack` so the shared Help view shows stack hints instead of CLI flags
- [ ] 5.7b Acceptance in real AWS (deploy, sign in, read, act, audit rows, destroy) and cleanup of the run's resources only

## 6. Phase 3 (optional)

- [ ] 6.1 Generated CloudWatch dashboard
- [ ] 6.2 Multi-account read roles
- [ ] 6.3 Quota alerts to SNS
