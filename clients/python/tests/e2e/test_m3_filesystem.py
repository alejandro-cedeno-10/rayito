"""M3 "filesystem": `FilesystemService` completo a través del proxy de AWS con
su postura de seguridad final (lista de denegación sobre la ruta canónica,
identidad de sistema de ficheros del usuario, escrituras atómicas, watches
acotados con `KeepAlive`) y la superficie `sbx.files` del SDK. Un único
MicroVM (≈ 3 min).

Los 18 bloques siguen `openspec/changes/m3-filesystem/design.md`
"Acceptance test list", en el mismo orden; cada bloque es una función para
que un fallo diga qué contrato se rompió. Los tiempos se imprimen para
pegarlos en `MILESTONES.md` y `AWS_API_NOTES.md` §16."""

from __future__ import annotations

import asyncio
import hashlib
import os
import statistics
import time
from collections.abc import Callable
from typing import TypeVar

import pytest

from rayito import (
    AsyncSandbox,
    FilesystemEvent,
    FilesystemEventType,
    FileType,
    Sandbox,
    WatchHandle,
    WriteEntry,
)
from rayito._aws import LambdaMicrovmsControlPlane
from rayito._limits import TERMINAL_STATES
from rayito.exceptions import (
    AuthenticationException,
    FileNotFoundException,
    InvalidArgumentException,
    TimeoutException,
)

from .test_m1_hello import wait_for_terminal_state

T = TypeVar("T")

BASE = "/home/user/m3"
WATCH_DIR = f"{BASE}/watch"
PAYLOAD_BYTES = 8_000_000
MEGABYTE = 1_000_000
READ_CHUNK_BYTES = 262_144
MIN_STREAM_CHUNKS = 30
TRANSFER_BUDGET_SECONDS = 10.0
# Subida a través del proxy: una ventana HTTP/2 de 64 KiB por RTT del cliente
# (≈ 0,66 MB/s a 93 ms, AWS_API_NOTES.md §16 Q32), no los 4 MB/s de bajada.
UPLOAD_BUDGET_SECONDS = 20.0
BATCH_FILES = 50
BATCH_BUDGET_SECONDS = 5.0
LIST_BUDGET_SECONDS = 1.0
WATCH_STARTED_BUDGET_SECONDS = 1.0
EVENT_BUDGET_SECONDS = 5.0
EVENT_POLL_SECONDS = 0.2
WATCH_TIMEOUT_SECONDS = 2
WATCH_TIMEOUT_BUDGET_SECONDS = 4.0
SEQUENTIAL_STATS = 30
SEQUENTIAL_BUDGET_SECONDS = 20.0
TEMP_PREFIX = ".rayito-tmp-"
HELLO = "hola ñ\n"


def report(label: str, seconds: float) -> None:
    print(f"\n[m3] {label}: {seconds:.2f} s", flush=True)


def report_rate(label: str, size: int, seconds: float) -> None:
    rate = size / MEGABYTE / seconds if seconds > 0 else float("inf")
    print(f"\n[m3] {label}: {seconds:.2f} s ({rate:.2f} MB/s)", flush=True)


def timed(action: Callable[[], T]) -> tuple[T, float]:
    started = time.perf_counter()
    result = action()
    return result, time.perf_counter() - started


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def named(events: list[FilesystemEvent]) -> list[tuple[str, FilesystemEventType]]:
    return [(event.name, event.type) for event in events]


def collect_until(
    handle: WatchHandle, done: Callable[[list[FilesystemEvent]], bool]
) -> list[FilesystemEvent]:
    """Sondea `get_new_events()` cada 0,2 s hasta `done` o 5 s."""
    collected: list[FilesystemEvent] = []
    deadline = time.monotonic() + EVENT_BUDGET_SECONDS
    while time.monotonic() < deadline:
        collected.extend(handle.get_new_events())
        if done(collected):
            return collected
        time.sleep(EVENT_POLL_SECONDS)
    return collected


