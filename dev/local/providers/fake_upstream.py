"""Upstream HTTPS falso para los proveedores de modelo del agente
(`make local-providers-up`, docs/site/docs/guias/probar-en-local.md).

Hace de los nueve upstreams de `testdata/agent/provider-catalogue.json`
(OpenAI, Gemini, Azure OpenAI, OpenRouter, Groq, Mistral, DeepSeek, xAI y un
proxy de LiteLLM) para que los tests `local` comprueben la pasarela de
secretos y los dos runtimes sin claves reales ni Internet:

- Al arrancar crea con `openssl` una CA de un solo uso y un certificado
  con todos esos hosts (`subjectAltName`), y deja en `CA_BUNDLE_DIR` el
  almacén del sistema más esa CA: el guest lo usa como `SSL_CERT_FILE`, así
  `rayd` (rustls con el almacén del sistema) confía en este servidor. La
  clave privada no sale de su tmpfs.
- En el 443 responde lo mínimo válido de cada forma de API: Chat
  Completions y Responses (con y sin `stream`) y `generateContent` /
  `streamGenerateContent` de Gemini (con y sin `alt=sse`). El texto es
  siempre `REPLY_TEXT`.
- En `ADMIN_PORT` (HTTP, sólo en la red `providers`) `GET /requests`
  devuelve lo recibido (método, host, ruta, cabeceras y cuerpo) y
  `DELETE /requests` lo vacía: los tests miran ahí qué cabecera llegó.

Sólo biblioteca estándar y el `openssl` de la imagen del runner. No guarda
nada fuera de sus volúmenes ni llama a ningún sitio.
"""

from __future__ import annotations

import json
import shutil
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

CATALOGUE: Final = Path("/src/testdata/agent/provider-catalogue.json")
TLS_DIR: Final = Path("/tls")
CA_BUNDLE_DIR: Final = Path("/ca")
CA_BUNDLE_NAME: Final = "bundle.pem"
SYSTEM_CA_BUNDLE: Final = Path("/etc/ssl/certs/ca-certificates.crt")
HTTPS_PORT: Final = 443
ADMIN_PORT: Final = 8081
#: Días de validez: el entorno local vive horas; uno basta.
CERT_DAYS: Final = "1"
REPLY_TEXT: Final = "rayito-fake-ok"
#: Uso de tokens que se devuelve: no nulo para que el SDK lo sume.
INPUT_TOKENS: Final = 7
OUTPUT_TOKENS: Final = 3
JSON_TYPE: Final = "application/json"
SSE_TYPE: Final = "text/event-stream"
CHAT_COMPLETIONS: Final = "/chat/completions"
RESPONSES: Final = "/responses"
GEMINI_STREAM: Final = ":streamGenerateContent"
GEMINI_UNARY: Final = ":generateContent"


def catalogue_hosts() -> list[str]:
    hosts = {urlsplit(preset["upstream"]).hostname for preset in load_catalogue()["presets"]}
    return sorted(host for host in hosts if host)


def load_catalogue() -> dict[str, Any]:
    return json.loads(CATALOGUE.read_text(encoding="utf-8"))


