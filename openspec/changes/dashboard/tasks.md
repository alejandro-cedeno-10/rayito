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

## 3. Phase 1: local dashboard

- [ ] 3.1 Versioned `/api/v1` JSON schema and contract tests
- [ ] 3.2 `rayito dashboard` server (loopback, Host/Origin, CSRF, session token)
- [ ] 3.3 Build-free SPA (ES modules, web components, docs tokens, light and dark)
- [ ] 3.4 Actions behind `--allow-actions`, confirmation and TPS-aware queue
- [ ] 3.5 Security tests; hygiene over fixtures

## 4. Phase 1: docs and acceptance

- [ ] 4.1 Spanish docs: guide, API reference, security page, "Coste y activación" box, `optional-features.md` row, `clients/typescript/scripts/cost-declarations/dashboard.json`, nav
- [ ] 4.2 `check_docs_examples.py`, `mkdocs --strict`, all gates
- [ ] 4.3 Acceptance in real AWS and cleanup of the run's resources only

## 5. Phase 2: in-account stack

- [ ] 5.1 Verify in the test account: Function URL `AuthType NONE` with handler-side Cognito JWT versus `AWS_IAM` with identity pool and SigV4
- [ ] 5.2 `infra/dashboard.yaml` (Lambda Function URL serving SPA and API, DynamoDB cache and audit, read-only function role plus an actions role assumed after the group check, Cost Explorer stack parameter off by default, security headers and CORS), `cfn-lint`
- [ ] 5.3 `OptionalStacks` component and `rayito stack deploy|status|destroy dashboard`
- [ ] 5.4 Lambda handler over the same services; shared contract tests
- [ ] 5.5 Audit rows per action; security review
- [ ] 5.6 Optional CloudFront plus S3 variant, documented untested if blocked
- [ ] 5.7 Docs and acceptance

## 6. Phase 3 (optional)

- [ ] 6.1 Generated CloudWatch dashboard
- [ ] 6.2 Multi-account read roles
- [ ] 6.3 Quota alerts to SNS
