# Files and transfers

`sbx.files` reads, writes, lists and watches sandbox files over gRPC
through the Lambda MicroVMs proxy. It also provides presigned S3 URLs
(`upload_url`/`download_url`), a path through S3 for large files, gzip,
per-file metadata and a deadline between read chunks.

!!! note "The S3 features require an image 0.3.0 or later"
    The URLs, large files through S3, gzip and per-file metadata need a
    `rayd` 0.3.0 or later in the image: on an earlier one the SDK raises
    `UnimplementedError("update the image")` before moving a single byte.

## The files surface { #la-superficie-de-ficheros }

| Method | What it does |
|---|---|
| `files.read(path, format=...)` | reads a regular file as `text`, `bytes` or `stream` (TS: also `blob`) |
| `files.write(path, data)` / `write_files([WriteEntry, ...])` | atomic write (temporary + rename), parents created; several files in one stream |
| `files.list(path, depth=)`, `exists`, `get_info`, `remove`, `rename`, `make_dir` | as in E2B |
| `files.watch_dir(path, on_event=...)` | inotify events until `stop()`; a `files.write` in the directory arrives as a single `WRITE` of the destination (with `entry` if `include_entry=True`), like E2B's example |
| `files.upload_url(path)` / `download_url(path)` | S3 URLs signed with your credentials |

## The basics { #lo-basico }

