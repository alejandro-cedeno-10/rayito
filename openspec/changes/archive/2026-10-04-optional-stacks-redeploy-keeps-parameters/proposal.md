## Why

`OptionalStacks.deploy()` (both SDKs, and `rayito stack deploy`) filled
every parameter the caller did not pass with its catalog default *before*
choosing between `CreateStack` and `UpdateStack`, and `UpdateStack` sent
that full list without `UsePreviousValue`. Redeploying an existing stack
without repeating every parameter therefore silently reset it, verified
from the catalog defaults:

- `s3-mounts` deployed with `Prefixes=team7/*` went back to `Prefixes='*'`
  (the execution-role policy widened to the whole bucket) and `ReadOnly`
  back to `'true'`.
- `metadata-index` deployed with `TableName=my-table` went back to
  `rayito-sandboxes`: `TableName` forces a replacement and the template
  has `UpdateReplacePolicy: Delete`, so the old table and its rows were
  deleted; `DeletionProtection`/`PointInTimeRecovery` went back to
  `'false'`.
- `secrets-access` deployed with `KmsKeyArn` went back to `''`, dropping
  `kms:Decrypt`.

## What Changes

- Catalog defaults apply **only on create** (or to a parameter the
  existing stack does not have yet, e.g. one a newer template adds). On
  update, each declared parameter the caller did not pass and the stack
  already has is sent as `{ParameterKey, UsePreviousValue: true}`.
- `StackStatus.parameters` carries `DescribeStacks` `Parameters` so the
  pure domain function `plan_parameters`/`planParameters` can decide.
- A missing `required` parameter is still rejected before any create or
  update call, but only after the single `DescribeStacks` (on update the
  stack may already have it). Unknown parameter names are still rejected
  before any AWS call.
- `parameter_changes()`/`parameterChanges()` returns what a `deploy()` with
  the same arguments would change, with only a `DescribeStacks`;
  `rayito stack deploy` prints it before asking for confirmation.

## Impact

- Code: `clients/python/src/rayito/_stacks/`, `clients/python/src/rayito/cli/stack.py`,
  `clients/typescript/src/stacks/`.
- No new AWS resource or call beyond the `DescribeStacks` `deploy()` already
  made; no cost change.
