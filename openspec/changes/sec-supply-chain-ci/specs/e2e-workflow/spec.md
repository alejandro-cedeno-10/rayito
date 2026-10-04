## MODIFIED Requirements

### Requirement: e2e workflow assumes an OIDC role and runs the acceptance suites with guardrails
`.github/workflows/e2e.yml` SHALL trigger on `workflow_dispatch` (inputs `suite` = `python` | `typescript` | `both`, default `python`; `template_version` optional; `expression` optional `-k` filter) and on a nightly schedule (Python suite only), with `concurrency: { group: e2e, cancel-in-progress: false }` and `permissions: contents: read`. Its jobs SHALL run in the GitHub environment `e2e` with `id-token: write`, `timeout-minutes` (75 for Python, 30 for TypeScript), SHALL be skipped by their `if:` while `vars.RAYITO_E2E_ROLE_ARN` is empty (there is no role to assume, so the nightly would only fail), and SHALL obtain credentials only through `aws-actions/configure-aws-credentials` with `role-to-assume: ${{ vars.RAYITO_E2E_ROLE_ARN }}`, `role-session-name: rayito-e2e-<run_id>` and `role-duration-seconds: 3600`. Before any test the job SHALL count the MicroVMs of `vars.RAYITO_E2E_TEMPLATE_ARN` whose state is not `TERMINATED`/`TERMINATING` with `aws lambda-microvms list-microvms --image-identifier <arn> --query "items[?state!='TERMINATED' && state!='TERMINATING'].microvmId" --output text | wc -w` and fail when the count exceeds 10. The suites SHALL run as `RAYITO_E2E=1 RAYITO_TEMPLATE=<arn> uv run pytest tests/e2e -m e2e -v` and `pnpm test:e2e`, without `RAYITO_EXECUTION_ROLE_ARN`. A final step with `if: always()` SHALL terminate every remaining non-terminated MicroVM of the template with `aws lambda-microvms terminate-microvm --microvm-identifier <id>`. The workflow SHALL NOT publish an image. Its header SHALL state the cost (≈ $0.03 per run from `AWS_API_NOTES.md` §12, ≈ $1 per month nightly) and the manual steps (deploy the role, environment `e2e` with required reviewers and deployment branches limited to `main`, the variables `RAYITO_E2E_ROLE_ARN`, `RAYITO_E2E_TEMPLATE_ARN`, `RAYITO_E2E_REGION`, an AWS Budget of $10/month on service `AWS Lambda` with an 80 % alert).

#### Scenario: pre-flight refuses a dirty account
- **WHEN** eleven MicroVMs of the template are `RUNNING` when the job starts
- **THEN** the pre-flight step exits 1 naming the count and no test runs

#### Scenario: nightly run leaves nothing behind
- **WHEN** the schedule fires and the Python suite finishes, whether green or after a `timeout-minutes` cancellation
- **THEN** the sweeper step ran, the pre-flight expression evaluated afterwards returns 0, and the job log shows the assumed session name `rayito-e2e-<run_id>`

#### Scenario: no role configured
- **WHEN** the schedule fires while the repository variable `RAYITO_E2E_ROLE_ARN` does not exist
- **THEN** both jobs are skipped and the run does not fail