def wait_for(predicate: Callable[[], bool], budget: float = EVENT_BUDGET_SECONDS) -> bool:
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(EVENT_POLL_SECONDS)
    return predicate()


def assert_clean_names(events: list[FilesystemEvent]) -> None:
    for event in events:
        assert "/" not in event.name, event
        assert not event.name.startswith(TEMP_PREFIX), event


def check_write_big(sandbox: Sandbox, payload: bytes) -> None:
    info, elapsed = timed(lambda: sandbox.files.write(f"{BASE}/big.bin", payload))
    report_rate(f"write {PAYLOAD_BYTES} B", PAYLOAD_BYTES, elapsed)
    assert elapsed <= UPLOAD_BUDGET_SECONDS
    assert info.size == PAYLOAD_BYTES
    assert info.type is FileType.FILE
    assert info.path == f"{BASE}/big.bin"
    assert info.name == "big.bin"
    assert (info.owner, info.group) == ("user", "user")
    assert info.permissions == "-rw-r--r--"
    assert info.mode == 0o644


def check_read_big(sandbox: Sandbox, payload: bytes) -> None:
    data, elapsed = timed(lambda: sandbox.files.read(f"{BASE}/big.bin", format="bytes"))
    report_rate(f"read {PAYLOAD_BYTES} B", PAYLOAD_BYTES, elapsed)
    assert elapsed <= TRANSFER_BUDGET_SECONDS
    assert sha256(data) == sha256(payload)
    chunks = list(sandbox.files.read(f"{BASE}/big.bin", format="stream"))
    assert b"".join(chunks) == payload
    assert all(len(chunk) <= READ_CHUNK_BYTES for chunk in chunks)
    assert len(chunks) >= MIN_STREAM_CHUNKS
    sandbox.files.write(f"{BASE}/hola.txt", HELLO)
    assert sandbox.files.read(f"{BASE}/hola.txt") == HELLO
    with pytest.raises(InvalidArgumentException):
        sandbox.files.read(f"{BASE}/big.bin")


def check_write_files(sandbox: Sandbox) -> None:
    files = [
        WriteEntry(f"{BASE}/many/f{i:02}.txt", f"file {i}\n".encode()) for i in range(BATCH_FILES)
    ]
    entries, elapsed = timed(lambda: sandbox.files.write_files(files))
    report(f"write_files x{BATCH_FILES} en un stream", elapsed)
    assert elapsed <= BATCH_BUDGET_SECONDS
    assert len(entries) == BATCH_FILES
    assert [entry.name for entry in entries] == [f"f{i:02}.txt" for i in range(BATCH_FILES)]
    assert all(entry.size == len(f"file {i}\n") for i, entry in enumerate(entries))
    assert sandbox.commands.run(f"ls {BASE}/many | wc -l").stdout.strip() == str(BATCH_FILES)


def check_list(sandbox: Sandbox) -> None:
    listing, elapsed = timed(lambda: sandbox.files.list(BASE, depth=2))
    report("list(depth=2)", elapsed)
    assert elapsed <= LIST_BUDGET_SECONDS
    top = [entry for entry in listing if entry.path == f"{BASE}/{entry.name}"]
    assert {entry.name for entry in top} == {"big.bin", "hola.txt", "many"}
    many_index = next(index for index, entry in enumerate(listing) if entry.name == "many")
    assert listing[many_index].type is FileType.DIR
    children = listing[many_index + 1 : many_index + 1 + BATCH_FILES]
    assert [entry.path for entry in children] == [
        f"{BASE}/many/f{i:02}.txt" for i in range(BATCH_FILES)
    ]
    depth_one = sandbox.files.list(BASE)
    assert all(entry.path == f"{BASE}/{entry.name}" for entry in depth_one)
    assert sandbox.files.list(BASE, depth=0) == depth_one
    with pytest.raises(InvalidArgumentException):
        sandbox.files.list(f"{BASE}/big.bin")
    with pytest.raises(FileNotFoundException):
        sandbox.files.list(f"{BASE}/nope")


