"""M0 probe for Lambda MicroVMs.

Single-file, stdlib-only HTTP service that answers the lifecycle hooks on one
port and exposes measurement endpoints on the application port. Every endpoint
returns JSON. Nothing here is production code: it exists to answer the open
questions in AWS_API_NOTES.md section 16 against a real account.

Runs on Linux inside the MicroVM and, degraded, on Windows for local testing.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import random
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HOOK_PREFIX = "/aws/lambda-microvms/runtime/v1/"
HOOK_PORT = int(os.environ.get("HOOK_PORT", "9000"))
APP_PORT = int(os.environ.get("APP_PORT", "8080"))
LOG_DIR = Path(os.environ.get("LOG_DIR", "/var/log/probe" if os.name != "nt" else Path.home() / "probe-log"))
KERNEL_TRANSPORT = os.environ.get("KERNEL_TRANSPORT", "ipc" if os.name != "nt" else "tcp")
KERNEL_AUTOSTART = os.environ.get("KERNEL_AUTOSTART", "1") == "1"
FAIL_SUSPEND = os.environ.get("PROBE_FAIL_SUSPEND", "0") == "1"
IMDS = "http://169.254.169.254"
RANDOM_AT_IMPORT = random.random()
BOOT_WALL = time.time()
BOOT_MONO = time.monotonic()

state: dict[str, Any] = {
    "hooks": [],
    "run": None,
    "suspend_at": None,
    "resume_at": None,
    "bg": None,
    "sleep": None,
    "kernel": None,
    "loopback": None,
    "unix": None,
    "outbound": None,
    "listen127": None,
    "ready": threading.Event(),
}
lock = threading.Lock()


def log(event: str, **fields: Any) -> None:
    record = {"ts": time.time(), "event": event, **fields}
    sys.stderr.write(json.dumps(record) + "\n")
    sys.stderr.flush()


def now() -> dict[str, float]:
    return {"wall": time.time(), "mono": time.monotonic()}


def append_jsonl(name: str, record: dict[str, Any]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_DIR / name, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def read_tail(name: str, n: int = 5) -> list[str]:
    path = LOG_DIR / name
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return lines[-n:]


class JsonHandler(BaseHTTPRequestHandler):
    server_version = "rayito-m0-probe/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def query(self) -> dict[str, str]:
        parsed = urllib.parse.urlparse(self.path)
        return {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}

    def route(self) -> str:
        return urllib.parse.urlparse(self.path).path


class HookHandler(JsonHandler):
    def do_GET(self) -> None:
        self.send_json({"error": "hooks are POST only", "path": self.route()}, 405)

    def do_POST(self) -> None:
        path = self.route()
        if not path.startswith(HOOK_PREFIX):
            self.send_json({"error": "unknown hook path", "path": path}, 404)
            return
        hook = path[len(HOOK_PREFIX):]
        body = self.read_body()
        record = {
            "hook": hook,
            **now(),
            "client": self.client_address[0],
            "http_version": self.request_version,
            "headers": {k: v for k, v in self.headers.items() if k.lower() != "authorization"},
            "body_len": len(body),
            "body_sha256": hashlib.sha256(body).hexdigest(),
        }
        with lock:
            state["hooks"].append(record)
        append_jsonl("hooks.jsonl", record)
        log("hook", hook=hook, client=record["client"], body_len=len(body))
        handler = getattr(self, f"hook_{hook}", None)
        if handler is None:
            self.send_json({"error": "unsupported hook", "hook": hook}, 404)
            return
        handler(body)

    def hook_ready(self, body: bytes) -> None:
        if state["ready"].is_set():
            self.send_json({"ready": True})
        else:
            self.send_json({"ready": False}, 503)

    def hook_validate(self, body: bytes) -> None:
        result = kernel_exec("sum(range(10))") if state["kernel"] else {"skipped": True}
        self.send_json({"validated": True, "kernel": result})

    def hook_run(self, body: bytes) -> None:
        try:
            parsed = json.loads(body or b"{}")
        except json.JSONDecodeError:
            parsed = {"_raw_len": len(body)}
        payload = parsed.get("runHookPayload") or ""
        with lock:
            state["run"] = {
                "microvmId": parsed.get("microvmId"),
                "payload_len": len(payload),
                "payload_sha256": hashlib.sha256(payload.encode()).hexdigest(),
                "payload_head": payload[:64],
                "received_at": now(),
                "keys": sorted(parsed.keys()),
            }
        random.seed()
        self.send_json({"ok": True})

    def hook_suspend(self, body: bytes) -> None:
        with lock:
            state["suspend_at"] = now()
        if FAIL_SUSPEND:
            self.send_json({"ok": False, "forced_failure": True}, 500)
            return
        self.send_json({"ok": True})

    def hook_resume(self, body: bytes) -> None:
        with lock:
            state["resume_at"] = now()
        random.seed()
        self.send_json({"ok": True, "delta": clock_delta()})

    def hook_terminate(self, body: bytes) -> None:
        self.send_json({"ok": True})


def clock_delta() -> dict[str, float] | None:
    s, r = state["suspend_at"], state["resume_at"]
    if not s or not r:
        return None
    return {"wall": r["wall"] - s["wall"], "mono": r["mono"] - s["mono"]}


def proc_status_caps() -> dict[str, str]:
    caps: dict[str, str] = {}
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("Cap"):
                key, _, value = line.partition(":")
                caps[key] = value.strip()
    except OSError:
        pass
    return caps


def decode_caps(hexmask: str) -> list[str]:
    names = [
        "chown", "dac_override", "dac_read_search", "fowner", "fsetid", "kill", "setgid", "setuid",
        "setpcap", "linux_immutable", "net_bind_service", "net_broadcast", "net_admin", "net_raw",
        "ipc_lock", "ipc_owner", "sys_module", "sys_rawio", "sys_chroot", "sys_ptrace", "sys_pacct",
        "sys_admin", "sys_boot", "sys_nice", "sys_resource", "sys_time", "sys_tty_config", "mknod",
        "lease", "audit_write", "audit_control", "setfcap", "mac_override", "mac_admin", "syslog",
        "wake_alarm", "block_suspend", "audit_read", "perfmon", "bpf", "checkpoint_restore",
    ]
    try:
        mask = int(hexmask, 16)
    except ValueError:
        return []
    return [name for bit, name in enumerate(names) if mask & (1 << bit)]


def ulimits() -> dict[str, Any]:
    try:
        import resource
    except ImportError:
        return {"unavailable": "no resource module"}
    out = {}
    for name in ("RLIMIT_NPROC", "RLIMIT_NOFILE", "RLIMIT_AS", "RLIMIT_CORE", "RLIMIT_FSIZE"):
        limit = getattr(resource, name, None)
        if limit is not None:
            out[name] = resource.getrlimit(limit)
    return out


def mounts() -> list[str]:
    try:
        lines = Path("/proc/mounts").read_text().splitlines()
    except OSError:
        return []
    return [line for line in lines if any(k in line for k in ("cgroup", " / ", "/tmp", "/dev "))]


def disk_free(path: str = "/") -> dict[str, int] | None:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    return {"total": usage.total, "used": usage.used, "free": usage.free}


def identity() -> dict[str, Any]:
    caps = proc_status_caps()
    return {
        "platform": platform.platform(),
        "python": sys.version,
        "uid": getattr(os, "getuid", lambda: None)(),
        "euid": getattr(os, "geteuid", lambda: None)(),
        "gid": getattr(os, "getgid", lambda: None)(),
        "caps_raw": caps,
        "cap_eff": decode_caps(caps.get("CapEff", "")),
        "cap_bnd": decode_caps(caps.get("CapBnd", "")),
        "ulimits": ulimits(),
        "mounts": mounts(),
        "cgroup_writable": os.access("/sys/fs/cgroup", os.W_OK),
        "disk_root": disk_free("/"),
        "tools": {t: shutil.which(t) for t in ("setpriv", "runuser", "su", "sudo", "iptables", "nft", "useradd", "curl", "tar", "ip")},
        "dockerenv": Path("/.dockerenv").exists(),
        "pid1_cgroup": (Path("/proc/1/cgroup").read_text().strip() if Path("/proc/1/cgroup").exists() else None),
        "iptables_list": run_capture(["iptables", "-L", "-n"]) if shutil.which("iptables") else None,
    }


def run_capture(cmd: list[str], timeout: float = 5) -> dict[str, Any]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {"rc": proc.returncode, "stdout": proc.stdout[-2000:], "stderr": proc.stderr[-500:]}


def imds_probe() -> dict[str, Any]:
    result: dict[str, Any] = {
        "env": {k: (k in os.environ) for k in ("AWS_ACCESS_KEY_ID", "AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_REGION")},
        "as": {"uid": getattr(os, "getuid", lambda: None)()},
    }
    try:
        req = urllib.request.Request(f"{IMDS}/latest/api/token", method="PUT", headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"})
        with urllib.request.urlopen(req, timeout=2) as resp:
            token = resp.read().decode()
        result["token_ok"] = True
        headers = {"X-aws-ec2-metadata-token": token}
        with urllib.request.urlopen(urllib.request.Request(f"{IMDS}/latest/meta-data/iam/security-credentials/", headers=headers), timeout=2) as resp:
            roles = resp.read().decode().split()
        result["roles"] = roles
        if roles:
            with urllib.request.urlopen(urllib.request.Request(f"{IMDS}/latest/meta-data/iam/security-credentials/{roles[0]}", headers=headers), timeout=2) as resp:
                creds = json.loads(resp.read())
            result["creds"] = {
                "Code": creds.get("Code"),
                "AccessKeyId_tail": (creds.get("AccessKeyId") or "")[-4:],
                "Expiration": creds.get("Expiration"),
                "LastUpdated": creds.get("LastUpdated"),
                "token_len": len(creds.get("Token") or ""),
            }
    except Exception as exc:  # noqa: BLE001 - probe reports every failure kind
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def imds_probe_as_uid(uid: int) -> dict[str, Any]:
    if os.name == "nt" or not hasattr(os, "setuid"):
        return {"error": "setuid unavailable on this platform"}

    def drop() -> None:
        os.setgid(uid)
        os.setuid(uid)

    try:
        proc = subprocess.run([sys.executable, __file__, "--imds-probe"], capture_output=True, text=True, timeout=10, preexec_fn=drop)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"error": "child produced no JSON", "stderr": proc.stderr[-500:], "stdout": proc.stdout[-500:]}


def bg_start() -> dict[str, Any]:
    if state["bg"]:
        return {"already": True, "pid": state["bg"]["proc"].pid}
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    script = (
        "import json,time,sys\n"
        f"p={str(LOG_DIR / 'bg.log')!r}\n"
        "n=0\n"
        "while True:\n"
        "    rec={'n':n,'wall':time.time(),'mono':time.monotonic()}\n"
        "    open(p,'a').write(json.dumps(rec)+'\\n')\n"
        "    print(json.dumps(rec),flush=True)\n"
        "    n+=1; time.sleep(1)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    entry = {"proc": proc, "pipe_lines": [], "started": now()}

    def reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            entry["pipe_lines"].append(line.strip())

    threading.Thread(target=reader, daemon=True).start()
    state["bg"] = entry
    return {"pid": proc.pid}


def bg_status() -> dict[str, Any]:
    entry = state["bg"]
    if not entry:
        return {"started": False}
    lines = entry["pipe_lines"]
    file_tail = read_tail("bg.log", 5)
    gaps = {"wall": 0.0, "mono": 0.0}
    parsed = [json.loads(line) for line in read_tail("bg.log", 100000) if line]
    for a, b in zip(parsed, parsed[1:]):
        gaps["wall"] = max(gaps["wall"], b["wall"] - a["wall"])
        gaps["mono"] = max(gaps["mono"], b["mono"] - a["mono"])
    return {
        "pid": entry["proc"].pid,
        "alive": entry["proc"].poll() is None,
        "pipe_lines": len(lines),
        "last_pipe_line": lines[-1] if lines else None,
        "file_lines": len(parsed),
        "file_tail": file_tail,
        "max_gap_between_ticks": gaps,
    }


def sleep_start(seconds: int) -> dict[str, Any]:
    proc = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])
    state["sleep"] = {"proc": proc, "seconds": seconds, "started": now()}
    return {"pid": proc.pid, "seconds": seconds}


def sleep_status() -> dict[str, Any]:
    entry = state["sleep"]
    if not entry:
        return {"started": False}
    current = now()
    return {
        "finished": entry["proc"].poll() is not None,
        "requested_seconds": entry["seconds"],
        "elapsed_wall": current["wall"] - entry["started"]["wall"],
        "elapsed_mono": current["mono"] - entry["started"]["mono"],
    }


def kernel_start(transport: str) -> dict[str, Any]:
    if state["kernel"]:
        return {"already": True, **kernel_info()}
    try:
        from jupyter_client import KernelManager
    except ImportError as exc:
        return {"error": f"jupyter_client missing: {exc}"}
    kwargs: dict[str, Any] = {"kernel_name": "python3", "transport": transport}
    if transport == "ipc":
        ipc_dir = LOG_DIR / "k"
        ipc_dir.mkdir(parents=True, exist_ok=True)
        kwargs["ip"] = str(ipc_dir / "kernel")
    km = KernelManager(**kwargs)
    started = now()
    km.start_kernel()
    kc = km.client()
    kc.start_channels()
    kc.wait_for_ready(timeout=60)
    state["kernel"] = {"km": km, "kc": kc, "transport": transport, "started": started}
    seed = kernel_exec("x = 42\nimport random\nrandom.random()")
    return {"ok": True, "startup_s": time.monotonic() - started["mono"], "seed_exec": seed, **kernel_info()}


def kernel_info() -> dict[str, Any]:
    entry = state["kernel"]
    if not entry:
        return {"started": False}
    km = entry["km"]
    return {
        "transport": entry["transport"],
        "connection_file": km.connection_file,
        "pid": getattr(getattr(km, "provisioner", None), "pid", None) or getattr(getattr(km, "kernel", None), "pid", None),
        "alive": km.is_alive(),
    }


def collect_execution(kc: Any, code: str, timeout: float = 30) -> dict[str, Any]:
    msg_id = kc.execute(code)
    out: dict[str, Any] = {"stdout": "", "stderr": "", "results": [], "error": None, "status": None}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            msg = kc.get_iopub_msg(timeout=max(0.1, deadline - time.monotonic()))
        except Exception:  # noqa: BLE001 - queue.Empty or zmq error, both mean timeout
            out["status"] = "timeout"
            break
        if msg.get("parent_header", {}).get("msg_id") != msg_id:
            continue
        kind, content = msg["msg_type"], msg["content"]
        if kind == "stream":
            out[content["name"]] += content["text"]
        elif kind in ("execute_result", "display_data"):
            out["results"].append(content["data"].get("text/plain"))
        elif kind == "error":
            out["error"] = {"name": content["ename"], "value": content["evalue"]}
        elif kind == "status" and content["execution_state"] == "idle":
            out["status"] = "idle"
            break
    return out


def kernel_exec(code: str, fresh: bool = False) -> dict[str, Any]:
    entry = state["kernel"]
    if not entry:
        return {"error": "kernel not started"}
    if fresh:
        from jupyter_client import BlockingKernelClient
        kc = BlockingKernelClient(connection_file=entry["km"].connection_file)
        kc.load_connection_file()
        kc.start_channels()
        try:
            kc.wait_for_ready(timeout=10)
            return {"fresh": True, **collect_execution(kc, code)}
        finally:
            kc.stop_channels()
    started = time.monotonic()
    result = collect_execution(entry["kc"], code)
    result["elapsed_s"] = time.monotonic() - started
    return result


def kernel_alive() -> dict[str, Any]:
    entry = state["kernel"]
    if not entry:
        return {"error": "kernel not started"}
    started = time.monotonic()
    try:
        entry["kc"].kernel_info(reply=True, timeout=5)
        return {"alive": True, "kernel_info_rtt_s": time.monotonic() - started, "is_alive": entry["km"].is_alive()}
    except Exception as exc:  # noqa: BLE001
        return {"alive": False, "error": f"{type(exc).__name__}: {exc}", "is_alive": entry["km"].is_alive()}


def kernel_stop() -> dict[str, Any]:
    entry = state["kernel"]
    if not entry:
        return {"started": False}
    entry["kc"].stop_channels()
    entry["km"].shutdown_kernel(now=True)
    if entry["transport"] == "ipc":
        entry["km"].cleanup_ipc_files()
    state["kernel"] = None
    return {"stopped": True}


def loopback_start() -> dict[str, Any]:
    if state["loopback"]:
        return {"already": True, "port": state["loopback"]["port"]}
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def serve() -> None:
        conn, _ = server.accept()
        with conn:
            while True:
                data = conn.recv(1024)
                if not data:
                    break
                conn.sendall(data)

    threading.Thread(target=serve, daemon=True).start()
    client = socket.create_connection(("127.0.0.1", port), timeout=3)
    state["loopback"] = {"server": server, "client": client, "port": port}
    return {"port": port}


def socket_roundtrip(sock: socket.socket, payload: bytes, expect_echo: bool) -> dict[str, Any]:
    started = time.monotonic()
    try:
        sock.settimeout(5)
        sock.sendall(payload)
        data = sock.recv(65536)
        ok = data.startswith(payload) if expect_echo else bool(data)
        return {"ok": ok, "received_len": len(data), "head": data[:60].decode(errors="replace"), "rtt_s": time.monotonic() - started}
    except OSError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "rtt_s": time.monotonic() - started}


def unix_start() -> dict[str, Any]:
    if state["unix"]:
        return {"already": True}
    a, b = socket.socketpair()

    def serve() -> None:
        while True:
            data = b.recv(1024)
            if not data:
                break
            b.sendall(data)

    threading.Thread(target=serve, daemon=True).start()
    state["unix"] = {"a": a, "b": b, "family": str(a.family)}
    return {"family": str(a.family)}


def outbound_connect(host: str, port: int) -> dict[str, Any]:
    if state["outbound"]:
        state["outbound"]["sock"].close()
    started = time.monotonic()
    sock = socket.create_connection((host, port), timeout=5)
    state["outbound"] = {"sock": sock, "host": host, "port": port}
    return {"connected": True, "peer": sock.getpeername(), "connect_s": time.monotonic() - started}


def outbound_send() -> dict[str, Any]:
    entry = state["outbound"]
    if not entry:
        return {"error": "not connected"}
    request = f"GET / HTTP/1.1\r\nHost: {entry['host']}\r\nConnection: keep-alive\r\n\r\n".encode()
    return socket_roundtrip(entry["sock"], request, expect_echo=False)


def listen127_start(port: int) -> dict[str, Any]:
    if state["listen127"]:
        return {"already": True, "port": state["listen127"]["port"]}

    class Local(JsonHandler):
        def do_GET(self) -> None:
            self.send_json({"loopback_listener": True, "port": port})

    server = ThreadingHTTPServer(("127.0.0.1", port), Local)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["listen127"] = {"server": server, "port": port}
    return {"port": port, "bound": "127.0.0.1"}


class AppHandler(JsonHandler):
    def do_POST(self) -> None:
        path = self.route()
        if path == "/upload":
            length = int(self.headers.get("Content-Length") or 0)
            remaining, digest, started = length, hashlib.sha256(), time.monotonic()
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                digest.update(chunk)
                remaining -= len(chunk)
            self.send_json({"received": length - remaining, "sha256": digest.hexdigest(), "seconds": time.monotonic() - started})
            return
        self.do_GET()

    def do_GET(self) -> None:  # noqa: C901 - a flat dispatch table is the clearest shape for a probe
        path, q = self.route(), self.query()
        try:
            if path in ("/", "/health"):
                self.send_json({"ok": True, "uptime_s": time.monotonic() - BOOT_MONO, "kernel_ready": bool(state["kernel"]), "hooks_seen": [h["hook"] for h in state["hooks"]]})
            elif path == "/hooks":
                self.send_json(state["hooks"])
            elif path == "/echo":
                self.send_json({"request_version": self.request_version, "client": self.client_address[0], "headers": dict(self.headers.items())})
            elif path == "/run-payload":
                self.send_json(state["run"] or {"received": False})
            elif path == "/env":
                self.send_json({"aws": {k: v for k, v in os.environ.items() if k.startswith(("AWS_", "LAMBDA"))}, "names": sorted(os.environ)})
            elif path == "/id":
                self.send_json(identity())
            elif path == "/creds":
                uid = q.get("as_uid")
                self.send_json(imds_probe_as_uid(int(uid)) if uid else imds_probe())
            elif path == "/time":
                self.send_json({**now(), "boot_wall": BOOT_WALL, "boot_mono": BOOT_MONO, "suspend_at": state["suspend_at"], "resume_at": state["resume_at"], "resume_delta": clock_delta(), "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
            elif path == "/rand":
                self.send_json({"random_at_import": RANDOM_AT_IMPORT, "random_now": random.random(), "urandom": os.urandom(8).hex()})
            elif path == "/bg/start":
                self.send_json(bg_start())
            elif path == "/bg/status":
                self.send_json(bg_status())
            elif path == "/sleep/start":
                self.send_json(sleep_start(int(q.get("seconds", "120"))))
            elif path == "/sleep/status":
                self.send_json(sleep_status())
            elif path == "/kernel/start":
                self.send_json(kernel_start(q.get("transport", KERNEL_TRANSPORT)))
            elif path == "/kernel/exec":
                self.send_json(kernel_exec(q.get("code", "print(x)")))
            elif path == "/kernel/fresh":
                self.send_json(kernel_exec(q.get("code", "print(x)"), fresh=True))
            elif path == "/kernel/alive":
                self.send_json(kernel_alive())
            elif path == "/kernel/info":
                self.send_json(kernel_info())
            elif path == "/kernel/stop":
                self.send_json(kernel_stop())
            elif path == "/tcp/loopback/start":
                self.send_json(loopback_start())
            elif path == "/tcp/loopback/send":
                entry = state["loopback"]
                self.send_json(socket_roundtrip(entry["client"], b"ping", expect_echo=True) if entry else {"error": "not started"})
            elif path == "/unix/start":
                self.send_json(unix_start())
            elif path == "/unix/send":
                entry = state["unix"]
                self.send_json(socket_roundtrip(entry["a"], b"ping", expect_echo=True) if entry else {"error": "not started"})
            elif path == "/tcp/outbound/connect":
                self.send_json(outbound_connect(q.get("host", "example.com"), int(q.get("port", "80"))))
            elif path == "/tcp/outbound/send":
                self.send_json(outbound_send())
            elif path == "/listen127/start":
                self.send_json(listen127_start(int(q.get("port", "3000"))))
            elif path == "/stream":
                self.stream(int(q.get("seconds", "3600")), float(q.get("interval", "1")))
            elif path == "/big":
                self.big(min(int(q.get("bytes", "1048576")), 200 * 1024 * 1024))
            else:
                self.send_json({"error": "unknown path", "path": path}, 404)
        except Exception as exc:  # noqa: BLE001 - a probe must never die on a bad request
            log("handler_error", path=path, error=f"{type(exc).__name__}: {exc}")
            self.send_json({"error": f"{type(exc).__name__}: {exc}", "path": path}, 500)

    def stream(self, seconds: int, interval: float) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.flush()
        deadline = time.monotonic() + seconds
        n = 0
        try:
            while time.monotonic() < deadline:
                time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
                self.wfile.write(f"data: {json.dumps({'n': n, **now()})}\n\n".encode())
                self.wfile.flush()
                n += 1
        except OSError as exc:
            log("stream_closed", after_ticks=n, error=f"{type(exc).__name__}: {exc}")

    def big(self, total: int) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(total))
        self.end_headers()
        chunk = b"\0" * 65536
        remaining = total
        while remaining > 0:
            piece = chunk[: min(len(chunk), remaining)]
            self.wfile.write(piece)
            remaining -= len(piece)


def serve(port: int, handler: type[BaseHTTPRequestHandler]) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True, name=f"http-{port}").start()
    return server


def main() -> None:
    if "--imds-probe" in sys.argv:
        print(json.dumps(imds_probe()))
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    serve(APP_PORT, AppHandler)
    serve(HOOK_PORT, HookHandler)
    log("listening", app_port=APP_PORT, hook_port=HOOK_PORT, kernel_autostart=KERNEL_AUTOSTART, transport=KERNEL_TRANSPORT)
    if KERNEL_AUTOSTART:
        try:
            log("kernel_autostart", **{k: v for k, v in kernel_start(KERNEL_TRANSPORT).items() if k != "seed_exec"})
        except Exception as exc:  # noqa: BLE001 - /ready must still answer; the build log records the failure
            log("kernel_autostart_failed", error=f"{type(exc).__name__}: {exc}")
    state["ready"].set()
    log("ready")
    threading.Event().wait()


if __name__ == "__main__":
    main()
