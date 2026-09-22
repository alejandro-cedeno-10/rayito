"""Local end-to-end test of the M0 probe without AWS.

Starts image/probe.py on two free ports, drives the lifecycle hooks the way
Lambda would (POST on /aws/lambda-microvms/runtime/v1/<hook>) and exercises the
measurement endpoints. Platform-specific endpoints (IMDS, capabilities) only
need to answer with a JSON body, not succeed.

Run: uv run --with jupyter_client --with ipykernel python spike/m0/test_probe_local.py
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROBE = HERE / "image" / "probe.py"
HOOK_PREFIX = "/aws/lambda-microvms/runtime/v1/"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def http(method: str, url: str, body: bytes | None = None, timeout: float = 60) -> tuple[int, dict]:
    request = urllib.request.Request(url, data=body, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def wait_for(url: str, seconds: float = 30) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            status, _ = http("GET", url, timeout=2)
            if status == 200:
                return
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(0.2)
    raise SystemExit(f"probe did not come up at {url}")


def check(name: str, condition: bool, detail: object = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name} {detail if not condition else ''}".rstrip())
    if not condition:
        FAILURES.append(name)


FAILURES: list[str] = []


def main() -> None:
    app_port, hook_port = free_port(), free_port()
    log_dir = Path(tempfile.mkdtemp(prefix="rayito-m0-"))
    env = {**os.environ, "APP_PORT": str(app_port), "HOOK_PORT": str(hook_port), "LOG_DIR": str(log_dir), "KERNEL_AUTOSTART": "1", "KERNEL_TRANSPORT": "tcp"}
    proc = subprocess.Popen([sys.executable, str(PROBE)], env=env, stderr=subprocess.PIPE, text=True)
    app = f"http://127.0.0.1:{app_port}"
    hooks = f"http://127.0.0.1:{hook_port}{HOOK_PREFIX}"
    try:
        wait_for(f"{app}/health")
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            status, ready = http("POST", f"{hooks}ready")
            if status == 200:
                break
            time.sleep(0.5)
        check("hook /ready returns 200 once the kernel autostart finished", status == 200, ready)

        status, _ = http("GET", f"{hooks}ready")
        check("hooks reject GET (Lambda uses POST)", status == 405)

        payload = json.dumps({"v": 1, "token_sha256": "a" * 64})
        status, body = http("POST", f"{hooks}run", json.dumps({"microvmId": "mvm-local-test", "runHookPayload": payload}).encode())
        check("hook /run accepts the AWS body shape", status == 200 and body.get("ok") is True, body)
        _, received = http("GET", f"{app}/run-payload")
        check("run payload captured (id + length + sha256)", received.get("microvmId") == "mvm-local-test" and received.get("payload_len") == len(payload), received)

        status, body = http("POST", f"{hooks}validate")
        check("hook /validate executes a kernel cell", status == 200 and body.get("kernel", {}).get("results") == ["45"], body)

        _, ident = http("GET", f"{app}/id")
        check("/id reports platform facts", "tools" in ident and "cap_eff" in ident)
        _, creds = http("GET", f"{app}/creds")
        check("/creds answers even without IMDS", "env" in creds, creds)

        _, started = http("GET", f"{app}/bg/start")
        check("background ticker started", "pid" in started, started)
        _, slept = http("GET", f"{app}/sleep/start?seconds=2")
        check("sleep child started", "pid" in slept, slept)

        _, info = http("GET", f"{app}/kernel/info")
        check("kernel autostarted at boot", info.get("alive") is True, info)
        _, execd = http("GET", f"{app}/kernel/exec?code=x")
        check("kernel keeps state (x == 42)", execd.get("results") == ["42"], execd)
        _, fresh = http("GET", f"{app}/kernel/fresh?code=x")
        check("fresh client from connection file sees the same kernel", fresh.get("results") == ["42"], fresh)
        _, alive = http("GET", f"{app}/kernel/alive")
        check("kernel_info round trip", alive.get("alive") is True, alive)

        http("GET", f"{app}/tcp/loopback/start")
        _, echoed = http("GET", f"{app}/tcp/loopback/send")
        check("loopback TCP echo", echoed.get("ok") is True, echoed)
        http("GET", f"{app}/unix/start")
        _, unix_echo = http("GET", f"{app}/unix/send")
        check("socketpair echo", unix_echo.get("ok") is True, unix_echo)

        _, listener = http("GET", f"{app}/listen127/start?port={free_port()}")
        check("127.0.0.1 listener started", listener.get("bound") == "127.0.0.1", listener)

        http("POST", f"{hooks}suspend")
        time.sleep(1.2)
        status, resumed = http("POST", f"{hooks}resume")
        check("hook /resume reports clock delta", status == 200 and (resumed.get("delta") or {}).get("wall", 0) >= 1.0, resumed)

        time.sleep(1.5)
        _, bg = http("GET", f"{app}/bg/status")
        check("ticker alive with pipe and file lines", bg.get("alive") is True and bg.get("pipe_lines", 0) >= 1 and bg.get("file_lines", 0) >= 1, bg)
        _, sleep_status = http("GET", f"{app}/sleep/status")
        check("sleep child finished with monotonic accounting", sleep_status.get("finished") is True, sleep_status)

        _, hook_log = http("GET", f"{app}/hooks")
        seen = [h["hook"] for h in hook_log]
        check("hook log records every hook in order", seen[:5] == ["ready", "run", "validate", "suspend", "resume"] or set(seen) >= {"ready", "run", "validate", "suspend", "resume"}, seen)
        check("hook records carry client ip and http version", all("client" in h and "http_version" in h for h in hook_log))

        status, _ = http("POST", f"{hooks}terminate")
        check("hook /terminate", status == 200)

        raw = urllib.request.urlopen(f"{app}/big?bytes=1048576", timeout=30).read()
        check("bandwidth endpoint streams the requested size", len(raw) == 1048576)

        _, stopped = http("GET", f"{app}/kernel/stop")
        check("kernel stop", stopped.get("stopped") is True, stopped)
    finally:
        proc.terminate()
        try:
            _, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, stderr = proc.communicate()
        events = [json.loads(line)["event"] for line in stderr.splitlines() if line.startswith("{")]
        print("probe log events:", events)
    if FAILURES:
        raise SystemExit(f"{len(FAILURES)} check(s) failed: {FAILURES}")
    print("ALL PASS")


if __name__ == "__main__":
    main()
