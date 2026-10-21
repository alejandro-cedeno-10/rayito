## Why

Rayito 0.15 deploys an agent to Amazon Bedrock AgentCore Runtime with one
command (`rayito agent deploy --target agentcore`, ADR-031) and prepares an
account with `rayito setup` (ADR-030). Teams that manage their AWS estate with
the AWS CDK cannot put either result in a stack: the runtime, its image and
the MicroVM image live outside CloudFormation, so they do not show up in a
`cdk diff`, they are not removed by `cdk destroy` and they cannot reference or
be referenced by the rest of the application's resources. ADR-031 D12 left
the CDK construct for later as a third, jsii-built package. AWS has since
graduated the AgentCore L2 to stable `aws-cdk-lib` (2.255.0) and published
`CfnMicrovmImage` (2.261.0), which makes two native constructs, shipped inside
the packages users already install, the smaller and safer path.

## What Changes

- **`RayitoAgent`** (TypeScript `rayito/cdk`, Python `rayito.cdk` behind the
  extra `rayito[cdk]`): `cdk deploy` deploys the same agent project that
  `rayito agent deploy --target agentcore` deploys, with the same names,
  the same image, the same least-privilege roles (the construct includes
  `infra/agentcore-runtime.yaml` as a nested stack) and MMDSv2 enforced.
  Default image build: CodeBuild in the account (no local Docker),
  `AgentImageBuild.codeBuild()`; `AgentImageBuild.localDocker()` is optional.
- **`RayitoMicrovmImage`**: `cdk deploy` publishes a Rayito MicroVM image
  (`AWS::Lambda::MicrovmImage`) from a verified `rayito-image.zip` (a local
  file with an optional SHA-256, or the release asset downloaded and checked
  against `SHA256SUMS` before synthesis), with the same hooks, sizes, OS
  capabilities and mount-bucket allowlist as `rayito image publish`. MicroVMs
  are still started by the SDK (`Sandbox.create(template=...)`); there is no
  CloudFormation resource for a running MicroVM.
- **Ownership marker `+cdk`.** CDK runtimes carry `rayito.agentcore/1+cdk` in
  their description and every resource carries `rayito:managed-by=rayito-cdk`.
  The SDKs and CLIs list, inspect and invoke a CDK-managed agent as before,
  but `rayito agent deploy` and `rayito agent delete` refuse it with
  `name_conflict` pointing to `cdk deploy` / `cdk destroy`, and
  `rayito image publish` refuses a CDK-managed image. Nothing that exists
  today carries the marker, so no current behaviour changes.
- **Zero AWS calls at synthesis**, and zero impact on users who do not
  install the extra or the optional peers: `import rayito`, the CLIs, MCP and
  the E2B entry points never load `aws-cdk-lib`, `constructs` or `jsii`.
- **Supersedes ADR-031 D12** with ADR-032; adds `AWS_API_NOTES.md` rows
  Q181-Q186 for the two CloudFormation resources.

## Capabilities

### New Capabilities

- `agentcore-cdk`: the CDK constructs `RayitoAgent` and `RayitoMicrovmImage`
  in both SDKs, their shared contract, and how the CLI treats resources they
  own.

### Modified Capabilities

None. The CLI refusals are specified in `agentcore-cdk`; the behaviour of
`agentcore-deploy`, `image-lifecycle` and `cli` for resources without the
marker is unchanged.

## Impact

- New public modules: Python `rayito.cdk` (`RayitoAgent`, `AgentImageBuild`,
  `RayitoMicrovmImage`, `MicrovmImageArtifact`, `RayitoCdkException`),
  TypeScript `rayito/cdk` (same names, `RayitoCdkError`).
- New optional dependencies: PyPI extra `cdk` (`aws-cdk-lib>=2.261.0,<3`,
  `constructs>=10.5.0,<11`); npm optional peers `aws-cdk-lib ^2.261.0`,
  `constructs ^10.5.0`. No release-pipeline change.
- New shared assets: `infra/agentcore-cdk-handler.py` (custom-resource
  handler) packaged by `scripts/gen_stack_assets.py` with the pinned botocore
  model of `bedrock-agentcore-control`; `limits.json` group `agentcore-cdk`;
  `testdata/agentcore-cdk/` and `scripts/gen_cdk_vectors.py`.
- Small changes in the AgentCore domain of both SDKs (marker acceptance and
  refusal) and in the image publish domain (pure configuration reused by the
  construct, refusal of CDK-managed images).
- Cost only on `cdk deploy`: CodeBuild minutes (codeBuild mode), ECR storage,
  AgentCore usage, MicroVM image builds and snapshot storage, logs, and a few
  Lambda and Step Functions invocations per deployment. Synthesis is free.
- No Rust, proto or `rayd` change.
