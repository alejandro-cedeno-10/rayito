## MODIFIED Requirements

### Requirement: Write is a multi-file stream with per-file atomic commits
`Write` SHALL accept a client-stream where a message with `path` starts a new file (committing the previous one) and messages without `path` append to the current file; `user` and `mode` SHALL only be accepted on a message with `path`; the first message without `path` and a stream with zero files SHALL fail with `INVALID_ARGUMENT`; a `chunk` larger than 1 MiB SHALL fail with `INVALID_ARGUMENT`; `mode` above `0o7777` SHALL fail with `INVALID_ARGUMENT` and default to `0o644`. Before creating a file's temporary, after canonicalisation and the deny list, `rayd` SHALL read the free space of the destination directory (`statvfs`, `f_bavail * f_frsize`) and refuse the file with `RESOURCE_EXHAUSTED` and details `disk_reserve` when fewer than 256 MiB are free; `ENOSPC` during a write or commit SHALL map to `RESOURCE_EXHAUSTED` with details `disk_full`. Each file SHALL be written to a temp file named `.rayito-tmp-<random>` in the destination directory (missing parents created with mode `0o755`), then `fsync`, `fchmod(mode)`, `fchown` to the user, `rename` over the final name and `fsync` of the directory. A failure or a cancelled stream SHALL unlink the temp file and leave the destination untouched; files already committed in the same stream SHALL stay committed. Before answering any error `rayd` SHALL keep draining the client's messages for at most 1 MiB or 2 s so the error reaches the client as a gRPC status. The response SHALL list one `EntryInfo` per file in request order. The SDK SHALL raise `DiskFullException` (a `SandboxException`) for both details. Error messages SHALL never include the path or the free-space figure.

#### Scenario: fifty files in one call
- **WHEN** the SDK calls `files.write_files([WriteEntry(f"/home/user/m3/many/f{i:02}.txt", ...) for i in range(50)])`
- **THEN** exactly one `Write` stream is opened, the response has 50 entries whose names are `f00.txt` … `f49.txt` in order, and `ls /home/user/m3/many | wc -l` prints `50`

#### Scenario: mode honoured
- **WHEN** the SDK calls `files.write(path, b"s", mode=0o600)`
- **THEN** `files.get_info(path).mode == 0o600`, `permissions == "-rw-------"` and `stat -c %a` prints `600`

#### Scenario: oversized chunk
- **WHEN** a `WriteRequest` carries a 1 MiB + 1 byte `chunk`
- **THEN** the RPC fails with `INVALID_ARGUMENT` and no `.rayito-tmp-*` file remains in the directory

#### Scenario: destination is a symlink
- **WHEN** the destination path is a symlink to another file and a `Write` completes
- **THEN** the destination becomes a regular file with the new content and the former target is unchanged

#### Scenario: denied destination through the proxy
- **WHEN** the SDK calls `files.write("/usr/local/bin/rayd", b"x")` against a real MicroVM
- **THEN** `AuthenticationException` is raised (the client receives `PERMISSION_DENIED`, not `CANCELLED`)

#### Scenario: disk reserve refused before any temporary
- **WHEN** an integration test's fake filesystem reports 10 MiB free and a `Write` stream begins a second file after a first one committed
- **THEN** the RPC fails with `RESOURCE_EXHAUSTED` and details `disk_reserve`, the first file stays committed, no `.rayito-tmp-*` was created for the second, and the SDK raises `DiskFullException`

#### Scenario: reserve not hit on AWS
- **WHEN** the e2e writes a 1 MiB file on a fresh MicroVM and reads `get_metrics()`
- **THEN** the write succeeds and `disk_total_bytes - disk_used_bytes` exceeds 256 MiB