def check_make_dir(sandbox: Sandbox) -> None:
    assert sandbox.files.make_dir(f"{BASE}/dir") is True
    assert sandbox.files.make_dir(f"{BASE}/dir") is False
    with pytest.raises(InvalidArgumentException):
        sandbox.files.make_dir(f"{BASE}/big.bin")
    assert sandbox.files.make_dir(f"{BASE}/a/b/c") is True
    assert sandbox.files.get_info(f"{BASE}/a/b").type is FileType.DIR


def check_exists_and_symlink(sandbox: Sandbox) -> None:
    assert sandbox.files.exists(f"{BASE}/missing") is False
    assert sandbox.files.exists(f"{BASE}/big.bin") is True
    with pytest.raises(FileNotFoundException):
        sandbox.files.get_info(f"{BASE}/missing")
    sandbox.commands.run(f"ln -s big.bin {BASE}/link")
    link = sandbox.files.get_info(f"{BASE}/link")
    assert link.type is FileType.SYMLINK
    assert link.symlink_target == "big.bin"
    with pytest.raises(InvalidArgumentException):
        sandbox.files.read(f"{BASE}/link", format="bytes")


def check_rename(sandbox: Sandbox) -> None:
    moved = sandbox.files.rename(f"{BASE}/hola.txt", f"{BASE}/dir/hola.txt")
    assert moved.path == f"{BASE}/dir/hola.txt"
    assert sandbox.files.exists(f"{BASE}/hola.txt") is False
    assert sandbox.files.read(f"{BASE}/dir/hola.txt") == HELLO
    with pytest.raises(FileNotFoundException):
        sandbox.files.rename(f"{BASE}/missing", f"{BASE}/x")
    with pytest.raises(InvalidArgumentException):
        sandbox.files.rename(f"{BASE}/dir/hola.txt", f"{BASE}/many")


def check_remove(sandbox: Sandbox) -> None:
    sandbox.files.remove(f"{BASE}/dir/hola.txt")
    assert sandbox.files.exists(f"{BASE}/dir/hola.txt") is False
    with pytest.raises(InvalidArgumentException):
        sandbox.files.remove(f"{BASE}/many", recursive=False)
    sandbox.files.remove(f"{BASE}/many")
    assert sandbox.files.exists(f"{BASE}/many") is False
    with pytest.raises(FileNotFoundException):
        sandbox.files.remove(f"{BASE}/many")
    sandbox.files.remove(f"{BASE}/link")
    assert sandbox.files.exists(f"{BASE}/big.bin") is True


def check_path_policy(sandbox: Sandbox) -> None:
    with pytest.raises(AuthenticationException) as excinfo:
        sandbox.files.read("/etc/passwd")
    assert excinfo.value.proxy_rejected is False
    with pytest.raises(AuthenticationException):
        sandbox.files.write("/usr/local/bin/rayd", b"x")
    with pytest.raises(AuthenticationException):
        sandbox.files.list("/proc")
    assert "etc" in [entry.name for entry in sandbox.files.list("/")]
    with pytest.raises(InvalidArgumentException):
        sandbox.files.read(f"{BASE}/../../etc/passwd")
    sandbox.files.write("m3/rel.txt", "r")
    assert sandbox.files.exists("/home/user/m3/rel.txt") is True


def check_identity(sandbox: Sandbox) -> None:
    owner = sandbox.commands.run(f"stat -c %U:%G {BASE}/big.bin").stdout.strip()
    assert owner == "user:user"
    with pytest.raises(AuthenticationException):
        sandbox.files.list("/root")
    with pytest.raises(AuthenticationException):
        sandbox.files.write("/root/x", b"x", user="root")