def make_certificates(hosts: list[str]) -> tuple[Path, Path]:
    """CA y certificado hoja firmados por ella; deja el bundle de confianza
    en `CA_BUNDLE_DIR` y devuelve (certificado, clave) de la hoja."""
    TLS_DIR.mkdir(parents=True, exist_ok=True)
    ca_key, ca_cert = TLS_DIR / "ca.key", TLS_DIR / "ca.pem"
    leaf_key, leaf_csr, leaf_cert = TLS_DIR / "leaf.key", TLS_DIR / "leaf.csr", TLS_DIR / "leaf.pem"
    extensions = TLS_DIR / "leaf.ext"
    openssl(
        "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
        "-keyout", ca_key, "-out", ca_cert, "-days", CERT_DAYS,
        "-subj", "/CN=rayito local fake upstream CA",
        "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign",
    )  # fmt: skip
    openssl(
        "req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
        "-keyout", leaf_key, "-out", leaf_csr, "-subj", "/CN=rayito local fake upstream",
    )  # fmt: skip
    extensions.write_text(
        "basicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature\n"
        "extendedKeyUsage=serverAuth\n"
        f"subjectAltName={','.join(f'DNS:{host}' for host in hosts)}\n",
        encoding="utf-8",
    )
    openssl(
        "x509", "-req", "-in", leaf_csr, "-CA", ca_cert, "-CAkey", ca_key, "-CAcreateserial",
        "-out", leaf_cert, "-days", CERT_DAYS, "-extfile", extensions,
    )  # fmt: skip
    CA_BUNDLE_DIR.mkdir(parents=True, exist_ok=True)
    staging = CA_BUNDLE_DIR / f".{CA_BUNDLE_NAME}.tmp"
    with staging.open("wb") as bundle:
        bundle.write(SYSTEM_CA_BUNDLE.read_bytes())
        bundle.write(ca_cert.read_bytes())
    staging.chmod(0o644)
    staging.replace(CA_BUNDLE_DIR / CA_BUNDLE_NAME)
    return leaf_cert, leaf_key


def openssl(*args: object) -> None:
    binary = shutil.which("openssl")
    if binary is None:
        sys.exit("fake_upstream: falta openssl en la imagen")
    subprocess.run([binary, *map(str, args)], check=True, capture_output=True)


