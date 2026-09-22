## ADDED Requirements

### Requirement: e2e workflow assumes an OIDC role and runs the acceptance suites with guardrails
`.github/workflows/e2e.yml` SHALL trigger on `workflow_dispatch` (inputs `suite` = `python` | `typescript` | `both`, default `python`; `template_version` optional; `expression` optional `-k` filter) and on a nightly schedule (Python suite only), with `concurrency: { group: e2e, cancel-in-progress: false }` and `permissions: contents: read`. Its jobs SHALL run in the GitHub environment `e2e` with `id-token: write`, `timeout-minutes` (75 for Python, 30 for TypeScript), and SHALL obtain credentials only through `aws-actions/configure-aws-credentials` with `role-to-assume: ${{ vars.RAYITO_E2E_ROLE_ARN }}`, `role-session-name: rayito-e2e-<run_id>` and `role-duration-seconds: 3600`. Before any test the job SHALL count the MicroVMs of `vars.RAYITO_E2E_TEMPLATE_ARN` whose state is not `TERMINATED`/`TERMINATING` with `aws lambda-microvms list-microvms --image-identifier <arn> --query "items[?state!='TERMINATED' && state!='TERMINATING'].microvmId" --output text | wc -w` and fail when the count exceeds 10. The suites SHALL run as `RAYITO_E2E=1 RAYITO_TEMPLATE=<arn> uv run pytest tests/e2e -m e2e -v` and `pnpm test:e2e`, without `RAYITO_EXECUTION_ROLE_ARN`. A final step with `if: always()` SHALL terminate every remaining non-terminated MicroVM of the template with `aws lambda-microvms terminate-microvm --microvm-identifier <id>`. The workflow SHALL NOT publish an image. Its header SHALL state the cost (≈ $0.03 per run from `AWS_API_NOTES.md` §12, ≈ $1 per month nightly) and the manual steps (deploy the role, environment `e2e` with required reviewers, the variables `RAYITO_E2E_ROLE_ARN`, `RAYITO_E2E_TEMPLATE_ARN`, `RAYITO_E2E_REGION`, an AWS Budget of $10/month on service `AWS Lambda` with an 80 % alert).

#### Scenario: pre-flight refuses a dirty account
- **WHEN** eleven MicroVMs of the template are `RUNNING` when the job starts
- **THEN** the pre-flight step exits 1 naming the count and no test runs

#### Scenario: nightly run leaves nothing behind
- **WHEN** the schedule fires and the Python suite finishes, whether green or after a `timeout-minutes` cancellation
- **THEN** the sweeper step ran, the pre-flight expression evaluated afterwards returns 0, and the job log shows the assumed session name `rayito-e2e-<run_id>`

### Requirement: OIDC role template with least privilege on the test image
`infra/ci-oidc-role.yaml` SHALL be a CloudFormation template, `cfn-lint` clean and accepted by `validate-template`, that is not deployed by this change. It SHALL take the parameters `GitHubRepository` (default `alejandro-cedeno-10/rayito`), `GitHubEnvironment` (default `e2e`), `TestImageArns` (list), `CreateOidcProvider` (default `true`), `ExecutionRoleArn` (optional) and `RoleName`; SHALL create an `AWS::IAM::OIDCProvider` for `https://token.actions.githubusercontent.com` with client id `sts.amazonaws.com` only when `CreateOidcProvider` is `true`; and SHALL create a role whose trust policy allows `sts:AssumeRoleWithWebIdentity` only when `token.actions.githubusercontent.com:aud` equals `sts.amazonaws.com` and `token.actions.githubusercontent.com:sub` equals exactly `repo:<GitHubRepository>:environment:<GitHubEnvironment>`, with `MaxSessionDuration` 3600. The role's only policy SHALL allow `lambda:RunMicrovm`, `lambda:GetMicrovm`, `lambda:SuspendMicrovm`, `lambda:ResumeMicrovm`, `lambda:TerminateMicrovm` and `lambda:CreateMicrovmAuthToken` on `TestImageArns`, `lambda:ListMicrovms` on `*`, `lambda:PassNetworkConnector` on `arn:aws:lambda:<region>:aws:network-connector:aws-network-connector:*`, and `iam:PassRole` on `ExecutionRoleArn` only when that parameter is set; it SHALL grant no image, S3, quota or tagging action. `infra/README.md` SHALL document the deploy command and `make infra-lint` SHALL lint this template together with the egress connector template.

#### Scenario: template lints clean
- **WHEN** `uvx cfn-lint==1.56.3 infra/ci-oidc-role.yaml` and `aws cloudformation validate-template --template-body file://infra/ci-oidc-role.yaml` run
- **THEN** both succeed with no findings

#### Scenario: another branch cannot assume the role
- **WHEN** a workflow of the same repository runs outside the `e2e` environment and calls `configure-aws-credentials` with the role
- **THEN** STS refuses the assumption because the `sub` claim does not match `repo:alejandro-cedeno-10/rayito:environment:e2e`
