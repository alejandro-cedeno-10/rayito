"""Un `hermes_cli.main` de doble para las pruebas del driver de Hermes: el
API server de Hermes (`/health`, `POST /v1/runs` y su SSE) con los eventos
grabados que apunta `FAKE_HERMES_EVENTS`, y un registro de lo recibido en
`FAKE_HERMES_LOG`. Se copia como `hermes_cli/main.py` de un directorio
temporal y lo arranca el driver como el `hermes gateway run` real."""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

KEY = os.environ["API_SERVER_KEY"]
LOG = os.environ["FAKE_HERMES_LOG"]
with open(os.environ["FAKE_HERMES_EVENTS"], encoding="utf-8") as handle:
    EVENTS = json.load(handle)
RECORD: dict[str, Any] = {
    "argv": sys.argv,
    "host": os.environ["API_SERVER_HOST"],
    "cwd": os.getcwd(),
    "home": os.environ["HERMES_HOME"],
    "bodies": [],
}


def save() -> None:
    with open(LOG, "w", encoding="utf-8") as handle:
        json.dump(RECORD, handle)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass

    def authorized(self) -> bool:
        return self.headers.get("Authorization") == f"Bearer {KEY}"

    def reply(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/health":
            self.reply(200, {"status": "ok"})
            return
        if not self.authorized():
            self.reply(401, {})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(b": open\n\n")
        for seq, event in enumerate(EVENTS):
            self.wfile.write(f"id: {seq}\ndata: {json.dumps(event)}\n\n".encode())
            self.wfile.write(b": keepalive\n\n")
        self.wfile.write(b": stream closed\n\n")

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        RECORD["bodies"].append(body)
        save()
        if not self.authorized():
            self.reply(401, {})
            return
        self.reply(202, {"run_id": "run_x", "status": "started", "replayed": False})


save()
ThreadingHTTPServer(("127.0.0.1", int(os.environ["API_SERVER_PORT"])), Handler).serve_forever()
