"""A stdlib-only kernel sidecar speaking protocol v1 with scripted cells, for
``crates/rayd/tests/m4_code.rs``. Python 3.11+.

Cells: ``x = 42`` (defines x), ``x`` (result "42" or NameError), ``print(x)``
(stdout), ``1/0`` (ZeroDivisionError), ``plot`` (png + chart), ``df`` (text,
html, data), ``sleep <s>`` (obeys interrupt), ``hang <s>`` (ignores it),
``big <n>`` (n stdout chunks of 64 KiB), ``omit`` (rayito/omitted), ``die``
(the sidecar exits with code 3), ``kernel-die`` (KernelDied + kernel_died,
then the context works again), ``print <text>`` (stdout), anything else
(result with the code as text).

Flags: ``--warmup-ms N`` delays ``ready``; ``--restart-ms N`` delays every
``restart_context`` reply; ``--log FILE`` appends every request as a JSON
line; ``--noisy-stderr`` writes a plain-text stderr line; ``--resume-lost
CTX`` reports that context (``created`` = every non-default context) as not
alive in the **first** ``resume`` reply only, so the following
``restart_context`` ends its running cell with ``KernelRestarted`` like a
real restart and a later ``resume`` finds every kernel alive;
``--resume-delay-ms N`` delays every ``resume`` reply; ``--create-delay-ms
N`` delays every ``create_context`` reply; ``--reseed-delay-ms N`` delays
every ``reseed`` reply (the M6 advisory-op case: ``rayd`` must log the
timeout without counting it towards the kill switch). The ``reseed`` reply
carries the M6 ``deferred`` list (always empty here: the fake never has a
cell in flight when it reseeds) and the M7 ``skipped`` list (every
non-Python context). ``--languages python,bash`` is what ``ready``
announces (M7; ``none`` omits the field like a pre-M7 sidecar);
``create_context`` honours ``language`` (unknown names are
``invalid_argument`` ``unknown language``, names outside the list
``language not installed``) and ``list_contexts`` echoes it.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import queue
import sys
import threading
import time

PNG_1X1 = base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478"
        "9c63f8ffff3f0005fe02fea7f1cc620000000049454e44ae426082"
    )
).decode()
CHART = json.dumps(
    {
        "type": "line",
        "title": None,
        "elements": [{"label": "Line 0", "points": [[0.0, 1.0], [1.0, 2.0], [2.0, 3.0]]}],
        "x_label": None,
        "y_label": None,
        "x_unit": None,
        "y_unit": None,
        "x_ticks": [0.0, 1.0, 2.0],
        "x_tick_labels": ["0", "1", "2"],
        "x_scale": "linear",
        "y_ticks": [1.0, 2.0, 3.0],
        "y_tick_labels": ["1", "2", "3"],
        "y_scale": "linear",
    }
)
CHUNK = "x" * 65536

ARGS = None
WRITE_LOCK = threading.Lock()
LOG_LOCK = threading.Lock()


def emit(event: dict) -> None:
    line = json.dumps(event, separators=(",", ":"))
    with WRITE_LOCK:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def log_request(request: dict) -> None:
    if ARGS is None or not ARGS.log:
        return
    with LOG_LOCK, open(ARGS.log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(request) + "\n")


def reply_ok(request_id: int, payload: dict) -> None:
    emit({"event": "reply", "id": request_id, "ok": True, "payload": payload})


def reply_error(request_id: int, code: str, message: str) -> None:
    emit(
        {
            "event": "reply",
            "id": request_id,
            "ok": False,
            "error": {"code": code, "message": message},
        }
    )


class Execution:
    def __init__(self, request: dict) -> None:
        self.request = request
        self.request_id = request["id"]
        self.execution_id = request.get("execution_id", "")
        self.code = request.get("code", "")
        self.interrupted = threading.Event()
        self.cancelled: str | None = None
        self.done = threading.Event()

    def base(self) -> dict:
        return {"id": self.request_id, "execution_id": self.execution_id}


KNOWN_LANGUAGES = ("python", "bash", "javascript")


def announced_languages() -> list[str] | None:
    if ARGS is None or ARGS.languages == "none":
        return None
    return sorted({name for name in ARGS.languages.split(",") if name})


class Context:
    def __init__(self, context_id: str, cwd: str, envs: dict, language: str = "python") -> None:
        self.context_id = context_id
        self.cwd = cwd
        self.language = language
        self.envs = dict(envs)
        self.kernel_pid = 40000 + hash(context_id) % 10000
        self.count = 0
        self.names: set[str] = set()
        self.queue: queue.Queue = queue.Queue()
        self.running: Execution | None = None
        self.lock = threading.Lock()
        self.state = "ready"
        self.thread = threading.Thread(target=self._worker, daemon=True)
        self.thread.start()

    def submit(self, execution: Execution) -> None:
        self.queue.put(execution)

    def interrupt(self, execution_id: str | None) -> None:
        with self.lock:
            running = self.running
        if running is not None and (execution_id is None or running.execution_id == execution_id):
            running.interrupted.set()
            return
        pending = []
        while True:
            try:
                item = self.queue.get_nowait()
            except queue.Empty:
                break
            pending.append(item)
        for item in pending:
            if item.execution_id == execution_id and item.cancelled is None:
                item.cancelled = "interrupted"
                emit({"event": "end", **item.base(), "execution_count": 0})
                continue
            self.queue.put(item)

    def cancel_all(self, reason: str) -> None:
        with self.lock:
            running = self.running
        pending = []
        while True:
            try:
                pending.append(self.queue.get_nowait())
            except queue.Empty:
                break
        for item in pending:
            emit(
                {
                    "event": "error",
                    **item.base(),
                    "name": reason,
                    "value": "cancelled",
                    "traceback": [],
                }
            )
            emit({"event": "end", **item.base(), "execution_count": 0})
        if running is not None:
            running.cancelled = reason
            running.interrupted.set()
            running.done.wait(5)

    def restart(self, envs: dict) -> int:
        self.state = "restarting"
        self.cancel_all("KernelRestarted")
        if ARGS is not None and ARGS.restart_ms:
            time.sleep(ARGS.restart_ms / 1000)
        self.envs = dict(envs)
        self.kernel_pid += 1
        self.count = 0
        self.names.clear()
        self.state = "ready"
        return self.kernel_pid

    def destroy(self) -> None:
        self.state = "dead"
        self.cancel_all("ContextDestroyed")

    def _worker(self) -> None:
        while True:
            execution = self.queue.get()
            if execution.cancelled is not None:
                continue
            with self.lock:
                self.running = execution
            try:
                self._run(execution)
            finally:
                with self.lock:
                    self.running = None
                execution.done.set()

    def _end(self, execution: Execution, count: int) -> None:
        emit({"event": "end", **execution.base(), "execution_count": count})

    def _run(self, execution: Execution) -> None:
        code = execution.code.strip()
        base = execution.base()
        if code == "die":
            sys.stdout.flush()
            os._exit(3)
        self.count += 1
        count = self.count
        emit({"event": "started", **base, "execution_count": count})
        if code == "x = 42":
            self.names.add("x")
        elif code == "x":
            if "x" in self.names:
                emit(
                    {
                        "event": "result",
                        **base,
                        "is_main_result": True,
                        "mime": {"text/plain": "42"},
                    }
                )
            else:
                emit(
                    {
                        "event": "error",
                        **base,
                        "name": "NameError",
                        "value": "name 'x' is not defined",
                        "traceback": ["NameError: name 'x' is not defined"],
                    }
                )
        elif code == "print(x)":
            emit({"event": "stdout", **base, "text": "42\n", "timestamp_unix_ns": time.time_ns()})
        elif code == "1/0":
            emit(
                {
                    "event": "error",
                    **base,
                    "name": "ZeroDivisionError",
                    "value": "division by zero",
                    "traceback": [
                        "Traceback (most recent call last)",
                        "Cell In[1], line 1",
                        "ZeroDivisionError: division by zero",
                    ],
                }
            )
        elif code == "plot":
            emit(
                {
                    "event": "result",
                    **base,
                    "is_main_result": False,
                    "mime": {"image/png": PNG_1X1, "e2b/chart": CHART},
                }
            )
        elif code == "df":
            emit(
                {
                    "event": "result",
                    **base,
                    "is_main_result": True,
                    "mime": {
                        "text/plain": "   a\n0  1\n1  2",
                        "text/html": "<table></table>",
                        "e2b/data": '{"a": [1, 2]}',
                    },
                }
            )
        elif code == "omit":
            emit(
                {
                    "event": "result",
                    **base,
                    "is_main_result": True,
                    "mime": {"text/plain": "big", "rayito/omitted": "image/png: 9000000 bytes"},
                }
            )
        elif code.startswith("sleep "):
            seconds = float(code.split()[1])
            if execution.interrupted.wait(seconds):
                if execution.cancelled is not None:
                    self._cancelled(execution)
                    return
                emit(
                    {
                        "event": "error",
                        **base,
                        "name": "KeyboardInterrupt",
                        "value": "",
                        "traceback": ["KeyboardInterrupt"],
                    }
                )
        elif code.startswith("hang "):
            seconds = float(code.split()[1])
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                if execution.cancelled is not None:
                    self._cancelled(execution)
                    return
                time.sleep(0.02)
        elif code.startswith("big "):
            chunks = int(code.split()[1])
            for _ in range(chunks):
                emit({"event": "stdout", **base, "text": CHUNK, "timestamp_unix_ns": time.time_ns()})
        elif code == "kernel-die":
            emit(
                {
                    "event": "error",
                    **base,
                    "name": "KernelDied",
                    "value": "kernel process exited (code 3)",
                    "traceback": [],
                }
            )
            self._end(execution, 0)
            emit(
                {
                    "event": "kernel_died",
                    "context_id": self.context_id,
                    "exit_code": 3,
                    "execution_id": execution.execution_id,
                }
            )
            self.kernel_pid += 1
            self.count = 0
            self.names.clear()
            return
        elif code.startswith("print "):
            emit(
                {
                    "event": "stdout",
                    **base,
                    "text": code[6:] + "\n",
                    "timestamp_unix_ns": time.time_ns(),
                }
            )
        else:
            emit(
                {
                    "event": "result",
                    **base,
                    "is_main_result": True,
                    "mime": {"text/plain": code},
                }
            )
        self._end(execution, count)

    def _cancelled(self, execution: Execution) -> None:
        emit(
            {
                "event": "error",
                **execution.base(),
                "name": execution.cancelled,
                "value": "cancelled",
                "traceback": [],
            }
        )
        self._end(execution, 0)


class Server:
    def __init__(self, default_context_id: str, default_cwd: str) -> None:
        self.contexts: dict[str, Context] = {}
        self.default_context_id = default_context_id
        self.default_cwd = default_cwd
        self.resume_lost_pending = True

    def lost_on_resume(self) -> set[str]:
        if ARGS is None or not ARGS.resume_lost or not self.resume_lost_pending:
            return set()
        self.resume_lost_pending = False
        lost: set[str] = set()
        for selector in ARGS.resume_lost:
            if selector == "created":
                lost.update(c for c in self.contexts if c != self.default_context_id)
            else:
                lost.add(selector)
        return lost

    def start(self) -> None:
        started = time.monotonic()
        if ARGS.warmup_ms:
            time.sleep(ARGS.warmup_ms / 1000)
        context = Context(self.default_context_id, self.default_cwd, {})
        self.contexts[context.context_id] = context
        ready = {
            "event": "ready",
            "v": 1,
            "default_context_id": context.context_id,
            "kernel_pid": context.kernel_pid,
            "warmup_ms": int((time.monotonic() - started) * 1000),
        }
        languages = announced_languages()
        if languages is not None:
            ready["languages"] = languages
        emit(ready)

    def handle(self, request: dict) -> None:
        log_request(request)
        request_id = request["id"]
        op = request.get("op")
        context = self.contexts.get(request.get("context_id", ""))
        if op == "ping":
            reply_ok(request_id, {"kernel_ready": True, "contexts": len(self.contexts)})
        elif op == "create_context":
            context_id = request["context_id"]
            if context_id in self.contexts:
                reply_error(request_id, "invalid_argument", "context already exists")
                return
            if request.get("cwd") == "/nonexistent":
                reply_error(request_id, "invalid_argument", "cwd does not exist")
                return
            language = request.get("language") or "python"
            if language not in KNOWN_LANGUAGES:
                reply_error(request_id, "invalid_argument", "unknown language")
                return
            if language != "python" and language not in (announced_languages() or []):
                reply_error(request_id, "invalid_argument", "language not installed")
                return
            if ARGS is not None and ARGS.create_delay_ms:
                time.sleep(ARGS.create_delay_ms / 1000)
            context = Context(
                context_id, request.get("cwd", ""), request.get("envs", {}), language
            )
            self.contexts[context_id] = context
            reply_ok(request_id, {"kernel_pid": context.kernel_pid})
        elif op == "execute":
            if context is None:
                reply_error(request_id, "not_found", "unknown context")
                return
            context.submit(Execution(request))
        elif op == "interrupt":
            if context is not None:
                context.interrupt(request.get("execution_id"))
            reply_ok(request_id, {})
        elif op == "destroy_context":
            if context is None:
                reply_error(request_id, "not_found", "unknown context")
                return
            context.destroy()
            self.contexts.pop(context.context_id, None)
            reply_ok(request_id, {})
        elif op == "restart_context":
            if context is None:
                reply_error(request_id, "not_found", "unknown context")
                return
            pid = context.restart(request.get("envs", {}))
            reply_ok(request_id, {"kernel_pid": pid})
        elif op == "list_contexts":
            reply_ok(
                request_id,
                {
                    "contexts": [
                        {
                            "context_id": c.context_id,
                            "language": c.language,
                            "cwd": c.cwd,
                            "kernel_pid": c.kernel_pid,
                            "state": c.state,
                        }
                        for c in self.contexts.values()
                    ]
                },
            )
        elif op == "reseed":
            if ARGS is not None and ARGS.reseed_delay_ms:
                time.sleep(ARGS.reseed_delay_ms / 1000)
            reply_ok(
                request_id,
                {
                    "reseeded": [c for c, ctx in self.contexts.items() if ctx.language == "python"],
                    "deferred": [],
                    "failed": [],
                    "skipped": [c for c, ctx in self.contexts.items() if ctx.language != "python"],
                },
            )
        elif op == "quiesce":
            reply_ok(request_id, {})
        elif op == "resume":
            if ARGS is not None and ARGS.resume_delay_ms:
                time.sleep(ARGS.resume_delay_ms / 1000)
            lost = self.lost_on_resume()
            reply_ok(
                request_id,
                {
                    "contexts": [
                        {"context_id": c, "alive": c not in lost} for c in self.contexts
                    ]
                },
            )
        else:
            reply_error(request_id, "invalid_argument", "unknown op")


def main() -> int:
    global ARGS
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket-root", default="/tmp/k")
    parser.add_argument("--sidecar-root", default=".")
    parser.add_argument("--default-cwd", default="/tmp")
    parser.add_argument("--default-context-id", default="default")
    parser.add_argument("--warmup-ms", type=int, default=0)
    parser.add_argument("--restart-ms", type=int, default=0)
    parser.add_argument("--log", default="")
    parser.add_argument("--noisy-stderr", action="store_true")
    parser.add_argument("--resume-lost", action="append", default=[])
    parser.add_argument("--resume-delay-ms", type=int, default=0)
    parser.add_argument("--create-delay-ms", type=int, default=0)
    parser.add_argument("--reseed-delay-ms", type=int, default=0)
    parser.add_argument("--languages", default="python")
    ARGS = parser.parse_args()
    if ARGS.noisy_stderr:
        sys.stderr.write("plain text noise: secret cell output must never reach rayd logs\n")
        sys.stderr.flush()
    sys.stderr.write(json.dumps({"level": "info", "msg": "fake sidecar starting", "warmup_ms": ARGS.warmup_ms, "code": "print(1)"}) + "\n")
    sys.stderr.flush()
    server = Server(ARGS.default_context_id, ARGS.default_cwd)
    server.start()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        threading.Thread(target=server.handle, args=(request,), daemon=True).start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
