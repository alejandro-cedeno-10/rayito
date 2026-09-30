## ADDED Requirements

### Requirement: Wire-visible agent messages are Spanish; the listed tokens are frozen
Every message `rayd` sends to a client SHALL be free Spanish text (`CONTRIBUTING.md` §4): the `Display` of every domain error in `rayd-core` (it becomes the gRPC status message, the `StreamError.message` of a stream or the refusal reason of `/run`), the in-stream `StreamError.message` of process and PTY ends, and every status literal of the gRPC adapters in `crates/rayd/src/grpc`. The `value` of the synthetic `ExecutionError`s that `rayd` writes into an execution (`KernelDied`, `ExecutionTimeout`, `OutputTruncated`, for example "the kernel sidecar exited") is execution data, not a status message, and stays as it is in 0.4.0 (deferred). Identifiers inside a message SHALL stay as they are: proto field names (`timeout_ms`, `context_id`, `from_seq`), enum values (`EXACT`, `AT_LEAST`), image names (`rayito-base-poly`, `rayito-base-caps`), `CAP_NET_ADMIN`, `x-access-token`, errno names and the operation names adapters pass in. This is the maintainer's choice for 0.4.0; the M9 architecture review recommended English, and the rest of the SDK messages were already Spanish.

The following wire tokens, which the 0.3.x and 0.4.0 SDKs parse byte for byte, SHALL NOT change and SHALL be defined once, as `pub const` in `rayd_core::wire_tokens`, from which the error types and the adapters take them (no second copy of the literal):

- `suspending`, `terminating` (the phase gate, stream closes and hook outcomes);
- `sandbox_timeout`, `lifecycle_unmanaged`;
- the prefix `timeout beyond cap` followed by `; cap_unix_ms=<n>`;
- the prefix `kernel not ready`, followed by `: <reason>` where the reason may be Spanish text or a sidecar state name;
- `disk_reserve`, `disk_full`, `metadata_unsupported`, `metadata_too_large`;
- the `<reason>:` tokens that open every transfer failure message: `too_large_for_put`, `file_changed` and every `FailureReason` token (`expired`, `no_object`, `access_denied`, `signature_rejected`, `wrong_region`, `bucket_missing`, `s3_unavailable`, `forbidden_address`, `unexpected_response`, `no_content_length`, `too_large`, `disk_reserve`, `disk_full`, `checksum_mismatch`, `file_shrank`, `cancelled`, `destination_rejected`, `metadata_unsupported`, `metadata_too_large`);
- `egress_update_failed: <step>` and `egress_verify_failed`.

A golden test in `rayd-core` SHALL pin the exact bytes of each token and check that the domain messages that carry one still start with it. The `StreamError.code` a persistence failure maps to `suspending` SHALL also come from `wire_tokens`. `RegistryError::Full` and `RegistryError::Unknown` SHALL take their text from the `TransferError` they become (`From<RegistryError> for TransferError`), never repeat it. Every transfer-manager site that turns a `RegistryError` into a `TransferError` (admission, lookup and cancel) SHALL use that single conversion.

#### Scenario: the SDK tokens survive the translation
- **WHEN** `SetTimeout` goes beyond the cap, a code RPC hits a sidecar that is not ready, and a write finds the disk reserve exhausted
- **THEN** the status messages start with `timeout beyond cap; cap_unix_ms=`, `kernel not ready: ` and equal `disk_reserve`, exactly as in 0.3.x

#### Scenario: free text is Spanish
- **WHEN** `ProcessService.Connect` asks for a pid that does not exist, and `SetTimeout` asks for 999 ms on an active sandbox
- **THEN** the messages are "no se encontró el pid <n>" and "el timeout debe ser de al menos 1 s"

#### Scenario: one text per transfer refusal
- **WHEN** the transfer registry refuses an admission because it is full, or a lookup because the id is unknown
- **THEN** the caller sees the same code and message as the `TransferError::Full` / `TransferError::UnknownTransfer` refusal, "demasiadas transferencias activas" / "transferencia desconocida"
