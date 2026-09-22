"""Drive a local ``rayd`` through the six AWS lifecycle hooks in platform order.

Mirrors what Lambda does (AWS_API_NOTES.md §8): HTTP/1.1 POSTs to
``/aws/lambda-microvms/runtime/v1/<hook>`` on the hooks port, ``/ready`` and
``/validate`` retried while they answer 503 (the kernel warm-up and the
validation cell; the budget covers rayd's 300 s ready escape), only ``/run``
carries a body (``{"microvmId", "runHookPayload"}``), and every call is
bounded by the timeout declared for that hook in the image. The hooks
themselves need the standard library only.

When ``suspend`` and ``resume`` are selected and the Python client's gRPC
stubs are importable (``cd clients/python && uv run python
../../scripts/hooks-sim.py ...``), the M5 drill also opens a ``Start`` stream
on ``sleep 60`` and a PTY on the gRPC port before ``/suspend``, asserts both
end with ``suspending``, and after ``/resume`` asserts ``Health`` advanced
``resume_generation`` and that ``Connect(pid, from_seq)`` / ``Pty.Connect``
replay without a gap. Without the stubs the drill is skipped and only the
hook replies (``streams_closed``, ``kernel_state_lost``) are checked.

The M6 ``forged`` step (selected by default, right after ``run``) plays the
holder of an ``allPorts`` token: a second ``/run`` with another payload must
answer ``already_ran``, three ``/suspend`` in a row ``changed`` then
``unchanged`` twice, the ``/resume`` ``changed``, and a ``/suspend`` right
behind that pair ``changed`` again (``rayd`` never refuses a transition: a
refusal would also hit the genuine hook behind a forged one); the step ends
with a ``/resume`` so the real suspend/resume drill starts from ``resumed``.
With the stubs it also prints ``Health.hook_anomalies`` (1 here: the forged
run; nothing else is anomalous).

Usage: ``python scripts/hooks-sim.py [--base-url http://127.0.0.1:9000]
[--grpc-target 127.0.0.1:8080] [--only ready,run,forged] [--skip-terminate]``.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import secrets
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

HOOK_PATH_PREFIX = "/aws/lambda-microvms/runtime/v1"
HOOK_ORDER = ("ready", "validate", "run", "forged", "suspend", "resume", "terminate")
FORGED_STEP = "forged"
DECLARED_TIMEOUT_S = {
    "ready": 600,
    "validate": 600,
    "run": 30,
    "resume": 30,
    "suspend": 30,
    "terminate": 10,
}
READY_RETRY_INTERVAL_S = 1.0
READY_RETRY_BUDGET_S = 330.0
VALIDATE_RETRY_INTERVAL_S = 1.0
VALIDATE_RETRY_BUDGET_S = 120.0


@dataclass(frozen=True)
class HookResult:
    hook: str
    status: int
    elapsed_ms: int
    body: str
    retries: int = 0

    @property
    def ok(self) -> bool:
        return self.status == 200

    def reply(self) -> dict[str, Any]:
        try:
            parsed = json.loads(self.body)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}


GRPC_DRILL_TIMEOUT_S = 10.0
KERNEL_READY_BUDGET_S = 60.0
KERNEL_READY_INTERVAL_S = 0.5


@dataclass
class StreamDrill:
    """The M5 streams opened before ``/suspend`` and checked after ``/resume``."""

    target: str
    secret: bytes
    process_pid: int = 0
    process_last_seq: int = 0
    pty_pid: int = 0
    pty_last_seq: int = 0
    generation_before: int = 0
    failures: list[str] = field(default_factory=list)
    _channel: Any = None
    _process_stream: Any = None
    _pty_stream: Any = None

    def metadata(self) -> list[tuple[str, str]]:
        token = base64.urlsafe_b64encode(self.secret).rstrip(b"=").decode()
        return [("x-access-token", token)]

    def open(self) -> bool:
        try:
            import grpc
            from rayito.v1 import (
                health_pb2,
                health_pb2_grpc,
                process_pb2,
                process_pb2_grpc,
                pty_pb2,
                pty_pb2_grpc,
            )
        except ImportError as error:
            print(f"--  grpc drill skipped: {error}")
            return False
        self._channel = grpc.insecure_channel(self.target)
        health = self._wait_kernel_ready(health_pb2, health_pb2_grpc)
        self.generation_before = health.resume_generation
        processes = process_pb2_grpc.ProcessServiceStub(self._channel)
        self._process_stream = processes.Start(
            process_pb2.StartRequest(
                process=process_pb2.ProcessConfig(
                    cmd="/bin/sh", args=["-c", "sleep 60"]
                )
            ),
            metadata=self.metadata(),
        )
        first = next(self._process_stream)
        self.process_pid = first.start.pid
        ptys = pty_pb2_grpc.PtyServiceStub(self._channel)
        self._pty_stream = ptys.Create(pty_pb2.PtyStart(), metadata=self.metadata())
        self.pty_pid = next(self._pty_stream).started.pid
        ptys.SendInput(
            process_pb2.SendInputRequest(pid=self.pty_pid, data=b"echo h''ola\n"),
            metadata=self.metadata(),
            timeout=GRPC_DRILL_TIMEOUT_S,
        )
        deadline = time.monotonic() + GRPC_DRILL_TIMEOUT_S
        buffer = b""
        while b"hola" not in buffer and time.monotonic() < deadline:
            message = next(self._pty_stream)
            if message.WhichOneof("message") == "data":
                buffer += message.data
                self.pty_last_seq = message.seq
        if b"hola" not in buffer:
            self.failures.append("pty never echoed hola before the suspend")
        print(
            f"--  grpc drill: process pid {self.process_pid}, pty pid {self.pty_pid}, "
            f"resume_generation {self.generation_before}"
        )
        return True

    def _wait_kernel_ready(self, health_pb2: Any, health_pb2_grpc: Any) -> Any:
        """``/run`` rotates the default kernel in the background; the SDK's
        ``create()`` only returns once ``kernel_ready`` is true, so the drill
        waits the same way before opening streams and suspending (a probe
        during that rotation would report the kernel as lost)."""
        stub = health_pb2_grpc.HealthServiceStub(self._channel)
        deadline = time.monotonic() + KERNEL_READY_BUDGET_S
        while True:
            health = stub.Health(
                health_pb2.HealthRequest(), timeout=GRPC_DRILL_TIMEOUT_S
            )
            if health.kernel_ready or time.monotonic() >= deadline:
                if not health.kernel_ready:
                    self.failures.append("kernel_ready never became true after /run")
                return health
            time.sleep(KERNEL_READY_INTERVAL_S)

    def expect_suspending_ends(self) -> None:
        end = self._drain_process_end()
        if end is None or end.status != "suspending":
            self.failures.append(f"process stream did not end with suspending: {end}")
        exited = self._drain_pty_exit()
        if exited is None or exited.status != "suspending":
            self.failures.append(f"pty stream did not end with suspending: {exited}")
        print("--  grpc drill: both streams ended with status suspending")

    def _drain_process_end(self) -> Any:
        for event in self._process_stream:
            kind = event.WhichOneof("event")
            if kind == "data":
                self.process_last_seq = event.data.seq
            elif kind == "end":
                return event.end
        return None

    def _drain_pty_exit(self) -> Any:
        for message in self._pty_stream:
            kind = message.WhichOneof("message")
            if kind == "data":
                self.pty_last_seq = message.seq
            elif kind == "exited":
                return message.exited
        return None

    def expect_resumed(self) -> None:
        from rayito.v1 import (
            health_pb2,
            health_pb2_grpc,
            process_pb2,
            process_pb2_grpc,
            pty_pb2,
            pty_pb2_grpc,
        )

        health = health_pb2_grpc.HealthServiceStub(self._channel).Health(
            health_pb2.HealthRequest(), timeout=GRPC_DRILL_TIMEOUT_S
        )
        if health.resume_generation != self.generation_before + 1:
            self.failures.append(
                f"resume_generation {health.resume_generation}, expected "
                f"{self.generation_before + 1}"
            )
        print(
            f"--  grpc drill: resume_generation {health.resume_generation}, "
            f"clock_offset_ms {health.clock_offset_ms}, "
            f"kernel_state_lost {health.kernel_state_lost}"
        )
        processes = process_pb2_grpc.ProcessServiceStub(self._channel)
        reconnected = processes.Connect(
            process_pb2.ConnectRequest(
                pid=self.process_pid, from_seq=self.process_last_seq + 1
            ),
            metadata=self.metadata(),
        )
        if next(reconnected).start.pid != self.process_pid:
            self.failures.append("Connect(from_seq) did not replay the process")
        reconnected.cancel()
        ptys = pty_pb2_grpc.PtyServiceStub(self._channel)
        pty = ptys.Connect(
            process_pb2.ConnectRequest(
                pid=self.pty_pid, from_seq=self.pty_last_seq + 1
            ),
            metadata=self.metadata(),
        )
        if next(pty).started.pid != self.pty_pid:
            self.failures.append("Pty.Connect did not replay the terminal")
        ptys.SendInput(
            process_pb2.SendInputRequest(pid=self.pty_pid, data=b"echo b''ack\n"),
            metadata=self.metadata(),
            timeout=GRPC_DRILL_TIMEOUT_S,
        )
        buffer = b""
        deadline = time.monotonic() + GRPC_DRILL_TIMEOUT_S
        while b"back" not in buffer and time.monotonic() < deadline:
            message = next(pty)
            if message.WhichOneof("message") == "data":
                buffer += message.data
        if b"back" not in buffer:
            self.failures.append("the re-attached pty did not answer after the resume")
        pty.cancel()
        print(
            "--  grpc drill: Connect(from_seq) and Pty.Connect replayed after the resume"
        )
        processes.SendSignal(
            process_pb2.SendSignalRequest(pid=self.process_pid, signal=9),
            metadata=self.metadata(),
            timeout=GRPC_DRILL_TIMEOUT_S,
        )
        ptys.Kill(
            pty_pb2.KillPtyRequest(pid=self.pty_pid),
            metadata=self.metadata(),
            timeout=GRPC_DRILL_TIMEOUT_S,
        )


def run_hook_payload(secret: bytes, user: str, workdir: str) -> str:
    return json.dumps(
        {
            "v": 1,
            "token_sha256": hashlib.sha256(secret).hexdigest(),
            "envs": {},
            "user": user,
            "workdir": workdir,
        }
    )


def run_envelope(microvm_id: str, payload: str) -> bytes:
    return json.dumps({"microvmId": microvm_id, "runHookPayload": payload}).encode()


def post(base_url: str, hook: str, body: bytes | None) -> HookResult:
    url = f"{base_url}{HOOK_PATH_PREFIX}/{hook}"
    headers = {"content-type": "application/json"} if body is not None else {}
    request = urllib.request.Request(
        url, data=body or b"", headers=headers, method="POST"
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(
            request, timeout=DECLARED_TIMEOUT_S[hook]
        ) as response:
            status, text = response.status, response.read().decode(errors="replace")
    except urllib.error.HTTPError as error:
        status, text = error.code, error.read().decode(errors="replace")
    elapsed_ms = int((time.monotonic() - started) * 1000)
    return HookResult(hook, status, elapsed_ms, text)


def post_with_retry(
    base_url: str, hook: str, budget_s: float, interval_s: float
) -> HookResult:
    started = time.monotonic()
    deadline = started + budget_s
    retries = 0
    while True:
        result = post(base_url, hook, None)
        if result.status != 503 or time.monotonic() >= deadline:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            return HookResult(hook, result.status, elapsed_ms, result.body, retries)
        retries += 1
        time.sleep(interval_s)


def post_ready_with_retry(base_url: str) -> HookResult:
    return post_with_retry(
        base_url, "ready", READY_RETRY_BUDGET_S, READY_RETRY_INTERVAL_S
    )


def post_validate_with_retry(base_url: str) -> HookResult:
    return post_with_retry(
        base_url, "validate", VALIDATE_RETRY_BUDGET_S, VALIDATE_RETRY_INTERVAL_S
    )


def expect(result: HookResult, outcome: str, label: str) -> int:
    """One line per forged call; a mismatch counts as a failure."""
    actual = result.reply().get("outcome")
    marker = "ok " if result.ok and actual == outcome else "ERR"
    print(
        f"{marker} forged:{label:<14} {result.status} {result.elapsed_ms:>5} ms  "
        f"outcome={actual} (expected {outcome})"
    )
    return 0 if marker == "ok " else 1


def read_hook_anomalies(grpc_target: str) -> int | None:
    try:
        import grpc
        from rayito.v1 import health_pb2, health_pb2_grpc
    except ImportError:
        return None
    with grpc.insecure_channel(grpc_target) as channel:
        health = health_pb2_grpc.HealthServiceStub(channel).Health(
            health_pb2.HealthRequest(), timeout=GRPC_DRILL_TIMEOUT_S
        )
    return int(health.hook_anomalies)


def wait_kernel_ready_if_possible(grpc_target: str) -> None:
    """A forged `/suspend` inside the `/run` rotation window would make the
    probe report the restarting kernel as lost (M5 note); with the stubs the
    drill waits like the SDK's `create()` does."""
    try:
        import grpc
        from rayito.v1 import health_pb2, health_pb2_grpc
    except ImportError:
        return
    deadline = time.monotonic() + KERNEL_READY_BUDGET_S
    with grpc.insecure_channel(grpc_target) as channel:
        stub = health_pb2_grpc.HealthServiceStub(channel)
        while time.monotonic() < deadline:
            health = stub.Health(
                health_pb2.HealthRequest(), timeout=GRPC_DRILL_TIMEOUT_S
            )
            if health.kernel_ready:
                return
            time.sleep(KERNEL_READY_INTERVAL_S)