def check_mode(sandbox: Sandbox) -> None:
    sandbox.files.write(f"{BASE}/secret.txt", b"s", mode=0o600)
    info = sandbox.files.get_info(f"{BASE}/secret.txt")
    assert info.mode == 0o600
    assert info.permissions == "-rw-------"
    assert sandbox.commands.run(f"stat -c %a {BASE}/secret.txt").stdout.strip() == "600"


def check_watch_shell_events(sandbox: Sandbox) -> None:
    sandbox.files.make_dir(WATCH_DIR)
    handle, elapsed = timed(lambda: sandbox.files.watch_dir(WATCH_DIR))
    report("watch_dir -> WatchStarted", elapsed)
    assert elapsed <= WATCH_STARTED_BUDGET_SECONDS
    started = time.perf_counter()
    sandbox.commands.run(
        f"echo hola > {WATCH_DIR}/a.txt; sleep 0.3; echo mas >> {WATCH_DIR}/a.txt; "
        f"sleep 0.3; rm {WATCH_DIR}/a.txt"
    )
    events = collect_until(
        handle,
        lambda seen: ("a.txt", FilesystemEventType.REMOVE) in named(seen),
    )
    report("create -> REMOVE recibido", time.perf_counter() - started)
    kinds = named(events)
    assert ("a.txt", FilesystemEventType.CREATE) in kinds
    assert ("a.txt", FilesystemEventType.WRITE) in kinds
    assert ("a.txt", FilesystemEventType.REMOVE) in kinds
    create_at = kinds.index(("a.txt", FilesystemEventType.CREATE))
    write_at = kinds.index(("a.txt", FilesystemEventType.WRITE))
    remove_at = kinds.index(("a.txt", FilesystemEventType.REMOVE))
    assert create_at < write_at < remove_at
    assert_clean_names(events)
    handle.stop()
    assert handle.is_running is False
    assert handle.get_new_events() == []


def check_watch_callback_and_entries(sandbox: Sandbox) -> None:
    seen: list[FilesystemEvent] = []
    handle = sandbox.files.watch_dir(WATCH_DIR, on_event=seen.append, include_entry=True)
    try:
        sandbox.files.write(f"{WATCH_DIR}/w.txt", b"w")
        assert wait_for(
            lambda: any(
                event.name == "w.txt"
                and event.type is FilesystemEventType.WRITE
                and event.entry is not None
                for event in seen
            )
        ), named(seen)
        assert ("w.txt", FilesystemEventType.RENAME) not in named(seen), named(seen)
        assert_clean_names(seen)
        sandbox.commands.run(f"chmod 600 {WATCH_DIR}/w.txt")

        def chmod_with_entry() -> bool:
            return any(
                event.name == "w.txt"
                and event.type is FilesystemEventType.CHMOD
                and event.entry is not None
                and event.entry.mode == 0o600
                for event in seen
            )

        assert wait_for(chmod_with_entry), named(seen)
    finally:
        handle.stop()


def check_watch_errors_and_timeout(sandbox: Sandbox) -> None:
    with pytest.raises(FileNotFoundException):
        sandbox.files.watch_dir(f"{BASE}/nope")
    with pytest.raises(InvalidArgumentException):
        sandbox.files.watch_dir(f"{BASE}/big.bin")
    handle = sandbox.files.watch_dir(WATCH_DIR, timeout=WATCH_TIMEOUT_SECONDS)
    assert wait_for(lambda: not handle.is_running, WATCH_TIMEOUT_BUDGET_SECONDS)
    with pytest.raises(TimeoutException):
        handle.get_new_events()
    assert handle.is_running is False


