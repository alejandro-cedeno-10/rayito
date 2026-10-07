# aws-region-resolution Specification

## Purpose
Every AWS session the SDKs and the CLI open resolves its region the same way: an explicit `region=`, then the caller's session, then `AWS_REGION`, then `AWS_DEFAULT_REGION`, then the profile.

## Requirements

### Requirement: Same region precedence in both SDKs and the CLI

The Python SDK, the TypeScript SDK and the CLI SHALL resolve the AWS region
with the precedence: explicit region argument, then (Python) the region of a
caller-supplied session, then `AWS_REGION`, then `AWS_DEFAULT_REGION`, then
the profile. In Python every boto3 session or client the SDK creates SHALL
obtain its region from a single adapter-layer resolver.

#### Scenario: AWS_REGION alone is enough in Python

- **WHEN** only `AWS_REGION=us-west-2` is set and `Sandbox.create()` (or any optional feature) is called without `region=`
- **THEN** every boto3 client the SDK builds targets `us-west-2`

#### Scenario: AWS_REGION beats AWS_DEFAULT_REGION

- **WHEN** `AWS_REGION` and `AWS_DEFAULT_REGION` hold different regions and no region argument is passed
- **THEN** the SDK uses `AWS_REGION`

#### Scenario: A caller session without a region falls through

- **WHEN** the caller passes a session whose region is unset and only `AWS_REGION` is set
- **THEN** every client built from that session targets `AWS_REGION`

#### Scenario: Explicit argument wins

- **WHEN** `region=` is passed
- **THEN** it is used regardless of the environment

#### Scenario: No region anywhere

- **WHEN** no region is resolvable
- **THEN** the error message names `AWS_REGION`