=== "Python"

    ```python
    from rayito import FilesystemEventType, Sandbox, WriteEntry

    with Sandbox.create() as sbx:
        sbx.files.write("/home/user/a.txt", "hello\n")
        sbx.files.write_files([
            WriteEntry("/home/user/src/main.py", "print('hello')\n"),
            WriteEntry("/home/user/run.sh", "#!/bin/sh\necho hi\n", mode=0o755),
        ])
        print(sbx.files.read("/home/user/a.txt"))                    # "hello\n"
        raw = sbx.files.read("/home/user/a.txt", format="bytes")
        for entry in sbx.files.list("/home/user", depth=2):
            print(entry.path, entry.type, entry.size)
        print(sbx.files.exists("/home/user/a.txt"), sbx.files.get_info("/home/user/run.sh").permissions)
        sbx.files.make_dir("/home/user/out")
        sbx.files.rename("/home/user/a.txt", "/home/user/out/a.txt")
        sbx.files.remove("/home/user/src")                             # recursive by default

        with sbx.files.watch_dir("/home/user/out") as watch:          # (1)!
            sbx.files.write("/home/user/out/b.txt", "b")
            sbx.commands.run("sleep 1")
            events = watch.get_new_events()
            print([(e.name, e.type) for e in events if e.type == FilesystemEventType.WRITE])
    ```

    1. Without `on_event=` the events accumulate for `get_new_events()`;
       leaving the `with` calls `stop()`.

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncSandbox, WriteEntry


    async def main() -> None:
        async with await AsyncSandbox.create() as sbx:
            await sbx.files.write("/home/user/a.txt", "hello\n")
            await sbx.files.write_files([WriteEntry("/home/user/b.txt", b"\x00\x01")])
            print(await sbx.files.read("/home/user/a.txt"))
            print([e.name for e in await sbx.files.list("/home/user")])
            async with await sbx.files.watch_dir("/home/user", on_event=print):
                await sbx.files.write("/home/user/c.txt", "c")
                await asyncio.sleep(1)
            await sbx.files.remove("/home/user/c.txt")


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    await using sbx = await Sandbox.create();
    await sbx.files.write("/home/user/a.txt", "hello\n");
    await sbx.files.writeFiles([
      { path: "/home/user/src/main.py", data: "print('hello')\n" },
      { path: "/home/user/run.sh", data: "#!/bin/sh\necho hi\n", mode: 0o755 },
    ]);
    console.log(await sbx.files.read("/home/user/a.txt")); // "hello\n"
    const raw = await sbx.files.read("/home/user/a.txt", { format: "bytes" }); // Uint8Array
    for (const entry of await sbx.files.list("/home/user", { depth: 2 })) {
      console.log(entry.path, entry.type, entry.size, raw.length);
    }
    console.log(await sbx.files.exists("/home/user/a.txt"));
    await sbx.files.makeDir("/home/user/out");
    await sbx.files.rename("/home/user/a.txt", "/home/user/out/a.txt");
    await sbx.files.remove("/home/user/src");

    {
      await using watch = await sbx.files.watchDir("/home/user/out", {
        onEvent: (event) => console.log(event.type, event.name),
      });
      await sbx.files.write("/home/user/out/b.txt", "b");
      await sbx.commands.run("sleep 1");
      console.log(watch.isRunning);
    }
    ```

- `read` returns `str` by default; `format="bytes"` gives `bytes`
  (`Uint8Array`) and `format="stream"` an iterator of chunks (a
  `ReadableStream` in TypeScript, which also accepts `"blob"`).
- `write` accepts `str`, `bytes` or an open file (TypeScript: `string`,
  `Uint8Array`, `ArrayBuffer`, `Blob` or `ReadableStream`), creates the
  parent directories and returns the `EntryInfo` written; `mode=` sets the
  permissions.
- `EntryInfo` carries `name`, `path`, `type` (`file`, `dir`, `symlink`),
  `size`, `mode`, `permissions`, `owner`, `group`, `modified_time`,
  `symlink_target` and `metadata`.
- `remove(path, recursive=True)`: with `recursive=False`, a non-empty
  directory fails. `make_dir` returns `False` if it already existed.
- `watch_dir` accepts `recursive=`, `include_entry=` (each event carries its
  `EntryInfo`), `on_exit=` and `timeout=` (`0`/`None`: until `stop()`). The
  events are `CREATE`, `WRITE`, `REMOVE`, `RENAME` and `CHMOD`.
- All of them accept `user=` and `request_timeout=` (TypeScript:
  `requestTimeoutMs`).

Through the proxy, a file uploads at ≈ 0.6 MB/s (the proxy's HTTP/2 window)
and downloads at 4-5 MB/s. Between the VM and S3, 55.7-106.2 MB/s upload and
84.2-99.0 MB/s download were measured. That is why large files go through
S3.

## `S3Staging`: the transfer bucket { #s3staging-el-bucket-de-transferencias }

The URLs and large files need a bucket of your own. There is no default
bucket: pass it in `create`/`connect` or in the environment.

=== "Python"

    ```python
    from rayito import S3Staging, Sandbox

    staging = S3Staging(
        "amzn-s3-demo-bucket",
        prefix="rayito-transfer",           # the default
        region=None,                        # the sandbox's; must be the bucket's
        max_expires_in=86400,               # cap on user URLs
        threshold_bytes=8 * 1024 * 1024,    # from here files.write/read go through S3
    )
    with Sandbox.create(transfer=staging) as sbx:
        print(sbx.transfer)
    # or: RAYITO_TRANSFER_BUCKET=amzn-s3-demo-bucket (and RAYITO_TRANSFER_PREFIX, RAYITO_TRANSFER_REGION)
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    await using sbx = await Sandbox.create({
      transfer: { bucket: "amzn-s3-demo-bucket", prefix: "rayito-transfer" },
    });
    console.log(sbx.transfer);
    ```

Keys end up under `<prefix>/<sandbox_id>/<up|down>/<id>`: one key per URL,
tied to the sandbox, which `rayd` checks before doing anything. The SDK
signs with SigV4 and the bucket's **regional** virtual host
(`<bucket>.s3.<region>.amazonaws.com`); `rayd` rejects any other form. The
prefix can never be `rayito` (the image artifacts) or the persistence one:
the 1-day lifecycle rule would delete them.

## `upload_url` and `download_url` { #upload_url-y-download_url }

`upload_url` arms an import in the sandbox and returns an upload URL;
whatever is uploaded to it lands at `path`.

=== "Python"

    ```python
    import requests
    from rayito import S3Staging, Sandbox

    with Sandbox.create(transfer=S3Staging("amzn-s3-demo-bucket")) as sbx:
        ticket = sbx.files.upload_url("/home/user/data.csv", expires_in=900, max_bytes=50_000_000)
        with open("data.csv", "rb") as source:
            requests.put(ticket, data=source, headers=ticket.headers).raise_for_status()
        entry = ticket.wait()          # the import finished: the file's EntryInfo

        link = sbx.files.download_url("/home/user/data.csv", filename="data.csv")
        print(entry.size, link.size, link.sha256)
        body = requests.get(link).content
    ```

=== "Python (async)"

    ```python
    import asyncio

    import httpx
    from rayito import AsyncSandbox, S3Staging


    async def main() -> None:
        staging = S3Staging("amzn-s3-demo-bucket")
        async with await AsyncSandbox.create(transfer=staging) as sbx:
            ticket = await sbx.files.upload_url("/home/user/data.csv", expires_in=900)
            async with httpx.AsyncClient() as http:
                with open("data.csv", "rb") as source:
                    response = await http.put(ticket.url, content=source.read(), headers=ticket.headers)
                response.raise_for_status()
                entry = await ticket.wait()
                print(entry.size, (await ticket.status()).phase)

                link = await sbx.files.download_url("/home/user/data.csv")
                body = (await http.get(link.url)).content
                print(len(body) == link.size)


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { readFile } from "node:fs/promises";
    import { Sandbox } from "rayito";

    await using sbx = await Sandbox.create({ transfer: { bucket: "amzn-s3-demo-bucket" } });
    const ticket = await sbx.files.uploadUrl("/home/user/data.csv", {
      expiresIn: 900,
      maxBytes: 50_000_000,
    });
    const put = await fetch(ticket.url, {
      method: "PUT",
      body: await readFile("data.csv"),
      headers: ticket.headers,
    });
    if (!put.ok) throw new Error(`PUT ${put.status}`);
    const entry = await ticket.wait({ timeoutMs: 120_000 });
    console.log(entry.size);

    const link = await sbx.files.downloadUrl("/home/user/data.csv", { filename: "data.csv" });
    console.log(link.size, link.sha256, link.expiresAt);
    const bytes = new Uint8Array(await (await fetch(link.url)).arrayBuffer());
    console.log(bytes.length === link.size);
    ```

From the terminal, with `curl` and the URLs that `upload_url` and
`download_url` return:

```bash
curl -T data.csv -H "Content-Type: application/octet-stream" "$UPLOAD_URL"
curl -o data.csv "$DOWNLOAD_URL"
curl -H "Range: bytes=0-1023" "$DOWNLOAD_URL"
```

- **The ticket is a `str`** (Python) with the URL; `ticket.headers` are the
  headers the `PUT` must carry (`Content-Type:
  application/octet-stream`). In TypeScript the URL is `ticket.url` (or
  `String(ticket)`).
- **`POST` form** for browsers: `upload_url(path, form=True)` gives
  `ticket.method == "POST"` and `ticket.fields`; the fields are sent plus
  the file in the `file` field. S3 limits the size to `max_bytes` (5 GiB
  otherwise).
- **Single use.** The first upload that lands closes the ticket; a second
  one is not imported. `ticket.status()` and `ticket.cancel()` query and
  cancel the import.
- **Expiry** always set: `expires_in` (3600 s by default), capped by
  `S3Staging.max_expires_in` and by SigV4's 7 days. If you sign with
  temporary credentials (SSO, a role), the URL stops working when they
  expire even if it says more.
- **`download_url` is a snapshot** of the file as it is when called (the
  export finishes before the URL is returned); a file that does not exist
  raises `FileNotFoundException` right away, a directory or a symlink
  `InvalidArgumentException`. From `S3Staging.multipart_threshold_bytes`
  (5 GiB) the export is multipart. An export that a suspension requeues
  rereads the file after resuming.
- The errors of an import (`ticket.wait()`) are
  `FileUploadException(code, reason)`: `too_large`, `expired`,
  `checksum_mismatch`, `disk_reserve`, `wrong_region`...; those of an
  export, `TransferException`.

### The read barrier after an upload { #la-barrera-de-lectura-tras-subida }

After your `PUT`, `rayd` sees the object on the next poll of the `GET`
(every 1 s for the first 10 minutes, then every 5 s) and writes it. So that
"I upload and then read or run" works without `wait()`, a later
`files.read`, `get_info`, `list` of the parent directory, `commands.run`,
`run_code` or `pty.create` waits up to 2 s for armed imports: if the object
is already in S3 the import completes before the operation. If it does not
arrive in that margin the operation goes on (it will see the old file or
`NotFound`); `ticket.wait()` is the deterministic way. Measured: between
the `PUT`'s `200` and the file being visible there are 0.34 s median and
0.83 s p95 with 1 MiB; with 50 MB, `ticket.wait()` returns 0.7 s after the
`200`.

## Large files through S3 { #ficheros-grandes-por-s3 }

With `transfer=` configured, `files.write`/`write_files` of a file of
`threshold_bytes` or more (8 MiB by default, and every non-seekable binary
stream) is uploaded by the SDK directly to S3 with your credentials while it
computes its sha256, and `rayd` imports it checking that sha256.
`files.read` of such a file exports it and the SDK downloads it verifying
the sha256; the temporary object is deleted when it finishes. Without
`transfer` the gRPC path does not change. `gzip` does not apply to what goes
through S3. Measured: inside the VM `rayd` imports at 60-74 MB/s and
exports at 28-86 MB/s (10 MiB to 200 MB), so the limit is your link to S3.
From a home connection of ~3.5 MB/s upload, 200 MB through S3 goes 6 times
faster than through gRPC when writing; when reading, gRPC already matches
that link. It has not been measured from a client in the same region.

=== "Python"

    ```python
    from rayito import S3Staging, Sandbox

    staging = S3Staging("amzn-s3-demo-bucket", threshold_bytes=8 * 1024 * 1024)
    with Sandbox.create(transfer=staging) as sbx:
        with open("model.bin", "rb") as source:          # ≥ 8 MiB: through S3, sha256 checked
            info = sbx.files.write("/home/user/model.bin", source)
        data = sbx.files.read("/home/user/model.bin", format="bytes")   # comes back through S3
        print(info.size == len(data))
    ```

=== "TypeScript"

    ```ts
    import { createReadStream } from "node:fs";
    import { Readable } from "node:stream";
    import { Sandbox } from "rayito";

    await using sbx = await Sandbox.create({
      transfer: { bucket: "amzn-s3-demo-bucket", thresholdBytes: 8 * 1024 * 1024 },
    });
    // A ReadableStream always goes through S3 (streaming upload with @aws-sdk/lib-storage).
    const body = Readable.toWeb(createReadStream("model.bin")) as ReadableStream<Uint8Array>;
    const info = await sbx.files.write("/home/user/model.bin", body);
    const data = await sbx.files.read("/home/user/model.bin", { format: "bytes" });
    console.log(info.size === data.length);
    ```

## gzip, metadata and `stream_idle_timeout` { #gzip-metadatos-y-stream_idle_timeout }

=== "Python"

    ```python
    from rayito import Sandbox

    with Sandbox.create() as sbx:
        big_text = "line\n" * 100_000
        sbx.files.write("/home/user/log.txt", big_text, gzip=True)           # gRPC compression
        text = sbx.files.read("/home/user/log.txt", gzip=True)               # compressed response
        sbx.files.write("/home/user/a.bin", b"\x00" * 16, metadata={"source": "ci", "batch": "7"})
        print(sbx.files.get_info("/home/user/a.bin").metadata)               # {'source': 'ci', 'batch': '7'}
        for chunk in sbx.files.read("/home/user/log.txt", format="stream", stream_idle_timeout=30):
            print(len(chunk))
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    await using sbx = await Sandbox.create();
    await sbx.files.write("/home/user/log.txt", "line\n".repeat(100_000), { gzip: true });
    const text = await sbx.files.read("/home/user/log.txt", { gzip: true });
    await sbx.files.write("/home/user/a.bin", new Uint8Array(16), {
      metadata: { source: "ci", batch: "7" },
    });
    console.log((await sbx.files.getInfo("/home/user/a.bin")).metadata);
    const blob = await sbx.files.read("/home/user/log.txt", { format: "blob", streamIdleTimeoutMs: 30_000 });
    console.log(text.length, blob.size);
    ```

- **gzip** is gRPC's standard compression: responses only arrive
  compressed if the request carries the `rayito-compress: gzip` header,
  which the SDK sets with `gzip=True`. It crosses the proxy in both
  directions: 20 MB of compressible text goes from 0.80 to 52.34 MB/s of
  write + read.
- **Metadata**: stored as `user.rayito.<key>` xattrs of the file (keys of
  HTTP token characters, lowercase; printable ASCII values; ≤ 64 keys and
  ≤ 4,000 B). Overwriting a file replaces the whole set, and overwriting it
  without `metadata` empties it (envd semantics). They are not secrets and
  are not written to logs.
- **`stream_idle_timeout`** (TS `streamIdleTimeoutMs`) cuts a read that has
  gone that long without receiving a chunk (`TimeoutException`).
- **`use_octet_stream`** is accepted with no effect (gRPC has no multipart
  form).

## S3 cost { #coste-de-s3 }

Without a transfer bucket (neither `transfer=` nor
`RAYITO_TRANSFER_BUCKET`), `sbx.files` does not touch S3 and adds no cost.
With a bucket, S3 charges on your bill:

- **Requests**: one `PutObject`/`GetObject` (or the parts of a multipart)
  for each file of `threshold_bytes` or more and for each URL that is used.
- **Temporary storage**: the objects under the transfer prefix until they
  are deleted; the 1-day lifecycle rule in `infra/README.md` sets the cap.

In return, large files take much less time, and that means fewer billed
MicroVM seconds. Figures and examples: [Costs](cost.md); current prices:
[Amazon S3](https://aws.amazon.com/s3/pricing/).

Note: exporting `RAYITO_TRANSFER_BUCKET` is enough to turn this path on in
every `create()` of the process, also in the E2B shim.

## IAM and the bucket { #iam-y-el-bucket }

The credentials that sign are those of **your** process (the caller), never
an execution role: `rayd` stores none. The caller's minimal policy is
parameterized in `infra/iam.yaml` (`TransferBucket`, `TransferPrefix`):
`s3:PutObject`, `s3:GetObject`, `s3:DeleteObject` and
`s3:AbortMultipartUpload` on `<bucket>/<prefix>/*` and `s3:ListBucket`
scoped to that prefix (without it, a missing key gives 403 instead of 404).
The bucket recipe (1-day lifecycle rule, a policy that requires SigV4 and
TLS, CORS for browsers, SSE-KMS) is in `infra/README.md`, "File transfers".

## In the E2B shim { #en-el-shim-de-e2b }

`rayito.e2b.Sandbox.create()` does not accept `transfer=`: the bucket comes
from `RAYITO_TRANSFER_BUCKET` (and `RAYITO_TRANSFER_PREFIX`,
`RAYITO_TRANSFER_REGION`).

=== "Python"

    ```python
    import os

    import requests
    from rayito.e2b import Sandbox

    os.environ.setdefault("RAYITO_TRANSFER_BUCKET", "amzn-s3-demo-bucket")
    with Sandbox.create() as sbx:
        url = sbx.upload_url("/home/user/in.csv", use_signature_expiration=600)
        requests.put(url, data=b"a,b\n1,2\n", headers=url.headers).raise_for_status()
        url.wait()                                   # Rayito only: waits for the import
        print(requests.get(sbx.download_url("/home/user/in.csv")).text)
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito/e2b";

    // RAYITO_TRANSFER_BUCKET=amzn-s3-demo-bucket in the environment
    await using sbx = await Sandbox.create();
    const url = await sbx.uploadUrl("/home/user/in.csv", { useSignatureExpiration: 600 });
    await fetch(url, {
      method: "PUT",
      body: "a,b\n1,2\n",
      headers: { "Content-Type": "application/octet-stream" },
    });
    // rayd's barrier makes the next read wait for the import (up to 2 s)
    console.log(await sbx.files.read("/home/user/in.csv"));
    const download = await sbx.downloadUrl("/home/user/in.csv");
    console.log(await (await fetch(download)).text());
    ```

In TypeScript the shim returns the URL as a `string`, like E2B; for the
ticket with `wait()` use `sbx.native.files.uploadUrl()`. The native
`Sandbox` also has this E2B form, `sbx.upload_url(path, user=,
use_signature_expiration=)` and `sbx.download_url(...)` (TypeScript:
`sbx.uploadUrl(path, { useSignatureExpiration })`, which returns the URL as
a `string`), next to `sbx.files.upload_url`/`download_url` with all the
options (`expires_in`, `max_bytes`, `form`, `filename`).

## Differences from E2B { #diferencias-con-e2b }

- Upload by `PUT` with the raw body instead of envd's multipart `POST`;
  `form=True` gives the fields of a `POST`.
- The upload lands asynchronously: the 2 s barrier and `ticket.wait()`
  cover it.
- Single-use tickets.
- The download is a snapshot taken when called.
- Expiry always set (≤ 7 days and ≤ the life of your credentials);
  `use_signature_expiration <= 0` raises.
- A transfer bucket is required; without it, `UnimplementedError`.
- A file that does not exist raises in `download_url`, not on download.
- `upload_url(path=None)` raises: an S3 URL carries no file name.
- In the async shim, `upload_url`/`download_url` are coroutines.

??? info "Sources and measurements"
    - Design: ADR-010 in [`ARCHITECTURE.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/ARCHITECTURE.md);
      threat model T16 in [`SECURITY.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/SECURITY.md).
    - Measurements in [`AWS_API_NOTES.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/AWS_API_NOTES.md):
      Q27 and Q32 (throughput through the proxy), Q59 (VM ↔ S3 throughput),
      Q62 (expiry with temporary credentials), Q71 (barrier after upload),
      Q74 (gzip) and Q75 (large files).