def forged_drill(args: argparse.Namespace) -> int:
    """What a holder of an ``allPorts`` token can do after the accepted
    ``/run`` (design D1): nothing that changes the token, cut the streams as
    often as it likes (every transition is accepted, none is refused), and
    the repeated ``/run`` visible in ``hook_anomalies``."""
    failures = 0
    wait_kernel_ready_if_possible(args.grpc_target)
    other_payload = run_hook_payload(secrets.token_bytes(32), args.user, args.workdir)
    failures += expect(
        post(args.base_url, "run", run_envelope("mvm-forged", other_payload)),
        "already_ran",
        "run again",
    )
    for index, outcome in enumerate(("changed", "unchanged", "unchanged")):
        failures += expect(
            post(args.base_url, "suspend", None), outcome, f"suspend #{index + 1}"
        )
    failures += expect(post(args.base_url, "resume", None), "changed", "resume")
    failures += expect(
        post(args.base_url, "suspend", None), "changed", "suspend after pair"
    )
    failures += expect(post(args.base_url, "resume", None), "changed", "resume #2")
    anomalies = read_hook_anomalies(args.grpc_target)
    if anomalies is None:
        print("--  forged: Health.hook_anomalies not read (grpc stubs unavailable)")
    else:
        marker = "ok " if anomalies == 1 else "ERR"
        print(f"{marker} forged: Health.hook_anomalies={anomalies} (expected 1)")
        failures += 0 if anomalies == 1 else 1
    return failures