class RequestLog:
    """Lo recibido por el 443, en orden; compartido entre hilos."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: list[dict[str, Any]] = []

    def add(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self._entries.append(entry)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


LOG: Final = RequestLog()


def usage_chat() -> dict[str, int]:
    return {
        "prompt_tokens": INPUT_TOKENS,
        "completion_tokens": OUTPUT_TOKENS,
        "total_tokens": INPUT_TOKENS + OUTPUT_TOKENS,
    }


def chat_completion(model: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-rayito-local",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": REPLY_TEXT},
                "finish_reason": "stop",
            }
        ],
        "usage": usage_chat(),
    }


def chat_chunks(model: str) -> list[dict[str, Any]]:
    base = {
        "id": "chatcmpl-rayito-local",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
    }
    return [
        {
            **base,
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": REPLY_TEXT},
                    "finish_reason": None,
                }
            ],
        },
        {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {**base, "choices": [], "usage": usage_chat()},
    ]


def response_object(model: str, status: str) -> dict[str, Any]:
    done = status == "completed"
    message = {
        "type": "message",
        "id": "msg_rayito_local",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": REPLY_TEXT, "annotations": []}],
    }
    return {
        "id": "resp_rayito_local",
        "object": "response",
        "created_at": int(time.time()),
        "status": status,
        "model": model,
        "output": [message] if done else [],
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "incomplete_details": None,
        "error": None,
        "usage": {
            "input_tokens": INPUT_TOKENS,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": OUTPUT_TOKENS,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": INPUT_TOKENS + OUTPUT_TOKENS,
        }
        if done
        else None,
    }


def response_events(model: str) -> list[dict[str, Any]]:
    item_id = "msg_rayito_local"
    in_progress_item = {
        "type": "message",
        "id": item_id,
        "status": "in_progress",
        "role": "assistant",
        "content": [],
    }
    part = {"type": "output_text", "text": REPLY_TEXT, "annotations": []}
    done_item = {**in_progress_item, "status": "completed", "content": [part]}
    location = {"item_id": item_id, "output_index": 0, "content_index": 0}
    events: list[dict[str, Any]] = [
        {"type": "response.created", "response": response_object(model, "in_progress")},
        {"type": "response.in_progress", "response": response_object(model, "in_progress")},
        {"type": "response.output_item.added", "output_index": 0, "item": in_progress_item},
        {
            "type": "response.content_part.added",
            **location,
            "part": {**part, "text": ""},
        },
        {"type": "response.output_text.delta", **location, "delta": REPLY_TEXT, "logprobs": []},
        {"type": "response.output_text.done", **location, "text": REPLY_TEXT, "logprobs": []},
        {"type": "response.content_part.done", **location, "part": part},
        {"type": "response.output_item.done", "output_index": 0, "item": done_item},
        {"type": "response.completed", "response": response_object(model, "completed")},
    ]
    for number, event in enumerate(events):
        event["sequence_number"] = number
    return events


def gemini_response(model: str) -> dict[str, Any]:
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": REPLY_TEXT}]},
                "finishReason": "STOP",
                "index": 0,
            }
        ],
        "usageMetadata": {
            "promptTokenCount": INPUT_TOKENS,
            "candidatesTokenCount": OUTPUT_TOKENS,
            "totalTokenCount": INPUT_TOKENS + OUTPUT_TOKENS,
        },
        "modelVersion": model,
        "responseId": "rayito-local",
    }


class UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        self._handle()

    def do_GET(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write(f"fake_upstream: {self.command} {self.path}\n")

    def _handle(self) -> None:
        body = self._read_body()
        LOG.add(
            {
                "method": self.command,
                "host": self.headers.get("host", ""),
                "path": self.path,
                "headers": {name.lower(): value for name, value in self.headers.items()},
                "body": body.decode("utf-8", "replace"),
            }
        )
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        url = urlsplit(self.path)
        path, query = url.path, url.query
        model = str(payload.get("model") or path.rsplit("/", 1)[-1].split(":", 1)[0])
        stream = bool(payload.get("stream"))
        if path.endswith(CHAT_COMPLETIONS):
            if stream:
                self._sse([("", chunk) for chunk in chat_chunks(model)], done_marker=True)
            else:
                self._json(chat_completion(model))
        elif path.endswith(RESPONSES):
            if stream:
                self._sse([(event["type"], event) for event in response_events(model)])
            else:
                self._json(response_object(model, "completed"))
        elif path.endswith(GEMINI_STREAM):
            if "alt=sse" in query:
                self._sse([("", gemini_response(model))])
            else:
                self._json([gemini_response(model)])
        elif path.endswith(GEMINI_UNARY):
            self._json(gemini_response(model))
        else:
            self._json({"error": {"message": "ruta desconocida en el upstream falso"}}, 404)

    def _read_body(self) -> bytes:
        if self.headers.get("transfer-encoding", "").lower() == "chunked":
            chunks = []
            while True:
                size = int(self.rfile.readline().split(b";", 1)[0].strip() or b"0", 16)
                if size == 0:
                    self.rfile.readline()
                    return b"".join(chunks)
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
        length = int(self.headers.get("content-length") or 0)
        return self.rfile.read(length) if length else b""

    def _json(self, value: object, status: int = 200) -> None:
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("content-type", JSON_TYPE)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _sse(self, events: list[tuple[str, object]], *, done_marker: bool = False) -> None:
        lines = []
        for name, value in events:
            prefix = f"event: {name}\n" if name else ""
            lines.append(f"{prefix}data: {json.dumps(value)}\n\n")
        if done_marker:
            lines.append("data: [DONE]\n\n")
        data = "".join(lines).encode()
        self.send_response(200)
        self.send_header("content-type", SSE_TYPE)
        self.send_header("cache-control", "no-cache")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class AdminHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        if self.path == "/health":
            self._reply(b"ok", "text/plain")
        elif self.path == "/requests":
            self._reply(json.dumps(LOG.snapshot()).encode(), JSON_TYPE)
        else:
            self._reply(b"", "text/plain", 404)

    def do_DELETE(self) -> None:
        LOG.clear()
        self._reply(b"", "text/plain", 204)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _reply(self, data: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)


def main() -> None:
    cert, key = make_certificates(catalogue_hosts())
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    upstream = ThreadingHTTPServer(("0.0.0.0", HTTPS_PORT), UpstreamHandler)
    upstream.socket = context.wrap_socket(upstream.socket, server_side=True)
    admin = ThreadingHTTPServer(("0.0.0.0", ADMIN_PORT), AdminHandler)
    threading.Thread(target=admin.serve_forever, daemon=True).start()
    sys.stderr.write(f"fake_upstream: {', '.join(catalogue_hosts())}\n")
    upstream.serve_forever()


if __name__ == "__main__":
    main()
