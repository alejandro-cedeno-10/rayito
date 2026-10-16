"""Un agente de ejemplo como app HTTP (contrato de AgentCore: `GET /ping` y
`POST /invocations`), para los tests `local` de `CustomRuntime(app=HttpApp(...))`
y del contenedor exportado. `python http_agent.py <puerto>`. Con el prompt
`stream` contesta con SSE. Sólo biblioteca estándar; no llama a ningún
modelo."""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STREAM_PROMPT = "stream"
LOOPBACK = "127.0.0.1"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    def _json(self, status: int, payload: object) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/ping":
            self._json(200, {"status": "Healthy"})
        else:
            self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        prompt = str(body.get("prompt", ""))
        if self.path != "/invocations":
            self._json(404, {"error": "not_found"})
        elif prompt == STREAM_PROMPT:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            for part in ("eco:", "stream"):
                self.wfile.write(f"data: {part}\n\n".encode())
                self.wfile.flush()
            self.close_connection = True
        else:
            self._json(200, {"response": f"eco:{prompt}"})


def main(argv: list[str]) -> int:
    ThreadingHTTPServer((LOOPBACK, int(argv[1])), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
