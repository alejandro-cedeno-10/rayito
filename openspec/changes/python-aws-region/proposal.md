## Why

The Python SDK built its boto3 sessions with botocore's default chain, which
reads `AWS_DEFAULT_REGION` and the profile but not `AWS_REGION`. The
TypeScript SDK (AWS SDK v3) and the CLI honour `AWS_REGION`, so the same
environment pointed Python and TypeScript at different regions (or left
Python without one), and the docs had to ask users to export both variables.

## What Changes

- New adapter module `rayito/_aws_region.py`: `resolve_region` and
  `aws_session`, the single place where the Python SDK picks a region, with
  the precedence explicit `region=` > caller session > `AWS_REGION` >
  `AWS_DEFAULT_REGION` > profile.
- Every boto3 session the SDK creates goes through it: the control plane
  (`lambda-microvms`, `sts`), `LazyClient` (DynamoDB index, Secrets Manager,
  EFS, EC2, CloudFormation, S3, Logs, CloudFront KVS), S3 staging, lifecycle
  events (DynamoDB, Secrets Manager) and the CLI session.
- The no-region errors of the index and `Template.build` name `AWS_REGION`
  again; the docs export only `AWS_REGION`, with a note on
  `AWS_DEFAULT_REGION` as fallback.
- No TypeScript change: it already follows this precedence.

## Capabilities

### New Capabilities

- `aws-region-resolution`: how both SDKs and the CLI pick the AWS region.

## Impact

`clients/python/src/rayito/` (adapters only), Python unit tests, docs site,
Python CHANGELOG. Not breaking: environments that only set
`AWS_DEFAULT_REGION` behave as before.
