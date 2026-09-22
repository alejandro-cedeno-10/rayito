"""Benchmark nightly de M3 (marker `bench`, `make test-bench`): 50 MB de
escritura + lectura a través del proxy con el deadline por defecto
(`60 s + 1 s/MB`). A 2 GB el endpoint limita a ≈ 4 MB/s por sentido, así que
la teoría son ≥ 12,5 s por trayecto; se exige ≤ 120 s en total y se imprimen
los MB/s de cada sentido para `MILESTONES.md` y `AWS_API_NOTES.md` §16."""

from __future__ import annotations

import hashlib
import os
import time

import pytest

from rayito import Sandbox

BASE = "/home/user/m3"
PAYLOAD_BYTES = 50_000_000
MEGABYTE = 1_000_000
TOTAL_BUDGET_SECONDS = 120.0


def report_rate(label: str, seconds: float) -> None:
    rate = PAYLOAD_BYTES / MEGABYTE / seconds if seconds > 0 else float("inf")
    print(f"\n[m3-bench] {label}: {seconds:.2f} s ({rate:.2f} MB/s)", flush=True)


@pytest.mark.bench
def test_m3_bench_fifty_megabytes_round_trip(sandbox: Sandbox) -> None:
    payload = os.urandom(PAYLOAD_BYTES)
    assert sandbox.files.make_dir(BASE) is True

    started = time.perf_counter()
    info = sandbox.files.write(f"{BASE}/bench.bin", payload)
    write_seconds = time.perf_counter() - started
    report_rate("write 50 MB", write_seconds)
    assert info.size == PAYLOAD_BYTES

    started = time.perf_counter()
    data = sandbox.files.read(f"{BASE}/bench.bin", format="bytes")
    read_seconds = time.perf_counter() - started
    report_rate("read 50 MB", read_seconds)

    total = write_seconds + read_seconds
    print(f"\n[m3-bench] total: {total:.2f} s", flush=True)
    assert hashlib.sha256(data).hexdigest() == hashlib.sha256(payload).hexdigest()
    assert total <= TOTAL_BUDGET_SECONDS