def report(result: HookResult) -> None:
    marker = "ok " if result.ok else "ERR"
    retried = f" after {result.retries} x 503" if result.retries else ""
    print(
        f"{marker} {result.hook:<9} {result.status} {result.elapsed_ms:>5} ms{retried}  {result.body.strip()}"
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:9000")
    parser.add_argument("--microvm-id", default="mvm-local-hooks-sim")
    parser.add_argument(
        "--only", default="", help="comma-separated subset of hooks, in platform order"
    )
    parser.add_argument("--skip-terminate", action="store_true")
    parser.add_argument("--user", default="user", help="`user` of the /run payload")
    parser.add_argument(
        "--workdir",
        default="/home/user",
        help="`workdir` of the /run payload (a directory the sandbox user can enter)",
    )
    parser.add_argument(
        "--grpc-target",
        default="127.0.0.1:8080",
        help="gRPC port of the same rayd, for the M5 stream drill around suspend/resume",
    )
    return parser.parse_args(argv)


def selected_hooks(args: argparse.Namespace) -> tuple[str, ...]:
    hooks = tuple(h for h in HOOK_ORDER if not args.only or h in args.only.split(","))
    if args.skip_terminate:
        hooks = tuple(h for h in hooks if h != "terminate")
    return hooks


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    secret = secrets.token_bytes(32)
    payload = run_hook_payload(secret, args.user, args.workdir)
    print(
        f"x-access-token for this boot: {base64.urlsafe_b64encode(secret).rstrip(b'=').decode()}"
    )
    failures = 0
    hooks = selected_hooks(args)
    drill = StreamDrill(args.grpc_target, secret) if "suspend" in hooks else None
    for hook in hooks:
        if hook == FORGED_STEP:
            failures += forged_drill(args)
            continue
        if hook == "suspend" and drill is not None and not drill.open():
            drill = None
        if hook == "ready":
            result = post_ready_with_retry(args.base_url)
        elif hook == "validate":
            result = post_validate_with_retry(args.base_url)
        elif hook == "run":
            result = post(args.base_url, hook, run_envelope(args.microvm_id, payload))
        else:
            result = post(args.base_url, hook, None)
        report(result)
        failures += 0 if result.ok else 1
        failures += check_suspend_resume_reply(hook, result)
        if drill is not None and hook == "suspend":
            drill.expect_suspending_ends()
        if drill is not None and hook == "resume":
            drill.expect_resumed()
    if drill is not None:
        for failure in drill.failures:
            print(f"ERR grpc drill: {failure}")
        failures += len(drill.failures)
    return 1 if failures else 0


def check_suspend_resume_reply(hook: str, result: HookResult) -> int:
    """The M5 hook bodies: ``streams_closed`` on suspend, ``kernel_state_lost``
    on resume (both only when the transition happened)."""
    reply = result.reply()
    if hook == "suspend" and reply.get("outcome") in ("changed", "unchanged"):
        if not isinstance(reply.get("streams_closed"), int):
            print("ERR suspend reply lacks streams_closed")
            return 1
        print(f"--  suspend: streams_closed={reply['streams_closed']}")
    if hook == "resume" and reply.get("outcome") in ("changed", "unchanged"):
        if not isinstance(reply.get("kernel_state_lost"), bool):
            print("ERR resume reply lacks kernel_state_lost")
            return 1
        print(f"--  resume: kernel_state_lost={reply['kernel_state_lost']}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except urllib.error.URLError as error:
        print(f"ERR cannot reach rayd: {error.reason}", file=sys.stderr)
        sys.exit(2)