def check_watch_recursive(sandbox: Sandbox) -> None:
    """El watch del subdirectorio nuevo se instala al recibir su `CREATE`;
    un fichero creado en ese hueco (a 1 vCPU, `mkdir sub && echo x > sub/n.txt`
    lo pierde una de cada dos veces) sólo aporta su `WRITE`, límite de
    inotify. Se espera el `CREATE` de `sub` antes de crear el fichero."""
    handle = sandbox.files.watch_dir(WATCH_DIR, recursive=True)
    try:
        sandbox.commands.run(f"mkdir {WATCH_DIR}/sub")
        created = collect_until(
            handle, lambda seen: ("sub", FilesystemEventType.CREATE) in named(seen)
        )
        assert ("sub", FilesystemEventType.CREATE) in named(created)
        sandbox.commands.run(f"echo x > {WATCH_DIR}/sub/n.txt")
        events = collect_until(
            handle,
            lambda seen: ("sub/n.txt", FilesystemEventType.CREATE) in named(seen),
        )
        assert ("sub/n.txt", FilesystemEventType.CREATE) in named(events)
    finally:
        handle.stop()


def check_connection_budget(sandbox: Sandbox) -> None:
    samples: list[float] = []
    started = time.perf_counter()
    for _ in range(SEQUENTIAL_STATS):
        exists, elapsed = timed(lambda: sandbox.files.exists(f"{BASE}/big.bin"))
        assert exists is True
        samples.append(elapsed)
    total = time.perf_counter() - started
    p95 = statistics.quantiles(samples, n=20)[-1]
    report(f"{SEQUENTIAL_STATS} exists secuenciales (p95 {p95:.3f} s)", total)
    assert total <= SEQUENTIAL_BUDGET_SECONDS
    assert sandbox.commands.list() == []


async def async_parity(sandbox_id: str, access_token: str, region: str) -> None:
    sandbox = await AsyncSandbox.connect(sandbox_id, access_token=access_token, region=region)
    try:
        await sandbox.files.write(f"{BASE}/async.txt", "async")
        assert await sandbox.files.read(f"{BASE}/async.txt") == "async"
        assert "async.txt" in [entry.name for entry in await sandbox.files.list(BASE)]
        handle = await sandbox.files.watch_dir(WATCH_DIR)
        await sandbox.commands.run(f"touch {WATCH_DIR}/z.txt")
        collected: list[FilesystemEvent] = []
        deadline = time.monotonic() + EVENT_BUDGET_SECONDS
        while time.monotonic() < deadline:
            collected.extend(await handle.get_new_events())
            if ("z.txt", FilesystemEventType.CREATE) in named(collected):
                break
            await asyncio.sleep(EVENT_POLL_SECONDS)
        assert ("z.txt", FilesystemEventType.CREATE) in named(collected), named(collected)
        await handle.stop()
        assert await sandbox.files.make_dir(f"{BASE}/adir") is True
        assert await sandbox.files.exists(f"{BASE}/nope") is False
    finally:
        await sandbox.close()


def check_async_parity(sandbox: Sandbox) -> None:
    asyncio.run(async_parity(sandbox.sandbox_id, sandbox.access_token, sandbox.region))


@pytest.mark.e2e
def test_m3_filesystem(sandbox: Sandbox, control_plane: LambdaMicrovmsControlPlane) -> None:
    assert sandbox.files.make_dir(BASE) is True
    payload = os.urandom(PAYLOAD_BYTES)
    check_write_big(sandbox, payload)
    check_read_big(sandbox, payload)
    check_write_files(sandbox)
    check_list(sandbox)
    check_make_dir(sandbox)
    check_exists_and_symlink(sandbox)
    check_rename(sandbox)
    check_remove(sandbox)
    check_path_policy(sandbox)
    check_identity(sandbox)
    check_mode(sandbox)
    check_watch_shell_events(sandbox)
    check_watch_callback_and_entries(sandbox)
    check_watch_errors_and_timeout(sandbox)
    check_watch_recursive(sandbox)
    check_connection_budget(sandbox)
    check_async_parity(sandbox)

    assert sandbox.kill() is True
    final, seconds = wait_for_terminal_state(sandbox.sandbox_id, control_plane)
    report(f"terminate-microvm -> {final.state}", seconds)
    assert final.state in TERMINAL_STATES
