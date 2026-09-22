## MODIFIED Requirements

### Requirement: PoolBackend is the pluggable seam, with an in-memory default and a 0600 JSON file backend
The SDK SHALL define `PoolBackend` as a `Protocol` with `persistent: bool`, `load() -> tuple[SlotRecord, ...]`, `save(record)` (upsert by `sandbox_id`) and `delete(sandbox_id)` (missing id is not an error); the pool SHALL serialise every backend call under its own lock and SHALL NOT require the backend to be thread-safe or multi-process safe. `InMemoryPoolBackend` (`persistent=False`) SHALL be the default. `JsonFilePoolBackend(path)` (`persistent=True`) SHALL store `{"schema": "rayito.pool/1", "slots": [...]}` with the access tokens in clear, dates as ISO-8601 UTC and `idle` as its three fields, SHALL return `()` for a missing file and raise `InvalidArgumentException` for another `schema`. Because that file holds every parked secret in clear, the write SHALL be atomic **and** unhijackable: the document SHALL be written to a temporary whose name is unpredictable (a random component in the same directory as the target, so `os.replace`/`rename` stays atomic), created exclusively so an existing file or symlink of that name is never reused (`O_CREAT|O_EXCL|O_WRONLY` with `O_NOFOLLOW` where the platform has it, `tempfile.mkstemp` in Python, flag `wx` in Node), with mode `0o600` applied to the open descriptor before the first byte is written (`os.fchmod` / `FileHandle.chmod`), and promoted with `os.replace`/`rename`; any failure SHALL remove the temporary and re-raise, so the directory holds only the state file afterwards. Reads SHALL refuse to follow a symlink at the state file's own path where the platform offers `O_NOFOLLOW`, raising `InvalidArgumentException`/`InvalidArgumentError` saying the state file must be a regular file. The TypeScript `JsonFilePoolBackend` SHALL apply the same rules and write the same schema so a file is interchangeable between SDKs. The docs SHALL state the file backend is single-process, for tests and same-host recovery, and as sensitive as `RAYITO_ACCESS_TOKEN`.

#### Scenario: JSON round-trip and permissions
- **WHEN** a unit test saves two records through `JsonFilePoolBackend`, reloads them with a new instance, and inspects the file on POSIX
- **THEN** the loaded records equal the saved ones, no temporary remains beside it, the mode is `0o600`, and the file text contains both tokens and the `schema` key

#### Scenario: a pre-created temporary is never reused
- **WHEN** another user creates `<path>.tmp` with mode `0o666` and known content, and the pool then saves a record (in either SDK)
- **THEN** the state file is a new inode with mode `0o600` holding the pool document, and the pre-created file still holds its original bytes

#### Scenario: a symlinked temporary truncates nothing
- **WHEN** `<path>.tmp` is a symlink to another file the process can write and the pool saves a record
- **THEN** the target of the symlink is untouched

#### Scenario: a symlinked state file is refused on read
- **WHEN** the state file's own path is a symlink to a valid `rayito.pool/1` document and `load()` runs on POSIX
- **THEN** it raises `InvalidArgumentException` (`InvalidArgumentError` in TypeScript) saying the state file must be a regular file, and no record is trusted

#### Scenario: foreign schema
- **WHEN** the file holds `{"schema": "other/1", "slots": []}`
- **THEN** `load()` raises `InvalidArgumentException` naming the schema
