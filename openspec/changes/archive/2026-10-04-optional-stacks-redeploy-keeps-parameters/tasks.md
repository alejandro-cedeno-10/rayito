## 1. Domain and adapters

- [x] 1.1 `ParameterPlan`/`ParameterChange` and `plan_parameters`/`planParameters`
  in the stacks domain model (Python, TypeScript).
- [x] 1.2 `StackStatus.parameters` read from `DescribeStacks`; `update()`
  sends `keep_previous` names with `UsePreviousValue`.

## 2. Service and CLI

- [x] 2.1 `OptionalStacks.deploy()` (sync, async, TypeScript) plans
  parameters after `describe`; `parameter_changes()`/`parameterChanges()`.
- [x] 2.2 `rayito stack deploy` prints the parameters that change before
  confirming.

## 3. Tests and docs

- [x] 3.1 Unit tests for the update path: `s3-mounts` `Prefixes`,
  `metadata-index` `TableName`/`DeletionProtection`, `secrets-access`
  `KmsKeyArn`, a parameter the stack lacks, create defaults (both SDKs).
- [x] 3.2 `pilas-opcionales.md`, `cli.md`, CHANGELOGs.
